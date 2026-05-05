"""
Gemini (gemini.google.com) integration for LOTUS Agent.

Architecture: see project memory 'Everything-in-browser architecture for LOTUS'
and the reusable primitives in browser.py.

v1 capabilities (interpretation A from 2026-04-18 session):
    ensure_thinking_mode()  -> bool   # idempotent; returns True if Thinking is active afterwards
    create_image(prompt)    -> bool   # sends "Generate an image: <prompt>" in chat

v1 does NOT auto-download generated images — that requires scripting the
macOS Save-As dialog or enabling Chrome's "Allow JavaScript from Apple Events"
to pull image URLs from the DOM. Add in v2 once v1 is trusted.

UI anchors discovered 2026-04-18 (tokens in logical/click coord space):
    'Ask Gemini'   placeholder in the prompt box              ~(960, 717)
    'Thinking' v   current mode label in model switcher       ~(1542, 767)
    'Create image' suggestion chip on welcome screen          ~(1113, 851)
Coordinates may drift when Google updates the UI — use OCR, not hardcoded points.
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pyautogui

import browser

GEMINI_URL_MATCH = "gemini.google.com"
GEMINI_URL_FULL = "https://gemini.google.com/app"


def _focus_or_open() -> None:
    """Ensure the Gemini tab is frontmost. Opens a new tab if not found."""
    if not browser.focus_chrome_tab(GEMINI_URL_MATCH):
        browser.open_url(GEMINI_URL_FULL)
        time.sleep(3.0)
    else:
        time.sleep(0.8)


def ensure_thinking_mode(verbose: bool = True) -> bool:
    """Make sure Gemini's mode selector shows 'Thinking'.

    Idempotent: checks current state first and only clicks if needed.
    Returns True iff Thinking is confirmed active when the function exits.
    """
    _focus_or_open()

    ocr = browser.ocr_data(browser.screenshot())
    if browser.find_text(ocr, "Thinking", min_conf=60):
        if verbose:
            print("[gemini] Thinking mode already active.")
        return True

    # Not visible — try to open the mode dropdown. Look for the down-arrow
    # chevron, or a model label like 'Fast' / '2.5' that sits in the same slot.
    anchor = None
    for candidate in ("Fast", "2.5", "Flash", "Pro "):
        hit = browser.find_text(ocr, candidate, min_conf=50)
        if hit:
            anchor = hit
            break

    if not anchor:
        if verbose:
            print("[gemini] Could not locate the mode selector. Aborting toggle.")
        return False

    x, y, _ = anchor
    browser.click(x, y)
    time.sleep(0.8)

    target = browser.wait_for_text("Thinking", timeout=6, poll=0.6, min_conf=50)
    if not target:
        if verbose:
            print("[gemini] Mode menu opened but 'Thinking' option not seen.")
        browser.press("escape")
        return False

    browser.click(target[0], target[1])
    time.sleep(0.6)

    ocr = browser.ocr_data(browser.screenshot())
    ok = browser.find_text(ocr, "Thinking", min_conf=60) is not None
    if verbose:
        print(f"[gemini] Thinking mode now active: {ok}")
    return ok


def create_image(prompt: str, verbose: bool = True) -> bool:
    """Send an image-generation prompt to Gemini.

    Pastes 'Generate an image: <prompt>' into the Ask-Gemini input and
    presses Enter. Does NOT wait for the image to render or download it.
    Returns True iff the prompt was pasted+sent without error.
    """
    if not prompt.strip():
        raise ValueError("prompt must not be empty")

    _focus_or_open()

    # If we're sitting in the expanded image detail panel (Edit Image near
    # the top of the screen), back out to chat view. Escape doesn't close
    # this panel (it's not a modal) and the close-X coordinates drift
    # between Gemini UI states, making a coord-based click unreliable.
    # Navigating the URL bar to the main chat page ALWAYS works.
    ocr = browser.ocr_data(browser.screenshot())
    edit = browser.find_phrase(ocr, ["edit", "image"], min_conf=40)
    if edit and edit[1] < 400:
        if verbose:
            print("[gemini] Expanded panel detected — navigating tab via AppleScript.")
        browser.navigate_chrome_tab(GEMINI_URL_FULL, wait=2.0)
        # Poll for Tools anchor to confirm fresh chat view is ready
        ready = browser.wait_for_text("Tools", timeout=12, poll=1.0, min_conf=40)
        if not ready:
            if verbose:
                print("[gemini] Chat view never rendered after URL nav. Aborting.")
            return False
        ocr = browser.ocr_data(browser.screenshot())

    # "Tools" is a unique label in the bottom input area and is always
    # present (unlike the placeholder, which disappears when the chat has
    # messages). Input click-point sits 88px left and 56px above it — offsets
    # measured from discovery on 2026-04-18.
    tools = browser.find_text(ocr, "Tools", min_conf=60)
    if not tools:
        # Lower confidence before giving up.
        tools = browser.find_text(ocr, "Tools", min_conf=35)
    if not tools:
        # Recovery: the tab is in an unexpected state (dialog, modal,
        # artifact panel other than Edit-Image, loading screen). Force a
        # fresh URL navigation and wait for the Tools anchor to paint.
        if verbose:
            print("[gemini] Tools anchor missing — forcing URL reset + retry.")
        browser.navigate_chrome_tab(GEMINI_URL_FULL, wait=3.0)
        hit = browser.wait_for_text("Tools", timeout=15, poll=1.0, min_conf=35)
        if not hit:
            # One more try: dismiss any overlay with Escape + reload.
            try:
                browser.press("escape"); time.sleep(0.4)
                browser.press("escape"); time.sleep(0.4)
            except Exception:
                pass
            browser.navigate_chrome_tab(GEMINI_URL_FULL, wait=3.5)
            hit = browser.wait_for_text("Tools", timeout=15, poll=1.0, min_conf=35)
        if not hit:
            if verbose:
                print("[gemini] Tools anchor still missing after 2 resets — aborting.")
            return False
        ocr = browser.ocr_data(browser.screenshot())
        tools = browser.find_text(ocr, "Tools", min_conf=35) or hit[:2] + (0,)
    x, y = tools[0] - 88, tools[1] - 56
    browser.click(x, y)
    time.sleep(0.4)

    full_prompt = prompt if prompt.lower().startswith(("generate", "create", "draw", "make")) \
                         else f"Generate an image: {prompt}"
    browser.paste_text(full_prompt)
    time.sleep(0.4)
    browser.press("enter")

    if verbose:
        print(f"[gemini] Sent: {full_prompt!r}")
    return True


def wait_for_image(timeout: float = 90.0, poll: float = 2.0) -> Optional[tuple]:
    """Poll the Gemini tab until a generated image appears.

    Completion is detected by the presence of the 'Edit Image' button, which
    Gemini renders under every generated image. Returns (img_cx, img_cy)
    estimated from the Edit-Image button position, or None on timeout.
    """
    _focus_or_open()
    deadline = time.time() + timeout
    while time.time() < deadline:
        ocr = browser.ocr_data(browser.screenshot())
        # "Edit" + "Image" appear as adjacent tokens on the image result card
        hit = browser.find_phrase(ocr, ["edit", "image"], min_conf=50)
        if hit:
            # The Edit-Image button sits below the image by ~15-30 px.
            # Image center is roughly 240 px above the button (empirically
            # from the 2026-04-18 lotus test: button y=767, image y≈525).
            return (hit[0], max(0, hit[1] - 240))
        time.sleep(poll)
    return None


def _locate_download_button(verbose: bool = True) -> Optional[tuple]:
    """Find Gemini's 'Download full size' button in the expanded image view.

    The button lives in a toolbar to the right of the 'Edit Image' text label
    and has NO visible label itself — only a tooltip that reveals 'Download
    full size' on hover. We locate 'Edit Image' via OCR, then hover at a
    position ~200 px to its right and confirm the tooltip text before
    returning the click coordinate.

    Returns (x, y) logical coords of the button, or None if not found.
    Assumes the Gemini tab is already focused AND the image is in expanded
    view (Edit Image sits near the top of the screen, not the bottom).
    """
    ocr = browser.ocr_data(browser.screenshot())
    edit = browser.find_phrase(ocr, ["edit", "image"], min_conf=40)
    if not edit:
        if verbose:
            print("[gemini] Edit Image anchor missing — not in expanded view?")
        return None
    ex, ey, _ = edit
    if ey > 400:
        if verbose:
            print(f"[gemini] 'Edit Image' at y={ey} — looks like chat view, not expanded.")
        return None

    # Sweep a few candidate x-offsets to the right of Edit Image.
    # Observed 2026-04-18: Edit Image at (2088-2135, 180), Download at (2316, 186).
    # That's roughly +180 to +230 px right of Edit Image's left edge.
    for dx in (200, 180, 220, 160, 240):
        cx, cy = ex + dx - 24, ey + 6   # ex is span center; account for "Edit Image" width ~48
        pyautogui.moveTo(cx, cy, duration=0.2)
        time.sleep(1.1)
        probe_ocr = browser.ocr_data(browser.screenshot())
        # Tooltip text we expect: 'Download full size'
        tip = browser.find_text(probe_ocr, "Download", min_conf=40)
        if tip:
            # Confirm it's a tooltip (should appear just BELOW our hover)
            if abs(tip[0] - cx) < 200 and 20 < tip[1] - cy < 120:
                if verbose:
                    print(f"[gemini] Download button located at ({cx},{cy}) — tooltip at ({tip[0]},{tip[1]})")
                return (cx, cy)
    if verbose:
        print("[gemini] Could not locate Download button via tooltip probe.")
    return None


def download_latest_image(save_to: str, img_xy: Optional[tuple] = None,
                          verbose: bool = True) -> Optional[str]:
    """Download the full-resolution image from Gemini via the in-app button.

    Flow:
      1. Ensure Gemini tab is focused.
      2. Detect whether we're in chat view (Edit Image near bottom) or
         expanded view (Edit Image near top). If chat view, click the
         image to enter expanded view.
      3. Locate the 'Download full size' button by hovering near Edit
         Image and confirming the tooltip.
      4. Click it. Chrome downloads the PNG to ~/Downloads.
      5. Move the new file to `save_to`.

    NEVER uses browser right-click 'Save Image As' — that saves a compressed
    preview, not the full-resolution original. See memory
    'feedback_image_download_fullsize.md'.
    """
    _focus_or_open()

    # Are we in chat view or expanded view?
    ocr = browser.ocr_data(browser.screenshot())
    edit_hit = browser.find_phrase(ocr, ["edit", "image"], min_conf=40)
    if not edit_hit:
        if verbose:
            print("[gemini] No 'Edit Image' anchor — no generated image present?")
        return None

    if edit_hit[1] > 400:
        # Chat view — click the image (estimated 240 px above Edit Image) to expand
        img_cx = img_xy[0] if img_xy else edit_hit[0]
        img_cy = img_xy[1] if img_xy else max(0, edit_hit[1] - 240)
        if verbose:
            print(f"[gemini] Chat view detected — clicking image at ({img_cx},{img_cy}) to expand.")
        pyautogui.click(img_cx, img_cy)
        time.sleep(2.0)

    downloads = Path(os.path.expanduser("~/Downloads"))
    before = {p.name: p.stat().st_mtime for p in downloads.iterdir() if p.is_file()}

    btn = _locate_download_button(verbose=verbose)
    if not btn:
        return None

    pyautogui.click(btn[0], btn[1])
    time.sleep(1.5)  # brief settle before we start polling

    # Wait up to 20 s for the final renamed file. Chrome uses two temp naming
    # patterns on macOS: `.com.google.Chrome.XXXXXX` (during transfer) and
    # `name.png.crdownload` (progressive download). Both rename to the final
    # filename atomically when done.
    def is_temp(name: str) -> bool:
        return name.startswith(".com.google.Chrome.") or name.endswith(".crdownload")

    src = None
    last_temp = None
    for _ in range(20):
        new_files = []
        for p in downloads.iterdir():
            if not p.is_file():
                continue
            prev_mtime = before.get(p.name)
            if prev_mtime is None or p.stat().st_mtime > prev_mtime:
                new_files.append((p.stat().st_mtime, p))
        final = [(m, p) for m, p in new_files if not is_temp(p.name)]
        if final:
            final.sort(reverse=True)
            src = final[0][1]
            break
        temp = [(m, p) for m, p in new_files if is_temp(p.name)]
        if temp:
            last_temp = sorted(temp, reverse=True)[0][1]
        time.sleep(1.0)

    if src is None and last_temp is not None:
        # Chrome never renamed — use the temp file directly (it's a valid
        # PNG, just hasn't been renamed). Give it a timestamp-based name.
        if verbose:
            print(f"[gemini] Chrome left temp file unrenamed — rescuing it.")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_name = last_temp.parent / f"gemini_{ts}.png"
        last_temp.rename(final_name)
        src = final_name

    if src is None:
        if verbose:
            print("[gemini] No new file appeared in ~/Downloads — download may have failed.")
        return None

    dst = Path(os.path.expanduser(save_to))
    if dst.is_dir() or (not dst.suffix and not dst.exists()):
        dst.mkdir(parents=True, exist_ok=True)
        dst = dst / src.name
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)

    shutil.move(str(src), str(dst))
    if verbose:
        print(f"[gemini] Full-size image saved → {dst}")
    return str(dst)


def create_image_and_download(prompt: str, save_to: str,
                              wait_timeout: Optional[float] = None,
                              verbose: bool = True) -> Optional[str]:
    """End-to-end: ensure thinking, send prompt, wait for image, download it.

    If `wait_timeout` is None, scale it with the prompt length:
      - up to 1 KB  → 120 s
      - 1-2 KB      → 210 s
      - 2-3 KB      → 300 s
      - 3 KB+       → 420 s
    Editorial prompts (typography-heavy, 2-3 KB) routinely need 2-3 min.
    Returns the saved file path, or None on any failure.
    """
    if wait_timeout is None:
        L = len(prompt or "")
        wait_timeout = 120.0 if L < 1000 else \
                       210.0 if L < 2000 else \
                       300.0 if L < 3000 else 420.0
    ensure_thinking_mode(verbose=verbose)
    if not create_image(prompt, verbose=verbose):
        return None
    if verbose:
        print(f"[gemini] Waiting up to {wait_timeout:.0f}s for image "
              f"(prompt is {len(prompt)} chars)...")
    img_xy = wait_for_image(timeout=wait_timeout)
    if not img_xy:
        if verbose:
            print("[gemini] Image did not finish generating within timeout.")
        return None
    if verbose:
        print(f"[gemini] Image ready at ~{img_xy}. Downloading...")
    return download_latest_image(save_to, img_xy=img_xy, verbose=verbose)


def dry_run() -> None:
    """Discovery helper — prints current UI anchors without clicking."""
    _focus_or_open()
    ocr = browser.ocr_data(browser.screenshot())
    tools = browser.find_text(ocr, "Tools", min_conf=40)
    if tools:
        input_xy = (tools[0] - 88, tools[1] - 56)
        print(f"  {'input (via Tools)':18} at {input_xy} conf={tools[2]}")
        print(f"  {'Tools':18} at ({tools[0]}, {tools[1]}) conf={tools[2]}")
    else:
        print(f"  {'Tools':18} NOT FOUND  (input location unresolvable)")

    thinking = browser.find_text(ocr, "Thinking", min_conf=40)
    if thinking:
        print(f"  {'Thinking':18} at ({thinking[0]}, {thinking[1]}) conf={thinking[2]}")
    else:
        print(f"  {'Thinking':18} NOT FOUND")

    chip = browser.find_phrase(ocr, ["create", "image"], min_conf=40)
    if chip:
        print(f"  {'Create image chip':18} at ({chip[0]}, {chip[1]}) conf={chip[2]}")
    else:
        print(f"  {'Create image chip':18} NOT FOUND  (welcome screen gone — normal if chat in progress)")


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args or args[0] == "dry-run":
        dry_run()
    elif args[0] == "thinking":
        ensure_thinking_mode()
    elif args[0] == "image":
        prompt = " ".join(args[1:]) or "a single red lotus flower on black background, photorealistic"
        ensure_thinking_mode()
        create_image(prompt)
    elif args[0] == "download":
        # Download-only — assumes an image is already on screen
        save_to = args[1] if len(args) > 1 else "~/LotusAgent/Projects/downloads/"
        download_latest_image(save_to)
    elif args[0] == "full":
        # End-to-end: thinking + image + download
        prompt = " ".join(args[2:]) if len(args) > 2 else "a single red lotus flower on black background, photorealistic"
        save_to = args[1] if len(args) > 1 else "~/LotusAgent/Projects/downloads/"
        create_image_and_download(prompt, save_to)
    else:
        print("Usage:  python gemini.py [dry-run|thinking|image <prompt>|download <dir>|full <dir> <prompt>]")
