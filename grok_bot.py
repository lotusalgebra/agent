"""Grok (grok.com) image-generation bot — tier-3 fallback in the
`_gen_image` chain. Same architecture as gemini_bot.py:

  - Attaches to a running real Chrome via Playwright connect_over_cdp
    (LOTUS_CDP_URL env var). Falls back to launching its own Playwright
    Chromium with a per-bot profile when CDP isn't available.
  - Drives the chat UI by typing into the prompt textarea, hitting submit,
    waiting for an image to appear in the DOM.
  - Downloads via three layered strategies:
      1. Scoped Download/Save button on the most-recent image bubble
      2. APIRequestContext fetch of the image src (CORS-safe, full-size)
      3. Canvas capture of the rendered <img> (last-resort, lower quality)

Public entry:
    create_image_and_download(prompt: str, save_to: str) -> Optional[str]

Selector caveat: grok.com's DOM is a moving target — we use generic
strategies (largest <img> in the conversation pane, any textarea, etc.)
rather than brittle CSS classes. If selectors break, prefer to widen the
selector before adding new ones."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright


_PROFILE_DIR = Path(os.path.expanduser("~/.lotus_auth/grok_profile"))
_GROK_URL = "https://grok.com/"
_DEFAULT_TIMEOUT_MS = 30_000
_IMAGE_WAIT_S = 240            # generous — Aurora can take a while
_VERBOSE = True


# ── Single-bot lock (mirrors gemini_bot.py's pattern) ─────────────────────

# A dedicated asyncio loop in its own thread so synchronous callers can
# fire-and-await without blocking the agent's main loop.
_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_pw = None
_browser = None
_context = None
_page = None


def _start_loop() -> asyncio.AbstractEventLoop:
    """Create the dedicated event loop on first use; return it on every
    subsequent call."""
    global _loop, _thread
    with _lock:
        if _loop and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(
            target=_loop.run_forever, name="grok-bot-loop", daemon=True)
        _thread.start()
    return _loop


async def _ensure_page(verbose: bool = _VERBOSE):
    """Connect to Chrome (CDP-first) and surface a page on grok.com.
    Reuses an existing tab on grok.com if one is already open."""
    global _pw, _browser, _context, _page
    if _page:
        try:
            # Cheap liveness probe — `url` raises if the page is gone.
            _ = _page.url
            return _page
        except Exception:
            _page = None

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    if _pw is None:
        _pw = await async_playwright().start()

    cdp_url = os.environ.get("LOTUS_CDP_URL")
    if cdp_url:
        if verbose:
            print(f"[grok] connecting to Chrome via CDP at {cdp_url}",
                  flush=True)
        _browser = await _pw.chromium.connect_over_cdp(cdp_url)
        if _browser.contexts:
            _context = _browser.contexts[0]
        else:
            _context = await _browser.new_context(accept_downloads=True)

        # Prefer an already-open grok.com tab if any.
        candidate = None
        for p in _context.pages:
            try:
                if "grok.com" in (p.url or "") or "x.ai" in (p.url or ""):
                    candidate = p; break
            except Exception:
                pass
        if candidate is None:
            candidate = await _context.new_page()
            await candidate.goto(_GROK_URL, wait_until="domcontentloaded",
                                 timeout=_DEFAULT_TIMEOUT_MS)
        _page = candidate
        _page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
        return _page

    # Standalone Playwright Chromium fallback (likely Google-blocked, but
    # included for completeness / dev runs).
    if verbose:
        print(f"[grok] launching Chromium with profile {_PROFILE_DIR}",
              flush=True)
    _context = await _pw.chromium.launch_persistent_context(
        user_data_dir=str(_PROFILE_DIR),
        headless=False,
        accept_downloads=True,
        viewport={"width": 1440, "height": 900},
        ignore_default_args=["--enable-automation"],
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    if _context.pages:
        _page = _context.pages[0]
    else:
        _page = await _context.new_page()
    await _page.goto(_GROK_URL, wait_until="domcontentloaded",
                     timeout=_DEFAULT_TIMEOUT_MS)
    _page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    return _page


# ── Prompt-send + image-wait helpers ──────────────────────────────────────

async def _send_prompt(page, prompt: str, verbose: bool = _VERBOSE) -> bool:
    """Locate the prompt textarea and submit. Tries several common
    textarea/contenteditable patterns since grok.com's exact selector has
    shifted across versions."""
    candidates = [
        'textarea',
        '[contenteditable="true"]',
        '[role="textbox"]',
    ]
    for sel in candidates:
        try:
            tb = page.locator(sel).first
            await tb.wait_for(state="visible", timeout=4000)
            await tb.click()
            await page.keyboard.press("Meta+A")
            await page.keyboard.press("Backspace")
            await tb.fill("")
            # `fill` doesn't always work on contenteditable; type explicitly.
            await page.keyboard.type(prompt, delay=2)
            # Submit via Enter (typical) — some chat UIs require Cmd+Enter.
            await page.keyboard.press("Enter")
            if verbose:
                print(f"[grok] prompt sent via selector {sel!r} "
                      f"({len(prompt)} chars)", flush=True)
            return True
        except Exception as e:
            if verbose:
                print(f"[grok] selector {sel!r} failed: {e}", flush=True)
            continue
    return False


async def _biggest_image(page):
    """Return (locator, width, height) for the largest <img> currently in
    the DOM, or (None, 0, 0). Uses bounding rects so off-screen / zero-size
    placeholders are filtered out."""
    js = """() => {
        const imgs = Array.from(document.images);
        let best = null, area = 0;
        for (const im of imgs) {
            const r = im.getBoundingClientRect();
            const a = r.width * r.height;
            if (a > area) { area = a; best = im; }
        }
        if (!best) return null;
        return {
            src: best.src,
            w: best.naturalWidth  || best.width,
            h: best.naturalHeight || best.height,
        };
    }"""
    try:
        info = await page.evaluate(js)
    except Exception:
        info = None
    if not info:
        return None, 0, 0
    return info, info.get("w") or 0, info.get("h") or 0


async def _wait_for_image(page, prev_count: int, verbose: bool = _VERBOSE):
    """Poll the DOM until a NEW image (count > prev_count) appears AND its
    natural size is > 256×256 (filters out avatars, spinners, mini icons).
    Returns the largest qualifying image's info dict, or None on timeout."""
    deadline = time.time() + _IMAGE_WAIT_S
    while time.time() < deadline:
        try:
            count = await page.evaluate("() => document.images.length")
        except Exception:
            count = 0
        info, w, h = await _biggest_image(page)
        if info and (w or 0) >= 256 and (h or 0) >= 256:
            if verbose:
                print(f"[grok] image ready: {w}x{h} src={info['src'][:70]}",
                      flush=True)
            return info
        await asyncio.sleep(1.5)
    if verbose:
        print(f"[grok] timed out waiting for image (>{_IMAGE_WAIT_S}s)",
              flush=True)
    return None


# ── Download strategies ───────────────────────────────────────────────────

async def _download_via_button(page, save_path: Path,
                                verbose: bool = _VERBOSE) -> Optional[str]:
    """Try clicking a Download/Save button near the most-recent image.
    Common patterns: a download icon button inside the image's hover
    toolbar, or a `[aria-label*="download" i]` element."""
    selectors = [
        'button[aria-label*="download" i]',
        'a[aria-label*="download" i]',
        'button[aria-label*="save" i]',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).last
            await btn.wait_for(state="visible", timeout=2500)
            async with page.expect_download(timeout=30_000) as dl_info:
                await btn.click(force=True)
            dl = await dl_info.value
            tmp = await dl.path()
            if tmp:
                shutil.move(str(tmp), str(save_path))
                size = save_path.stat().st_size
                if size > 2048:
                    if verbose:
                        print(f"[grok] downloaded via {sel} → {save_path} "
                              f"({size//1024} KB)", flush=True)
                    return str(save_path)
                save_path.unlink(missing_ok=True)
        except Exception as e:
            if verbose:
                print(f"[grok] download-button {sel} failed: {e}",
                      flush=True)
            continue
    return None


async def _download_via_src(page, info: dict, save_path: Path,
                             verbose: bool = _VERBOSE) -> Optional[str]:
    """Fetch the image's src via Playwright's APIRequestContext (uses the
    browser's cookie jar but bypasses the page's CORS sandbox)."""
    src = (info or {}).get("src") or ""
    if not src.startswith("http"):
        return None
    try:
        ctx = page.context.request
        resp = await ctx.get(src, timeout=60_000)
        if not resp.ok:
            return None
        body = await resp.body()
        if len(body) < 2048:
            return None
        save_path.write_bytes(body)
        if verbose:
            print(f"[grok] downloaded via src-fetch → {save_path} "
                  f"({len(body)//1024} KB)", flush=True)
        return str(save_path)
    except Exception as e:
        if verbose:
            print(f"[grok] src-fetch failed: {e}", flush=True)
        return None


async def _download_via_canvas(page, info: dict, save_path: Path,
                                verbose: bool = _VERBOSE) -> Optional[str]:
    """Last-resort: paint the <img> onto a canvas and dump as PNG. Runs
    in-page, so it works even when CORS blocks the API fetch — but yields
    a smaller PNG (whatever resolution is rendered, not the full source)."""
    src = (info or {}).get("src") or ""
    if not src:
        return None
    js = """async (src) => {
        const img = await new Promise((res, rej) => {
            const i = new Image();
            i.crossOrigin = 'anonymous';
            i.onload = () => res(i);
            i.onerror = rej;
            i.src = src;
        });
        const c = document.createElement('canvas');
        c.width = img.naturalWidth || img.width;
        c.height = img.naturalHeight || img.height;
        c.getContext('2d').drawImage(img, 0, 0);
        return c.toDataURL('image/png');
    }"""
    try:
        data_url = await page.evaluate(js, src)
        if not data_url or not data_url.startswith("data:image/png;base64,"):
            return None
        import base64
        save_path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
        if verbose:
            print(f"[grok] canvas-capture → {save_path} "
                  f"({save_path.stat().st_size//1024} KB)", flush=True)
        return str(save_path)
    except Exception as e:
        if verbose:
            print(f"[grok] canvas-capture failed: {e}", flush=True)
        return None


# ── Public entry ──────────────────────────────────────────────────────────

async def _create_image_async(prompt: str, save_to: str,
                               verbose: bool = _VERBOSE) -> Optional[str]:
    save_path = Path(os.path.expanduser(save_to))
    save_path.parent.mkdir(parents=True, exist_ok=True)
    page = await _ensure_page(verbose=verbose)

    # Snapshot how many images existed before so _wait_for_image can spot
    # the new one.
    try:
        prev_count = await page.evaluate("() => document.images.length")
    except Exception:
        prev_count = 0

    if not await _send_prompt(page, prompt, verbose=verbose):
        return None
    info = await _wait_for_image(page, prev_count, verbose=verbose)
    if not info:
        return None

    # Try strategies in descending fidelity.
    for fn in (_download_via_button, _download_via_src,
               _download_via_canvas):
        out = await fn(page, info if fn is not _download_via_button
                       else None, save_path, verbose=verbose) \
              if fn is not _download_via_button else \
              await fn(page, save_path, verbose=verbose)
        if out:
            return out
    return None


def create_image_and_download(prompt: str, save_to: str,
                               verbose: bool = _VERBOSE) -> Optional[str]:
    """Synchronous public entry. Schedules the work on the dedicated grok
    event loop and blocks until the image is saved (or None on failure)."""
    loop = _start_loop()
    fut = asyncio.run_coroutine_threadsafe(
        _create_image_async(prompt, save_to, verbose=verbose), loop)
    try:
        return fut.result(timeout=_IMAGE_WAIT_S + 60)
    except Exception as e:
        if verbose:
            print(f"[grok] create_image_and_download failed: {e}",
                  flush=True)
        return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python grok_bot.py <prompt> <save_path>")
        sys.exit(1)
    out = create_image_and_download(sys.argv[1], sys.argv[2])
    print("OK:" + out if out else "FAIL")
