"""
Gemini web-app automation via Playwright ASYNC API.

Why async: the mother process runs an asyncio WebSocket server, and
Playwright's sync API refuses to run anywhere in a process that has an
asyncio loop. Async API works cleanly under our own asyncio.run_until_complete.

Public surface matches gemini.py / gemini_api.py so agent_phase1.py's
`_gen_image` router needs no changes:

    create_image_and_download(prompt: str, save_to: str) -> Optional[str]

Setup:
  - First run opens a persistent Chromium window. Log into your Pro
    Google account in it. Cookies persist in ~/.lotus_auth/chrome_profile/
    forever after.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
import threading
from pathlib import Path
from typing import Optional

_PROFILE_DIR = Path(os.path.expanduser("~/.lotus_auth/chrome_profile"))
GEMINI_URL = "https://gemini.google.com/app"

# JS expression → true iff a REAL, visible "Stop generating" button is
# present. We deliberately avoid `aria-label*="Stop"` because that fuzzy
# match also hits "More options for <prompt>" buttons on previous chat
# bubbles whenever an old prompt contained the word "stop".
_JS_STOP_BTN_PRESENT = """
    (() => Array.from(document.querySelectorAll(
        'button[aria-label], button[data-testid]'
    )).some(b => {
        const a = (b.getAttribute('aria-label')||'').toLowerCase().trim();
        const t = (b.getAttribute('data-testid')||'').toLowerCase().trim();
        const labelMatch = a === 'stop' || a.startsWith('stop ') ||
                           a === 'stop response' || a === 'stop generating';
        const testidMatch = t === 'stop-button' || t === 'stop_button' ||
                            t.endsWith('-stop') || t.endsWith('_stop');
        if (!labelMatch && !testidMatch) return false;
        const r = b.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }))()
"""

# A single persistent browser + page, owned by its own asyncio loop in a
# dedicated thread. All `create_image_and_download` calls go through a
# command queue so exactly one call is in flight at a time and the
# Playwright objects never cross loops.
_worker_thread: Optional[threading.Thread] = None
_worker_loop:   Optional[asyncio.AbstractEventLoop] = None
_worker_ready:  threading.Event = threading.Event()
_worker_lock:   threading.Lock  = threading.Lock()


def _run_worker_loop():
    """Thread target — owns an asyncio loop forever."""
    global _worker_loop
    _worker_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_worker_loop)
    _worker_ready.set()
    try:
        _worker_loop.run_forever()
    finally:
        _worker_loop.close()


def _ensure_worker():
    """Spin up the worker thread + loop once, lazily."""
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_ready.clear()
        _worker_thread = threading.Thread(target=_run_worker_loop,
                                          name="gemini-bot-worker",
                                          daemon=True)
        _worker_thread.start()
        _worker_ready.wait(timeout=5.0)


def _submit(coro, timeout: float = 600.0):
    """Run an async coroutine on the worker loop (from any thread) and
    block until it finishes. Caller is whatever thread the pipeline
    worker runs on; Playwright objects live inside the worker loop only."""
    _ensure_worker()
    fut = asyncio.run_coroutine_threadsafe(coro, _worker_loop)
    return fut.result(timeout=timeout)


# ── Async Playwright internals ──────────────────────────────────────

_pw = None
_context = None
_page = None


async def _connect_cdp_with_retry(pw, cdp_url: str, *, verbose: bool = True):
    """Connect to a real Chrome via CDP with up to 5 attempts.

    Real Chrome can be unreachable on :9222 for several reasons:
      - target churn (tab opening/closing the moment we connect)
      - user closed Chrome and the preflight relaunched it (~5–10 s)
      - laptop just woke from sleep / network stack stalled
    Exponential backoff (1, 2, 4, 8, 16 s) covers up to ~31 s of
    downtime before giving up — long enough to ride out a Chrome
    relaunch without killing the in-flight render.
    """
    last_err = None
    delays = [1.0, 2.0, 4.0, 8.0, 16.0]   # 5 attempts, 4 sleeps between
    total_attempts = len(delays)
    for attempt in range(1, total_attempts + 1):
        try:
            return await pw.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            last_err = e
            if verbose:
                print(f"[bot] CDP attach attempt {attempt}/{total_attempts} "
                      f"failed: {e!r}")
            if attempt < total_attempts:
                await asyncio.sleep(delays[attempt - 1])
    raise last_err


async def _ensure_browser(headless: bool = False, verbose: bool = True):
    global _pw, _context, _page
    if _page is not None:
        try:
            await _page.evaluate("1")
            return _page
        except Exception:
            _page = None
            _context = None
            try: await _pw.stop()
            except Exception: pass
            _pw = None
    from playwright.async_api import async_playwright
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _pw = await async_playwright().start()

    cdp_url = os.environ.get("LOTUS_CDP_URL")
    if cdp_url:
        # Attach to an already-running Chrome via DevTools Protocol. Used
        # to recover chats from a Google account that Gemini blocks when
        # signed-in via the Playwright-bundled Chromium for Testing — a
        # stealth-detection mismatch. The user launches their real Chrome
        # with --remote-debugging-port=9222 and a non-default profile; we
        # attach to the first context/page there.
        if verbose:
            print(f"[bot] connecting to Chrome via CDP at {cdp_url}")
        _browser = await _connect_cdp_with_retry(_pw, cdp_url, verbose=verbose)
        if _browser.contexts:
            _context = _browser.contexts[0]
        else:
            _context = await _browser.new_context(accept_downloads=True)
        # Re-use an existing tab if there is one, or open a new one.
        if _context.pages:
            # Prefer a tab already on gemini.google.com if any.
            _page = None
            for p in _context.pages:
                try:
                    if "gemini.google.com" in (p.url or ""):
                        _page = p; break
                except Exception: pass
            if _page is None:
                _page = _context.pages[0]
        else:
            _page = await _context.new_page()
        _page.set_default_timeout(30_000)
        return _page

    if verbose:
        print(f"[bot] launching Chromium with profile {_PROFILE_DIR}")
    _context = await _pw.chromium.launch_persistent_context(
        user_data_dir=str(_PROFILE_DIR),
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1440, "height": 900},
        ignore_default_args=["--enable-automation"],
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    # Stealth — patches navigator.webdriver, plugin list, WebGL fingerprint,
    # codec strings etc. so Google is less likely to flag us as a bot and
    # throw a captcha / rate-limit. Required after repeated same-account
    # runs started tripping Google's anti-automation detection.
    try:
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(_context)
        if verbose: print("[bot] stealth applied to browser context")
    except Exception as e:
        if verbose: print(f"[bot] stealth unavailable ({e}) — continuing without")
    if _context.pages:
        _page = _context.pages[0]
    else:
        _page = await _context.new_page()
    _page.set_default_timeout(30_000)
    return _page


async def _dismiss_modals(page, verbose: bool = True):
    for _ in range(3):
        try: await page.keyboard.press("Escape")
        except Exception: pass
        await page.wait_for_timeout(150)
    for label in ["Got it", "Continue", "OK", "Dismiss", "Close", "Not now", "No thanks"]:
        try:
            btn = page.locator(f'button:has-text("{label}")').first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=2000)
                if verbose: print(f"[bot] dismissed modal: {label}")
                await page.wait_for_timeout(300)
        except Exception:
            pass


async def _goto_chat(page, verbose: bool = True) -> bool:
    try:
        await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        if verbose: print(f"[bot] goto failed: {e}")
        return False
    try:
        await page.wait_for_selector(
            'rich-textarea [contenteditable="true"], '
            'div[contenteditable="true"][role="textbox"], '
            'textarea', timeout=20_000)
        return True
    except Exception:
        if await page.locator('a:has-text("Sign in"), button:has-text("Sign in")').count() > 0:
            if verbose:
                print("[bot] NOT LOGGED IN — sign into Gemini in the window, then retry.")
        return False


async def _find_input(page):
    for sel in (
        'rich-textarea [contenteditable="true"]',
        'div[contenteditable="true"][aria-label*="prompt" i]',
        'div[contenteditable="true"][aria-label*="message" i]',
        'div[contenteditable="true"][role="textbox"]',
        'textarea[aria-label*="prompt" i]',
        'textarea',
    ):
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


async def _send_prompt(page, prompt: str, verbose: bool = True) -> bool:
    async def _try() -> bool:
        inp = await _find_input(page)
        if not inp: return False
        try:
            await inp.scroll_into_view_if_needed(timeout=3000)
            await inp.click(timeout=6000)
            await page.keyboard.press("Meta+A")
            await page.wait_for_timeout(80)
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(120)
            try:
                await inp.fill(prompt, timeout=8000)
            except Exception:
                await page.keyboard.insert_text(prompt)
            await page.wait_for_timeout(350)
            await page.keyboard.press("Enter")
            if verbose: print(f"[bot] prompt sent ({len(prompt)} chars)")
            return True
        except Exception as e:
            if verbose:
                print(f"[bot]   send attempt failed: {type(e).__name__}: {str(e)[:160]}")
            return False

    if await _try(): return True
    if verbose: print("[bot] send stuck — dismissing modals + retrying")
    await _dismiss_modals(page, verbose=verbose)
    try:
        await page.wait_for_selector(
            'rich-textarea [contenteditable="true"], '
            'div[contenteditable="true"][role="textbox"], '
            'textarea', timeout=8000)
    except Exception: pass
    if await _try(): return True
    if verbose: print("[bot] still stuck — reloading tab + retrying")
    try:
        await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)
        await _dismiss_modals(page, verbose=False)
        try:
            await page.wait_for_selector(
                'rich-textarea [contenteditable="true"], '
                'div[contenteditable="true"][role="textbox"], '
                'textarea', timeout=15_000)
        except Exception: pass
    except Exception as e:
        if verbose: print(f"[bot] reload failed: {e}")
        return False
    return await _try()


async def _wait_for_image_element(page, timeout_s: float = 240.0, verbose: bool = True):
    """Poll the DOM for a rendered image. Returns the largest qualifying
    <img> locator, or None on timeout.

    Adaptive patience — `timeout_s` is the IDLE timeout (how long we wait
    without any progress). While Gemini is actively "thinking" (streaming
    indicator visible), the idle clock is reset every poll. This lets the
    bot ride out traffic spikes where a frame takes 5-8 minutes.

    A hard cap of 15 minutes per frame prevents a permanently stuck tab
    from blocking the pipeline.
    """
    HARD_CAP_S = 900.0   # 15 min absolute max, no matter what
    hard_deadline = time.time() + HARD_CAP_S
    idle_deadline = time.time() + timeout_s
    if verbose:
        print(f"[bot] waiting (idle≤{timeout_s:.0f}s, hard≤{HARD_CAP_S:.0f}s) for image...")
    last_count = -1
    last_streaming = False
    while time.time() < hard_deadline and time.time() < idle_deadline:
        try:
            # Returns {count, biggest_w, biggest_h, has_data, has_blob, has_http}
            info = await page.evaluate("""
                () => {
                    const imgs = [...document.querySelectorAll('img')];
                    let best = null, bestArea = 0;
                    for (const i of imgs) {
                        const r = i.getBoundingClientRect();
                        const area = r.width * r.height;
                        const src = i.src || '';
                        if (!src) continue;
                        if (src.startsWith('chrome-') || src.startsWith('chrome://')) continue;
                        // Filter clearly-avatar-sized elements (< 100x100).
                        if (r.width < 100 || r.height < 100) continue;
                        if (area > bestArea) { best = i; bestArea = area; }
                    }
                    if (!best) return {count: 0};
                    const r = best.getBoundingClientRect();
                    return {
                        count: imgs.length,
                        biggest_w: r.width, biggest_h: r.height,
                        src_kind: best.src.startsWith('data:') ? 'data'
                                : best.src.startsWith('blob:') ? 'blob'
                                : best.src.startsWith('http')  ? 'http' : 'other',
                    };
                }
            """)
            area = (info.get("biggest_w") or 0) * (info.get("biggest_h") or 0)
            # Is Gemini actively generating? (Stop button / thinking / streaming)
            streaming = await page.evaluate("""
                () => {
                    if (document.querySelector('[data-is-streaming="true"]')) return true;
                    if (""" + _JS_STOP_BTN_PRESENT + """) return true;
                    return false;
                }
            """)
            if verbose and (info.get("count") != last_count or streaming != last_streaming):
                print(f"[bot]   dom has {info.get('count')} img(s), "
                      f"biggest {info.get('biggest_w')}x{info.get('biggest_h')} "
                      f"kind={info.get('src_kind')} streaming={streaming}")
                last_count = info.get("count")
                last_streaming = streaming
            # Reset the idle deadline while Gemini is still working.
            if streaming:
                idle_deadline = time.time() + timeout_s
            # Consider "ready" when we have a >= 150px image AND Gemini is
            # NOT currently streaming more content.
            if area >= 150 * 150 and not streaming:
                await page.wait_for_timeout(800)  # settle
                # Pick the biggest <img> as our target
                imgs = await page.locator("img").all()
                best, best_area = None, 0
                for img in imgs:
                    try:
                        box = await img.bounding_box()
                        if not box: continue
                        a = box["width"] * box["height"]
                        if a >= 150 * 150 and a > best_area:
                            best, best_area = img, a
                    except Exception: continue
                if best:
                    return best
        except Exception: pass
        await asyncio.sleep(2.0)
    if verbose: print("[bot] timed out waiting for image")
    return None


async def _download_image(page, img_loc, save_to: str, verbose: bool = True) -> Optional[str]:
    """Save the rendered image to disk.

    PRIMARY: hover the image's message bubble and click the toolbar
    Download button. This is the only path that gives us the *real,
    full-size* generated image — Gemini's inline <img> src often points
    at a shared blob that resolves to a preview/thumbnail; every slide's
    preview-blob deserialises to the same bytes as the first one, which
    is how we got 70 identical PNGs on earlier runs.

    FALLBACK: read <img src> and fetch directly (handles data:, blob:,
    http:). Used only if the Download button isn't available.
    """
    save_path = Path(save_to)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    # PRIMARY — SCOPED hover & click. Walk up from THIS img to its
    # message-bubble container, then click the Download button that
    # lives inside THAT bubble. Without scoping, a page-wide Download
    # selector always clicks the FIRST message's Download and every
    # save ends up as the same image.
    try:
        # Use a JS-driven scroll that centers the element in the viewport
        # (Playwright's scroll_into_view_if_needed aligns to top which
        # often leaves the img partly obscured by a sticky header, and
        # hover() then refuses to fire).
        await img_loc.evaluate(
            "el => el.scrollIntoView({block: 'center', inline: 'center', "
            "behavior: 'instant'})"
        )
        await page.wait_for_timeout(500)
        # Dismiss any lingering hover state.
        await page.mouse.move(10, 10)
        await page.wait_for_timeout(200)
        # Move the mouse to the image's bounding-box center manually —
        # this gives the toolbar the hover event even if Playwright's
        # Locator.hover() fails visibility heuristics on a re-rendered
        # response.
        box = None
        try:
            box = await img_loc.bounding_box(timeout=3000)
        except Exception: pass
        if box and box.get("width", 0) > 10 and box.get("height", 0) > 10:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            try:
                await page.mouse.move(cx, cy)
            except Exception: pass
        else:
            try:
                await img_loc.hover(timeout=8000)
            except Exception: pass
        await page.wait_for_timeout(900)

        # Resolve the Download button that is inside the same bubble as
        # this exact img element. We use a JS path that walks up the DOM
        # from the hovered <img> looking for a descendant with a
        # Download-ish aria-label — on each ancestor level.
        clicked = await img_loc.evaluate("""
            (img) => {
                const isDl = (el) => {
                    const a = (el.getAttribute('aria-label')||'').toLowerCase();
                    const d = (el.getAttribute('data-test-id')||'').toLowerCase();
                    const t = (el.textContent||'').trim().toLowerCase();
                    return (a === 'download' || a === 'download image' ||
                            a.startsWith('download ') ||
                            d.includes('download') || t === 'download');
                };
                let node = img;
                while (node && node !== document.body) {
                    node = node.parentElement;
                    if (!node) break;
                    const btns = node.querySelectorAll(
                        'button, [role="button"]'
                    );
                    for (const b of btns) {
                        if (!isDl(b)) continue;
                        const r = b.getBoundingClientRect();
                        if (r.width < 1 || r.height < 1) continue;
                        // Mark it so the Python side can see we matched.
                        b.setAttribute('data-lotus-picked', '1');
                        return true;
                    }
                }
                return false;
            }
        """)

        if clicked:
            dl_button = page.locator('[data-lotus-picked="1"]').first
            try:
                await dl_button.wait_for(state="visible", timeout=6000)
            except Exception: pass
            # Hover the button itself to keep the toolbar from collapsing
            # as we focus it.
            try:
                bb = await dl_button.bounding_box(timeout=2000)
                if bb:
                    await page.mouse.move(bb["x"] + bb["width"]/2,
                                          bb["y"] + bb["height"]/2)
                    await page.wait_for_timeout(300)
            except Exception: pass
            async with page.expect_download(timeout=60_000) as dl_info:
                await dl_button.click(timeout=15_000, force=True)
            # Clear the marker so subsequent slides don't race on it.
            try:
                await page.evaluate(
                    "document.querySelectorAll('[data-lotus-picked]')"
                    ".forEach(el => el.removeAttribute('data-lotus-picked'))"
                )
            except Exception: pass
            dl = await dl_info.value
            tmp = await dl.path()
            shutil.move(str(tmp), str(save_path))
            size = save_path.stat().st_size
            if size > 2048:
                if verbose:
                    print(f"[bot] downloaded via scoped button → {save_path} "
                          f"({size//1024} KB)")
                return str(save_path)
            try: save_path.unlink()
            except Exception: pass
            if verbose:
                print(f"[bot] scoped download returned tiny file ({size}b) — falling back")
        else:
            if verbose:
                print("[bot] no Download button found within this image's bubble — falling back")
    except Exception as e:
        if verbose: print(f"[bot] scoped-download path failed: {e}")

    # FALLBACK 2 — Playwright APIRequestContext fetch. Uses the browser's
    # cookie jar but runs OUTSIDE the page's JS context, so CORS from
    # lh3.googleusercontent.com doesn't apply. For Google image CDN URLs
    # we request the FULL-SIZE original (`=s0`) rather than the inline
    # thumbnail — the <img src> Gemini serves is a downsized preview
    # (~130 KB). Full-size typically runs several MB.
    try:
        src = await img_loc.get_attribute("src")
        if src and src.startswith("http"):
            # Google photo/image CDN URLs accept a size suffix after "=".
            # Strip any existing suffix and request full-resolution.
            if "googleusercontent.com" in src or "ggpht.com" in src:
                # Everything after a trailing "=..." is a size directive
                # (e.g. "=w540-h675-p-k-no"); replace it with "=s0" to get
                # the original resolution.
                base = src.split("=", 1)[0]
                full_src = base + "=s0"
            else:
                full_src = src
            print(f"[bot] trying request-context fetch: {full_src[:100]}")
            ctx = page.context
            resp = await ctx.request.get(full_src, timeout=45_000)
            if resp.ok:
                body = await resp.body()
                if body and len(body) > 2048:
                    save_path.write_bytes(body)
                    if verbose:
                        print(f"[bot] request-context fetched → {save_path} "
                              f"({len(body)//1024} KB)")
                    return str(save_path)
                if verbose:
                    print(f"[bot] request-context got tiny body ({len(body or b'')}b)"
                          f" — retrying with raw src")
                # Try again without the size suffix (some URLs reject =s0).
                if full_src != src:
                    resp = await ctx.request.get(src, timeout=30_000)
                    if resp.ok:
                        body = await resp.body()
                        if body and len(body) > 2048:
                            save_path.write_bytes(body)
                            if verbose:
                                print(f"[bot] request-context (raw) → {save_path} "
                                      f"({len(body)//1024} KB)")
                            return str(save_path)
            else:
                if verbose:
                    print(f"[bot] request-context status {resp.status}")
    except Exception as e:
        if verbose: print(f"[bot] request-context fetch failed: {e}")

    # FALLBACK 3 — direct in-page src fetch (blob:/data: URLs only work here).
    try:
        src = await img_loc.get_attribute("src")
        print(f"[bot] download src (in-page fallback): {str(src)[:100]}")
        if src:
            if src.startswith("data:"):
                import base64
                try:
                    _, b64 = src.split(",", 1)
                    save_path.write_bytes(base64.b64decode(b64))
                    if verbose:
                        print(f"[bot] saved data-URL → {save_path} "
                              f"({save_path.stat().st_size//1024} KB)")
                    return str(save_path)
                except Exception as e:
                    if verbose: print(f"[bot] data: parse failed: {e}")
            if src.startswith("blob:") or src.startswith("http"):
                try:
                    b64 = await page.evaluate(
                        """async (u) => {
                            const r = await fetch(u);
                            const buf = await r.arrayBuffer();
                            const bytes = new Uint8Array(buf);
                            let bin = '';
                            for (let i=0; i<bytes.length; i++)
                                bin += String.fromCharCode(bytes[i]);
                            return btoa(bin);
                        }""", src)
                    import base64
                    if b64:
                        save_path.write_bytes(base64.b64decode(b64))
                        size = save_path.stat().st_size
                        if size > 2048:
                            if verbose:
                                print(f"[bot] fetched src → {save_path} "
                                      f"({size//1024} KB)")
                            return str(save_path)
                        try: save_path.unlink()
                        except Exception: pass
                        if verbose:
                            print(f"[bot] src fetch returned tiny file ({size}b)")
                except Exception as e:
                    if verbose: print(f"[bot] src-fetch path failed: {e}")
    except Exception as e:
        if verbose: print(f"[bot] src lookup failed: {e}")

    # FALLBACK 4 — Canvas capture. If Gemini revoked the blob URL before
    # we got here, the <img> still has the decoded bitmap in memory (blob
    # revocation frees the URL, not the cached image data). Draw it to an
    # offscreen canvas and read as PNG data-URL. Blob-revocation-proof and
    # doesn't depend on a Playwright download event firing.
    try:
        data_url = await img_loc.evaluate("""
            (img) => {
                if (!img || !img.complete || !img.naturalWidth) return null;
                try {
                    const c = document.createElement('canvas');
                    c.width = img.naturalWidth;
                    c.height = img.naturalHeight;
                    c.getContext('2d').drawImage(img, 0, 0);
                    return c.toDataURL('image/png');
                } catch (e) { return 'ERR:' + e.message; }
            }
        """)
        if isinstance(data_url, str) and data_url.startswith("data:image/"):
            import base64
            _, b64 = data_url.split(",", 1)
            save_path.write_bytes(base64.b64decode(b64))
            size = save_path.stat().st_size
            if size > 2048:
                if verbose:
                    print(f"[bot] canvas-capture → {save_path} ({size//1024} KB)")
                return str(save_path)
            try: save_path.unlink()
            except Exception: pass
            if verbose:
                print(f"[bot] canvas-capture returned tiny file ({size}b)")
        elif isinstance(data_url, str) and data_url.startswith("ERR:"):
            if verbose: print(f"[bot] canvas-capture rejected: {data_url[4:]}")
    except Exception as e:
        if verbose: print(f"[bot] canvas-capture path failed: {e}")

    # NO screenshot fallback — element screenshots at browser DPI
    # include the surrounding UI overlay ("Try again" text, toolbar)
    # and look terrible for demo. Better to leave a gap the user can
    # manually regen than ship a wonky screenshot.
    if verbose:
        print("[bot] ALL proper download strategies failed — leaving a gap for manual regen")
    return None


async def _ensure_page_pool(count: int, verbose: bool = True) -> list:
    """Return a list of `count` Playwright Page objects, all in the same
    persistent context (one Chromium, N tabs). Creates new tabs as needed.
    The first page is the main one (index 0); additional tabs are new_page()."""
    await _ensure_browser(verbose=verbose)
    pages = list(_context.pages)
    while len(pages) < count:
        p = await _context.new_page()
        p.set_default_timeout(30_000)
        pages.append(p)
        if verbose:
            print(f"[bot] opened tab #{len(pages)}/{count}")
    # Navigate all pages that aren't already on Gemini.
    for p in pages[:count]:
        try:
            if "gemini.google.com" not in (p.url or ""):
                await p.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            pass
    return pages[:count]


async def _render_on_page(page, prompt: str, save_to: str,
                          wait_timeout: float, verbose: bool,
                          label: str = "") -> Optional[str]:
    """One full render on a specific page: navigate fresh → send → wait → download."""
    tag = f"[tab{label}]" if label else "[bot]"
    try:
        await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1500)
        await _dismiss_modals(page, verbose=False)
    except Exception as e:
        if verbose: print(f"{tag} goto failed: {e}")
        return None
    for attempt in (1, 2):
        if not await _send_prompt(page, prompt, verbose=verbose):
            if attempt == 2: return None
            continue
        img = await _wait_for_image_element(
            page, timeout_s=wait_timeout, verbose=verbose)
        if img:
            return await _download_image(page, img, save_to, verbose=verbose)
        if verbose:
            print(f"{tag} attempt {attempt}/2 timed out" +
                  (" — retrying" if attempt == 1 else " — giving up"))
    return None


async def _send_in_same_chat(page, prompt: str, prev_img_count: int,
                             wait_timeout: float, verbose: bool,
                             label: str = "") -> Optional[object]:
    """Send a prompt IN THE CURRENT CONVERSATION (no navigation). Waits
    for a NEW image to appear (img count > prev_img_count). Returns the
    new image locator or None on timeout/failure.

    Used by the post-batch flow where one tab drains all of a post's 10
    prompts in sequence, keeping every slide in the same Gemini chat."""
    tag = f"[tab{label}]" if label else "[bot]"
    if not await _send_prompt(page, prompt, verbose=verbose):
        if verbose: print(f"{tag} send failed")
        return None
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        try:
            info = await page.evaluate("""
                () => {
                    const imgs = [...document.querySelectorAll('img')]
                        .filter(i => {
                            const r = i.getBoundingClientRect();
                            return r.width >= 150 && r.height >= 150 &&
                                   !!i.src && !i.src.startsWith('chrome-');
                        });
                    return imgs.length;
                }
            """)
            streaming = await page.evaluate("""
                () => !!(document.querySelector('[data-is-streaming="true"]') ||
                         """ + _JS_STOP_BTN_PRESENT + """)
            """)
            if info > prev_img_count and not streaming:
                await page.wait_for_timeout(800)
                imgs = await page.locator("img").all()
                best, best_area = None, 0
                for img in imgs:
                    try:
                        box = await img.bounding_box()
                        if not box: continue
                        a = box["width"] * box["height"]
                        if a >= 150 * 150 and a > best_area:
                            best, best_area = img, a
                    except Exception: continue
                return best
        except Exception: pass
        await asyncio.sleep(2.0)
    if verbose: print(f"{tag} timed out waiting for new image")
    return None


async def _count_images(page) -> int:
    try:
        return await page.evaluate("""
            () => [...document.querySelectorAll('img')]
                .filter(i => {
                    const r = i.getBoundingClientRect();
                    return r.width >= 150 && r.height >= 150 &&
                           !!i.src && !i.src.startsWith('chrome-');
                }).length
        """)
    except Exception:
        return 0


async def _snapshot_image_srcs(page) -> set:
    """Return the set of `src` URLs of every image currently in the DOM
    (no size filter — prior slides may be scrolled off-screen with zero
    bounding-box and would otherwise slip through as 'new'). Used as a
    before-send baseline so we can identify which image is genuinely NEW
    after a prompt."""
    try:
        srcs = await page.evaluate("""
            () => [...document.querySelectorAll('img')]
                .map(i => i.src || i.currentSrc || '')
                .filter(s => !!s && !s.startsWith('chrome-'))
        """)
        return set(srcs or [])
    except Exception:
        return set()


async def _find_new_image_locator(page, seen_srcs: set, verbose: bool = False):
    """Find a Playwright Locator for the LATEST chat-message image.

    Picks the image that is LAST in document order among images whose src
    isn't in `seen_srcs`. Doc-order-last is the most recent message bubble
    — important because Gemini re-allocates blob URLs for PRIOR messages
    too when a new turn arrives, so a simple src-set diff can light up
    multiple candidates. The latest one is always furthest down the DOM.
    Returns None if no new image is present."""
    try:
        info = await page.evaluate("""
            (seen) => {
                const seenSet = new Set(seen || []);
                const all = [...document.querySelectorAll('img')]
                    .map((i, idx) => {
                        const r = i.getBoundingClientRect();
                        const s = i.src || i.currentSrc || '';
                        return {src: s, area: r.width * r.height,
                                w: r.width, h: r.height, dom_idx: idx};
                    })
                    .filter(o => !!o.src && !o.src.startsWith('chrome-'));
                const newOnes = all.filter(o => !seenSet.has(o.src));
                const big = newOnes.filter(o => o.w >= 150 && o.h >= 150);
                // Doc-order LAST = most recently appended = latest message bubble.
                big.sort((a, b) => b.dom_idx - a.dom_idx);
                return {
                    total: all.length,
                    new_total: newOnes.length,
                    new_big: big.length,
                    picked: big.length ? big[0].src : null,
                    picked_area: big.length ? big[0].area : 0,
                    picked_idx: big.length ? big[0].dom_idx : -1,
                };
            }
        """, list(seen_srcs))
        if verbose:
            print(f"[diag] imgs total={info.get('total')} "
                  f"new={info.get('new_total')} new_big={info.get('new_big')} "
                  f"picked_idx={info.get('picked_idx')} "
                  f"picked_area={info.get('picked_area')} "
                  f"picked={(info.get('picked') or '')[:80]}")
        new_src = info.get("picked") if info else None
        if not new_src:
            return None
        # Target THIS specific element by DOM index, not by src — src may
        # mutate again between now and the download.
        locs = page.locator("img")
        try:
            return locs.nth(info["picked_idx"])
        except Exception:
            return page.locator(f'img[src="{new_src}"]').last
    except Exception as e:
        if verbose: print(f"[diag] find-new-image err: {e}")
        return None


async def _find_image_index_for_prompt(page, prompt_text: str,
                                        verbose: bool = False) -> int:
    """Return the index into `document.querySelectorAll('img')` of the
    model-response image that corresponds to the user message containing
    `prompt_text`.

    We anchor on the PROMPT text because it's stable — it renders into
    the user's bubble and stays there for the whole conversation, even
    as Gemini rewrites blob URLs on the model side. For each prompt we
    find the user-turn element, then walk forward through the DOM to
    the next <img> that's large enough to be a generated image.

    Returns -1 if not found.
    """
    try:
        # NB2 prompts all share the opening phrase
        # "Ultra-photorealistic technical engineering cutaway illustration"
        # (~65 chars) so a short prefix produces ambiguous matches and
        # slides with the same prefix stomp each other's img_idx. Use 200
        # chars so we get well past the prompt-body divergence point.
        needle = (prompt_text or "").strip()[:200]
        if not needle:
            return -1
        idx = await page.evaluate("""
            (needle) => {
                const n = needle.toLowerCase();
                const imgs = Array.from(document.querySelectorAll('img'));
                // All elements whose inner text contains the needle AND
                // whose own text isn't huge (avoid matching <body>).
                const MAX_HOST_LEN = Math.max(n.length * 8, 4000);
                const candidates = [];
                const tw = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_ELEMENT, null);
                let el = tw.nextNode();
                while (el) {
                    const txt = (el.innerText || '').toLowerCase();
                    if (txt.length <= MAX_HOST_LEN && txt.includes(n)) {
                        candidates.push(el);
                    }
                    el = tw.nextNode();
                }
                if (!candidates.length) return -1;
                // The SMALLEST matching element (deepest in the tree) is
                // the user-bubble itself, not an ancestor container.
                candidates.sort((a, b) =>
                    (a.innerText||'').length - (b.innerText||'').length);
                const bubble = candidates[0];
                // Walk forward in document order to find the next <img>
                // with render-worthy dimensions.
                function nextElement(node) {
                    if (node.firstElementChild) return node.firstElementChild;
                    while (node) {
                        if (node.nextElementSibling) return node.nextElementSibling;
                        node = node.parentElement;
                    }
                    return null;
                }
                let cur = nextElement(bubble);
                while (cur) {
                    if (cur.tagName === 'IMG') {
                        const r = cur.getBoundingClientRect();
                        const w = Math.max(r.width, cur.naturalWidth || 0);
                        const h = Math.max(r.height, cur.naturalHeight || 0);
                        const s = cur.src || cur.currentSrc || '';
                        if (w >= 150 && h >= 150 && s && !s.startsWith('chrome-')) {
                            return imgs.indexOf(cur);
                        }
                    }
                    cur = nextElement(cur);
                }
                return -1;
            }
        """, needle)
        if verbose:
            print(f"[prompt-match] prefix={needle[:40]!r} → img_idx={idx}")
        return int(idx) if idx is not None else -1
    except Exception as e:
        if verbose: print(f"[prompt-match] err: {e}")
        return -1


async def _enable_thinking_mode(page, verbose: bool = False) -> bool:
    """Switch Gemini's model-selector to a Thinking / Pro variant so image
    generations use the higher-quality (slower) pipeline. "Thinking" in
    Gemini is a MODEL, not a tool chip — user must click the model
    dropdown (shows current model name) and pick "2.5 Pro" or a similar
    variant labelled with "Thinking" / "Pro" / "Deep".

    Returns True when we clicked a Thinking/Pro model option.
    """
    try:
        await _dismiss_modals(page, verbose=False)
    except Exception: pass

    # Step 1 — open the model-selector dropdown. We identify it as a
    # visible button whose aria-label or text is a Gemini model name
    # (contains "Gemini", "Flash", "Pro", "2.0", "2.5"), with aria-haspopup
    # or an adjacent chevron.
    try:
        opened_info = await page.evaluate("""
            () => {
                const want = (s) => {
                    s = (s||'').toLowerCase().trim();
                    return /\\b(flash|pro|gemini|2\\.\\d|think|model)\\b/.test(s);
                };
                const btns = Array.from(document.querySelectorAll(
                    'button, [role="button"], [role="combobox"]'
                ));
                // Prefer buttons that look like mode/model pickers.
                const scored = btns.map(b => {
                    const a = (b.getAttribute('aria-label')||'').trim();
                    const t = (b.textContent||'').trim();
                    const hp = b.getAttribute('aria-haspopup') || '';
                    const d = (b.getAttribute('data-test-id')||'').toLowerCase();
                    let score = 0;
                    if (hp) score += 2;
                    if (d.includes('mode') || d.includes('model')) score += 3;
                    if (want(a) || want(t)) score += 2;
                    const r = b.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) score = -1;
                    return {b, a, t, score};
                }).filter(x => x.score > 0).sort((x, y) => y.score - x.score);
                if (!scored.length) return {opened: false, labels: []};
                const best = scored[0];
                best.b.click();
                return {opened: true, clicked_label: best.a || best.t || '(no label)'};
            }
        """)
    except Exception as e:
        if verbose: print(f"[bot] thinking-mode dropdown open err: {e}")
        opened_info = {"opened": False}

    if not opened_info or not opened_info.get("opened"):
        if verbose: print("[bot] thinking-mode: model dropdown not found")
        return False
    if verbose:
        print(f"[bot] thinking-mode: opened model dropdown ({opened_info.get('clicked_label')!r})")

    await page.wait_for_timeout(700)

    # Step 2 — within the opened menu, click an option whose label says
    # "Thinking" or contains "Pro" / "Deep" (but not "Flash").
    try:
        pick = await page.evaluate("""
            () => {
                const items = Array.from(document.querySelectorAll(
                    '[role="menuitem"], [role="option"], [role="radio"], li button, li'
                ));
                const scoreLbl = (s) => {
                    s = (s||'').toLowerCase();
                    if (s.includes('flash')) return -10;
                    let sc = 0;
                    if (s.includes('think')) sc += 5;
                    if (s.includes('deep')) sc += 3;
                    if (s.includes('pro')) sc += 2;
                    if (s.includes('2.5')) sc += 1;
                    return sc;
                };
                const ranked = items.map(el => {
                    const a = (el.getAttribute('aria-label')||'');
                    const t = (el.textContent||'');
                    const r = el.getBoundingClientRect();
                    const visible = r.width > 0 && r.height > 0;
                    return {el, a, t, score: scoreLbl(a + ' ' + t), visible};
                }).filter(x => x.visible && x.score > 0)
                  .sort((x, y) => y.score - x.score);
                if (!ranked.length) {
                    // Return what we saw so the caller can diagnose.
                    const seen = items.slice(0, 30).map(el => (
                        (el.getAttribute('aria-label')||'') ||
                        (el.textContent||'').trim().slice(0, 40)
                    ));
                    return {picked: false, seen};
                }
                const best = ranked[0];
                best.el.click();
                return {picked: true,
                        label: (best.a || best.t || '').trim().slice(0, 80)};
            }
        """)
    except Exception as e:
        if verbose: print(f"[bot] thinking-mode menu click err: {e}")
        pick = {"picked": False}

    # Dismiss anything still open.
    try: await page.keyboard.press("Escape")
    except Exception: pass
    await page.wait_for_timeout(400)

    if pick and pick.get("picked"):
        if verbose: print(f"[bot] thinking-mode: selected {pick.get('label')!r}")
        return True
    if verbose:
        print(f"[bot] thinking-mode: dropdown opened but no Thinking item matched. "
              f"Seen: {pick.get('seen') if pick else 'n/a'}")
        # Dump what buttons / menu-items ARE on the page so we can pick the
        # right selector next iteration.
        try:
            labels = await page.evaluate("""
                () => Array.from(document.querySelectorAll(
                    'button, [role="button"], [role="menuitem"], [role="option"]'
                ))
                    .map(el => {
                        const a = (el.getAttribute('aria-label')||'').trim();
                        const t = (el.textContent||'').trim();
                        const d = (el.getAttribute('data-test-id')||'').trim();
                        return (a || t || d || '').slice(0, 60);
                    })
                    .filter(s => s.length > 0 && s.length < 60)
                    .slice(0, 60)
            """)
            print("[bot] thinking-mode: no control found. Button labels on page:")
            for lbl in labels:
                print(f"   • {lbl}")
        except Exception as e:
            print(f"[bot] thinking-mode label dump failed: {e}")
    return False


async def _select_create_image_tool(page, verbose: bool = False) -> bool:
    """Open Gemini's tool picker and select "Create image" so the conversation
    defaults to image-generation. Returns True on success, False if the picker
    or the option couldn't be located. Mirrors `_enable_thinking_mode`'s
    two-step (open dropdown → pick item) pattern; never raises.
    """
    try:
        await _dismiss_modals(page, verbose=False)
    except Exception:
        pass

    # Step 1 — open the tool picker. Score buttons whose aria-label / text
    # suggests a tool/attachment menu; prefer ones near the prompt input.
    try:
        opened = await page.evaluate("""
            () => {
                const cands = Array.from(document.querySelectorAll(
                    'button, [role="button"]'
                ));
                const rank = (b) => {
                    const a = (b.getAttribute('aria-label')||'').toLowerCase().trim();
                    const t = (b.textContent||'').trim();
                    const d = (b.getAttribute('data-test-id')||'').toLowerCase();
                    let s = 0;
                    if (/^create image$/i.test(a) || /^create image$/i.test(t)) s += 10;
                    if (/(tool|tools)/.test(a)) s += 4;
                    if (/(add|plus|attach|more)/.test(a)) s += 2;
                    if (a === '+' || t === '+') s += 3;
                    if (d.includes('tool')) s += 3;
                    const r = b.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) s = -1;
                    return s;
                };
                const scored = cands.map(b => ({b, s: rank(b)}))
                    .filter(x => x.s > 0)
                    .sort((x, y) => y.s - x.s);
                if (!scored.length) return {opened: false};
                const best = scored[0];
                best.b.click();
                return {
                    opened: true,
                    clicked: (best.b.getAttribute('aria-label') ||
                              best.b.textContent || '').trim().slice(0, 60),
                    direct: (best.s >= 10),
                };
            }
        """)
    except Exception as e:
        if verbose: print(f"[bot] create-image picker err: {e}")
        opened = {"opened": False}

    if not opened or not opened.get("opened"):
        if verbose: print("[bot] create-image: tool picker not found")
        return False
    if verbose:
        print(f"[bot] create-image: opened tool picker ({opened.get('clicked')!r})")

    # If we already clicked "Create image" directly (button on page, not menu),
    # we're done.
    if opened.get("direct"):
        await page.wait_for_timeout(400)
        if verbose: print("[bot] create-image: selected directly (no menu)")
        return True

    await page.wait_for_timeout(500)

    # Step 2 — click the "Create image" option in the opened menu.
    try:
        pick = await page.evaluate("""
            () => {
                const items = Array.from(document.querySelectorAll(
                    '[role="menuitem"], [role="option"], li button, li, button'
                ));
                const score = (s) => {
                    s = (s||'').toLowerCase().trim();
                    let sc = 0;
                    if (/create.*image|generate.*image/.test(s)) sc += 8;
                    if (s === 'image' || s === 'images') sc += 6;
                    return sc;
                };
                const ranked = items.map(el => {
                    const a = (el.getAttribute('aria-label')||'');
                    const t = (el.textContent||'');
                    const r = el.getBoundingClientRect();
                    return {el, a, t,
                            score: score(a + ' ' + t),
                            visible: r.width > 0 && r.height > 0};
                }).filter(x => x.visible && x.score > 0)
                  .sort((x, y) => y.score - x.score);
                if (!ranked.length) {
                    const seen = items.slice(0, 30).map(el => (
                        (el.getAttribute('aria-label')||'') ||
                        (el.textContent||'').trim().slice(0, 40)
                    )).filter(Boolean);
                    return {picked: false, seen};
                }
                const best = ranked[0];
                best.el.click();
                return {picked: true,
                        label: (best.a || best.t || '').trim().slice(0, 80)};
            }
        """)
    except Exception as e:
        if verbose: print(f"[bot] create-image menu click err: {e}")
        pick = {"picked": False}

    try: await page.keyboard.press("Escape")
    except Exception: pass
    await page.wait_for_timeout(300)

    if pick and pick.get("picked"):
        if verbose: print(f"[bot] create-image: selected {pick.get('label')!r}")
        return True
    if verbose:
        print("[bot] create-image: tool picker opened but no Create-image item matched. "
              f"Seen: {pick.get('seen') if pick else 'n/a'}")
    return False


async def _start_new_chat(page, verbose: bool = False) -> bool:
    """Open a fresh Gemini conversation so the caller gets an empty chat
    history. Tries the sidebar "New chat" control first; falls back to a
    cache-busting navigation that forces a new thread."""
    # 1) Try clicking the sidebar "New chat" button.
    try:
        clicked = await page.evaluate("""
            () => {
                const btn = Array.from(document.querySelectorAll(
                    'button, a'
                )).find(el => {
                    const a = (el.getAttribute('aria-label')||'').toLowerCase().trim();
                    const d = (el.getAttribute('data-test-id')||'').toLowerCase().trim();
                    const tt = (el.getAttribute('mattooltip')||'').toLowerCase().trim();
                    return a === 'new chat' || a === 'start new chat' ||
                           a === 'new conversation' ||
                           d === 'new-chat-button' ||
                           tt === 'new chat';
                });
                if (!btn) return false;
                const r = btn.getBoundingClientRect();
                if (r.width < 1 || r.height < 1) return false;
                btn.click();
                return true;
            }
        """)
        if clicked:
            await page.wait_for_timeout(1200)
            if verbose: print("[bot] new chat opened via sidebar button")
            return True
    except Exception:
        pass
    # 2) Fallback — goto /app with a cache-busting hash so Gemini treats it
    # as a fresh load (most reliable way to clear conversation state).
    try:
        await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1200)
        if verbose: print("[bot] new chat via page.goto reload")
        return True
    except Exception as e:
        if verbose: print(f"[bot] new-chat fallback failed: {e}")
        return False


async def _click_regenerate(page, verbose: bool = False) -> bool:
    """Click Gemini's 'Regenerate' / 'Redo' / 'Try again' button on the
    most recent assistant response. Used when a prompt comes back as a
    text-only answer instead of an image — Gemini's Thinking model often
    does research/report instead of gen on the first try, and clicking
    regenerate kicks it into image mode."""
    try:
        # Look for the button among model-response action buttons. The
        # aria-label is typically "Regenerate response" or similar; the
        # text or tooltip is "Regenerate". Match all three ways.
        clicked = await page.evaluate("""
            () => {
                const wantsRegen = (s) => {
                    s = (s||'').toLowerCase().trim();
                    return s === 'regenerate' ||
                           s.startsWith('regenerate ') ||
                           s === 'regenerate response' ||
                           s === 'redo' ||
                           s.startsWith('redo ') ||
                           s === 'try again' ||
                           s === 'rewrite';
                };
                // Find ALL candidate buttons, then pick the one furthest
                // down in the DOM (most recent response).
                const all = Array.from(document.querySelectorAll(
                    'button, [role="button"], [role="menuitem"]'
                )).filter(b => {
                    const a = b.getAttribute('aria-label') || '';
                    const t = b.textContent || '';
                    const tt = b.getAttribute('mattooltip') || '';
                    if (!(wantsRegen(a) || wantsRegen(t) || wantsRegen(tt))) return false;
                    const r = b.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                if (!all.length) {
                    // Alternate path — the regenerate control may be
                    // hidden inside a "More options" overflow menu that
                    // pops up on hover. Try clicking that first, then
                    // requery.
                    const more = Array.from(document.querySelectorAll(
                        'button[aria-label*="more" i], button[aria-label*="options" i]'
                    )).filter(b => {
                        const r = b.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    if (more.length) {
                        more[more.length - 1].click();
                    }
                    return false;
                }
                all[all.length - 1].click();
                return true;
            }
        """)
        if clicked:
            if verbose: print("[bot] clicked Regenerate on last response")
            await page.wait_for_timeout(500)
            return True
        # Second pass — if an overflow menu was opened by the first try,
        # the Regenerate item may now be visible as a menuitem.
        clicked2 = await page.evaluate("""
            () => {
                const items = Array.from(document.querySelectorAll(
                    '[role="menuitem"], [role="menuitemradio"]'
                )).filter(el => {
                    const a = (el.getAttribute('aria-label') || '').toLowerCase();
                    const t = (el.textContent || '').toLowerCase();
                    return a.includes('regenerate') || a.includes('redo') ||
                           t.includes('regenerate') || t.includes('redo');
                });
                if (items.length) {
                    items[items.length - 1].click();
                    return true;
                }
                return false;
            }
        """)
        if clicked2:
            if verbose: print("[bot] clicked Regenerate from overflow menu")
            await page.wait_for_timeout(500)
            return True
    except Exception as e:
        if verbose: print(f"[bot] regenerate click err: {e}")
    if verbose: print("[bot] no Regenerate button found")
    return False


async def _wait_gemini_idle(page, max_wait_s: float = 420.0,
                            verbose: bool = False) -> bool:
    """Block until Gemini is NOT currently generating — i.e. there's no
    Stop / blue-square button and no streaming indicator.

    Note: we do NOT require a Send button to be ENABLED. On a fresh chat
    with an empty input, Gemini's send button is DISABLED by design until
    you type text (the button then flips from a mic icon to an arrow).
    Idle = simply the absence of an in-progress generation.
    """
    deadline = time.time() + max_wait_s
    last_log = 0
    while time.time() < deadline:
        try:
            # Returns {busy: bool, why: str} — the `why` tells us which
            # selector matched so we can diagnose false positives.
            status = await page.evaluate("""
                () => {
                    // Gemini's REAL stop button's aria-label is exactly
                    // "Stop" or "Stop response" / "Stop generating". Use
                    // exact-or-starts-with matching so we DON'T match the
                    // three-dots "More options for <prompt-that-contains-
                    // -the-word-stop>" buttons on previous chat bubbles.
                    const stopBtn = Array.from(document.querySelectorAll(
                        'button[aria-label], button[data-testid]'
                    )).find(b => {
                        const a = (b.getAttribute('aria-label') || '').trim();
                        const t = (b.getAttribute('data-testid') || '').trim();
                        const al = a.toLowerCase();
                        const tl = t.toLowerCase();
                        // Exact "stop" or starts with "stop " + visible.
                        const labelMatch = al === 'stop' ||
                                           al.startsWith('stop ') ||
                                           al === 'stop response' ||
                                           al === 'stop generating';
                        const testidMatch = tl === 'stop-button' ||
                                            tl === 'stop_button' ||
                                            tl.endsWith('-stop') ||
                                            tl.endsWith('_stop');
                        if (!labelMatch && !testidMatch) return false;
                        const r = b.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    if (stopBtn) {
                        return {busy: true, why: 'stop-btn:' +
                            (stopBtn.getAttribute('aria-label') || '') +
                            '|testid:' + (stopBtn.getAttribute('data-testid') || '')};
                    }
                    const stream = document.querySelector('[data-is-streaming="true"]');
                    if (stream) {
                        return {busy: true, why: 'streaming-attr:' + (stream.tagName||'')};
                    }
                    return {busy: false, why: ''};
                }
            """)
            busy = bool(status and status.get("busy"))
            if not busy:
                # Settle briefly for post-streaming animations.
                await page.wait_for_timeout(800)
                return True
            if verbose and (time.time() - last_log) > 20:
                why = (status or {}).get("why", "")
                print(f"[idle] still busy ({int(deadline - time.time())}s left) — {why[:120]}")
                last_log = time.time()
        except Exception: pass
        await asyncio.sleep(2.0)
    if verbose: print("[idle] still busy at timeout")
    return False


async def _send_same_chat_strict(page, prompt: str,
                                 verbose: bool = True,
                                 max_wait_s: float = 90.0) -> bool:
    """Send a prompt in the CURRENT chat conversation. Does NOT reload
    the tab on failure (which would wipe chat history). Just scrolls to
    the bottom, waits for the input to become enabled, fills, submits.
    Returns True only if the prompt was actually submitted."""
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        try:
            # Make sure the input is in view (Gemini's chat scrolls past it
            # as history grows).
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
            await page.wait_for_timeout(400)
            # Dismiss any rate-this-response / onboarding modals.
            await _dismiss_modals(page, verbose=False)
            inp = await _find_input(page)
            if not inp:
                await asyncio.sleep(1.5)
                continue
            is_disabled = await page.evaluate("""
                () => {
                    const el = document.querySelector(
                        'rich-textarea [contenteditable="true"], '+
                        'div[contenteditable="true"][role="textbox"], textarea'
                    );
                    if (!el) return true;
                    if (el.getAttribute('aria-disabled') === 'true') return true;
                    if (el.disabled) return true;
                    const r = el.getBoundingClientRect();
                    if (r.width < 50 || r.height < 10) return true;  // off-screen / collapsed
                    return false;
                }
            """)
            if is_disabled:
                await asyncio.sleep(1.5)
                continue
            # Input is enabled — but the send button may still be a MIC
            # icon (empty state) or a SQUARE (streaming). We'll type the
            # text first (mic → arrow transition), THEN wait for the arrow
            # and click it explicitly.
            try:
                await inp.scroll_into_view_if_needed(timeout=3000)
                await inp.click(timeout=4000)
                await page.keyboard.press("Meta+A")
                await page.wait_for_timeout(80)
                await page.keyboard.press("Delete")
                await page.wait_for_timeout(120)
                try:
                    await inp.fill(prompt, timeout=6000)
                except Exception:
                    await page.keyboard.insert_text(prompt)
                # Text is in the input; Gemini should now render the arrow
                # send button. Poll for it (up to 10s) and then click it.
                arrow_deadline = time.time() + 10
                arrow_clicked = False
                while time.time() < arrow_deadline:
                    clicked = await page.evaluate("""
                        () => {
                            // Refuse if the model is still generating.
                            if (""" + _JS_STOP_BTN_PRESENT + """) return false;
                            const send = document.querySelector(
                                'button[aria-label="Send" i], '+
                                'button[aria-label*="Submit" i], '+
                                'button[aria-label*="Send message" i], '+
                                'button[mattooltip*="Send" i], '+
                                'button[data-testid*="send" i]'
                            );
                            if (!send) return false;
                            if (send.disabled) return false;
                            if (send.getAttribute('aria-disabled') === 'true') return false;
                            send.click();
                            return true;
                        }
                    """)
                    if clicked:
                        arrow_clicked = True
                        break
                    await asyncio.sleep(0.4)
                if not arrow_clicked:
                    # Arrow never appeared — fallback to Enter, which also
                    # submits in Gemini's input (but only when the button is
                    # in its send-state, not mic-state).
                    await page.keyboard.press("Enter")
                    if verbose:
                        print(f"[send] arrow didn't appear — fell back to Enter")
                if verbose: print(f"[send] prompt sent ({len(prompt)} chars)")
                return True
            except Exception as e:
                if verbose: print(f"[send]   attempt failed: {type(e).__name__}: {str(e)[:160]}")
                await asyncio.sleep(2.0)
                continue
        except Exception:
            await asyncio.sleep(1.5)
    if verbose: print("[send] input never became ready — giving up on this slide")
    return False


async def _wait_input_ready(page, max_wait_s: float = 45.0) -> bool:
    """Poll for the Gemini input box to be enabled + empty + accepting text.
    Used between sends in the 'burst send' flow so we don't try to paste
    while Gemini is still rendering the previous response."""
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        try:
            inp = await _find_input(page)
            if inp:
                # Check it's focusable and has no text.
                try:
                    content = await inp.evaluate("el => el.innerText || el.value || ''")
                except Exception:
                    content = ""
                is_disabled = await page.evaluate("""
                    () => {
                        const inp = document.querySelector(
                            'rich-textarea [contenteditable="true"], '+
                            'div[contenteditable="true"][role="textbox"], textarea'
                        );
                        if (!inp) return true;
                        if (inp.getAttribute('aria-disabled') === 'true') return true;
                        if (inp.disabled) return true;
                        return false;
                    }
                """)
                if not is_disabled:
                    return True
        except Exception: pass
        await asyncio.sleep(1.0)
    return False


async def _do_post_sequential_mode(
    post_group: list, wait_timeout: float, verbose: bool, on_done=None,
) -> list:
    """Two-phase single-post flow.

    Phase 1 — GENERATE ALL:
      Start a fresh Gemini chat, then for each slide in order:
        a. Wait for idle (no streaming)
        b. Send the prompt
        c. Wait for the next image count to go up (i.e., this slide's
           image has finished rendering) before firing the next prompt.
      We do NOT try to save images during generation — Gemini rewrites
      blob URLs as the chat grows and `<img src>` stops uniquely
      identifying each turn, producing duplicate saves.

    Phase 2 — DOWNLOAD ALL:
      Scroll the chat from the top and, for each model-response image
      in document order, hover to reveal the toolbar and click Download.
      Per-image download via the UI button is the only route that
      returns the CORRECT bytes for each turn.

    `post_group` = list of (prompt, save_to). Returns saved paths (or None).
    """
    page = await _ensure_browser(verbose=verbose)
    n = len(post_group)
    if not n: return []

    try:
        # Force a fresh Gemini conversation so this post's 10 slides own
        # the chat history (not a stale one from a prior run).
        await _start_new_chat(page, verbose=verbose)
        await page.wait_for_timeout(1500)
        await _dismiss_modals(page, verbose=False)
        # Switch to Thinking mode for higher-quality image generation.
        await _enable_thinking_mode(page, verbose=verbose)
        # Select the Create-image tool so Gemini defaults to image gen.
        await _select_create_image_tool(page, verbose=verbose)
    except Exception as e:
        if verbose: print(f"[seq] new-chat failed: {e}")
        return [None] * n

    results: list = [None] * n

    # ── Phase 1: send all N prompts in one conversation ─────────────
    baseline_count = await _count_images(page)
    expected = baseline_count  # grows by ~1 per successful slide
    sent_indices: list = []
    for i, (prompt, _save_to) in enumerate(post_group):
        if verbose: print(f"[seq] phase1 slide {i+1}/{n} — waiting for idle…")
        await _wait_gemini_idle(page, max_wait_s=wait_timeout, verbose=verbose)

        if verbose: print(f"[seq] phase1 slide {i+1}/{n} — sending ({len(prompt)} chars)")
        if not await _send_same_chat_strict(page, prompt, verbose=verbose):
            if verbose: print(f"[seq] phase1 slide {i+1} — SEND FAILED, moving on")
            if on_done:
                try: on_done(0, i, None)
                except Exception: pass
            continue

        # Wait until the image count grows past `expected` (+1 for this
        # slide) AND streaming has stopped — so we don't fire slide N+1
        # while slide N is still rendering.
        #
        # If the response stops streaming WITHOUT an image appearing,
        # Gemini gave us a text-only answer (happens a lot in Thinking
        # mode). Click Regenerate and try again — up to MAX_REGEN times.
        expected += 1
        if verbose: print(f"[seq] phase1 slide {i+1}/{n} — waiting for image count ≥ {expected}…")
        grew = False
        MAX_REGEN = 2
        regens = 0
        per_attempt_timeout = max(200.0, wait_timeout / (MAX_REGEN + 1))
        while regens <= MAX_REGEN and not grew:
            attempt_deadline = time.time() + per_attempt_timeout
            stalled_at_idle = False
            while time.time() < attempt_deadline:
                cnt = await _count_images(page)
                streaming = await page.evaluate("""
                    () => !!(document.querySelector('[data-is-streaming="true"]') ||
                             """ + _JS_STOP_BTN_PRESENT + """)
                """)
                if cnt >= expected and not streaming:
                    grew = True
                    break
                if not streaming and cnt < expected:
                    # Gemini finished responding but no image — text-only.
                    stalled_at_idle = True
                    break
                await asyncio.sleep(3.0)
            if grew: break
            if stalled_at_idle and regens < MAX_REGEN:
                regens += 1
                if verbose:
                    print(f"[seq] phase1 slide {i+1} — text-only response, "
                          f"clicking Regenerate (attempt {regens}/{MAX_REGEN})")
                try:
                    await _click_regenerate(page, verbose=verbose)
                except Exception: pass
                # Wait briefly for the regenerate to start streaming again.
                await asyncio.sleep(4.0)
            else:
                break
        if grew:
            sent_indices.append(i)
            if verbose: print(f"[seq] phase1 slide {i+1} — rendered (imgs now ≥ {expected})")
        else:
            # Image never appeared — roll the expected counter back so the
            # next slide's count check isn't offset.
            expected -= 1
            if verbose: print(f"[seq] phase1 slide {i+1} — IMAGE TIMEOUT after "
                              f"{regens} regenerate(s), skipping download")
            if on_done:
                try: on_done(0, i, None)
                except Exception: pass

    if not sent_indices:
        return results

    # ── Phase 2: download each image in order via hover + Download ──
    if verbose: print(f"[seq] phase2 — downloading {len(sent_indices)} image(s) "
                      "via hover+Download button")
    # Scroll to the top of the chat so the first generated image is in
    # view. The page locator queries work regardless, but hovering needs
    # the target to be visible in the viewport.
    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(400)
    except Exception: pass

    # Anchor each slide on its own prompt text (which is stable in the
    # user-bubble) and walk forward to that turn's image. Doc-order index
    # fallback is used only if the prompt-text match misses.
    all_indices = await page.evaluate("""
        () => {
            const out = [];
            document.querySelectorAll('img').forEach((i, idx) => {
                const r = i.getBoundingClientRect();
                const s = i.src || i.currentSrc || '';
                if (!s || s.startsWith('chrome-')) return;
                const w = Math.max(r.width, i.naturalWidth || 0);
                const h = Math.max(r.height, i.naturalHeight || 0);
                if (w >= 150 && h >= 150) out.push(idx);
            });
            return out;
        }
    """)
    fallback_indices = all_indices[-len(sent_indices):] if all_indices else []
    if verbose: print(f"[seq] phase2 — {len(all_indices)} generated image(s) in chat, "
                      f"{len(sent_indices)} to download")

    used_idx_set = set()
    for k, slide_idx in enumerate(sent_indices):
        prompt, save_to = post_group[slide_idx]
        # Primary: find the img that belongs to THIS prompt's user bubble.
        img_idx = await _find_image_index_for_prompt(page, prompt, verbose=verbose)
        if img_idx < 0 or img_idx in used_idx_set:
            # Fallback: doc-order position among the trailing generated imgs.
            if k < len(fallback_indices):
                img_idx = fallback_indices[k]
        if img_idx < 0 or img_idx in used_idx_set:
            if verbose: print(f"[seq] phase2 slide {slide_idx+1} — no matching DOM image")
            if on_done:
                try: on_done(0, slide_idx, None)
                except Exception: pass
            continue
        used_idx_set.add(img_idx)
        img_loc = page.locator("img").nth(img_idx)
        path = await _download_image(page, img_loc, save_to, verbose=verbose)
        results[slide_idx] = path
        if verbose:
            print(f"[seq] phase2 slide {slide_idx+1} img#{img_idx} — "
                  f"{'saved → ' + str(path) if path else 'DOWNLOAD FAILED'}")
        if on_done:
            try: on_done(0, slide_idx, path)
            except Exception: pass

    return results


def create_post_burst(post_group: list,
                      wait_timeout: Optional[float] = None,
                      verbose: bool = True, on_done=None) -> list:
    """Public sync entry — one post, sequential same-chat flow.
    Each slide: wait for idle → send → wait for image → save → next.
    `post_group` = list of (prompt, save_to). Returns list of saved paths."""
    if not post_group: return []
    if wait_timeout is None:
        longest = max(len(p) for p, _ in post_group)
        wait_timeout = 300.0 if longest < 1500 else 360.0 if longest < 2500 else 420.0
    with _worker_lock:
        return _submit(
            _do_post_sequential_mode(post_group, wait_timeout, verbose, on_done),
            timeout=wait_timeout * len(post_group) + 300.0,
        )


async def _do_create_post_batches(
    post_groups: list, concurrency: int, wait_timeout: float,
    verbose: bool, on_done=None,
) -> list:
    """Drain `concurrency` posts in parallel — each post's 10 prompts are
    sent SEQUENTIALLY in ONE tab (one Gemini conversation), so chat
    history stays aligned per post.

    `post_groups` is a list of posts, each post is a list of
    (prompt, save_to) tuples (up to 10 per post).

    `on_done(post_idx, slide_idx, path_or_None)` fires as each slide lands.
    Returns a flat [(post_idx, slide_idx, path_or_None)] list.
    """
    pages = await _ensure_page_pool(concurrency, verbose=verbose)
    results: list = []
    sem = asyncio.Semaphore(concurrency)

    async def _do_one_post(post_idx: int, page, post_tasks: list):
        async with sem:
            tag = str(post_idx + 1)
            # Stagger start: first tab at 0s, second at +6s, third +12s…
            if post_idx > 0:
                await asyncio.sleep(6.0 * post_idx)
            if verbose:
                print(f"[tab{tag}] starting post — {len(post_tasks)} slide(s)")
            # Open a fresh chat so this post owns the conversation history.
            try:
                await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1500)
                await _dismiss_modals(page, verbose=False)
            except Exception as e:
                if verbose: print(f"[tab{tag}] initial goto failed: {e}")
                # Fail the whole post cleanly.
                for si, _ in enumerate(post_tasks):
                    if on_done: on_done(post_idx, si, None)
                return

            prev_count = await _count_images(page)
            for slide_idx, (prompt, save_to) in enumerate(post_tasks):
                # Small pause between slides so Google doesn't see a pure burst.
                if slide_idx > 0:
                    await asyncio.sleep(2.5)
                if verbose:
                    print(f"[tab{tag}] slide {slide_idx+1}/{len(post_tasks)} "
                          f"({len(prompt)} chars)")
                img = await _send_in_same_chat(
                    page, prompt, prev_count, wait_timeout, verbose, label=tag)
                if img is None:
                    if on_done: on_done(post_idx, slide_idx, None)
                    results.append((post_idx, slide_idx, None))
                    # Re-count to try to sync anyway
                    prev_count = await _count_images(page)
                    continue
                path = await _download_image(page, img, save_to, verbose=verbose)
                if on_done: on_done(post_idx, slide_idx, path)
                results.append((post_idx, slide_idx, path))
                prev_count = await _count_images(page)

            if verbose:
                print(f"[tab{tag}] post complete")

    coros = [_do_one_post(i, pages[i], g) for i, g in enumerate(post_groups)]
    await asyncio.gather(*coros, return_exceptions=True)
    return results


async def _do_create_images_batch(
    tasks: list, concurrency: int, wait_timeout: float,
    verbose: bool, on_done=None,
) -> list:
    """Run up to `concurrency` image renders in parallel across `concurrency`
    tabs of the persistent Chromium. Returns a list of (prompt_idx, save_path_or_None)
    tuples in the same order as tasks."""
    pages = await _ensure_page_pool(concurrency, verbose=verbose)
    results = [None] * len(tasks)

    # Semaphore so only `concurrency` renders are in flight at once even
    # if `tasks` has more entries than tabs (e.g. batch 20 across 10 tabs).
    sem = asyncio.Semaphore(concurrency)

    async def _one(idx: int, page_slot: int, prompt: str, save_to: str):
        # Stagger-start: each subsequent tab waits N seconds before firing
        # its request. Spreads the burst so Google's anti-automation doesn't
        # see N identical calls at the same millisecond (which triggers the
        # robot-check CAPTCHA on consumer Pro accounts).
        stagger_s = 4.0 + (idx * 3.0)
        if idx > 0:
            await asyncio.sleep(min(stagger_s - 4.0, 30.0))
        async with sem:
            page = pages[page_slot % len(pages)]
            out = await _render_on_page(
                page, prompt, save_to, wait_timeout, verbose,
                label=str(page_slot + 1),
            )
            results[idx] = out
            if on_done:
                try: on_done(idx, out)
                except Exception: pass

    coros = [_one(i, i, p, s) for i, (p, s) in enumerate(tasks)]
    await asyncio.gather(*coros, return_exceptions=True)
    return results


async def _do_create_image(prompt: str, save_to: str,
                           wait_timeout: float, verbose: bool) -> Optional[str]:
    try:
        page = await _ensure_browser(verbose=verbose)
    except Exception as e:
        if verbose: print(f"[bot] browser launch failed: {e}")
        return None

    # Up to 2 attempts per prompt: if the first render times out, reload
    # the tab and re-send. Most Gemini timeouts are transient; a second
    # try usually succeeds and saves the frame.
    for attempt in (1, 2):
        if not await _goto_chat(page, verbose=verbose):
            return None
        try:
            await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(1500)
        except Exception:
            pass
        if not await _send_prompt(page, prompt, verbose=verbose):
            if attempt == 2: return None
            continue
        img = await _wait_for_image_element(
            page, timeout_s=wait_timeout, verbose=verbose)
        if img:
            return await _download_image(page, img, save_to, verbose=verbose)
        if verbose:
            print(f"[bot] attempt {attempt}/2 timed out — "
                  f"{'retrying with fresh tab' if attempt == 1 else 'giving up on this frame'}")
    return None


# ── Public sync API (called from the pipeline worker) ──────────────

def create_image_and_download(prompt: str, save_to: str,
                              wait_timeout: Optional[float] = None,
                              verbose: bool = True) -> Optional[str]:
    if not prompt or not prompt.strip(): return None
    if wait_timeout is None:
        # Bumped from 180s floor → 300s floor. Gemini occasionally takes
        # 3-4 min per frame during busy periods; 180s was too tight and
        # caused false timeouts on otherwise-fine renders.
        L = len(prompt)
        wait_timeout = 300.0 if L < 1500 else 360.0 if L < 2500 else 420.0
    # Serialise calls — one Chromium, one generation at a time.
    # Outer timeout accommodates the 2-attempt retry inside `_do_create_image`
    # plus modest Chromium overhead per attempt.
    with _worker_lock:
        return _submit(
            _do_create_image(prompt, save_to, wait_timeout, verbose),
            timeout=wait_timeout * 2 + 120.0,
        )


def create_post_batches(post_groups: list, concurrency: int = 5,
                        wait_timeout: Optional[float] = None,
                        verbose: bool = True, on_done=None) -> list:
    """Run multiple POSTS in parallel — each post's slides are sent
    sequentially in ONE Gemini chat (one conversation per post, so
    history stays aligned).

    `post_groups[i]` = list of `(prompt, save_to)` for post `i` (up to 10).
    `on_done(post_idx, slide_idx, path)` fires as each slide lands.
    Returns list of `(post_idx, slide_idx, path_or_None)`.
    """
    if not post_groups: return []
    if wait_timeout is None:
        longest = max(
            (len(p) for group in post_groups for p, _ in group),
            default=2000,
        )
        wait_timeout = 300.0 if longest < 1500 else 360.0 if longest < 2500 else 420.0
    with _worker_lock:
        # Outer cap: slowest post = ~10 slides × wait_timeout + overhead.
        max_slides = max(len(g) for g in post_groups)
        outer = wait_timeout * max_slides + 300.0
        return _submit(
            _do_create_post_batches(
                post_groups, concurrency, wait_timeout, verbose, on_done),
            timeout=outer,
        )


async def _do_recover_from_history(save_dir: str, max_recover: int,
                                   verbose: bool) -> list:
    """Iterate Gemini's conversation-history sidebar, open each past chat,
    extract any generated images, and save them.

    Returns list of (conversation_title, saved_path) tuples."""
    page = await _ensure_browser(verbose=verbose)
    if not await _goto_chat(page, verbose=verbose):
        return []
    save_root = Path(save_dir)
    save_root.mkdir(parents=True, exist_ok=True)

    # Open the chat-history sidebar if collapsed. Gemini's menu button
    # varies by version — try several common aria-labels.
    for aria in ("Main menu", "Open side panel", "Menu", "Expand menu"):
        try:
            btn = page.locator(f'button[aria-label*="{aria}" i]').first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                await page.wait_for_timeout(400)
                break
        except Exception: pass

    # Sidebar list items typically use role=button inside a <nav>.
    history_items = page.locator(
        'nav a[href*="/app/"], nav button[role="button"]:has-text(""), '
        '[data-test-id*="conversation"], [aria-label*="conversation" i]'
    )
    try:
        count = await history_items.count()
    except Exception:
        count = 0
    if verbose:
        print(f"[recover] sidebar has ~{count} history entries")

    saved: list = []
    max_i = min(count, max(5, max_recover))
    for i in range(max_i):
        if len(saved) >= max_recover: break
        try:
            item = history_items.nth(i)
            title = (await item.text_content() or f"chat_{i+1}").strip()[:60]
            await item.click(timeout=5000)
            await page.wait_for_timeout(2500)
            # Look for the largest visible image on this chat page.
            img = await _wait_for_image_element(page, timeout_s=20.0, verbose=False)
            if not img:
                if verbose: print(f"[recover] {i+1}. {title!r} — no image")
                continue
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", title)[:40] or f"chat_{i+1}"
            dst = str(save_root / f"recovered_{i+1:02d}_{safe}.png")
            path = await _download_image(page, img, dst, verbose=False)
            if path:
                saved.append((title, path))
                if verbose: print(f"[recover] {i+1}. {title!r} → {path}")
            else:
                if verbose: print(f"[recover] {i+1}. {title!r} — download failed")
        except Exception as e:
            if verbose: print(f"[recover] {i+1}. error: {e}")
            continue
    return saved


async def _do_recover_posts_from_history(
    post_groups: list, save_root: str, verbose: bool,
) -> list:
    """For each post's list of (prompt, save_to), open the matching
    Gemini past conversation and download the 10 images by prompt-match
    + scoped Download button click.

    `post_groups` = list of posts; each post is list of (prompt, save_to).
    We assume the N most recent history chats correspond to the N posts
    in reverse order (latest chat = last post generated). Caller passes
    posts in the SAME order they were originally generated.

    Returns list of saved paths (or None) in the same flat order as the
    concatenation of all posts' slides.
    """
    page = await _ensure_browser(verbose=verbose)
    if not await _goto_chat(page, verbose=verbose):
        return []
    Path(save_root).mkdir(parents=True, exist_ok=True)

    # Snapshot the landing state before we try anything — if Gemini
    # is showing a block/captcha/empty page, at least we have evidence.
    try:
        shot = Path(save_root) / "_recover_landing.png"
        await page.screenshot(path=str(shot), full_page=False)
        if verbose: print(f"[recover] landing screenshot → {shot}")
    except Exception: pass

    # Ensure sidebar is open. Gemini's button labels drift across
    # releases — try every plausible label + a few class heuristics.
    for aria in ("Main menu", "Open side panel", "Menu", "Expand menu",
                 "Show side panel", "Open history", "Show history",
                 "Expand side panel", "Open conversations"):
        try:
            btn = page.locator(f'button[aria-label*="{aria}" i]').first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                await page.wait_for_timeout(600)
                if verbose: print(f"[recover] clicked sidebar opener: {aria!r}")
                break
        except Exception: pass
    await page.wait_for_timeout(800)

    # Permissive history-item detection: anything in a sidebar/nav
    # that looks like a conversation row.
    hist_count = 0
    history_items = None
    for selector in (
        'nav a[href*="/app/"]',
        '[data-test-id*="conversation"]',
        '[aria-label*="conversation" i]',
        'side-nav a[href*="/app/"]',
        'div[role="list"] a[href*="/app/"]',
        'a[jslog][href*="/app/"]',
        'button[mat-list-item]',
        '.conversation-title',
        'div[jsname] > a[href*="/app/"]',
    ):
        try:
            loc = page.locator(selector)
            c = await loc.count()
            if c >= len(post_groups):
                history_items = loc
                hist_count = c
                if verbose:
                    print(f"[recover] sidebar matched {c} items with selector "
                          f"{selector!r}")
                break
        except Exception: pass

    if history_items is None:
        if verbose:
            print("[recover] sidebar history selector didn't match — dumping "
                  "visible <a href='/app/…'> hrefs for inspection")
            try:
                hrefs = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('a[href*="/app/"]'))
                        .slice(0, 40).map(a => a.getAttribute('href'))
                """)
                for h in hrefs: print(f"   • {h}")
            except Exception: pass
        return [None] * sum(len(g) for g in post_groups)
    if verbose:
        print(f"[recover] sidebar has {hist_count} history entries; "
              f"need {len(post_groups)} matching the post pattern")

    # Filter sidebar entries by title: anything beginning with
    # "Ultra-photorealistic" is a candidate (Gemini auto-titles chats
    # with the first ~60 chars of the first user message — which in our
    # NB2 pipeline always starts with "Ultra-photorealistic…").
    all_titles = []
    for i in range(hist_count):
        try:
            t = (await history_items.nth(i).text_content() or "").strip()
        except Exception:
            t = ""
        all_titles.append(t)
    if verbose:
        print("[recover] sidebar titles:")
        for i, t in enumerate(all_titles):
            mark = "✓" if t.lower().lstrip().startswith("ultra-photo") else "·"
            print(f"   {mark} [{i}] {t[:80]!r}")

    matching_sidebar_idx = [
        i for i, t in enumerate(all_titles)
        if t.lower().lstrip().startswith("ultra-photo")
    ]
    if verbose:
        print(f"[recover] {len(matching_sidebar_idx)} sidebar entries "
              f"start with 'Ultra-photorealistic'")
    if not matching_sidebar_idx:
        return [None] * sum(len(g) for g in post_groups)

    # NEW SIMPLE STRATEGY — for each matching sidebar chat (title starts
    # with "Ultra-photorealistic"), open it and dump every image in doc
    # order into its own Chat_NN_<slug>/Frame{M}.png folder. User does
    # the final Post{N} assignment manually. This avoids fragile
    # sidebar-index-to-post reverse-mapping.
    results: list = []
    save_root_path = Path(save_root)
    for run_idx, sidebar_idx in enumerate(matching_sidebar_idx):
        try:
            item = history_items.nth(sidebar_idx)
            title = (await item.text_content() or f"chat_{sidebar_idx}").strip()[:80]
            if verbose:
                print(f"[recover] ── chat {sidebar_idx} "
                      f"(match {run_idx+1}/{len(matching_sidebar_idx)}) — {title!r}")
            # Retry the click in case the sidebar re-renders.
            for attempt in range(3):
                try:
                    await item.click(timeout=8000)
                    break
                except Exception:
                    await page.wait_for_timeout(1000)
                    item = history_items.nth(sidebar_idx)
            await page.wait_for_timeout(3000)
            await _dismiss_modals(page, verbose=False)
            # Force lazy images to load by scrolling full chat length.
            try:
                for _ in range(3):
                    await page.evaluate("window.scrollTo(0, 0)")
                    await page.wait_for_timeout(400)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(800)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(800)
            except Exception: pass
        except Exception as e:
            if verbose: print(f"[recover] chat {sidebar_idx} click failed: {e}")
            continue

        # Find ALL generated images in this chat (doc order).
        try:
            all_indices = await page.evaluate("""
                () => {
                    const out = [];
                    document.querySelectorAll('img').forEach((i, idx) => {
                        const s = i.src || i.currentSrc || '';
                        if (!s || s.startsWith('chrome-')) return;
                        const w = Math.max(
                            i.getBoundingClientRect().width, i.naturalWidth || 0);
                        const h = Math.max(
                            i.getBoundingClientRect().height, i.naturalHeight || 0);
                        if (w >= 150 && h >= 150) out.push(idx);
                    });
                    return out;
                }
            """)
        except Exception:
            all_indices = []

        if verbose:
            print(f"[recover]   {len(all_indices)} img(s) found in this chat")
        if len(all_indices) < 2:
            # Not a post chat. Skip.
            if verbose: print("[recover]   not enough images — skipping")
            continue

        # Per-chat save folder.
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", title)[:40] or f"chat_{sidebar_idx}"
        chat_dir = save_root_path / f"Chat_{run_idx+1:02d}_{slug}"
        chat_dir.mkdir(parents=True, exist_ok=True)

        for local_idx, img_dom_idx in enumerate(all_indices):
            img_loc = page.locator("img").nth(img_dom_idx)
            frame_path = chat_dir / f"Frame{local_idx+1}.png"
            try:
                p = await _download_image(page, img_loc, str(frame_path),
                                          verbose=verbose)
            except Exception as e:
                p = None
                if verbose: print(f"[recover]   slide {local_idx+1} err: {e}")
            results.append(p)
            if verbose:
                print(f"[recover]   slide {local_idx+1} img#{img_dom_idx} — "
                      f"{'saved → ' + str(p) if p else 'FAILED'}")
    return results


def recover_posts_from_history(post_groups: list, save_root: str,
                               verbose: bool = True) -> list:
    """Sync entry for recover_posts_from_history. `post_groups` is a
    list of posts, each post is a list of (prompt, save_to) tuples."""
    n = sum(len(g) for g in post_groups)
    if not n: return []
    with _worker_lock:
        return _submit(
            _do_recover_posts_from_history(post_groups, save_root, verbose),
            timeout=max(600.0, 30.0 * n + 120.0),
        )


def recover_from_history(save_dir: str, max_recover: int = 60,
                         verbose: bool = True) -> list:
    """Recover previously-generated images from Gemini's chat history.
    Saves them to `save_dir` with names `recovered_NN_<title>.png`.
    Returns the list of (title, path) pairs."""
    with _worker_lock:
        return _submit(
            _do_recover_from_history(save_dir, max_recover, verbose),
            timeout=max_recover * 30.0 + 120.0,
        )


def create_images_batch(tasks: list, concurrency: int = 10,
                        wait_timeout: Optional[float] = None,
                        verbose: bool = True, on_done=None) -> list:
    """Render up to `concurrency` images in parallel.

    `tasks` is a list of (prompt: str, save_to: str). Returns a list of
    file paths or None (one per task, same order). `on_done(idx, path)`
    is called as each frame lands so the pipeline can broadcast progress
    in real time instead of waiting for the whole batch.
    """
    if not tasks: return []
    if wait_timeout is None:
        # Pick a timeout based on the longest prompt in the batch.
        longest = max(len(p) for p, _ in tasks)
        wait_timeout = 300.0 if longest < 1500 else 360.0 if longest < 2500 else 420.0
    with _worker_lock:
        return _submit(
            _do_create_images_batch(tasks, concurrency, wait_timeout, verbose, on_done),
            timeout=wait_timeout * 2 + 180.0,
        )


def is_ready(verbose: bool = False) -> bool:
    try:
        import playwright  # noqa
    except Exception:
        return False
    try:
        with _worker_lock:
            page = _submit(_ensure_browser(verbose=verbose), timeout=30.0)
            return _submit(_goto_chat(page, verbose=verbose), timeout=60.0)
    except Exception as e:
        if verbose: print(f"[bot] readiness check failed: {e}")
        return False


def status() -> dict:
    try:
        import playwright
        pw_ok = True
    except Exception:
        pw_ok = False
    return {
        "playwright_installed": pw_ok,
        "profile_dir":          str(_PROFILE_DIR),
        "profile_exists":       _PROFILE_DIR.exists(),
        "session_bytes":        sum(
            (f.stat().st_size for f in _PROFILE_DIR.rglob("*") if f.is_file()),
            0,
        ) if _PROFILE_DIR.exists() else 0,
        "worker_alive":         _worker_thread is not None and _worker_thread.is_alive(),
    }


def close():
    """Tear down the browser + worker loop. Call at process exit."""
    global _worker_loop, _worker_thread
    async def _shut():
        global _pw, _context, _page
        try:
            if _context: await _context.close()
        except Exception: pass
        try:
            if _pw: await _pw.stop()
        except Exception: pass
        _pw = _context = _page = None
    try:
        if _worker_loop and _worker_loop.is_running():
            asyncio.run_coroutine_threadsafe(_shut(), _worker_loop).result(timeout=10)
            _worker_loop.call_soon_threadsafe(_worker_loop.stop)
    except Exception: pass
    _worker_thread = None
    _worker_loop = None


if __name__ == "__main__":
    import json, sys
    print(json.dumps(status(), indent=2))
    if len(sys.argv) >= 3:
        out = create_image_and_download(sys.argv[1], sys.argv[2])
        print(f"result: {out}")
        close()
