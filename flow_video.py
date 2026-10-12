"""
Google Flow (labs.google/fx/tools/flow) VIDEO generation via Playwright + CDP.

Companion to flow_bot.py (which handles Flow *image* gen). This module drives
Flow's **Video** mode — Google's Veo/Omni video model — for both:

    image_to_video(image_path, prompt, ...) -> Optional[str]   # animate a frame
    text_to_video(prompt, ...)              -> Optional[str]   # prompt-only clip

`image_to_video`'s signature is a superset of grok_video.image_to_video so the
two are interchangeable in a video-engine fallback chain (Flow primary, Grok
fallback): agent_phase1._gen_video tries this first, falls through to
grok_video on failure/quota.

Verified Flow Video UI (2026-07-23, PRO account):
  • The bottom composer has a param chip (e.g. "🍌 Nano Banana Pro · 16:9 · 1x"
    in Image mode, "Video · 8s · 1x" in Video mode). Clicking it opens a Radix
    popover — the ONLY place generation params live.
  • Popover rows, top to bottom:
      Image | Video                    (mode — must REAL-click "Video")
      Frames | Ingredients             (video sub-mode; only shown in Video mode)
      9:16 | 16:9                       (video supports just these two aspects)
      1x | x2 | x3 | x4                 (quantity)
      <model> ▾   (e.g. "Omni Flash")   (video model dropdown)
      4s | 6s | 8s | 10s               (duration)
      "Generating will use N credits"  (VIDEO COSTS CREDITS — image is 0)
  • Radix tabs/buttons ignore JS .click(); they need a real Playwright pointer
    event. We open the popover by clicking the chip's screen rect via
    page.mouse, then click rows by accessible name via get_by_role.
  • Prompt input is the widest `[contenteditable="true"]` (not a textarea).
  • Submit button accessible name is "arrow_forward Create".
  • Frames (image-to-video) accepts the start frame on a hidden
    `input[type=file]` (accept="image/*", multiple) — set_input_files works.
  • The finished clip surfaces as an inline `<video>` in the gallery; we
    baseline the existing `<video>` srcs, wait for a NEW one, and fetch it
    (APIRequestContext for http, in-page fetch for blob:) — same proven path
    as grok_video._download_video.

⚠️ UNVERIFIED-UNTIL-SMOKE-TESTED: the exact frame-upload affordance in Frames
mode and the finished-video download surface could not be exercised without
spending Veo credits. Those steps are modeled on grok_video + flow_bot and are
marked inline with `# SMOKE:` — re-check them after the first real render.

Concurrency: single dedicated worker thread + asyncio loop (mirrors flow_bot).
Exactly one Flow render in flight at a time; Playwright objects never cross
loops.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

_CDP_URL = os.environ.get("LOTUS_CDP_URL", "http://127.0.0.1:9222")
def _authuser() -> int:
    """Google account index (see lotus_config.google_authuser). Veo credits
    belong to the account, so this decides whose balance a clip spends."""
    try:
        import lotus_config
        return lotus_config.google_authuser()
    except Exception:
        return 0


_AU = _authuser()
_FLOW_URL = ("https://labs.google/fx/tools/flow"
             + (f"?authuser={_AU}" if _AU else ""))

# Veo renders are far slower than image gen (typically 60-180s for a short
# clip, longer under load). Generous ceiling; the caller can override.
_RENDER_TIMEOUT_SEC = 360.0

_VALID_DURATIONS = ("4s", "6s", "8s", "10s")


# ── Public availability gate ────────────────────────────────────────────

def is_available(verbose: bool = False) -> bool:
    """Cheap reachability check: CDP on :9222 responds and at least one tab
    is on labs.google. Does NOT validate login or Veo credits — those
    surface as a clear runtime failure (returns None) on the first call."""
    import urllib.request, json
    try:
        with urllib.request.urlopen(_CDP_URL.rstrip("/") + "/json", timeout=2) as r:
            tabs = json.loads(r.read())
        on_flow = any("labs.google" in (t.get("url") or "") for t in tabs)
        if verbose:
            print(f"[flow-video] CDP reachable, flow tab present={on_flow}")
        return on_flow
    except Exception as e:
        if verbose:
            print(f"[flow-video] CDP not reachable: {e!r}")
        return False


# ── Worker thread / async submit (mirrors flow_bot) ─────────────────────

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
                                          name="flow-video-worker",
                                          daemon=True)
        _worker_thread.start()
        _worker_ready.wait(timeout=5.0)


def _submit(coro, timeout: float):
    _ensure_worker()
    fut = asyncio.run_coroutine_threadsafe(coro, _worker_loop)
    return fut.result(timeout=timeout)


# ── Param normalization ─────────────────────────────────────────────────

def _norm_aspect(aspect: str, save_to: str = "") -> str:
    """Flow video supports only 9:16 and 16:9. Map anything portrait-ish to
    9:16 and anything landscape/square to 16:9. Story paths force 9:16."""
    a = (aspect or "").strip().replace(" ", "")
    if a in ("9:16", "16:9"):
        return a
    base = os.path.basename(save_to).lower()
    if "story" in base or a in ("3:4", "4:5"):
        return "9:16"
    return "16:9"


def _norm_duration(duration) -> str:
    """Coerce to one of Flow's discrete durations. Accepts '8s', 8, '8'."""
    if duration is None:
        return "8s"
    s = str(duration).strip().lower()
    if not s.endswith("s"):
        s = f"{s}s"
    if s in _VALID_DURATIONS:
        return s
    # snap to the nearest allowed value
    try:
        n = int(re.sub(r"\D", "", s) or "8")
    except ValueError:
        n = 8
    allowed = [int(d[:-1]) for d in _VALID_DURATIONS]
    nearest = min(allowed, key=lambda x: abs(x - n))
    return f"{nearest}s"


# ── CDP + navigation (mirrors flow_bot) ─────────────────────────────────

async def _connect_cdp_with_retry(pw, verbose: bool = True):
    last_err = None
    delays = [1.0, 2.0, 4.0, 8.0, 16.0]
    for attempt in range(1, len(delays) + 1):
        try:
            return await pw.chromium.connect_over_cdp(_CDP_URL)
        except Exception as e:
            last_err = e
            if verbose:
                print(f"[flow-video] CDP attach {attempt}/{len(delays)} failed: {e!r}")
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt - 1])
    raise last_err


async def _find_or_open_flow_page(browser, verbose: bool = True):
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                url = pg.url or ""
            except Exception:
                url = ""
            if "labs.google" in url and "flow" in url:
                if verbose:
                    print(f"[flow-video] reusing existing tab: {url}")
                return pg
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await ctx.new_page()
    await page.goto(_FLOW_URL, wait_until="domcontentloaded")
    if verbose:
        print("[flow-video] opened new Flow tab")
    return page


async def _exit_detail_view(page, verbose: bool = True) -> None:
    """Leave an image detail/edit view (URL .../edit/<media>) back to the
    composer. Clicks 'Done' if present, else navigates to the bare project
    URL. Idempotent — no-op when not in a detail view."""
    try:
        if "/edit/" not in (page.url or ""):
            return
        done = page.get_by_role("button", name="Done")
        if await done.count() > 0:
            await done.first.click(timeout=3000)
            await page.wait_for_timeout(800)
        if "/edit/" in (page.url or ""):
            m = re.match(r"(.*/project/[0-9a-fA-F-]+)", page.url or "")
            if m:
                await page.goto(m.group(1), wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)
        if verbose:
            print("[flow-video] exited detail view")
    except Exception as e:
        if verbose:
            print(f"[flow-video] exit-detail failed: {e}")


async def _ensure_in_project(page, verbose: bool = True):
    await page.bring_to_front()
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(800)
    if "/project/" in (page.url or ""):
        # A lingering image detail/edit view (URL .../project/<id>/edit/<media>)
        # hides the composer, so the param popover can't open. Exit it first —
        # click "Done", else navigate back to the bare project URL.
        if "/edit/" in (page.url or ""):
            await _exit_detail_view(page, verbose=verbose)
        if verbose:
            print(f"[flow-video] already in project: {page.url}")
        return
    if verbose:
        print("[flow-video] not in a project — creating one")
    # Clear promo/onboarding overlays FIRST — they sit above the New-project
    # button and silently absorb the click (see _dismiss_onboarding).
    await _dismiss_onboarding(page, verbose=verbose)
    await page.evaluate("""() => {
        const b = Array.from(document.querySelectorAll('button,[role=button],a'))
            .find(e => (e.getAttribute('aria-label') || e.textContent || '')
                        .toLowerCase().includes('new project'));
        if (b) { b.scrollIntoView({block: 'center'}); b.click(); }
    }""")
    for _ in range(24):
        await page.wait_for_timeout(500)
        if "/project/" in (page.url or ""):
            if verbose:
                print(f"[flow-video] project ready: {page.url}")
            return
    raise RuntimeError("Flow project URL didn't appear after 'New project' click")


async def _dismiss_onboarding(page, verbose: bool = True) -> None:
    """Clear overlays that swallow clicks on the Flow home/composer.

    Two distinct things appear here and BOTH intercept pointer events:
      - the first-time Video onboarding banner ('Got it'), and
      - Google's rotating promo banner ('Take Google Flow on the road…')
        whose control is labelled 'Dismiss'.
    The promo banner is what made `_ensure_project` fail with "Flow project
    URL didn't appear after 'New project' click": the JS click dispatched
    fine but landed on the banner, so no navigation happened. Exact-name
    matching keeps this away from 'Delete project' / 'Edit project'.
    Idempotent — safe to call repeatedly.
    """
    for name in ("Got it", "Got it!", "Dismiss"):
        try:
            btn = page.get_by_role("button", name=name, exact=True)
            if await btn.count() > 0:
                await btn.first.click(timeout=1500)
                if verbose:
                    print(f"[flow-video] dismissed banner ({name!r})")
                await page.wait_for_timeout(400)
        except Exception:
            pass


# ── Param popover control ───────────────────────────────────────────────

async def _open_param_popover(page, verbose: bool = True) -> bool:
    """Open the composer param popover by clicking the chip's screen rect.
    The chip is the bottom-area button carrying a crop-aspect token
    (`crop_16_9` / `crop_landscape` / …) OR a model marker (🍌 / Nano /
    Omni / Veo) plus a quantity (`1x`). We POLL for it (a freshly-created
    project renders the composer a beat late — the one-shot lookup was the
    'param chip not found' failure), then click via page.mouse so Radix
    gets a real pointer event (JS .click() is ignored)."""
    find_chip = """() => {
        const els = Array.from(document.querySelectorAll('button,[role=button]'));
        const chip = els.find(e => {
            const t = (e.getAttribute('aria-label') || e.textContent || '');
            const r = e.getBoundingClientRect();
            const bottomish = r.width > 0 && r.height > 0 &&
                              r.bottom > window.innerHeight - 220;
            if (!bottomish) return false;
            // crop token, OR (model marker AND a quantity/duration token)
            return /crop_(16_9|9_16|landscape|portrait|square)/i.test(t) ||
                   ((/🍌|nano|omni|veo/i.test(t)) && /\\b\\dx\\b|\\b\\d+s\\b|1x/i.test(t));
        });
        if (!chip) return null;
        const r = chip.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }"""
    rect = None
    deadline = time.time() + 15.0
    while time.time() < deadline:
        try:
            rect = await page.evaluate(find_chip)
        except Exception:
            rect = None
        if rect:
            break
        await page.wait_for_timeout(700)
    if not rect:
        if verbose:
            print("[flow-video] param chip not found (after 15s wait)")
        return False
    await page.mouse.click(rect["x"], rect["y"])
    await page.wait_for_timeout(700)
    # Popover open == the aspect tabs are visible.
    ok = await page.evaluate("""() => {
        return Array.from(document.querySelectorAll('[role=tab]'))
            .some(e => /16:9|9:16/.test(e.textContent || '') &&
                       e.getBoundingClientRect().width > 0);
    }""")
    if verbose and not ok:
        print("[flow-video] popover didn't open (no aspect tabs visible)")
    return bool(ok)


async def _click_pop(page, pattern: str, role: str = "tab",
                     verbose: bool = True) -> bool:
    """Real-click a popover row by accessible-name substring. Radix tabs
    honor genuine Playwright clicks (unlike JS .click())."""
    try:
        loc = page.get_by_role(role, name=re.compile(pattern, re.I))
        if await loc.count() == 0 and role != "button":
            loc = page.get_by_role("button", name=re.compile(pattern, re.I))
        if await loc.count() == 0:
            return False
        await loc.first.click(timeout=3000)
        await page.wait_for_timeout(350)
        return True
    except Exception as e:
        if verbose:
            print(f"[flow-video] click '{pattern}' ({role}) failed: {e}")
        return False


async def _set_video_params(page, *, aspect: str, duration: str,
                            frames_mode: bool, verbose: bool = True) -> None:
    """Open the popover and set: Video mode → (Frames|default) → aspect →
    1x → duration. Best-effort per row; missing rows are non-fatal so the
    chain can still fall through to Grok if Flow's UI shifts."""
    if not await _open_param_popover(page, verbose=verbose):
        raise RuntimeError("couldn't open Flow param popover")

    if not await _click_pop(page, r"Video", role="tab", verbose=verbose):
        raise RuntimeError("couldn't switch to Video mode")
    await page.wait_for_timeout(500)

    # Video sub-mode: Frames = image-to-video, default (neither) = text-to-video.
    if frames_mode:
        await _click_pop(page, r"Frames", role="tab", verbose=verbose)  # SMOKE
        await page.wait_for_timeout(300)

    ok_a = await _click_pop(page, re.escape(aspect), role="tab", verbose=verbose)
    ok_q = await _click_pop(page, r"^1x$|\b1x\b", role="tab", verbose=verbose)
    ok_d = await _click_pop(page, rf"\b{re.escape(duration)}\b", role="tab", verbose=verbose)
    if verbose:
        print(f"[flow-video] params — Video:on Frames:{frames_mode} "
              f"aspect({aspect}):{ok_a} 1x:{ok_q} dur({duration}):{ok_d}")
    # Close popover so it doesn't intercept the prompt input.
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(300)


async def _restore_image_mode(page, verbose: bool = True) -> None:
    """Leave the project in Image mode so the mother's image pipeline
    (flow_bot) isn't left staring at a Video-mode composer."""
    try:
        if await _open_param_popover(page, verbose=False):
            await _click_pop(page, r"Image", role="tab", verbose=False)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
    except Exception:
        pass


# ── Upload / prompt / submit ────────────────────────────────────────────

async def _upload_frame(page, image_path: str, verbose: bool = True) -> bool:
    """Set the start frame on Flow's hidden file input (accept image/*)."""
    try:
        inp = page.locator('input[type="file"]').first
        await inp.set_input_files(image_path)  # SMOKE: confirm this is the frame input
        await page.wait_for_timeout(1200)      # let Flow read + thumbnail it
        if verbose:
            print(f"[flow-video] uploaded frame {image_path}")
        return True
    except Exception as e:
        if verbose:
            print(f"[flow-video] frame upload failed: {e}")
        return False


async def _type_prompt(page, prompt: str, verbose: bool = True) -> bool:
    """Type into the widest visible contenteditable (the prompt box)."""
    try:
        idx = await page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('[contenteditable="true"]'));
            let best = -1, bestW = 0;
            for (let i = 0; i < els.length; i++) {
                const r = els[i].getBoundingClientRect();
                if (r.width > bestW && r.height > 0) { bestW = r.width; best = i; }
            }
            return best;
        }""")
        if idx < 0:
            if verbose:
                print("[flow-video] no contenteditable prompt box found")
            return False
        loc = page.locator('[contenteditable="true"]').nth(idx)
        await loc.click(timeout=4000)
        await page.keyboard.press("Meta+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(prompt, delay=3)
        await page.wait_for_timeout(300)
        if verbose:
            print(f"[flow-video] prompt set ({len(prompt)} chars)")
        return True
    except Exception as e:
        if verbose:
            print(f"[flow-video] prompt set failed: {e}")
        return False


async def _click_create(page, verbose: bool = True) -> bool:
    try:
        create = page.get_by_role("button", name="arrow_forward Create")
        if await create.count() == 0:
            create = page.get_by_role("button", name=re.compile(r"\bCreate\b"))
        await create.first.click(timeout=5000)
        if verbose:
            print("[flow-video] submitted — waiting for render")
        return True
    except Exception as e:
        if verbose:
            print(f"[flow-video] submit failed: {e}")
        return False


# ── Wait + download (mirrors grok_video) ────────────────────────────────

async def _snapshot_video_srcs(page) -> set:
    return set(await page.evaluate("""() => Array.from(document.querySelectorAll('video'))
        .map(v => v.currentSrc || v.src || '')
        .filter(s => s && (s.startsWith('http') || s.startsWith('blob:')))"""))


async def _wait_for_new_video(page, baseline: set, timeout: float,
                              verbose: bool = True) -> Optional[str]:
    t0 = time.time()
    last_log = 0.0
    while time.time() - t0 < timeout:
        try:
            srcs = await _snapshot_video_srcs(page)
        except Exception:
            srcs = set()
        new = srcs - baseline
        if new:
            src = next(iter(new))
            if verbose:
                print(f"[flow-video] new video after {time.time()-t0:.0f}s: {src[:80]}")
            return src
        if verbose and time.time() - last_log > 30:
            last_log = time.time()
            print(f"[flow-video] still rendering… ({int(timeout-(time.time()-t0))}s left)")
        await asyncio.sleep(2.5)
    if verbose:
        print(f"[flow-video] no video after {timeout:.0f}s")
    return None


async def _download_video(page, src: str, save_to: str,
                          verbose: bool = True) -> Optional[str]:
    """Fetch the MP4: APIRequestContext for http (uses Chrome's cookie jar),
    in-page fetch→base64 for blob:. Mirrors grok_video._download_video."""
    save_path = Path(os.path.expanduser(save_to))
    save_path.parent.mkdir(parents=True, exist_ok=True)
    if src.startswith("http"):
        try:
            resp = await page.context.request.get(src, timeout=180_000)
            if not resp.ok:
                if verbose:
                    print(f"[flow-video] http fetch {resp.status}")
                return None
            body = await resp.body()
            if len(body) < 4096:
                if verbose:
                    print("[flow-video] http body too small — treating as failure")
                return None
            save_path.write_bytes(body)
            if verbose:
                print(f"[flow-video] saved → {save_path} ({len(body)//1024} KB)")
            return str(save_path)
        except Exception as e:
            if verbose:
                print(f"[flow-video] http fetch failed: {e}")
            return None
    # blob: URL
    try:
        b64 = await page.evaluate("""async (src) => {
            const r = await fetch(src);
            const buf = await r.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let s = '';
            for (let i = 0; i < bytes.length; i += 4096)
                s += String.fromCharCode.apply(null, bytes.subarray(i, i + 4096));
            return btoa(s);
        }""", src)
        if not b64:
            return None
        import base64
        save_path.write_bytes(base64.b64decode(b64))
        if verbose:
            print(f"[flow-video] saved (blob) → {save_path} "
                  f"({save_path.stat().st_size//1024} KB)")
        return str(save_path)
    except Exception as e:
        if verbose:
            print(f"[flow-video] blob fetch failed: {e}")
        return None


# ── Async orchestration ─────────────────────────────────────────────────

async def _do_generate(*, prompt: str, save_to: str, aspect: str,
                       duration: str, image_path: Optional[str],
                       timeout: float, verbose: bool) -> Optional[str]:
    from playwright.async_api import async_playwright
    frames_mode = image_path is not None
    pw = await async_playwright().start()
    page = None
    try:
        browser = await _connect_cdp_with_retry(pw, verbose=verbose)
        page = await _find_or_open_flow_page(browser, verbose=verbose)
        await _ensure_in_project(page, verbose=verbose)
        await _dismiss_onboarding(page, verbose=verbose)
        await _set_video_params(page, aspect=aspect, duration=duration,
                                frames_mode=frames_mode, verbose=verbose)
        if frames_mode:
            if not await _upload_frame(page, image_path, verbose=verbose):
                return None
        if not await _type_prompt(page, prompt, verbose=verbose):
            return None
        baseline = await _snapshot_video_srcs(page)
        if not await _click_create(page, verbose=verbose):
            return None
        src = await _wait_for_new_video(page, baseline, timeout, verbose=verbose)
        if not src:
            return None
        return await _download_video(page, src, save_to, verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"[flow-video] FAILED: {e}")
        return None
    finally:
        if page is not None:
            await _restore_image_mode(page, verbose=verbose)
        try:
            await pw.stop()
        except Exception:
            pass


# ── Public sync entries ─────────────────────────────────────────────────

def image_to_video(image_path: str, prompt: str, *,
                   aspect: str = "9:16",
                   duration="8s",
                   quality: Optional[str] = None,   # accepted for grok compat; unused
                   mode: Optional[str] = None,       # accepted for grok compat; unused
                   save_to: Optional[str] = None,
                   timeout: float = _RENDER_TIMEOUT_SEC,
                   verbose: bool = True) -> Optional[str]:
    """Animate `image_path` with `prompt` using Flow's Veo/Omni video model
    (Frames mode). Saves the MP4 to `save_to` (default: `<image>/Videos/
    <stem>.mp4`). Returns the saved path, or None on any failure — never
    raises, matching grok_video.image_to_video so a video-engine chain can
    fall through to Grok.

    ⚠️ Each render spends Veo credits (~7+ for a short clip)."""
    if not prompt or not image_path:
        return None
    image_path = os.path.abspath(os.path.expanduser(image_path))
    if not os.path.isfile(image_path):
        if verbose:
            print(f"[flow-video] source frame not found: {image_path}")
        return None
    if save_to is None:
        base = Path(image_path)
        save_to = str(base.parent / "Videos" / (base.stem + ".mp4"))
    asp = _norm_aspect(aspect, save_to)
    dur = _norm_duration(duration)
    with _worker_lock:
        return _submit(_do_generate(prompt=prompt, save_to=save_to, aspect=asp,
                                    duration=dur, image_path=image_path,
                                    timeout=timeout, verbose=verbose),
                       timeout=timeout + 90)


def text_to_video(prompt: str, *,
                  save_to: str,
                  aspect: str = "9:16",
                  duration="8s",
                  timeout: float = _RENDER_TIMEOUT_SEC,
                  verbose: bool = True) -> Optional[str]:
    """Generate a clip from `prompt` alone (no source frame) and save the
    MP4 to `save_to`. Returns the saved path or None. ⚠️ Spends Veo credits."""
    if not prompt or not save_to:
        return None
    asp = _norm_aspect(aspect, save_to)
    dur = _norm_duration(duration)
    with _worker_lock:
        return _submit(_do_generate(prompt=prompt, save_to=save_to, aspect=asp,
                                    duration=dur, image_path=None,
                                    timeout=timeout, verbose=verbose),
                       timeout=timeout + 90)


# ════════════════════════════════════════════════════════════════════════
#  TEXT-TO-IMAGE  (Google Flow / Nano Banana — the Flow Image pipeline)
# ════════════════════════════════════════════════════════════════════════
# Uses the SAME popover harness as the video path but in Image mode. Flow
# image gen is FREE (0 credits). Full-res download via the detail-view
# "Original size" menu (mirrors flow_bot) so we never save a low-res preview.

_IMAGE_RENDER_TIMEOUT_SEC = 180.0
_IMAGE_ASPECTS = ("16:9", "4:3", "1:1", "3:4", "9:16")
# Flow Nano Banana originals run ~1024-1376px long edge; only reject clear
# thumbnails below this. (The old Gemini ≥1600 gate does not apply to Flow.)
_FLOW_IMAGE_MIN_LONG_EDGE = 800
# Flow download-menu size to pick: "1K" (Original), "2K" (Upscaled), or "4K"
# (Upscaled — needs a paid upgrade). Overridable via LOTUS_FLOW_IMAGE_SIZE.
_FLOW_IMAGE_DOWNLOAD_SIZE = os.environ.get("LOTUS_FLOW_IMAGE_SIZE", "2K").strip() or "2K"


def _norm_image_aspect(aspect: str, save_to: str = "") -> str:
    """Flow image supports 16:9/4:3/1:1/3:4/9:16 (no native 4:5). Map 4:5 →
    3:4 (closest); story paths → 9:16."""
    a = (aspect or "").strip().replace(" ", "")
    if a in _IMAGE_ASPECTS:
        return a
    base = os.path.basename(save_to).lower()
    if "story" in base or a == "9:16":
        return "9:16"
    if a in ("4:5", "3:4"):
        return "3:4"
    return "3:4"


async def _set_image_params(page, aspect: str, verbose: bool = True) -> None:
    """Open the popover and set Image mode + aspect + 1x. Best-effort per row."""
    if not await _open_param_popover(page, verbose=verbose):
        raise RuntimeError("couldn't open Flow param popover")
    if not await _click_pop(page, r"Image", role="tab", verbose=verbose):
        raise RuntimeError("couldn't switch to Image mode")
    await page.wait_for_timeout(400)
    ok_a = await _click_pop(page, re.escape(aspect), role="tab", verbose=verbose)
    ok_q = await _click_pop(page, r"^1x$|\b1x\b", role="tab", verbose=verbose)
    if verbose:
        print(f"[flow-image] params — Image:on aspect({aspect}):{ok_a} 1x:{ok_q}")
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(300)


async def _snapshot_image_urls(page) -> set:
    return set(await page.evaluate("""() => Array.from(document.querySelectorAll('img'))
        .filter(i => { const r=i.getBoundingClientRect(); return r.width>=80 && r.height>=80; })
        .map(i => i.src || i.currentSrc || '')
        .filter(s => s && s.startsWith('http'))"""))


async def _wait_for_new_image(page, baseline: set, timeout: float,
                              verbose: bool = True) -> Optional[str]:
    """Wait for THIS prompt's freshly-rendered image. Hardened against the
    'new image after 0s' bug: when many frames render into one Flow project
    back-to-back, a lazy-loaded thumbnail or the previous frame's re-srced
    tile would look 'new' and get grabbed instantly (wrong content). We now
    require (1) a real minimum render time, (2) the gallery image COUNT to
    actually grow, and (3) the candidate src to be STABLE across two polls
    (so we don't grab a transient loading placeholder)."""
    base_count = len(baseline)
    # Real Flow renders take ~20-40s; anything "new" before this is stale.
    MIN_RENDER_SEC = 8.0
    t0 = time.time()
    last_log = 0.0
    stable_src = None
    while time.time() - t0 < timeout:
        try:
            cur = await _snapshot_image_urls(page)
        except Exception:
            cur = set()
        new = cur - baseline
        elapsed = time.time() - t0
        if new and elapsed >= MIN_RENDER_SEC and len(cur) > base_count:
            # With 1x quantity exactly one new tile should settle. Require the
            # candidate to persist across two consecutive polls before trusting
            # it (a placeholder resolves to the final URL, changing once).
            cand = sorted(new)[0] if len(new) > 1 else next(iter(new))
            if cand == stable_src:
                if verbose:
                    print(f"[flow-image] new image after {elapsed:.0f}s "
                          f"(count {base_count}→{len(cur)})")
                return cand
            stable_src = cand
        elif not new:
            stable_src = None
        if verbose and time.time() - last_log > 15:
            last_log = time.time()
            print(f"[flow-image] generating… ({int(timeout-elapsed)}s left)")
        await asyncio.sleep(2.0)
    if verbose:
        print(f"[flow-image] no new image after {timeout:.0f}s")
    return None


async def _download_image_fullres(page, new_src: str, save_to: str,
                                  verbose: bool = True) -> Optional[str]:
    """Open the new image's detail view → Download → Original size, capture via
    expect_download, reject anything below full-res (mirrors flow_bot)."""
    clicked = await page.evaluate("""(target_src) => {
        const i = Array.from(document.querySelectorAll('img'))
            .find(e => (e.src || e.currentSrc || '') === target_src);
        if (!i) return false;
        let host = i.closest('button, [role=button], a, [tabindex]') || i;
        host.scrollIntoView({block: 'center'}); host.click();
        return true;
    }""", new_src)
    if not clicked:
        if verbose:
            print("[flow-image] couldn't open detail view")
        return None
    await page.wait_for_timeout(900)
    Path(save_to).parent.mkdir(parents=True, exist_ok=True)
    try:
        # Flow's Download split-button opens a menu with size options:
        #   "1K Original size" · "2K Upscaled" · "4K Upscaled (Upgrade)".
        # Prefer the requested size (default 2K); the 2K upscale runs server-
        # side so the download can take a bit longer — hence the wider timeout.
        # Fall back to Original size if the preferred option isn't present.
        async with page.expect_download(timeout=60000) as dl_info:
            dl_btn = page.get_by_role("button", name=re.compile(r"download", re.I))
            await dl_btn.first.click(timeout=4000)
            await page.wait_for_timeout(700)
            menu = page.get_by_role("menuitem")
            picked = False
            # Menu items read "1KOriginal size" / "2KUpscaled" / "4KUpscaled…"
            # (no space after the size), so match a leading boundary only.
            for pat in (rf"\b{re.escape(_FLOW_IMAGE_DOWNLOAD_SIZE)}", r"original"):
                loc = menu.filter(has_text=re.compile(pat, re.I))
                if await loc.count() > 0:
                    await loc.first.click(timeout=4000)
                    if verbose:
                        print(f"[flow-image] download size: "
                              f"{'2K' if 'K' in pat else 'Original'}")
                    picked = True
                    break
            if not picked and await menu.count() > 0:
                await menu.first.click(timeout=4000)
        dl = await dl_info.value
        await dl.save_as(save_to)
        # 2K upscaled ≈ 2048px long edge; 1K Original ≈ 1024-1376px. Reject only
        # obvious thumbnails (< 800px). The old Gemini ≥1600 gate does not apply.
        try:
            import lotus_imgutil
            edge = lotus_imgutil.long_edge(save_to)
        except Exception:
            edge = None
        if edge is not None and edge < _FLOW_IMAGE_MIN_LONG_EDGE:
            os.unlink(save_to)
            if verbose:
                print(f"[flow-image] rejected thumbnail ({edge}px < {_FLOW_IMAGE_MIN_LONG_EDGE})")
            return None
        if verbose:
            print(f"[flow-image] saved → {save_to} "
                  f"({edge}px, {os.path.getsize(save_to)//1024} KB)")
        return save_to
    except Exception as e:
        if verbose:
            print(f"[flow-image] download failed: {e}")
        return None
    finally:
        try:
            done = page.get_by_role("button", name="Done")
            if await done.count() > 0:
                await done.first.click(timeout=2000)
        except Exception:
            pass


async def _do_generate_image(*, prompt: str, save_to: str, aspect: str,
                             timeout: float, verbose: bool) -> Optional[str]:
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    page = None
    try:
        browser = await _connect_cdp_with_retry(pw, verbose=verbose)
        page = await _find_or_open_flow_page(browser, verbose=verbose)
        await _ensure_in_project(page, verbose=verbose)
        await _dismiss_onboarding(page, verbose=verbose)
        await _set_image_params(page, aspect, verbose=verbose)
        baseline = await _snapshot_image_urls(page)
        if not await _type_prompt(page, prompt, verbose=verbose):
            return None
        if not await _click_create(page, verbose=verbose):
            return None
        src = await _wait_for_new_image(page, baseline, timeout, verbose=verbose)
        if not src:
            return None
        return await _download_image_fullres(page, src, save_to, verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"[flow-image] FAILED: {e}")
        return None
    finally:
        try:
            await pw.stop()
        except Exception:
            pass


def text_to_image(prompt: str, save_to: str, *,
                  aspect: str = "9:16",
                  timeout: float = _IMAGE_RENDER_TIMEOUT_SEC,
                  verbose: bool = True) -> Optional[str]:
    """Generate one image from `prompt` via Google Flow (Nano Banana) and save
    it to `save_to`. Full-res only (rejects previews). Returns the path or
    None — never raises. FREE (0 credits). Signature mirrors the other image
    engines' create_image_and_download(prompt, target, verbose)."""
    if not prompt or not save_to:
        return None
    asp = _norm_image_aspect(aspect, save_to)
    with _worker_lock:
        return _submit(_do_generate_image(prompt=prompt, save_to=save_to,
                                          aspect=asp, timeout=timeout,
                                          verbose=verbose),
                       timeout=timeout + 60)


# Engine-compatible alias so flow_video can slot into the image gen chain
# exactly like flow_bot/gemini_bot (prompt, target, verbose) -> Optional[str].
# Aspect is inferred FROM THE TARGET PATH (Story*→9:16, everything else→3:4,
# the closest Flow option to the Post/Reel 4:5 slot) — passing "" makes
# _norm_image_aspect read the path instead of forcing a fixed aspect.
def create_image_and_download(prompt: str, save_to: str,
                              verbose: bool = True) -> Optional[str]:
    return text_to_image(prompt, save_to, aspect="", verbose=verbose)


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args and args[0] == "text" and len(args) >= 3:
        # python flow_video.py text "<prompt>" <save.mp4> [aspect] [duration]
        out = text_to_video(args[1], save_to=args[2],
                            aspect=args[3] if len(args) > 3 else "9:16",
                            duration=args[4] if len(args) > 4 else "8s")
    elif len(args) >= 2:
        # python flow_video.py <image> "<prompt>" [save.mp4] [aspect] [duration]
        out = image_to_video(args[0], args[1],
                            save_to=args[2] if len(args) > 2 else None,
                            aspect=args[3] if len(args) > 3 else "9:16",
                            duration=args[4] if len(args) > 4 else "8s")
    else:
        print("Usage:\n"
              "  python flow_video.py <image> '<prompt>' [save.mp4] [aspect] [duration]\n"
              "  python flow_video.py text '<prompt>' <save.mp4> [aspect] [duration]")
        sys.exit(2)
    print(f"\nResult: {out}")
    sys.exit(0 if out else 1)
