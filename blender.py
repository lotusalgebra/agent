"""Blender connector — drives a local Blender 4.x install headlessly to
turn a still PNG into an animated MP4 (Phase 1a) or render an artist-built
.blend template with text/image placeholder substitution (Phase 1b).

Architecture mirrors photoshop.py / grok_video.py: a templated Blender
Python script (`blender_jobs.py.tmpl`) is materialised per call with
token replacements, then run via:

    /Applications/Blender.app/Contents/MacOS/Blender --background --python <script>

Blender renders MP4 directly (built-in FFmpeg encoder) — no external
ffmpeg call from Lotus, which keeps us aligned with the user's
"FFmpeg → Premiere Pro" preference (Premiere is for cinematic-join /
final cut; Blender's own export is for unit-level reels).

Public entries:
    image_to_reel(image_path, output_mp4, *, motion='ken_burns',
                   duration=10, fps=30, format='reel'|'youtube'|(W,H))

    render_template(blend_template, output_mp4, *, replacements=None,
                     fps=None, format=None)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Union

_BLENDER_BIN_CANDIDATES = [
    # Blender 4.2 LTS — has FFmpeg, the only build that can produce MP4
    # without an external encoder. Prefer this for image-to-reel work.
    "/Applications/Blender 4.2 LTS.app/Contents/MacOS/Blender",
    "/Applications/Blender 4.2.app/Contents/MacOS/Blender",
    "/Applications/Blender 2.app/Contents/MacOS/Blender",   # macOS-renamed dup
    "/Applications/Blender LTS.app/Contents/MacOS/Blender",
    # Bare /Applications/Blender.app falls back last — could be 5.x
    # (no FFmpeg) so we'd fail on render. Caller can override
    # `LOTUS_BLENDER_BIN` to force a specific binary.
    "/Applications/Blender.app/Contents/MacOS/Blender",
]


def _resolve_blender_bin() -> str:
    """Pick the first Blender binary that exists. `LOTUS_BLENDER_BIN`
    env var overrides for one-off / dev runs."""
    env = os.environ.get("LOTUS_BLENDER_BIN", "").strip()
    if env and os.path.isfile(env):
        return env
    for p in _BLENDER_BIN_CANDIDATES:
        if os.path.isfile(p):
            return p
    return _BLENDER_BIN_CANDIDATES[0]   # error message will surface later


BLENDER_BIN = _resolve_blender_bin()
TEMPLATE = Path(__file__).parent / "blender_jobs.py.tmpl"
DEFAULT_TIMEOUT_S = 600   # 10 min per render


_FORMATS = {
    "reel":    (1080, 1920),   # IG / YT Shorts vertical
    "story":   (1080, 1920),
    "youtube": (1920, 1080),   # YouTube horizontal
    "yt":      (1920, 1080),
    "square":  (1080, 1080),
    "post":    (1080, 1350),   # 4:5 carousel
}


def _resolve_format(fmt) -> tuple:
    if isinstance(fmt, (tuple, list)) and len(fmt) == 2:
        return int(fmt[0]), int(fmt[1])
    if isinstance(fmt, str) and fmt.lower() in _FORMATS:
        return _FORMATS[fmt.lower()]
    return 1080, 1920   # default reel


def _materialise_script(*, mode: str,
                         image: str = "",
                         output_path: str = "",
                         duration: float = 10.0,
                         fps: int = 30,
                         res_x: int = 1080,
                         res_y: int = 1920,
                         motion: str = "ken_burns",
                         template_blend: str = "",
                         replacements: Optional[dict] = None) -> str:
    """Replace tokens in the JSX-equivalent .py.tmpl and return the path
    to a temp file. Caller is responsible for cleanup."""
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"missing template: {TEMPLATE}")
    body = TEMPLATE.read_text()
    body = (body
            .replace("<<MODE>>",            mode)
            .replace("<<IMAGE>>",           image)
            .replace("<<OUTPUT_PATH>>",     output_path)
            .replace("<<DURATION>>",        f"{float(duration):.4f}")
            .replace("<<FPS>>",             str(int(fps)))
            .replace("<<RES_X>>",           str(int(res_x)))
            .replace("<<RES_Y>>",           str(int(res_y)))
            .replace("<<MOTION>>",          motion)
            .replace("<<TEMPLATE_BLEND>>",  template_blend)
            .replace("<<REPLACEMENTS_JSON>>",
                     json.dumps(replacements or {})))
    fh = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    fh.write(body)
    fh.close()
    return fh.name


def _resolve_blender_output(out_target: str) -> Optional[str]:
    """Blender appends the frame range to `scene.render.filepath` for FFMPEG
    output (e.g. `output_0001-0300.mp4`). After render, find the matching
    file and rename to the user's clean target.

    Returns the final path on success, None otherwise."""
    out_dir = os.path.dirname(out_target) or "."
    base = os.path.basename(out_target)
    stem = base[:-4] if base.lower().endswith(".mp4") else base
    if os.path.isfile(out_target):
        return out_target  # Blender wrote directly to the target
    # Look for `<stem>*.mp4` in the dir.
    candidates = sorted(Path(out_dir).glob(f"{stem}*.mp4"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        latest = str(candidates[0])
        if latest != out_target:
            try:
                shutil.move(latest, out_target)
                return out_target
            except Exception:
                return latest
        return latest
    return None


def _run_blender(script_path: str, *, timeout: int,
                 verbose: bool = True) -> dict:
    """Subprocess Blender headless. Returns {ok, stdout, stderr}."""
    if not os.path.isfile(BLENDER_BIN):
        return {"ok": False, "error": f"Blender not at {BLENDER_BIN}"}
    cmd = [BLENDER_BIN, "--background", "--python", script_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "error": f"Blender did not finish within {timeout}s"}
    if proc.returncode != 0:
        return {"ok": False,
                "error": (proc.stderr.strip() or
                          "Blender exited non-zero"),
                "stdout": proc.stdout, "stderr": proc.stderr}
    if verbose:
        # Show only the [blender] tagged lines for clean logs.
        for line in (proc.stdout or "").splitlines():
            if line.startswith("[blender]"):
                print(line, flush=True)
    return {"ok": True, "stdout": proc.stdout, "stderr": proc.stderr}


# ── Public entries ────────────────────────────────────────────────────────

def image_to_reel(image_path: str, output_mp4: str, *,
                  motion: str = "ken_burns",
                  duration: float = 10.0,
                  fps: int = 30,
                  format: Union[str, tuple] = "reel",
                  timeout: int = DEFAULT_TIMEOUT_S,
                  verbose: bool = True) -> Optional[str]:
    """Animate a single still PNG into an MP4 with a camera move.

    `motion`: ken_burns / zoom_in / zoom_out / pan_left / pan_right / static
    `format`: 'reel' (1080×1920) / 'youtube' (1920×1080) / 'square' /
              'post' / (W, H) tuple.

    Returns the absolute path of the produced MP4 on success, or None.
    """
    image_path = os.path.abspath(os.path.expanduser(image_path))
    output_mp4 = os.path.abspath(os.path.expanduser(output_mp4))
    if not os.path.isfile(image_path):
        if verbose: print(f"[blender] image not found: {image_path}",
                          flush=True)
        return None
    Path(output_mp4).parent.mkdir(parents=True, exist_ok=True)
    res_x, res_y = _resolve_format(format)

    script_path = _materialise_script(
        mode="image", image=image_path, output_path=output_mp4,
        duration=duration, fps=fps, res_x=res_x, res_y=res_y, motion=motion)
    try:
        result = _run_blender(script_path, timeout=timeout, verbose=verbose)
    finally:
        try: os.unlink(script_path)
        except Exception: pass

    if not result.get("ok"):
        if verbose: print(f"[blender] failed: {result.get('error')}",
                          flush=True)
        return None

    final = _resolve_blender_output(output_mp4)
    if not final:
        if verbose:
            print(f"[blender] render reported success but no MP4 found at "
                  f"{output_mp4} or stem variants", flush=True)
        return None
    if verbose:
        size_kb = os.path.getsize(final) // 1024 if os.path.isfile(final) else 0
        print(f"[blender] reel ready → {final} ({size_kb} KB)", flush=True)
    return final


def render_template(blend_template: str, output_mp4: str, *,
                    replacements: Optional[dict] = None,
                    fps: int = 0,
                    format: Union[str, tuple, None] = None,
                    timeout: int = DEFAULT_TIMEOUT_S,
                    verbose: bool = True) -> Optional[str]:
    """Open an artist-authored .blend file, swap text/image placeholders
    named `lotus_text_<slot>` and `lotus_image_<slot>` from `replacements`,
    render the existing animation timeline to `output_mp4`.

    `replacements` example:
        {
          "headline":   "DEFENCE BUDGET 2026",
          "subheadline": "₹6.81 LAKH CR",
          "frame":      "/path/to/Post1/Frame1.png",
        }

    Override `fps` / `format` only if you want to deviate from the
    template's own settings — pass 0 / None to keep them.
    """
    blend_template = os.path.abspath(os.path.expanduser(blend_template))
    output_mp4 = os.path.abspath(os.path.expanduser(output_mp4))
    if not os.path.isfile(blend_template):
        if verbose: print(f"[blender] template not found: {blend_template}",
                          flush=True)
        return None
    Path(output_mp4).parent.mkdir(parents=True, exist_ok=True)

    if format:
        res_x, res_y = _resolve_format(format)
    else:
        res_x = res_y = 0   # template's own setting wins

    # Materialise replacement paths to absolute (Blender CWD = template dir
    # which may not match callers' relative paths).
    abs_repl: dict = {}
    for k, v in (replacements or {}).items():
        if isinstance(v, str) and (v.endswith(".png") or v.endswith(".jpg")
                                    or v.endswith(".jpeg")):
            abs_repl[k] = os.path.abspath(os.path.expanduser(v))
        else:
            abs_repl[k] = v

    script_path = _materialise_script(
        mode="template", output_path=output_mp4,
        res_x=res_x, res_y=res_y, fps=fps,
        template_blend=blend_template, replacements=abs_repl)
    try:
        result = _run_blender(script_path, timeout=timeout, verbose=verbose)
    finally:
        try: os.unlink(script_path)
        except Exception: pass

    if not result.get("ok"):
        if verbose: print(f"[blender] template render failed: "
                          f"{result.get('error')}", flush=True)
        return None
    final = _resolve_blender_output(output_mp4)
    if final and verbose:
        size_kb = os.path.getsize(final) // 1024
        print(f"[blender] template render → {final} ({size_kb} KB)",
              flush=True)
    return final


# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python blender.py image  <png>  <out.mp4>  "
              "[--motion ken_burns] [--duration 10] [--fps 30] [--format reel]")
        print("  python blender.py template <blend> <out.mp4> "
              "[--repl JSON] [--format reel] [--fps 30]")
        sys.exit(1)
    sub = sys.argv[1]
    args = sys.argv[2:]
    if sub == "image" and len(args) >= 2:
        image, out = args[0], args[1]
        kw = {}
        i = 2
        while i < len(args):
            if args[i] == "--motion":   kw["motion"]   = args[i+1]; i += 2
            elif args[i] == "--duration": kw["duration"] = float(args[i+1]); i += 2
            elif args[i] == "--fps":      kw["fps"]    = int(args[i+1]);   i += 2
            elif args[i] == "--format":   kw["format"] = args[i+1];        i += 2
            else: i += 1
        path = image_to_reel(image, out, **kw)
        print(json.dumps({"ok": bool(path), "out": path}, indent=2))
    elif sub == "template" and len(args) >= 2:
        blend, out = args[0], args[1]
        kw = {}
        i = 2
        while i < len(args):
            if args[i] == "--repl":   kw["replacements"] = json.loads(args[i+1]); i += 2
            elif args[i] == "--format": kw["format"] = args[i+1]; i += 2
            elif args[i] == "--fps":    kw["fps"] = int(args[i+1]); i += 2
            else: i += 1
        path = render_template(blend, out, **kw)
        print(json.dumps({"ok": bool(path), "out": path}, indent=2))
    else:
        print("invalid subcommand or missing args"); sys.exit(1)
