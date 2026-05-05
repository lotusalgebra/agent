"""Grok image-to-video bot — drives grok.com/imagine via Playwright +
CDP-attached real Chrome to animate a still PNG into a 6-10s MP4.

Architecture mirrors gemini_bot.py / grok_bot.py: connects via
LOTUS_CDP_URL to the user's real signed-in Chrome (no API key, no separate
Playwright Chromium — Google would block that anyway).

Selectors are pinned to the live DOM probe of grok.com/imagine on
2026-04-26 — they're text-based ("Video", "720p", "Aspect Ratio", "Submit")
which is more durable than CSS class names. If the UI shifts, the probe
script in `tests/probe_grok_imagine.py` (TODO) regenerates fresh ones.

Public entry:
    image_to_video(image_path, prompt, *,
                   mode='Video', quality='720p', duration='10s',
                   aspect='9:16', save_to=None, timeout=600) -> Optional[str]

Workflow:
    1. Connect via CDP, navigate to grok.com/imagine if not there.
    2. Click 'Video' tab (the alternative is 'Image').
    3. Upload reference PNG via input[type="file"][name="files"].
    4. Click quality button ('720p' / '1080p' / '480p').
    5. Click duration button ('6s' / '10s').
    6. Click 'Aspect Ratio' button → pick the requested ratio
       from the popover.
    7. Type prompt into the contenteditable textarea.
    8. Click 'Submit'.
    9. Poll the DOM until a <video> element with a finished mp4 src
       appears (or timeout).
   10. Fetch the MP4 via APIRequestContext (uses Chrome cookie jar) and
       save to `save_to`. Falls back to `<video>.src` direct fetch.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright


_GROK_IMAGINE_URL = "https://grok.com/imagine"
_DEFAULT_TIMEOUT_MS = 30_000
_VIDEO_WAIT_S = 600           # video gen can take several minutes
_VERBOSE = True


# ── Single dedicated event loop (mirrors grok_bot/gemini_bot pattern) ─────

_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_pw = None
_browser = None
_context = None
_page = None


def _start_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(
            target=_loop.run_forever, name="grok-video-loop", daemon=True)
        _thread.start()
    return _loop


async def _ensure_page(verbose: bool = _VERBOSE):
    """Open or reuse a grok.com/imagine tab on the CDP-attached Chrome."""
    global _pw, _browser, _context, _page
    if _page:
        try:
            _ = _page.url
            # If we drifted off /imagine, navigate back.
            if "/imagine" not in (_page.url or ""):
                await _page.goto(_GROK_IMAGINE_URL,
                                 wait_until="domcontentloaded",
                                 timeout=_DEFAULT_TIMEOUT_MS)
            return _page
        except Exception:
            _page = None

    if _pw is None:
        _pw = await async_playwright().start()

    cdp_url = os.environ.get("LOTUS_CDP_URL")
    if not cdp_url:
        raise RuntimeError(
            "grok_video requires LOTUS_CDP_URL pointing at a real Chrome "
            "with grok.com signed in (Playwright-launched Chromium would "
            "be blocked).")

    if verbose:
        print(f"[grok-video] connecting via CDP at {cdp_url}", flush=True)
    _browser = await _pw.chromium.connect_over_cdp(cdp_url)
    if _browser.contexts:
        _context = _browser.contexts[0]
    else:
        _context = await _browser.new_context(accept_downloads=True)

    # Prefer an already-open grok tab.
    candidate = None
    for p in _context.pages:
        try:
            if "grok.com" in (p.url or ""):
                candidate = p; break
        except Exception:
            pass
    if candidate is None:
        candidate = await _context.new_page()
    if "/imagine" not in (candidate.url or ""):
        await candidate.goto(_GROK_IMAGINE_URL,
                             wait_until="domcontentloaded",
                             timeout=_DEFAULT_TIMEOUT_MS)
    candidate.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    _page = candidate
    return _page


# ── UI primitives ─────────────────────────────────────────────────────────

async def _click_text(page, text: str, *, exact: bool = True,
                       timeout: int = 5000, verbose: bool = _VERBOSE) -> bool:
    """Click the first visible button whose text matches `text`."""
    try:
        loc = page.get_by_role("button", name=text, exact=exact)
        await loc.first.wait_for(state="visible", timeout=timeout)
        await loc.first.click(force=True)
        if verbose:
            print(f"[grok-video] clicked button {text!r}", flush=True)
        return True
    except Exception as e:
        if verbose:
            print(f"[grok-video] click {text!r} failed: {e}", flush=True)
        return False


async def _set_aspect_ratio(page, aspect: str,
                             verbose: bool = _VERBOSE) -> bool:
    """The Aspect Ratio control opens a popover with options; click the one
    matching the requested ratio (e.g. '9:16', '16:9', '1:1', '4:5'). Falls
    back to text-based menuitem search if button-role doesn't match."""
    if not await _click_text(page, "Aspect Ratio", exact=False, timeout=5000):
        return False
    # Wait briefly for popover, then look for the ratio option.
    await page.wait_for_timeout(300)
    for role in ("menuitem", "option", "button"):
        try:
            loc = page.get_by_role(role, name=aspect, exact=True)
            await loc.first.wait_for(state="visible", timeout=2000)
            await loc.first.click(force=True)
            if verbose:
                print(f"[grok-video] aspect ratio set to {aspect}",
                      flush=True)
            return True
        except Exception:
            continue
    # Last-ditch: any visible element matching the text exactly.
    try:
        loc = page.locator(f"text='{aspect}'").first
        await loc.wait_for(state="visible", timeout=2000)
        await loc.click(force=True)
        if verbose:
            print(f"[grok-video] aspect ratio set via text-locator: {aspect}",
                  flush=True)
        return True
    except Exception as e:
        if verbose:
            print(f"[grok-video] aspect set failed for {aspect!r}: {e}",
                  flush=True)
        return False


async def _upload_image(page, image_path: str,
                         verbose: bool = _VERBOSE) -> bool:
    """Set the reference image on the hidden file input."""
    try:
        inp = page.locator('input[type="file"]').first
        await inp.set_input_files(image_path)
        if verbose:
            print(f"[grok-video] uploaded {image_path}", flush=True)
        # Give Grok a moment to read the file + show the thumbnail.
        await page.wait_for_timeout(800)
        return True
    except Exception as e:
        if verbose:
            print(f"[grok-video] file upload failed: {e}", flush=True)
        return False


async def _set_prompt(page, prompt: str,
                       verbose: bool = _VERBOSE) -> bool:
    """Type prompt into the wide contenteditable textarea."""
    try:
        # The wide one (~720px wide per probe) is the prompt; narrow ones
        # are search/etc. Pick the widest visible contenteditable.
        idx = await page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('[contenteditable="true"]'));
            let best = -1, bestW = 0;
            for (let i = 0; i < els.length; i++) {
                const r = els[i].getBoundingClientRect();
                if (r.width > bestW) { bestW = r.width; best = i; }
            }
            return best;
        }""")
        if idx < 0:
            if verbose:
                print("[grok-video] no contenteditable found", flush=True)
            return False
        loc = page.locator('[contenteditable="true"]').nth(idx)
        await loc.click()
        await page.keyboard.press("Meta+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(prompt, delay=2)
        if verbose:
            print(f"[grok-video] prompt set ({len(prompt)} chars)",
                  flush=True)
        return True
    except Exception as e:
        if verbose:
            print(f"[grok-video] prompt set failed: {e}", flush=True)
        return False


async def _wait_for_video(page, deadline: float,
                           verbose: bool = _VERBOSE) -> Optional[str]:
    """Poll the DOM for a <video> element whose src is a real MP4 URL.
    Returns the src on success, or None on timeout."""
    js = """() => {
        const vids = Array.from(document.querySelectorAll('video'));
        // Grok's render shows an inline <video> once the job finishes;
        // pick the most recently added one with a non-blob https src or
        // any blob src that has duration set.
        for (const v of vids) {
            const src = v.currentSrc || v.src || '';
            if (src && (src.startsWith('http') || src.startsWith('blob:'))) {
                if (v.readyState >= 1 || src.startsWith('http')) {
                    return src;
                }
            }
        }
        return null;
    }"""
    last_logged = 0
    while time.time() < deadline:
        try:
            src = await page.evaluate(js)
        except Exception:
            src = None
        if src:
            if verbose:
                print(f"[grok-video] video ready: {src[:80]}", flush=True)
            return src
        # Log progress every 30 s so the agent log shows we're still waiting.
        if verbose and time.time() - last_logged > 30:
            last_logged = time.time()
            remaining = int(deadline - time.time())
            print(f"[grok-video] still waiting for video… ({remaining}s left)",
                  flush=True)
        await asyncio.sleep(2.0)
    return None


async def _download_video(page, src: str, save_path: Path,
                           verbose: bool = _VERBOSE) -> Optional[str]:
    """Fetch the MP4 — APIRequestContext for http URLs (full-res, full
    quality), or in-page fetch+download for blob: URLs."""
    if src.startswith("http"):
        try:
            ctx = page.context.request
            resp = await ctx.get(src, timeout=180_000)
            if not resp.ok:
                if verbose:
                    print(f"[grok-video] http fetch returned {resp.status}",
                          flush=True)
                return None
            body = await resp.body()
            if len(body) < 4096:
                return None
            save_path.write_bytes(body)
            if verbose:
                print(f"[grok-video] saved → {save_path} "
                      f"({len(body)//1024} KB)", flush=True)
            return str(save_path)
        except Exception as e:
            if verbose:
                print(f"[grok-video] http fetch failed: {e}", flush=True)
            return None
    # blob: URL — fetch in-page, return base64
    try:
        b64 = await page.evaluate("""async (src) => {
            const r = await fetch(src);
            const buf = await r.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let s = '';
            for (let i = 0; i < bytes.length; i += 4096) {
                s += String.fromCharCode.apply(null, bytes.subarray(i, i + 4096));
            }
            return btoa(s);
        }""", src)
        if not b64:
            return None
        import base64
        save_path.write_bytes(base64.b64decode(b64))
        if verbose:
            print(f"[grok-video] saved (blob) → {save_path} "
                  f"({save_path.stat().st_size//1024} KB)", flush=True)
        return str(save_path)
    except Exception as e:
        if verbose:
            print(f"[grok-video] blob fetch failed: {e}", flush=True)
        return None


# ── Public entry ──────────────────────────────────────────────────────────

async def _image_to_video_async(image_path: str, prompt: str, *,
                                 mode: str, quality: str, duration: str,
                                 aspect: str, save_to: str,
                                 timeout: int,
                                 verbose: bool) -> Optional[str]:
    image_path = os.path.abspath(os.path.expanduser(image_path))
    save_path = Path(os.path.expanduser(save_to))
    save_path.parent.mkdir(parents=True, exist_ok=True)

    if not os.path.isfile(image_path):
        raise FileNotFoundError(image_path)

    page = await _ensure_page(verbose=verbose)

    # 1. Switch to Video mode (button is on the toolbar; safe to click even
    # if already selected).
    await _click_text(page, mode, verbose=verbose)
    # 2. Upload the reference image.
    if not await _upload_image(page, image_path, verbose=verbose):
        return None
    # 3. Quality (720p / 480p / 1080p).
    await _click_text(page, quality, verbose=verbose)
    # 4. Duration (6s / 10s).
    await _click_text(page, duration, verbose=verbose)
    # 5. Aspect ratio (popover).
    await _set_aspect_ratio(page, aspect, verbose=verbose)
    # 6. Prompt.
    if not await _set_prompt(page, prompt, verbose=verbose):
        return None
    # 7. Submit.
    if not await _click_text(page, "Submit", verbose=verbose):
        return None

    # 8. Wait for the rendered <video>.
    deadline = time.time() + timeout
    src = await _wait_for_video(page, deadline, verbose=verbose)
    if not src:
        return None
    # 9. Save.
    return await _download_video(page, src, save_path, verbose=verbose)


def image_to_video(image_path: str, prompt: str, *,
                   mode: str = "Video",
                   quality: str = "720p",
                   duration: str = "10s",
                   aspect: str = "9:16",
                   save_to: Optional[str] = None,
                   timeout: int = _VIDEO_WAIT_S,
                   verbose: bool = _VERBOSE) -> Optional[str]:
    """Sync entry. Animate `image_path` with `prompt` and save the MP4 to
    `save_to` (default: `<image>.mp4` next to source). Returns the saved
    path, or None on failure.
    """
    if save_to is None:
        base = Path(os.path.expanduser(image_path))
        save_to = str(base.parent / "Videos" / (base.stem + ".mp4"))
    loop = _start_loop()
    fut = asyncio.run_coroutine_threadsafe(
        _image_to_video_async(image_path, prompt,
                              mode=mode, quality=quality,
                              duration=duration, aspect=aspect,
                              save_to=save_to, timeout=timeout,
                              verbose=verbose),
        loop)
    try:
        return fut.result(timeout=timeout + 60)
    except Exception as e:
        if verbose:
            print(f"[grok-video] image_to_video failed: {e}", flush=True)
        return None


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 3:
        print("Usage: python grok_video.py <image> <prompt> "
              "[--quality 720p] [--duration 10s] [--aspect 9:16] [--out PATH]")
        sys.exit(1)
    args = sys.argv[1:]
    image, prompt = args[0], args[1]
    kwargs = {}
    i = 2
    while i < len(args):
        if args[i] == "--quality":   kwargs["quality"] = args[i+1]; i += 2
        elif args[i] == "--duration":kwargs["duration"] = args[i+1]; i += 2
        elif args[i] == "--aspect":  kwargs["aspect"] = args[i+1]; i += 2
        elif args[i] == "--out":     kwargs["save_to"] = args[i+1]; i += 2
        else: i += 1
    out = image_to_video(image, prompt, **kwargs)
    print(json.dumps({"ok": bool(out), "out": out}, indent=2))
