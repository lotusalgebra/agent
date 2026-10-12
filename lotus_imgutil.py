"""Shared image-resolution helpers — the single source of truth for the
question "is this saved file a real full-resolution asset, or a downscaled
preview/thumbnail?"

Used by the render save paths (`gemini_bot`, `flow_bot`) and by the pipeline
resume/reuse + regenerate logic in `agent_phase1`. Centralising it stops the
two halves from disagreeing about what "full-res" means — the disagreement is
exactly what let a 1024px preview get saved as "done" and, worse, let the
resume path destroy-and-regenerate good frames.
"""
from __future__ import annotations

# Full-resolution NB2 output always has a long edge >= 2048 px (observed across
# 1800+ real saves: 1856x2304, 1536x2752, 2816x1536, 2048x2048, 3200x1758).
# Gemini / Flow on-screen previews and inline <img> thumbnails top out around
# 1024-1376 px. 1600 sits in the gap with margin, cleanly separating a real
# asset from a preview regardless of aspect ratio.
MIN_FULLRES_LONG_EDGE = 1600


def long_edge(path) -> int:
    """Return max(width, height) in pixels for an image file, or 0 if the file
    is missing / unreadable / not an image.

    Format-agnostic on purpose: `gemini_bot` saves PNG, `flow_bot` saves JPEG
    (even when the target name ends in .png). Pillow handles every format the
    engines can emit; the dependency-free PNG/JPEG header parse below is a
    fallback in case Pillow is ever unavailable in a packaged build."""
    # Primary: Pillow (bundled via requirements.txt). Reads the header only.
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        if w and h:
            return max(int(w), int(h))
    except Exception:
        pass
    # Fallback: parse the container header directly (PNG + baseline JPEG).
    try:
        with open(path, "rb") as fh:
            data = fh.read(65536)
    except Exception:
        return 0
    # PNG: 8-byte signature, then IHDR width/height at bytes 16..24 (big-endian).
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return max(w, h)
    # JPEG: walk the marker segments to the Start-Of-Frame, which carries the
    # frame's height (offset +5) and width (offset +7).
    if data[:2] == b"\xff\xd8":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            # SOF0..SOF15, excluding the non-SOF markers C4 (DHT), C8 (JPG),
            # CC (DAC).
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return max(w, h)
            seg = int.from_bytes(data[i + 2:i + 4], "big")
            if seg <= 0:
                break
            i += 2 + seg
    return 0


def is_fullres(path) -> bool:
    """True if the file at `path` is a real full-resolution asset (long edge
    >= MIN_FULLRES_LONG_EDGE); False for previews/thumbnails/missing files."""
    return long_edge(path) >= MIN_FULLRES_LONG_EDGE
