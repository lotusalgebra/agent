"""
Google Flow (labs.google/fx/tools/flow) image generation via Playwright + CDP.

Same public surface as gemini_bot.create_image_and_download and gemini_api so
the agent's image-gen chain can include it as another engine:

    create_image_and_download(prompt: str, save_to: str, verbose: bool = True)
        -> Optional[str]

Why this exists: Flow's image gen (Nano Banana 2 via Gemini Omni) has a
SEPARATE quota from the Gemini-chat browser path that LOTUS otherwise uses.
On a day where the chat path is rate-limited or hanging on long prompts,
the Flow path often still works. It also produces files via Chrome's
download API (caught by Playwright's expect_download) which avoids the
mid-stream image-count race that plagues gemini_bot's phase-2 mapping.

UI shape (verified 2026-05-21):
  • Flow URL: https://labs.google/fx/tools/flow → project pages have
    URLs of the form /flow/project/<uuid>
  • Inside a project, the bottom param panel has tabs:
      Image | Video
      Frames | Ingredients  (video-only)
      Aspect: 16:9 / 4:3 / 1:1 / 3:4 / 9:16
      Quantity: 1x / x2 / x3 / x4
      Model dropdown: 🍌 Nano Banana 2 (for Image)
  • The prompt input is a `<div contenteditable="true">` (NOT a textarea)
  • Submit button: aria-label "arrow_forward Create"
  • A first-time-visiting-Video onboarding banner sometimes appears with
    a "Got it" button — dismiss it before interacting with the input
  • The picker is a Radix popper that intercepts pointer events on the
    contenteditable below it — press Escape before typing
  • Generated image appears in the gallery; clicking it opens a detail
    view (full-screen). The top-right has a Download icon (split-button)
    which opens a sub-menu with "Original size" as the only choice.
  • Capture via page.expect_download() → save_as(target_path).
    Image format is JPEG even when save_to ends in .png; the .png
    extension is preserved for compatibility with LOTUS's frame layout.

Aspect inference from save_to path:
  • Story*.png OR contains "9_16" → 9:16
  • Otherwise → 3:4 (closest Flow option to LOTUS's 4:5 Post/Reel format)

Concurrency:
  Single dedicated worker thread + asyncio loop, identical to the
  pattern in gemini_bot.py — exactly one Flow render in flight at a time,
  Playwright objects never cross loops.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

# CDP URL of the real Chrome the preflight launched (or attached to).
# Honor the env var the rest of LOTUS uses; default to the documented port.
_CDP_URL = os.environ.get("LOTUS_CDP_URL", "http://127.0.0.1:9222")
_FLOW_URL = "https://labs.google/fx/tools/flow"

# Generation timeout. Nano Banana 2 usually delivers in 15-40s but we
# allow plenty of margin for slow runs.
_RENDER_TIMEOUT_SEC = 180.0


# ── Public availability gate ────────────────────────────────────────────

def is_available(verbose: bool = False) -> bool:
    """Cheap reachability check. We require CDP on :9222 to respond and at
    least one tab to be on labs.google. Doesn't validate login state —
    that's surfaced as a clear runtime failure during the first call."""
    import urllib.request, json
    try:
        with urllib.request.urlopen(_CDP_URL.rstrip("/") + "/json", timeout=2) as r:
            tabs = json.loads(r.read())
        on_flow = any("labs.google" in (t.get("url") or "") for t in tabs)
        if verbose:
            print(f"[flow] CDP reachable, flow tab present={on_flow}")
        return on_flow
    except Exception as e:
        if verbose:
            print(f"[flow] CDP not reachable: {e!r}")
        return False


# ── Worker thread / async submit ────────────────────────────────────────

_worker_thread: Optional[threading.Thread] = None
_worker_loop:   Optional[asyncio.AbstractEventLoop] = None
_worker_ready:  threading.Event = threading.Event()
_worker_lock:   threading.Lock  = threading.Lock()


def _run_worker_loop():
    global _worker_loop
    _worker_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_worker_loop)
    _worker_ready.set()
    try:
        _worker_loop.run_forever()
    finally:
        _worker_loop.close()


def _ensure_worker():
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_ready.clear()
        _worker_thread = threading.Thread(target=_run_worker_loop,
                                          name="flow-bot-worker",
                                          daemon=True)
        _worker_thread.start()
        _worker_ready.wait(timeout=5.0)


def _submit(coro, timeout: float = 600.0):
    _ensure_worker()
    fut = asyncio.run_coroutine_threadsafe(coro, _worker_loop)
    return fut.result(timeout=timeout)


# ── Aspect inference ────────────────────────────────────────────────────

def _infer_aspect(save_to: str) -> str:
    """Map a LOTUS save path to one of Flow's aspect options.

    Flow offers 16:9, 4:3, 1:1, 3:4, 9:16 — no native 4:5. The carousel
    pipeline writes 4:5 PostN/FrameM.png and 9:16 StoryK.png. 3:4 (1080×1440)
    is the closest in-Flow approximation to LOTUS's 4:5 (1080×1350); the
    extra 90 pixels of height can be cropped downstream.
    """
    p = save_to.lower()
    if "story" in os.path.basename(p) or "/9_16/" in p or "9-16" in os.path.basename(p):
        return "9:16"
    return "3:4"


# ── Async internals ─────────────────────────────────────────────────────

async def _connect_cdp_with_retry(pw, verbose: bool = True):
    """5-attempt CDP attach with exponential backoff, mirroring
    gemini_bot._connect_cdp_with_retry."""
    last_err = None
    delays = [1.0, 2.0, 4.0, 8.0, 16.0]
    for attempt in range(1, len(delays) + 1):
        try:
            return await pw.chromium.connect_over_cdp(_CDP_URL)
        except Exception as e:
            last_err = e
            if verbose:
                print(f"[flow] CDP attach {attempt}/{len(delays)} failed: {e!r}")
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt - 1])
    raise last_err


async def _find_or_open_flow_page(browser, verbose: bool = True):
    """Return a Page that is on labs.google/fx/tools/flow. Reuses an
    existing Flow tab if any; otherwise opens a new one in the first
    context."""
    for ctx in browser.contexts:
        for pg in ctx.pages:
            url = ""
            try: url = pg.url or ""
            except Exception: pass
            if "labs.google" in url and "flow" in url:
                if verbose: print(f"[flow] reusing existing tab: {url}")
                return pg
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await ctx.new_page()
    await page.goto(_FLOW_URL, wait_until="domcontentloaded")
    if verbose: print("[flow] opened new Flow tab")
    return page


async def _ensure_in_project(page, verbose: bool = True):
    """Make sure the current URL is /project/<uuid>. If on the landing
    page, click "+ New project" to create one."""
    await page.bring_to_front()
    await page.wait_for_timeout(500)
    if "/project/" in (page.url or ""):
        if verbose: print(f"[flow] already in project: {page.url}")
        return
    if verbose: print("[flow] not in a project — creating one")
    try:
        opened = await page.evaluate("""
            () => {
                const b = Array.from(document.querySelectorAll('button, [role="button"]'))
                    .find(e => (e.getAttribute('aria-label') || e.textContent || '')
                                .toLowerCase().includes('new project'));
                if (!b) return false;
                b.scrollIntoView({block: 'center'});
                b.click();
                return true;
            }
        """)
        if not opened:
            raise RuntimeError("Couldn't find 'New project' button on Flow landing")
        # Wait for URL to become /project/...
        for _ in range(20):
            await page.wait_for_timeout(500)
            if "/project/" in (page.url or ""):
                if verbose: print(f"[flow] project ready: {page.url}")
                return
        raise RuntimeError("Project URL didn't appear after click")
    except Exception as e:
        if verbose: print(f"[flow] project-create failed: {e}")
        raise


async def _dismiss_onboarding(page, verbose: bool = True) -> None:
    """Click the 'Got it' button on first-time onboarding banners.
    Idempotent — silent no-op when the banner isn't visible."""
    try:
        btn = page.get_by_role("button", name="Got it")
        if await btn.count() > 0:
            await btn.first.click(timeout=2000)
            if verbose: print("[flow] dismissed onboarding banner")
            await page.wait_for_timeout(400)
    except Exception:
        pass


async def _exit_detail_view(page, verbose: bool = True) -> None:
    """If a media detail view is open (URL contains '/edit/' OR a 'Done'
    button is visible in the top-right), click Done to return to the
    gallery. Idempotent — no-op otherwise."""
    try:
        url = page.url or ""
        if "/edit/" not in url:
            return
        done = page.get_by_role("button", name="Done")
        if await done.count() > 0:
            await done.first.click(timeout=2000)
            if verbose: print("[flow] exited detail view")
            await page.wait_for_timeout(600)
    except Exception:
        pass


async def _set_params(page, aspect: str, verbose: bool = True) -> None:
    """Open the params picker and force Image + 1x.

    The MVP intentionally does NOT switch aspect — empirically the aspect
    tabs in Flow's Radix popper close the popper on first interaction
    AND refuse subsequent clicks until the popper is reopened. Trying to
    chain Image → aspect → 1x is brittle. Instead, we trust the user
    to set the aspect in Flow's UI to match their typical workload
    (9:16 for Story carousels = default; switch to 3:4 manually if you
    intend to render Post slides through Flow).

    For aspect mismatches the agent chain still has playwright + api as
    fallbacks, so a Post slide that needs 4:5 will route through Gemini
    even when Flow is primary.
    """
    # Clear any stale popper / focus state first.
    try: await page.keyboard.press("Escape")
    except Exception: pass
    await page.wait_for_timeout(300)

    async def _open_picker() -> bool:
        try:
            chip = page.get_by_role("button", name=re.compile(r"\b1x\b"))
            await chip.first.click(timeout=3000)
            await page.wait_for_timeout(900)
            return True
        except Exception as e:
            if verbose: print(f"[flow] couldn't open picker: {e}")
            return False

    async def _click_tab(name: str) -> bool:
        try:
            tab = page.get_by_role("tab", name=name)
            if await tab.count() == 0:
                return False
            await tab.first.click(timeout=2500)
            return True
        except Exception:
            return False

    if not await _open_picker():
        return
    img_ok = await _click_tab("Image")
    await page.wait_for_timeout(400)
    if not await _open_picker():
        return
    qty_ok = await _click_tab("1x")
    await page.wait_for_timeout(300)
    if verbose:
        print(f"[flow] params set — Image:{img_ok} 1x:{qty_ok} "
              f"(aspect left at Flow's current selection — caller wanted {aspect})")
    try: await page.keyboard.press("Escape")
    except Exception: pass
    await page.wait_for_timeout(300)


async def _snapshot_image_urls(page) -> set:
    """Return the set of `data-media-url` or src strings currently visible
    in the gallery — used as a baseline before generation so we can
    detect the NEW image when it appears."""
    return set(await page.evaluate("""
        () => Array.from(document.querySelectorAll('img'))
            .filter(i => {
                const r = i.getBoundingClientRect();
                return r.width >= 80 && r.height >= 80;
            })
            .map(i => i.src || i.currentSrc || '')
            .filter(s => s && !s.startsWith('data:'))
    """))


async def _type_and_submit(page, prompt: str, verbose: bool = True) -> None:
    """Type the prompt into the contenteditable, then click Create."""
    # Focus the prompt input
    loc = page.locator('[contenteditable="true"]').first
    await loc.click(timeout=4000)
    await page.wait_for_timeout(200)
    # Clear any existing content first (in case of leftover from a prior run)
    await page.keyboard.press("Meta+A")
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(100)
    await page.keyboard.type(prompt, delay=4)
    await page.wait_for_timeout(400)
    # Click Create. Two buttons match by accessible name "Create" (the
    # gallery's "+ New project" and the prompt submit), so use the
    # more-specific "arrow_forward Create" form which Playwright derives
    # from the icon-span + label-span text.
    create = page.get_by_role("button", name="arrow_forward Create")
    await create.click(timeout=5000)
    if verbose: print(f"[flow] submitted ({len(prompt)} chars)")


async def _wait_for_new_image(page, baseline: set, verbose: bool = True) -> str:
    """Poll until a NEW image src (one not in `baseline`) appears. Returns
    the new src. Raises TimeoutError after _RENDER_TIMEOUT_SEC."""
    t0 = time.time()
    last_progress_log = 0.0
    while time.time() - t0 < _RENDER_TIMEOUT_SEC:
        urls = await _snapshot_image_urls(page)
        new = urls - baseline
        if new:
            src = next(iter(new))
            if verbose:
                print(f"[flow] new image after {time.time()-t0:.1f}s: {src[:80]}")
            return src
        # Optional verbose progress log: read the placeholder percent
        if verbose and (time.time() - last_progress_log) >= 10:
            pct = await page.evaluate("""
                () => {
                    const els = Array.from(document.querySelectorAll('*'));
                    for (const e of els) {
                        const t = (e.textContent||'').trim();
                        if (/^\\d+%$/.test(t)) return t;
                    }
                    return '';
                }
            """)
            if pct:
                print(f"[flow] generating… {pct}")
            last_progress_log = time.time()
        await asyncio.sleep(2.0)
    raise TimeoutError(f"Flow image not ready after {_RENDER_TIMEOUT_SEC}s")


async def _download_via_detail_view(page, new_src: str, save_to: str,
                                    verbose: bool = True) -> str:
    """Open the new image's detail view, click Download → Original size,
    capture via page.expect_download(), save to `save_to`. Returns
    `save_to` on success."""
    # Click the image to open detail view. Locate by src.
    clicked = await page.evaluate("""
        (target_src) => {
            const i = Array.from(document.querySelectorAll('img'))
                .find(e => (e.src || e.currentSrc || '') === target_src);
            if (!i) return false;
            // The clickable card is usually the closest button-y ancestor.
            let host = i.closest('button, [role=button], a, [tabindex]') || i;
            host.scrollIntoView({block: 'center'});
            host.click();
            return true;
        }
    """, new_src)
    if not clicked:
        raise RuntimeError("Couldn't locate the new image to open detail view")
    await page.wait_for_timeout(900)

    Path(save_to).parent.mkdir(parents=True, exist_ok=True)

    try:
        async with page.expect_download(timeout=25000) as dl_info:
            # Click Download (icon at the top-right of detail view).
            # Playwright derives the accessible name as "download Download"
            # (icon-span " " label-span) — match by either the visible
            # "Download" label OR a substring regex for robustness.
            dl_btn = page.get_by_role("button", name="Download")
            await dl_btn.first.click(timeout=4000)
            await page.wait_for_timeout(700)
            # Sub-menu has a single "Original size" item
            orig = page.get_by_role("menuitem").filter(has_text=re.compile(r"original", re.I))
            await orig.first.click(timeout=4000)
            if verbose: print("[flow] clicked Download → Original size")
        dl = await dl_info.value
        await dl.save_as(save_to)
        if verbose:
            size = os.path.getsize(save_to)
            print(f"[flow] saved → {save_to} ({size} bytes)")
        return save_to
    finally:
        # Close detail view so subsequent renders start cleanly
        try:
            done = page.get_by_role("button", name="Done")
            if await done.count() > 0:
                await done.first.click(timeout=2000)
        except Exception:
            pass


async def _do_create_image(prompt: str, save_to: str, verbose: bool = True) -> Optional[str]:
    from playwright.async_api import async_playwright
    aspect = _infer_aspect(save_to)
    if verbose: print(f"[flow] aspect for {save_to}: {aspect}")
    pw = await async_playwright().start()
    try:
        browser = await _connect_cdp_with_retry(pw, verbose=verbose)
        page = await _find_or_open_flow_page(browser, verbose=verbose)
        await _ensure_in_project(page, verbose=verbose)
        await _exit_detail_view(page, verbose=verbose)
        await _dismiss_onboarding(page, verbose=verbose)
        await _set_params(page, aspect, verbose=verbose)
        baseline = await _snapshot_image_urls(page)
        await _type_and_submit(page, prompt, verbose=verbose)
        new_src = await _wait_for_new_image(page, baseline, verbose=verbose)
        path = await _download_via_detail_view(page, new_src, save_to, verbose=verbose)
        return path
    except Exception as e:
        if verbose: print(f"[flow] FAILED: {e}")
        return None
    finally:
        try: await pw.stop()
        except Exception: pass


# ── Public sync entry ───────────────────────────────────────────────────

def create_image_and_download(prompt: str, save_to: str,
                              verbose: bool = True) -> Optional[str]:
    """Generate one image with Google Flow (Nano Banana 2) and save it to
    `save_to`. Returns the absolute save path on success, None on any
    failure. Never raises — matches the contract of the other engines so
    the agent's gen-image chain can fall through to the next tier.

    MVP scope: only 9:16 frames are handled. Other aspects (Post/Reel
    slides at 4:5) return None so the chain falls through to
    playwright/api — Flow's UI doesn't reliably switch aspect tabs from
    Playwright, and forcing the user to pre-set the aspect per-call
    would break batch automation."""
    if not prompt or not save_to:
        return None
    aspect = _infer_aspect(save_to)
    if aspect != "9:16":
        if verbose:
            print(f"[flow] skipping {save_to} — inferred aspect {aspect} "
                  f"is not 9:16 (Flow MVP only handles Story-style frames)")
        return None
    with _worker_lock:
        return _submit(_do_create_image(prompt, save_to, verbose),
                       timeout=_RENDER_TIMEOUT_SEC + 60)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("Usage: python flow_bot.py '<prompt>' <save_to_path>")
        sys.exit(2)
    out = create_image_and_download(sys.argv[1], sys.argv[2])
    print(f"\nResult: {out}")
    sys.exit(0 if out else 1)
