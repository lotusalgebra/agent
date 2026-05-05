"""Photoshop integration — drive the user's local Adobe Photoshop 2026
via osascript + JSX. Two operations exposed:

  - `remove_texts(image, texts)` — Tesseract OCRs the image, finds each
    requested string's bounding box, and Content-Aware-Fills those regions.
  - `place_watermark(image, watermark_path, position, margin, scale)` —
    pastes a watermark PNG as a new layer, scales it to a fraction of the
    document width, positions at one of the named anchors, flattens.

Combined entry: `process_frame(image, remove_texts, watermark_path,
position, margin, scale, output_path)` runs both in one Photoshop session.

Why this architecture (matches Lotus's existing pattern):
  - AppleScript launcher (`osascript`) drives the user's REAL Photoshop —
    not a headless or remote API. Same tier as `browser.py` driving Chrome.
  - JSX has full Photoshop DOM access (selection, fill, layers, save) so
    we don't need any Adobe Cloud API or sign-in flow. Works offline.
  - Templated JSX (`photoshop_jobs.jsx`) is regenerated per job by token
    replacement so we don't need IPC between Python and Photoshop.

Photoshop must be installed; it is auto-launched by osascript if closed.
The user's signed-in Adobe state is irrelevant — Content-Aware Fill is a
local feature.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

PS_APP_NAME = "Adobe Photoshop 2026"
JSX_TEMPLATE = Path(__file__).parent / "photoshop_jobs.jsx"
DEFAULT_TIMEOUT_S = 240


# ── OCR helper ────────────────────────────────────────────────────────────

def _ocr_find_regions(image_path: str, texts: list[str],
                      pad: int = 8) -> list[tuple]:
    """OCR `image_path` and return [(x,y,w,h), ...] — bounding boxes for
    every word that case-insensitively matches one of `texts`. Boxes are
    expanded by `pad` pixels on each side for cleaner Content-Aware Fill
    (the fill needs surrounding context to inpaint correctly).

    Multi-word targets ("no 2") match if the words appear adjacent in OCR
    order with the same line number. Single-word targets are matched per
    word."""
    import pytesseract
    from PIL import Image
    img = Image.open(image_path)
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    # Normalise targets — split each into its component words for matching.
    targets: list[list[str]] = [
        [w.lower() for w in t.split() if w]
        for t in (texts or [])
        if t and t.strip()
    ]
    if not targets:
        return []

    n = len(data["text"])
    regions: list[tuple] = []

    for i in range(n):
        word_i = (data["text"][i] or "").strip().lower()
        if not word_i:
            continue
        line_i = (data["block_num"][i], data["par_num"][i],
                  data["line_num"][i])
        for tgt in targets:
            # First word: substring match so "techengine" matches the OCR
            # word "@techengine.lab" (Tesseract often glues punctuation
            # into a single token).
            if tgt[0] not in word_i:
                continue
            # Try to extend to match all words of `tgt` on the same line.
            # Subsequent words also use substring match for the same reason.
            matched_indices = [i]
            ok = True
            for k in range(1, len(tgt)):
                j = i + k
                if j >= n:
                    ok = False; break
                line_j = (data["block_num"][j], data["par_num"][j],
                          data["line_num"][j])
                if line_j != line_i:
                    ok = False; break
                next_word = (data["text"][j] or "").strip().lower()
                if tgt[k] not in next_word:
                    ok = False; break
                matched_indices.append(j)
            if not ok:
                continue
            xs = [data["left"][k] for k in matched_indices]
            ys = [data["top"][k] for k in matched_indices]
            ws = [data["left"][k] + data["width"][k] for k in matched_indices]
            hs = [data["top"][k]  + data["height"][k] for k in matched_indices]
            x = max(0, min(xs) - pad)
            y = max(0, min(ys) - pad)
            w = (max(ws) - min(xs)) + 2 * pad
            h = (max(hs) - min(ys)) + 2 * pad
            regions.append((x, y, w, h))

    return regions


# ── Position resolver ─────────────────────────────────────────────────────

def _resolve_position(position: str, doc_w: int, doc_h: int,
                       wm_w: int, wm_h: int, margin: int) -> tuple:
    """Return (x, y) top-left pixel for the watermark layer given a named
    anchor. Falls back to bottom-right on unknown names."""
    p = (position or "").lower().replace("_", "-")
    if p == "bottom-right":
        return (doc_w - wm_w - margin, doc_h - wm_h - margin)
    if p == "bottom-left":
        return (margin, doc_h - wm_h - margin)
    if p == "bottom-center":
        return ((doc_w - wm_w) // 2, doc_h - wm_h - margin)
    if p == "top-right":
        return (doc_w - wm_w - margin, margin)
    if p == "top-left":
        return (margin, margin)
    if p == "top-center":
        return ((doc_w - wm_w) // 2, margin)
    if p == "center":
        return ((doc_w - wm_w) // 2, (doc_h - wm_h) // 2)
    return (doc_w - wm_w - margin, doc_h - wm_h - margin)


# ── Public entry ──────────────────────────────────────────────────────────

def process_frame(image_path: str, *,
                  remove_texts: Optional[list] = None,
                  watermark_path: Optional[str] = None,
                  position: str = "bottom-right",
                  margin: int = 40,
                  scale: float = 0.15,
                  output_path: Optional[str] = None,
                  timeout: int = DEFAULT_TIMEOUT_S) -> dict:
    """Run a single Photoshop job. In-place by default (output overwrites
    input). Returns:

        {
          "ok":             bool,
          "output_path":    str,
          "regions_filled": int,    # how many OCR matches got CAF'd
          "watermark":      bool,   # whether a watermark was placed
          "error":          str,    # only present on failure
        }
    """
    image_path = os.path.abspath(os.path.expanduser(image_path))
    out_path = os.path.abspath(os.path.expanduser(output_path or image_path))
    if not os.path.isfile(image_path):
        return {"ok": False, "error": f"image not found: {image_path}"}

    # 1. OCR for removal regions (fast — ~200 ms for a 1080×1350 image).
    regions: list[tuple] = []
    if remove_texts:
        try:
            regions = _ocr_find_regions(image_path, remove_texts)
        except Exception as e:
            return {"ok": False, "error": f"OCR failed: {e}"}

    # 2. Compute watermark target position from doc dims (PIL-side, before
    # we hand off to Photoshop — keeps the JSX simple).
    wm_x, wm_y, wm_active = 0, 0, False
    if watermark_path:
        wm_path = os.path.abspath(os.path.expanduser(watermark_path))
        if not os.path.isfile(wm_path):
            return {"ok": False,
                    "error": f"watermark not found: {wm_path}"}
        try:
            from PIL import Image
            with Image.open(image_path) as img:
                doc_w, doc_h = img.size
            with Image.open(wm_path) as wm:
                wm_src_w, wm_src_h = wm.size
            target_w = max(1, int(doc_w * scale))
            scale_factor = target_w / wm_src_w
            target_h = max(1, int(wm_src_h * scale_factor))
            wm_x, wm_y = _resolve_position(
                position, doc_w, doc_h, target_w, target_h, margin)
            wm_active = True
            watermark_path = wm_path
        except Exception as e:
            return {"ok": False, "error": f"watermark prep failed: {e}"}

    # 3. Render JSX from template.
    try:
        tpl = JSX_TEMPLATE.read_text()
    except FileNotFoundError:
        return {"ok": False,
                "error": f"JSX template missing at {JSX_TEMPLATE}"}
    jsx = (tpl
           .replace("<<IMAGE_PATH>>",     image_path.replace("\\", "/"))
           .replace("<<OUTPUT_PATH>>",    out_path.replace("\\", "/"))
           .replace("<<REGIONS>>",        json.dumps([list(r) for r in regions]))
           .replace("<<WATERMARK_PATH>>", (watermark_path or "").replace("\\", "/") if wm_active else "")
           .replace("<<WM_X>>",           str(int(wm_x)))
           .replace("<<WM_Y>>",           str(int(wm_y)))
           .replace("<<WM_SCALE>>",       f"{float(scale):.4f}"))

    # 4. Drop JSX to a temp file and execute via osascript. We pass the
    # path (rather than the body) because long inline scripts run into
    # AppleScript's quoting limits.
    with tempfile.NamedTemporaryFile("w", suffix=".jsx", delete=False) as f:
        f.write(jsx)
        jsx_path = f.name

    try:
        cmd = [
            "osascript", "-e",
            f'tell application "{PS_APP_NAME}" to do javascript file "{jsx_path}"',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
        if proc.returncode != 0:
            return {"ok": False,
                    "error": (proc.stderr.strip() or
                              "osascript exited non-zero")}
        out = (proc.stdout or "").strip()
        if out.startswith("ERR:"):
            return {"ok": False, "error": out[4:]}
        return {
            "ok": True,
            "output_path": out_path,
            "regions_filled": len(regions),
            "watermark": wm_active,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Photoshop did not respond within {timeout}s"}
    finally:
        try: os.unlink(jsx_path)
        except Exception: pass


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python photoshop.py <image> [--remove TEXT [TEXT...]] "
              "[--watermark PNG] [--position bottom-right] "
              "[--margin 40] [--scale 0.15] [--out PATH]")
        sys.exit(1)
    args = sys.argv[1:]
    image = args.pop(0)
    remove_texts: list = []
    wm = None
    pos = "bottom-right"
    margin = 40
    scale = 0.15
    out = None
    while args:
        flag = args.pop(0)
        if flag == "--remove":
            while args and not args[0].startswith("--"):
                remove_texts.append(args.pop(0))
        elif flag == "--watermark":
            wm = args.pop(0)
        elif flag == "--position":
            pos = args.pop(0)
        elif flag == "--margin":
            margin = int(args.pop(0))
        elif flag == "--scale":
            scale = float(args.pop(0))
        elif flag == "--out":
            out = args.pop(0)
    print(json.dumps(process_frame(
        image, remove_texts=remove_texts or None,
        watermark_path=wm, position=pos, margin=margin, scale=scale,
        output_path=out,
    ), indent=2))
