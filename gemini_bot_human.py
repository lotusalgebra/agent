"""
Gemini web-app automation via OS-level human input — Plan B / Stage 3 of
the pipeline reliability roadmap (see project_lotus_pipeline_v2_plan.md).

Why this exists alongside gemini_bot.py:
- gemini_bot.py drives the page via Playwright (CDP). For most flows that's
  fine, but Gemini's bot-detection sometimes silently throttles / returns
  text-only when it spots robotic patterns (page.evaluate JS scoring,
  expect_download interception, no-jitter timing).
- This module drives the *user's real Chrome window* via pyautogui +
  AppleScript + clipboard + OCR — exactly what a human does. There is no
  automation surface for Gemini to detect because there isn't any
  programmatic surface at all.
- Same public API as gemini_bot.py so it slots into agent_phase1._gen_image
  as an additional tier. Old gemini_bot.py is NOT replaced — this lives
  side-by-side and is opt-in via env flag (LOTUS_USE_HUMAN_BOT=1).

Design constraints (from project memory):
- Image download MUST use the in-app full-resolution path (img.src + curl
  with Chrome cookies, OR hover-toolbar Download button). NEVER right-click
  → Save Image As — that gives a compressed preview.
  See: feedback_image_download_fullsize.md
- Cross-platform-aware: macOS-first today (AppleScript paths in browser.py
  are darwin-only), Windows code paths to follow when the foundation works.

Public surface (matches gemini_bot.py):
    async def create_image_and_download(prompt: str, save_to: str) -> Optional[str]

Internal primitives — each independently testable against running Chrome:
    _focus_gemini_tab() -> bool
    _find_prompt_input(timeout=10) -> Optional[tuple[int, int]]
    _send_prompt(text) -> bool
    _wait_for_image_ready(timeout=180) -> bool          (TODO — Step 3)
    _download_latest_image_via_jssrc(save_to) -> Optional[str]    (TODO — Step 3)
    _download_latest_image_via_hover(save_to) -> Optional[str]    (TODO — fallback)
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

import pyperclip

import browser  # local module — focus_chrome_tab / screenshot / ocr_data / etc.

try:
    import websockets  # for CDP DOM-locator path
except ImportError:
    websockets = None  # OCR fallback will still work without it

GEMINI_URL_FRAGMENT = "gemini.google.com"
CDP_URL = os.environ.get("LOTUS_CDP_URL", "http://localhost:9222")


# ── CDP helpers — DOM-based locators via Chrome DevTools Protocol ─────
#
# Why CDP rather than AppleScript-execute-JS: Chrome's "Allow JavaScript
# from Apple Events" menu is locked off in this Chrome instance (likely an
# enterprise / managed-policy default), so the AppleScript path is dead.
# CDP is enabled by the --remote-debugging-port=9222 flag we already pass
# at startup, so it Just Works without further setup.
#
# We only use CDP for read-only DOM queries (locate elements, check page
# state). All actual *input* (clicks, typing, Enter) goes through pyautogui
# at the OS level — that's what keeps the actions human-like and avoids
# the bot-detection patterns Playwright accumulates.


def _cdp_get_gemini_tab() -> Optional[dict]:
    """Return the CDP target dict for the live Gemini tab, or None."""
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json", timeout=2) as r:
            tabs = json.loads(r.read())
    except Exception:
        return None
    for t in tabs:
        if (t.get("type") == "page"
                and GEMINI_URL_FRAGMENT in (t.get("url") or "")):
            return t
    return None


async def _cdp_evaluate_async(ws_url: str, js: str,
                              timeout: float = 8.0) -> dict:
    """Run JS in the page via CDP Runtime.evaluate and return parsed result.

    JS expressions in this module always return a JSON-serialised string,
    which we parse on the Python side. Returns either the parsed dict from
    the page OR an `{"error": ...}` dict on failure (never raises).
    """
    if websockets is None:
        return {"error": "websockets_module_missing"}
    try:
        async with websockets.connect(ws_url, max_size=4_000_000) as ws:
            await ws.send(json.dumps({
                "id": 1, "method": "Runtime.evaluate",
                "params": {
                    "expression": js,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            }))
            resp = json.loads(
                await asyncio.wait_for(ws.recv(), timeout=timeout))
    except Exception as e:
        return {"error": f"cdp_ws_error: {type(e).__name__}: {e}"}
    if "error" in resp:
        return {"error": f"cdp_protocol_error: {resp['error']}"}
    val = resp.get("result", {}).get("result", {}).get("value")
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {"error": "non_json_response", "raw": val[:200]}
    if isinstance(val, dict):
        return val
    return {"error": f"unexpected_value_type: {type(val).__name__}"}


def _cdp_evaluate(js: str) -> dict:
    """Sync wrapper for _cdp_evaluate_async. Returns the JSON dict that the
    page's JS produced, or `{"error": ...}` if anything went wrong."""
    tab = _cdp_get_gemini_tab()
    if not tab:
        return {"error": "no_gemini_tab"}
    return asyncio.run(_cdp_evaluate_async(tab["webSocketDebuggerUrl"], js))


# ── JS templates ──────────────────────────────────────────────────────

_JS_FIND_INPUT = """
(() => {
  const sels = ['rich-textarea',
                '[contenteditable="true"][role="textbox"]',
                '[contenteditable="true"]',
                'textarea'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el) {
      const r = el.getBoundingClientRect();
      const off = window.outerHeight - window.innerHeight;
      return JSON.stringify({
        selector: s,
        screen_x: Math.round(window.screenX + r.left + r.width/2),
        screen_y: Math.round(window.screenY + off + r.top + r.height/2),
        w: Math.round(r.width), h: Math.round(r.height),
        tag: el.tagName.toLowerCase(),
      });
    }
  }
  return JSON.stringify({error: "no_input_element_found"});
})()
"""

# Realistic-human pacing. Every action is followed by a small randomized
# wait. Without this, even pyautogui input looks robotic to a watching
# detector (uniform 200ms between every event).
def _human_pause(low: float = 0.25, high: float = 0.65) -> None:
    time.sleep(random.uniform(low, high))


# ── macOS-reliable keyboard helpers ───────────────────────────────────
#
# pyautogui.hotkey("command", "v") is unreliable on macOS — Quartz event
# synthesis sometimes drops the Cmd modifier when targeting rich-text
# editors (Gemini's Quill editor reproduces this consistently). Empirical
# A/B test: pyautogui Cmd+V → no paste; AppleScript keystroke → pastes
# correctly. Same OS-level synthetic event, different code path.
#
# For non-modifier keys (Enter, Delete) pyautogui is fine.

def _press_cmd_letter(letter: str) -> None:
    """Send Cmd+<letter> via AppleScript System Events. Use for Cmd+V,
    Cmd+A, Cmd+C, etc. — combos that pyautogui drops on macOS."""
    if not (len(letter) == 1 and letter.isalpha()):
        raise ValueError(f"single letter required, got {letter!r}")
    subprocess.run(
        ["osascript", "-e",
         f'tell application "System Events" to keystroke "{letter.lower()}" '
         'using {command down}'],
        check=False, capture_output=True,
    )


def _paste_via_keystroke(text: str) -> None:
    """Copy `text` to clipboard, paste into the focused field via Cmd+V.
    The clipboard hop keeps Unicode + multiline content intact; the
    AppleScript Cmd+V is the part that actually works on macOS."""
    pyperclip.copy(text)
    time.sleep(0.12)  # let the pasteboard settle
    _press_cmd_letter("v")


# ── Step 1: foundation primitives ─────────────────────────────────────


def _focus_gemini_tab() -> bool:
    """Bring an existing Chrome tab on gemini.google.com to the front.

    Returns False if no Gemini tab exists — caller should open one. We
    never silently open a new tab here because the canonical LOTUS startup
    procedure has the user do that explicitly (per startup memory).
    """
    if sys.platform != "darwin":
        raise NotImplementedError(
            "Windows/Linux focus path not yet implemented — Mac-first per "
            "the staged migration plan."
        )
    return browser.focus_chrome_tab(GEMINI_URL_FRAGMENT)


def _find_phrase_below(ocr: dict, words: list, y_min: int,
                       max_gap_px: int = 80, min_conf: int = 30):
    """Like browser.find_phrase but only accepts matches whose top >= y_min.

    The stock find_phrase returns the FIRST match in OCR order, which is
    typically top-to-bottom — so it can't be used to "find this phrase
    only in the bottom half of the screen". This helper does that filter.
    """
    if not words:
        return None
    words = [w.lower().strip() for w in words]
    texts = [str(t).lower() for t in ocr["text"]]
    tops = ocr["top"]
    n = len(texts)

    for i in range(n):
        if tops[i] < y_min:
            continue
        if words[0] not in texts[i]:
            continue
        try:
            c0 = int(ocr["conf"][i])
        except (ValueError, TypeError):
            c0 = 0
        if c0 < min_conf:
            continue

        last_right = ocr["left"][i] + ocr["width"][i]
        y0 = tops[i]
        j = i
        ok = True
        for w in words[1:]:
            j += 1
            while j < n and not ocr["text"][j].strip():
                j += 1
            if j >= n or w not in texts[j]:
                ok = False
                break
            try:
                cj = int(ocr["conf"][j])
            except (ValueError, TypeError):
                cj = 0
            if cj < min_conf:
                ok = False
                break
            if abs(tops[j] - y0) > 12:
                ok = False
                break
            if ocr["left"][j] - last_right > max_gap_px:
                ok = False
                break
            last_right = ocr["left"][j] + ocr["width"][j]
        if ok:
            span_left = ocr["left"][i]
            x = (span_left + last_right) // 2
            y = y0 + ocr["height"][i] // 2
            return (x, y)
    return None


def _find_prompt_input(timeout: float = 10.0) -> Optional[tuple]:
    """Locate the Gemini prompt input via DOM (CDP) with OCR as fallback.

    Primary path — CDP `Runtime.evaluate` + `getBoundingClientRect()`:
      - Reliable across Gemini's home / chat / Create-image modes.
      - Doesn't depend on placeholder text being visible.
      - Returns exact pixel-center of the real `rich-textarea` element.
      - No "Allow JavaScript from Apple Events" Chrome setting needed —
        works through the --remote-debugging-port=9222 we already pass.

    Fallback — OCR placeholder search in the viewport bottom half:
      - Used only if CDP itself fails (e.g. no Gemini tab open). Same
        brittleness as before, but we never rely on it as the primary.

    Returns (x, y) logical screen coords ready for browser.click(), or None.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _cdp_evaluate(_JS_FIND_INPUT)
        if isinstance(info, dict) and "screen_x" in info:
            return (int(info["screen_x"]), int(info["screen_y"]))
        # CDP failed or returned an error — try OCR fallback ONCE per
        # iteration before sleeping. Extreme-edge fallback only.
        screen_h = browser.screen_size()[1]
        y_min = screen_h // 2
        ocr = browser.ocr_data(browser.screenshot())
        for words in (["describe", "your", "image"],
                      ["enter", "a", "prompt"],
                      ["ask", "gemini"]):
            hit = _find_phrase_below(ocr, words, y_min=y_min)
            if hit:
                return hit
        time.sleep(0.5)
    return None


def _send_prompt(text: str, *, click_first: bool = True) -> bool:
    """Send `text` into the Gemini prompt input and submit with Enter.

    Workflow:
      1. (optional) click the prompt input area to focus it
      2. small human pause
      3. clear any existing content (Cmd+A, Delete)
      4. paste the prompt via clipboard (Unicode-safe + faster than typing)
      5. small pause
      6. press Enter to submit

    Returns True on success, False if the prompt input couldn't be found.
    """
    if click_first:
        coords = _find_prompt_input()
        if coords is None:
            return False
        browser.click(coords[0], coords[1])
        _human_pause(0.3, 0.7)

    # Clear any existing draft / partial input. Cmd+A goes through
    # AppleScript (pyautogui drops Cmd+letter combos on macOS, see helper
    # comments). Plain Delete is fine through pyautogui.
    _press_cmd_letter("a")
    _human_pause(0.1, 0.25)
    browser.press("delete")
    _human_pause(0.15, 0.35)

    # Paste via AppleScript Cmd+V (pyautogui Cmd+V is unreliable here).
    _paste_via_keystroke(text)
    _human_pause(0.4, 0.9)

    # Submit. Plain Enter has no modifier so pyautogui works fine.
    browser.press("enter")
    return True


# ── Step 3 — image-tool activation, ready-detection, download ─────────


def _navigate_fresh_chat() -> bool:
    """Navigate the live Gemini tab to a fresh /app — clears conversation
    state so the home-screen suggestion chips ("Create image", etc.) are
    visible. Returns True on success."""
    tab = _cdp_get_gemini_tab()
    if not tab:
        return False
    async def _nav():
        if websockets is None:
            return False
        async with websockets.connect(tab["webSocketDebuggerUrl"],
                                      max_size=4_000_000) as ws:
            await ws.send(json.dumps({
                "id": 1, "method": "Page.navigate",
                "params": {"url": "https://gemini.google.com/app"},
            }))
            await asyncio.wait_for(ws.recv(), timeout=8.0)
        return True
    try:
        return asyncio.run(_nav())
    except Exception:
        return False


_JS_FIND_IMAGE_CHIP = """
(() => {
  // Suggestion chip text often has an emoji prefix ("🖼️ Create image").
  // Strip non-word chars before matching so the emoji doesn't kill us.
  const all = document.querySelectorAll('button, [role="button"], mat-chip');
  for (const el of all) {
    const raw = (el.textContent || '').trim();
    if (raw.length > 60) continue;
    const cleaned = raw.toLowerCase().replace(/[^\\w\\s]/g, '').trim();
    if (cleaned === 'create image' ||
        (cleaned.includes('create') && cleaned.includes('image') &&
         !cleaned.includes('music') && !cleaned.includes('video'))) {
      const r = el.getBoundingClientRect();
      if (r.width < 5) continue;
      const off = window.outerHeight - window.innerHeight;
      return JSON.stringify({
        found: true, raw_text: raw,
        screen_x: Math.round(window.screenX + r.left + r.width/2),
        screen_y: Math.round(window.screenY + off + r.top + r.height/2),
      });
    }
  }
  return JSON.stringify({found: false});
})()
"""


def _click_image_tool_chip(timeout: float = 8.0) -> bool:
    """Find the "Create image" suggestion chip via CDP and click it via
    pyautogui. Returns True on click, False if the chip wasn't found."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _cdp_evaluate(_JS_FIND_IMAGE_CHIP)
        if info.get("found"):
            browser.focus_chrome_tab(GEMINI_URL_FRAGMENT)
            _human_pause(0.3, 0.5)
            browser.click(info["screen_x"], info["screen_y"])
            return True
        time.sleep(0.5)
    return False


_JS_CHECK_IMAGE_MODE = """
(() => {
  const ta = document.querySelector('rich-textarea');
  if (!ta) return JSON.stringify({mode: 'no_textarea'});
  const editable = ta.querySelector('[contenteditable]');
  const ph = (editable && editable.getAttribute('data-placeholder')) || '';
  return JSON.stringify({
    placeholder: ph,
    is_image_mode: ph.toLowerCase().includes('describe'),
  });
})()
"""


def _wait_for_image_mode(timeout: float = 8.0) -> bool:
    """After clicking the Create-image chip, wait for the input placeholder
    to switch to "Describe your image" — confirms image-tool is active."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _cdp_evaluate(_JS_CHECK_IMAGE_MODE)
        if info.get("is_image_mode"):
            return True
        time.sleep(0.4)
    return False


_JS_POLL_IMAGE_RESPONSE = """
(() => {
  const msgs = document.querySelectorAll('message-content');
  const last = msgs[msgs.length - 1];
  const stopBtn = document.querySelector('button[aria-label*="Stop" i]');
  if (!last) return JSON.stringify({phase: 'no_response_yet', stop: !!stopBtn});
  const imgs = last.querySelectorAll('img');
  const loaded = Array.from(imgs).filter(i => i.complete && i.naturalWidth > 0);
  // Loaded image = done enough for our purposes. Gemini sometimes keeps
  // streaming descriptive text *after* the image is rendered, leaving
  // the Stop button up for many extra seconds. Waiting for Stop to
  // disappear unnecessarily blocks download.
  let phase;
  if (loaded.length > 0) phase = 'done_with_image';
  else if (stopBtn) phase = 'streaming';
  else if (imgs.length === 0) phase = 'done_text_only';
  else phase = 'image_loading';
  return JSON.stringify({
    phase,
    img_count: imgs.length,
    loaded_count: loaded.length,
    stop_visible: !!stopBtn,
    last_text: last.textContent.slice(0, 120),
  });
})()
"""


def _wait_for_image_response_done(timeout: float = 180.0,
                                  verbose: bool = True) -> dict:
    """Poll Gemini's response until image generation is done OR fails.

    Returns the last polled state dict — caller checks `.phase`:
      'done_with_image'  → success, image is loaded and ready to extract
      'done_text_only'   → Gemini refused / replied with text instead
      'streaming' / etc  → timed out
    """
    deadline = time.time() + timeout
    last_state = {"phase": "no_response_yet"}
    last_phase_logged = None
    while time.time() < deadline:
        last_state = _cdp_evaluate(_JS_POLL_IMAGE_RESPONSE)
        phase = last_state.get("phase")
        if verbose and phase != last_phase_logged:
            elapsed = int(timeout - (deadline - time.time()))
            print(f"[human-bot] t={elapsed:3d}s  phase={phase}  "
                  f"imgs={last_state.get('img_count')}/"
                  f"{last_state.get('loaded_count')} loaded")
            last_phase_logged = phase
        if phase in ("done_with_image", "done_text_only"):
            return last_state
        time.sleep(2.0)
    return last_state


async def _download_via_playwright_capture(save_to: str,
                                           verbose: bool = True) -> Optional[str]:
    """Click the scoped Download button and capture the resulting file via
    Playwright's expect_download context manager.

    This replicates the exact recipe from gemini_bot.py:_download_image —
    the proven path. Why we don't replace it with raw CDP + setDownload-
    Behavior:
      - Chrome only routes downloads to active CDP subscribers (i.e. the
        expect_download listener). Without an active listener, the click
        triggers Gemini's Angular handler but the resulting download is
        silently dropped — confirmed empirically (4 click mechanisms +
        Browser/Page setDownloadBehavior, all produced no file).
      - Playwright's expect_download is the existing battle-tested
        subscriber. Reusing it is the right move per the user's "stop
        reinventing, use what works" directive.

    The "human-like" character of this module is preserved: input typing
    via AppleScript keystroke, tool-chip click via pyautogui. Download
    capture is plumbing, not a place bot-detection cares about.
    """
    cdp_url = os.environ.get("LOTUS_CDP_URL", "http://localhost:9222")
    save_path = Path(save_to)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            if verbose: print(f"[human-bot] CDP attach failed: {e!r}")
            return None
        try:
            if not browser.contexts:
                if verbose: print("[human-bot] no Chrome contexts found")
                return None
            ctx = browser.contexts[0]
            page = next((p for p in ctx.pages
                         if "gemini.google.com" in (p.url or "")), None)
            if not page:
                if verbose: print("[human-bot] no Gemini page in Chrome")
                return None

            # Locate the latest image (last message bubble)
            img_loc = page.locator("message-content").last.locator("img").first
            try:
                await img_loc.wait_for(state="visible", timeout=10_000)
            except Exception as e:
                if verbose: print(f"[human-bot] image not visible: {e!r}")
                return None

            # Scroll into view + hover sequence (same as gemini_bot._download_image)
            try:
                await img_loc.evaluate(
                    "el => el.scrollIntoView({block: 'center', "
                    "inline: 'center', behavior: 'instant'})"
                )
            except Exception:
                pass
            await page.wait_for_timeout(500)
            # Clear stale hover by parking mouse top-left
            await page.mouse.move(10, 10)
            await page.wait_for_timeout(200)

            box = None
            try:
                box = await img_loc.bounding_box(timeout=3000)
            except Exception:
                pass
            if box and box.get("width", 0) > 10:
                await page.mouse.move(box["x"] + box["width"]/2,
                                      box["y"] + box["height"]/2)
            else:
                try: await img_loc.hover(timeout=8000)
                except Exception: pass
            await page.wait_for_timeout(900)

            # Mark the SCOPED Download button (in this image's bubble only)
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
                        for (const b of node.querySelectorAll('button, [role=\"button\"]')) {
                            if (!isDl(b)) continue;
                            const r = b.getBoundingClientRect();
                            if (r.width < 1 || r.height < 1) continue;
                            b.setAttribute('data-lotus-picked', '1');
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if not clicked:
                if verbose: print("[human-bot] Download button not found in image bubble")
                return None

            dl_button = page.locator('[data-lotus-picked="1"]').first
            try:
                await dl_button.wait_for(state="visible", timeout=6000)
            except Exception:
                pass

            # Hover button to keep toolbar from collapsing as we focus it
            try:
                bb = await dl_button.bounding_box(timeout=2000)
                if bb:
                    await page.mouse.move(bb["x"] + bb["width"]/2,
                                          bb["y"] + bb["height"]/2)
                    await page.wait_for_timeout(300)
            except Exception:
                pass

            # Capture the download — this is the part that was missing
            try:
                async with page.expect_download(timeout=60_000) as dl_info:
                    await dl_button.click(timeout=15_000, force=True)
            except Exception as e:
                if verbose: print(f"[human-bot] download click/capture failed: {e!r}")
                return None
            finally:
                # Clear scoping marker so subsequent calls don't race on it
                try:
                    await page.evaluate(
                        "document.querySelectorAll('[data-lotus-picked]')"
                        ".forEach(el => el.removeAttribute('data-lotus-picked'))"
                    )
                except Exception: pass

            dl = await dl_info.value
            tmp = await dl.path()
            import shutil
            shutil.move(str(tmp), str(save_path))
            size = save_path.stat().st_size
            if size <= 2048:
                if verbose: print(f"[human-bot] download was tiny ({size}b) — discarding")
                try: save_path.unlink()
                except Exception: pass
                return None
            if verbose:
                print(f"[human-bot] downloaded {size:,} bytes → {save_path}")
            return str(save_path)
        finally:
            # Don't close — it's the user's real Chrome, NOT ours to close
            pass


_JS_CANVAS_EXPORT = """
(async () => {
  try {
    const msgs = document.querySelectorAll('message-content');
    const last = msgs[msgs.length - 1];
    if (!last) return JSON.stringify({error: 'no_response'});
    const img = last.querySelector('img');
    if (!img) return JSON.stringify({error: 'no_img'});
    if (!img.complete || !img.naturalWidth)
      return JSON.stringify({error: 'not_loaded'});

    // Paint the loaded <img> onto a same-size canvas. Gemini loads images
    // at full backend resolution into <img> (naturalWidth/Height ==
    // backend dims), so canvas paint at those dims gets the full-res
    // bytes — same as the in-app Download button would produce. The
    // alternative paths (fetch blob URL / click Download button) don't
    // work in this Chrome — see project_lotus_pipeline_v2_plan.md.
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0);
    let dataUrl;
    try {
      dataUrl = canvas.toDataURL('image/png');
    } catch (e) {
      return JSON.stringify({error: 'tainted_canvas', detail: String(e)});
    }
    const prefix = 'data:image/png;base64,';
    if (!dataUrl.startsWith(prefix))
      return JSON.stringify({error: 'bad_prefix', head: dataUrl.slice(0, 40)});
    return JSON.stringify({
      ok: true,
      base64: dataUrl.slice(prefix.length),
      width: img.naturalWidth,
      height: img.naturalHeight,
    });
  } catch (e) {
    return JSON.stringify({error: 'exception', msg: String(e)});
  }
})()
"""


def _canvas_export_image(save_to: str) -> Optional[str]:
    """Extract the latest generated image's full-res bytes via canvas-paint
    and write to `save_to`. Returns save_to on success, None on failure.

    Why canvas-paint instead of clicking the Download button:
      - Download button (aria="Download full-sized image") fires its
        Angular click handler but doesn't produce a file in ~/Downloads
        — verified empirically with both pyautogui OS-click and CDP
        Runtime.evaluate `.click()` calls. Mechanism unclear; possibly
        `event.isTrusted` gating or component lifecycle issue.
      - blob URL fetch (img.src) returns "TypeError: Failed to fetch"
        because Gemini revokes the URL after the <img> has loaded.
      - Canvas paints from the live <img> (which holds the full-res
        bytes loaded from Gemini's backend). naturalWidth/Height match
        the backend dims, so output is full-res, not a thumbnail.

    Tradeoff (per feedback_image_download_fullsize.md): canvas works for
    Gemini specifically because <img>.naturalWidth equals the backend
    resolution. For sites where the visible img is a thumbnail of a
    larger backend image, this would lose resolution.
    """
    import base64 as _b64
    info = _cdp_evaluate(_JS_CANVAS_EXPORT)
    if not info.get("ok"):
        return None
    try:
        raw = _b64.b64decode(info["base64"])
        Path(save_to).parent.mkdir(parents=True, exist_ok=True)
        Path(save_to).write_bytes(raw)
    except Exception:
        return None
    return save_to


# ── Public surface — matches gemini_bot.py for drop-in replacement ────


def create_image_and_download(prompt: str, save_to: str,
                              *, verbose: bool = True) -> Optional[str]:
    """Generate one image from `prompt` via OS-level human-input automation
    and save to `save_to`. Returns the saved path on success, None on
    failure (so agent_phase1._gen_image falls through to the next tier).

    Sync signature matching gemini_bot.create_image_and_download. Plugs
    into agent_phase1._gen_image as the 'human' engine — selectable via
    the dashboard pipeline switch (`POST /api/config/pipeline`) or env
    `LOTUS_GEN_IMAGE_PIPELINE=human` at boot.

    Workflow:
      1. Navigate to fresh /app (resets to home, makes Create-image chip
         visible).
      2. Click "Create image" suggestion chip → activates image tool.
      3. Wait for placeholder to switch to "Describe your image".
      4. Send the prompt via clipboard paste + Enter.
      5. Poll for image-response-done.
      6. Canvas-paint the loaded <img> → write PNG to disk.
    """
    if verbose: print("[human-bot] navigate to fresh /app")
    if not _navigate_fresh_chat():
        if verbose: print("[human-bot] navigate failed (no Gemini tab?)")
        return None
    time.sleep(3.0)  # let the home page render

    if verbose: print("[human-bot] click Create image chip")
    if not _click_image_tool_chip():
        if verbose: print("[human-bot] Create image chip not found")
        return None

    if verbose: print("[human-bot] wait for image mode")
    if not _wait_for_image_mode():
        if verbose: print("[human-bot] placeholder didn't switch — image mode not active")
        return None

    if verbose: print(f"[human-bot] send prompt ({len(prompt)} chars)")
    if not _send_prompt(prompt):
        if verbose: print("[human-bot] send_prompt failed (input not located)")
        return None

    if verbose: print("[human-bot] wait for image response")
    state = _wait_for_image_response_done(verbose=verbose)
    if state.get("phase") != "done_with_image":
        if verbose:
            print(f"[human-bot] response was {state.get('phase')!r} — "
                  f"text preview: {state.get('last_text','')!r}")
        return None

    # Primary download path: Playwright expect_download capture (proven
    # working in gemini_bot.py — the existing pipeline's recipe).
    if verbose: print(f"[human-bot] download via Playwright capture → {save_to}")
    saved = asyncio.run(_download_via_playwright_capture(save_to, verbose=verbose))

    # Fallback: canvas-export. Used only if the Playwright path fails for
    # any reason (CDP attach issue, button locator change, etc.). Same
    # full-res bytes since <img>.naturalWidth/Height matches Gemini's
    # backend resolution.
    if not saved:
        if verbose: print("[human-bot] Playwright capture failed → falling back to canvas-export")
        saved = _canvas_export_image(save_to)
        if not saved:
            if verbose: print("[human-bot] canvas export also failed — giving up")
            return None

    if verbose:
        size = Path(saved).stat().st_size if Path(saved).exists() else 0
        print(f"[human-bot] ✓ saved {size:,} bytes to {saved}")
    return saved


def _dev_smoke_full_pipeline(save_to: Optional[str] = None) -> str:
    """End-to-end smoke test of the full pipeline. Saves to a real
    canonical lotus_config path by default — proves the folder layout
    integration works the same way as agent_phase1._gen_image would
    feed it in production. Returns the saved path or a failure string."""
    if save_to is None:
        # Use lotus_config to compute the next FrameN path under the
        # canonical Projects/<brand><MMDDYYYY>/Post1/ layout. This is
        # exactly how agent_phase1._gen_image picks `target` in real
        # pipelines.
        try:
            import lotus_config as _lc
            save_to = _lc.next_child_path("image")
        except Exception as e:
            return f"FAILED — couldn't get canonical path from lotus_config: {e}"

    print(f"[smoke] saving to canonical path: {save_to}")
    result = create_image_and_download(
        "Generate a single small abstract image of a green circle on white background",
        save_to, verbose=True,
    )
    if result:
        size = Path(result).stat().st_size if Path(result).exists() else 0
        return f"OK saved={result} size={size:,} bytes"
    return "FAILED — see log lines above for the failing stage"


# ── Dev helpers (run directly from the shell to verify a primitive) ───


def _dev_smoke_focus_and_input() -> str:
    """Manually-runnable smoke test for Step 1 primitives. NO image work.
    Focuses the Gemini tab, finds the prompt input, sends a harmless test
    prompt that won't actually generate an image (just plain text).

    Run from venv:
        venv/bin/python -c 'from gemini_bot_human import _dev_smoke_focus_and_input as t; print(t())'

    Will take focus + move mouse for ~5s. Hands off the keyboard while it
    runs. Returns "ok" on success or a failure reason string.
    """
    if not _focus_gemini_tab():
        return "no Gemini tab — open https://gemini.google.com/app first"
    _human_pause(0.4, 0.8)
    coords = _find_prompt_input()
    if coords is None:
        return "OCR couldn't find 'Ask Gemini' input — UI may have changed"
    test_prompt = "Reply with the single word: ACK"
    if not _send_prompt(test_prompt):
        return "send_prompt returned False"
    return f"ok — sent test prompt at {coords}"


if __name__ == "__main__":
    # Direct invocation runs the Step 1 smoke test.
    print(_dev_smoke_focus_and_input())
