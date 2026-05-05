"""
Browser automation primitives for LOTUS Agent.

Architecture (see project memory: 'Everything-in-browser architecture for LOTUS'):
- All external web services (Gemini, Drive, Grok, Claude.ai, ...) are driven by
  focusing the already-logged-in Chrome browser and performing UI actions via
  pyautogui + AppleScript + clipboard.
- Selenium is intentionally NOT used — it can't share a running Chrome's
  user-data-dir, and quitting Chrome every run breaks the workflow.
- UI elements are located by OCR (pytesseract) rather than hardcoded
  coordinates, so the automations survive most Google UI refreshes.

Primitives (all safe to import; Chrome is only touched when you call one):
    focus_chrome_tab(url_substring)  -> bool
    open_url(url)                    -> None
    screenshot(save_to=None)         -> PIL.Image
    ocr_data(img)                    -> dict (pytesseract image_to_data output)
    find_text(ocr, query)            -> (x, y, confidence) or None
    click(x, y, button='left')       -> None
    paste_text(text)                 -> None        # clipboard + Cmd+V
    press(*keys)                     -> None        # e.g. press('cmd', 'l')
    wait_for_text(query, timeout=20, poll=1.0) -> (x, y) or None
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

import pyautogui
import pyperclip
from PIL import Image
import pytesseract

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.2


# ── Tab focus ────────────────────────────────────────────────────────────

def focus_chrome_tab(url_substring: str) -> bool:
    """Activate the first Chrome tab whose URL contains url_substring.

    Returns True on success, False if no matching tab exists. macOS only
    (AppleScript). Does not open a new tab — use open_url() for that.
    """
    if sys.platform != "darwin":
        raise NotImplementedError("focus_chrome_tab is macOS-only for now")

    script = f'''
    tell application "Google Chrome"
        repeat with w in windows
            set tabIdx to 0
            repeat with t in tabs of w
                set tabIdx to tabIdx + 1
                if URL of t contains "{url_substring}" then
                    set active tab index of w to tabIdx
                    set index of w to 1
                    activate
                    return "true"
                end if
            end repeat
        end repeat
    end tell
    return "false"
    '''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    found = "true" in r.stdout.lower()
    if found:
        time.sleep(0.4)  # let Chrome come to foreground
    return found


def open_url(url: str) -> None:
    """Open url in Google Chrome (always — never system default). On macOS
    uses the last-used profile so the user lands in their logged-in session
    (Gemini / NB2 stays signed in, Claude.ai stays signed in, etc.)."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if sys.platform == "darwin":
        # Read last-used profile so we open in the signed-in session
        profile = None
        try:
            import json as _json
            pref = Path(os.path.expanduser(
                "~/Library/Application Support/Google/Chrome/Local State"))
            if pref.exists():
                data = _json.loads(pref.read_text())
                profile = data.get("profile", {}).get("last_used")
        except Exception:
            pass
        cmd = ["open", "-a", "Google Chrome", url]
        if profile:
            cmd += ["--args", f"--profile-directory={profile}"]
        try:
            subprocess.run(cmd, check=False, capture_output=True)
            time.sleep(2.5)
            return
        except Exception:
            pass   # fall through to default browser as a last resort
    webbrowser.open(url)
    time.sleep(2.5)


def navigate_chrome_tab(url: str, url_match: Optional[str] = None,
                        wait: float = 2.5) -> bool:
    """Navigate the focused (or url_match-matched) Chrome tab to url.

    Uses AppleScript to set `URL of active tab` directly — bypasses keyboard
    focus issues where Cmd+L keystrokes can be absorbed by modal panels.
    Returns True if AppleScript executed without error.
    """
    if sys.platform != "darwin":
        raise NotImplementedError("navigate_chrome_tab is macOS-only for now")
    if url_match:
        focus_chrome_tab(url_match)
        time.sleep(0.3)
    script = f'''
    tell application "Google Chrome"
        if (count of windows) is 0 then
            return "false"
        end if
        set URL of active tab of front window to "{url}"
        return "true"
    end tell
    '''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    ok = "true" in r.stdout.lower()
    if ok and wait > 0:
        time.sleep(wait)
    return ok


# ── Screen capture + OCR ─────────────────────────────────────────────────

def screenshot(save_to: Optional[str] = None) -> Image.Image:
    """Take a full-screen screenshot. Optionally save to path."""
    img = pyautogui.screenshot()
    if save_to:
        img.save(save_to)
    return img


def ocr_data(img: Image.Image) -> dict:
    """Run pytesseract OCR and return its DICT output, with coordinates
    already scaled to pyautogui's *logical* click coordinate space.

    On Retina Macs, pyautogui.screenshot() returns physical pixels (e.g.
    5120x2880) while pyautogui.click() uses logical coords (2560x1440).
    We detect the ratio and divide every (left, top, width, height) by it,
    so downstream callers can pass OCR-derived (x, y) directly to click().
    """
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    logical_w, logical_h = pyautogui.size()
    phys_w, phys_h = img.size
    sx = phys_w / logical_w if logical_w else 1.0
    sy = phys_h / logical_h if logical_h else 1.0
    if sx != 1.0 or sy != 1.0:
        for key, s in (("left", sx), ("width", sx), ("top", sy), ("height", sy)):
            data[key] = [int(v / s) for v in data[key]]
    return data


def find_text(ocr: dict, query: str, min_conf: int = 30) -> Optional[tuple]:
    """Find `query` (substring, case-insensitive) in OCR data.

    Returns (center_x, center_y, confidence) of the first match, or None.
    """
    q = query.lower().strip()
    for i, text in enumerate(ocr["text"]):
        if not text:
            continue
        try:
            conf = int(ocr["conf"][i])
        except (ValueError, TypeError):
            conf = 0
        if conf < min_conf:
            continue
        if q in str(text).lower():
            x = ocr["left"][i] + ocr["width"][i] // 2
            y = ocr["top"][i] + ocr["height"][i] // 2
            return (x, y, conf)
    return None


def find_phrase(ocr: dict, words: list, max_gap_px: int = 80,
                min_conf: int = 30) -> Optional[tuple]:
    """Find a multi-word phrase split across adjacent OCR tokens.

    `words` is a list of lowercase tokens (e.g. ["ask", "gemini"]). A match
    requires each word's token to appear consecutively on roughly the same
    baseline (y within 12 px) with an x-gap no larger than max_gap_px.

    Returns (center_x, center_y, min_conf_of_span) of the full span, or None.
    """
    if not words:
        return None
    words = [w.lower().strip() for w in words]
    texts = [str(t).lower() for t in ocr["text"]]
    n = len(texts)

    for i in range(n):
        if words[0] not in texts[i]:
            continue
        try:
            c0 = int(ocr["conf"][i])
        except (ValueError, TypeError):
            c0 = 0
        if c0 < min_conf:
            continue

        span_ok = True
        last_right = ocr["left"][i] + ocr["width"][i]
        y0 = ocr["top"][i]
        min_c = c0
        j = i

        for w in words[1:]:
            # advance j to next non-empty token
            j += 1
            while j < n and not ocr["text"][j].strip():
                j += 1
            if j >= n:
                span_ok = False
                break
            if w not in texts[j]:
                span_ok = False
                break
            try:
                cj = int(ocr["conf"][j])
            except (ValueError, TypeError):
                cj = 0
            if cj < min_conf:
                span_ok = False
                break
            if abs(ocr["top"][j] - y0) > 12:
                span_ok = False
                break
            if ocr["left"][j] - last_right > max_gap_px:
                span_ok = False
                break
            last_right = ocr["left"][j] + ocr["width"][j]
            min_c = min(min_c, cj)

        if span_ok:
            span_left = ocr["left"][i]
            span_right = last_right
            x = (span_left + span_right) // 2
            y = ocr["top"][i] + ocr["height"][i] // 2
            return (x, y, min_c)
    return None


def find_all_text(ocr: dict, query: str, min_conf: int = 30) -> list:
    """Like find_text but returns every match."""
    q = query.lower().strip()
    hits = []
    for i, text in enumerate(ocr["text"]):
        if not text:
            continue
        try:
            conf = int(ocr["conf"][i])
        except (ValueError, TypeError):
            conf = 0
        if conf < min_conf:
            continue
        if q in str(text).lower():
            x = ocr["left"][i] + ocr["width"][i] // 2
            y = ocr["top"][i] + ocr["height"][i] // 2
            hits.append((str(text), x, y, conf))
    return hits


# ── Input actions ────────────────────────────────────────────────────────

def click(x: int, y: int, button: str = "left") -> None:
    """macOS Retina note: pyautogui click coords are in the *logical* coord
    space (same as screenshot pixel space ÷ scale factor on some setups).
    Pass coordinates derived from pytesseract on a pyautogui screenshot —
    they will be consistent with each other.
    """
    pyautogui.click(x, y, button=button)


def paste_text(text: str) -> None:
    """Unicode-safe text input: copy to clipboard, paste with Cmd+V."""
    pyperclip.copy(text)
    time.sleep(0.1)
    pyautogui.hotkey("command", "v")


def press(*keys: str) -> None:
    """Wrapper for pyautogui.hotkey — e.g. press('command', 'l')."""
    pyautogui.hotkey(*keys)


# ── Polling ──────────────────────────────────────────────────────────────

def wait_for_text(query: str, timeout: float = 20.0, poll: float = 1.0,
                  min_conf: int = 30) -> Optional[tuple]:
    """Repeatedly screenshot + OCR until `query` appears on screen.

    Returns (x, y) of the first match, or None on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        hit = find_text(ocr_data(screenshot()), query, min_conf=min_conf)
        if hit:
            return (hit[0], hit[1])
        time.sleep(poll)
    return None


# ── Coordinate scaling helper ────────────────────────────────────────────

def screen_size() -> tuple:
    """Return (width, height) as reported by pyautogui (logical pixels)."""
    return pyautogui.size()
