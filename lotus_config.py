"""
Persistent user preferences for LOTUS Agent.

Stored at ~/.lotus_config.json so settings survive restarts.

Content-pipeline layout for Somendra / @techengine.lab (locked 2026-04-18):

    <projects_root>/
    └── <brand><MMDDYYYY>/              ← parent, e.g. Techengine04182026
        ├── Post1/                       ← multi-frame section (subfolder)
        │   ├── Frame1.png
        │   └── Frame2.png
        ├── Post2/
        │   └── Frame1..10.png
        ├── Reel/
        │   └── Frame1..N.png
        ├── ReelCover.png                ← single-item slot (file in parent)
        ├── Story1.png
        ├── Story2.png
        ├── Story3.png
        └── prompts.md                   ← Claude-generated NB2 prompts

Frame numbering is inferred from the filesystem (max existing + 1) per
section, so a new section starts at Frame1, and restarting mid-run picks
up where you left off.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path(os.path.expanduser("~/.lotus_config.json"))

# Slot names whose file lives DIRECTLY in the parent folder (no Frame subfile).
SINGLE_ITEM_SLOTS = ("ReelCover", "Story1", "Story2", "Story3")
# Section names that contain multiple FrameN files in a subfolder.
MULTI_FRAME_SECTIONS = ("Post1", "Post2", "Post3", "Post4", "Post5", "Reel")


def _default_config() -> dict:
    return {
        "image": {
            "projects_root": "~/LotusAgent/Projects",
            "brand": "Techengine",
            "parent_format": "%m%d%Y",      # brand + this = Techengine04182026
            "active_section": "Post1",       # where next create_image lands
            "frame_prefix": "Frame",
            "frame_ext": ".png",
            # Primary image-gen engine. Fall-back chain in _gen_image runs
            # whichever isn't primary as a backup. Valid values:
            #   "playwright" — gemini_bot.py (Playwright on Gemini web app)
            #   "human"      — gemini_bot_human.py (OS-level pyautogui+OCR)
            #   "api"        — gemini_api.py (official paid API)
            # Set via dashboard / POST /api/config/pipeline at runtime, or
            # with the LOTUS_GEN_IMAGE_PIPELINE env var at boot.
            "pipeline": "playwright",
        },
        "ollama": {
            # The Gemma variant LOTUS uses for every local-brain call
            # (reviewer, parser, archiver summary, controller patterns).
            # Changed via `lotus-model switch <name>` — don't hand-edit
            # unless you know the name is already pulled on Ollama.
            "model": "gemma4:e4b",
        },
    }


def load() -> dict:
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            base = _default_config()
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    base[k] = {**base[k], **v}
                else:
                    base[k] = v
            return base
        except (json.JSONDecodeError, OSError):
            pass
    return _default_config()


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ── Parent folder (brand + date) ───────────────────────────────────────────

def parent_folder_for(when: Optional[datetime] = None) -> str:
    """Return absolute path to the parent folder, creating it if needed.
    e.g. ~/LotusAgent/Projects/Techengine04182026"""
    cfg = load()["image"]
    when = when or datetime.now()
    root = os.path.expanduser(cfg["projects_root"])
    brand = cfg.get("brand", "")
    date_str = when.strftime(cfg["parent_format"])
    path = os.path.join(root, f"{brand}{date_str}")
    os.makedirs(path, exist_ok=True)
    return path


def parent_folder_for_pipeline(folder_name: str) -> str:
    """Return absolute path to a per-pipeline parent folder, creating it.
    `folder_name` is whatever the Pipeline stamped at construction time —
    e.g. `defence_04262026`. The folder lives directly under the projects
    root, NOT under the brand+date folder; this is the whole point —
    pipelines own their own top-level folder so frames don't overwrite."""
    cfg = load()["image"]
    root = os.path.expanduser(cfg["projects_root"])
    path = os.path.join(root, folder_name)
    os.makedirs(path, exist_ok=True)
    return path


def archive_parent_folder(suffix: str,
                          when: Optional[datetime] = None) -> Optional[tuple]:
    """Rename today's parent folder so the next run creates a fresh one.

    Example: Techengine04222026 → Techengine04222026_LuxuryFull_223825
    Matches Somendra's naming pattern (`_luxury_0920`, `_luxury_recovered`).

    Returns (old_path, new_path) on success, or None if:
      - parent folder doesn't exist (nothing to archive)
      - destination collides even after suffix dedup

    Collision handling: if `<parent>_<suffix>` exists, append `_2`, `_3`,
    ... until we find a free name. Caller should surface the final name.
    """
    cfg = load()["image"]
    when = when or datetime.now()
    root = os.path.expanduser(cfg["projects_root"])
    brand = cfg.get("brand", "")
    date_str = when.strftime(cfg["parent_format"])
    base = os.path.join(root, f"{brand}{date_str}")
    if not os.path.isdir(base):
        return None

    # Sanitise suffix — no path separators, keep a reasonable length.
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", (suffix or "run")).strip("_")
    if not safe:
        safe = "run"
    candidate = f"{base}_{safe}"
    n = 2
    while os.path.exists(candidate):
        candidate = f"{base}_{safe}_{n}"
        n += 1
        if n > 99:
            return None
    try:
        os.rename(base, candidate)
    except Exception:
        return None
    return (base, candidate)


# ── Section resolution (subfolder vs single-item) ─────────────────────────

def is_single_item(section: str) -> bool:
    """True if `section` is a single-image slot (ReelCover, Story1, ...)"""
    norm = _normalize_section(section)
    return norm in SINGLE_ITEM_SLOTS


def _normalize_section(section: str) -> str:
    """Turn 'post 1', 'POST1', 'post one', 'reel cover' → 'Post1', 'ReelCover'."""
    raw = (section or "").strip().lower().replace(" ", "")
    for canonical in MULTI_FRAME_SECTIONS + SINGLE_ITEM_SLOTS:
        if canonical.lower() == raw:
            return canonical
    # Also accept 'post 1' → 'Post1', 'story 2' → 'Story2'
    m = re.match(r"^(post|story|reel)(cover)?(\d*)$", raw)
    if m:
        word = m.group(1)
        suffix = m.group(3) or ""
        is_cover = bool(m.group(2))
        if word == "reel" and is_cover:
            return "ReelCover"
        if word == "reel" and not suffix:
            return "Reel"
        title = word.capitalize()
        return f"{title}{suffix}" if suffix else title
    return section


def section_folder(section: Optional[str] = None,
                    when: Optional[datetime] = None,
                    parent: Optional[str] = None) -> str:
    """Return the folder where Frame files for `section` live (created if needed).
    Only valid for MULTI_FRAME_SECTIONS — use single_item_path for slots.
    Defaults to the active_section from config.

    `parent` overrides the brand-based date folder — used by the
    multi-pipeline render path so frames land in `<pipeline_folder>/Post1/`
    instead of `<brand><MMDDYYYY>/Post1/`. When None, falls back to
    parent_folder_for(when) (brand+date — the old behaviour)."""
    cfg = load()["image"]
    if section is None:
        section = cfg.get("active_section") or "Post1"
    section = _normalize_section(section)
    if section in SINGLE_ITEM_SLOTS:
        raise ValueError(f"{section} is a single-item slot, not a subfolder")
    if parent is None:
        parent = parent_folder_for(when)
    path = os.path.join(parent, section)
    os.makedirs(path, exist_ok=True)
    return path


def single_item_path(slot: str, when: Optional[datetime] = None,
                     parent: Optional[str] = None) -> str:
    """Return the file path for a single-item slot like ReelCover or Story1.
    `parent` override behaves identically to `section_folder`'s — pass a
    pipeline-specific folder to keep slots from overwriting between runs."""
    slot = _normalize_section(slot)
    if slot not in SINGLE_ITEM_SLOTS:
        raise ValueError(f"{slot} is not a single-item slot")
    if parent is None:
        parent = parent_folder_for(when)
    ext = load()["image"].get("frame_ext", ".png")
    return os.path.join(parent, f"{slot}{ext}")


# ── Frame numbering ────────────────────────────────────────────────────────

def _existing_frame_numbers(folder: str, prefix: str, ext: str) -> list:
    if not os.path.isdir(folder):
        return []
    ext_l, pref_l = ext.lower(), prefix.lower()
    nums = []
    for name in os.listdir(folder):
        n = name.lower()
        if not n.startswith(pref_l) or not n.endswith(ext_l):
            continue
        mid = name[len(prefix):len(name) - len(ext)]
        if mid.isdigit():
            nums.append(int(mid))
    return sorted(nums)


def next_frame_path(section: Optional[str] = None,
                    frame_number: Optional[int] = None,
                    when: Optional[datetime] = None,
                    parent: Optional[str] = None) -> str:
    """Return the path for the NEXT frame in `section` (or explicit `frame_number`).
    For single-item slots, ignores frame_number and returns the fixed path.
    `parent` (optional) overrides the brand+date folder — see section_folder."""
    if section and _normalize_section(section) in SINGLE_ITEM_SLOTS:
        return single_item_path(section, when=when, parent=parent)
    cfg = load()["image"]
    folder = section_folder(section, when, parent=parent)
    prefix = cfg.get("frame_prefix", "Frame")
    ext = cfg.get("frame_ext", ".png")
    if frame_number is not None:
        return os.path.join(folder, f"{prefix}{frame_number}{ext}")
    nums = _existing_frame_numbers(folder, prefix, ext)
    n = (max(nums) + 1) if nums else 1
    return os.path.join(folder, f"{prefix}{n}{ext}")


# Legacy alias — existing call sites use next_child_path("image")
def next_child_path(kind: str = "image", when: Optional[datetime] = None) -> str:
    if kind == "reel":
        # Legacy: reel was a single mp4; now it's a Reel/ folder of frames.
        # Keep behaviour for callers that explicitly ask for 'reel'.
        parent = parent_folder_for(when)
        return os.path.join(parent, "reel.mp4")
    return next_frame_path(when=when)


# ── Setters ────────────────────────────────────────────────────────────────

def set_active_section(section: str) -> dict:
    """Switch where create_image saves by default."""
    cfg = load()
    cfg["image"]["active_section"] = _normalize_section(section)
    save(cfg)
    return cfg


def set_brand(brand: str) -> dict:
    cfg = load()
    cfg["image"]["brand"] = brand
    save(cfg)
    return cfg


def set_projects_root(path: str) -> dict:
    cfg = load()
    cfg["image"]["projects_root"] = os.path.expanduser(path)
    os.makedirs(cfg["image"]["projects_root"], exist_ok=True)
    save(cfg)
    return cfg


def reset_image() -> dict:
    cfg = load()
    cfg["image"] = _default_config()["image"]
    save(cfg)
    return cfg


def describe_image_settings() -> str:
    cfg = load()["image"]
    parent = os.path.basename(parent_folder_for())
    active = cfg.get("active_section", "Post1")
    next_path = next_frame_path(active)
    return (f"brand={cfg.get('brand','')}  parent={parent}  "
            f"active={active}  next={os.path.basename(next_path)}")


# ── Prompts file (Claude-generated NB2 prompts) ────────────────────────────

def prompts_md_path(when: Optional[datetime] = None) -> str:
    """Return the path to prompts.md inside today's parent folder."""
    return os.path.join(parent_folder_for(when), "prompts.md")


def _clean_prompt(raw: str) -> str:
    """Strip markdown noise from a prompt string."""
    s = raw.strip()
    # Strip surrounding bold/italic/code markers
    s = re.sub(r"^[`*_\s]+|[`*_\s]+$", "", s).strip()
    # Strip leading "**Prompt 1:**" / "Prompt 1 -" / "Image 2:" / "Frame 3:"
    # Optional ** around the whole label, optional trailing ** before body.
    s = re.sub(
        r"^\*{0,2}\s*(?:prompt|image|frame|title|description|caption)"
        r"\s*\d*\s*[:\-–—]?\s*\*{0,2}\s*",
        "", s, flags=re.IGNORECASE,
    )
    # Collapse internal whitespace
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def parse_prompts_md(text: str) -> list:
    """Parse markdown text into a list of prompt strings.

    Tries multiple strategies in order, returns whichever gives the most
    prompts. Handles: numbered lists (multi-line), headings-as-titles,
    bullet lists, fenced code blocks, and blank-line-separated paragraphs.
    """
    strategies = []

    # 1. Numbered list (1. / 1)) with multi-line content
    numbered = re.findall(
        r"^\s*\d+[\.\)]\s+(.+?)(?=^\s*\d+[\.\)]|\Z)",
        text, flags=re.DOTALL | re.MULTILINE,
    )
    strategies.append([_clean_prompt(s) for s in numbered if s.strip()])

    # 2. Markdown headings (## Prompt 1, ### Frame 2, etc.) — use the
    #    content UNDER each heading as the prompt
    heading_split = re.split(r"^#{1,6}\s+.*$", text, flags=re.MULTILINE)
    # First chunk is text before any heading; drop if short
    heading_parts = [p.strip() for p in heading_split if p.strip() and len(p.strip()) > 10]
    strategies.append([_clean_prompt(s) for s in heading_parts])

    # 3. Bulleted list (- / *) with multi-line content
    bulleted = re.findall(
        r"^\s*[-*]\s+(.+?)(?=^\s*[-*]|\Z)",
        text, flags=re.DOTALL | re.MULTILINE,
    )
    strategies.append([_clean_prompt(s) for s in bulleted if s.strip()])

    # 4. Fenced code blocks (```prompt```)
    fenced = re.findall(r"```(?:\w*\n)?(.*?)```", text, flags=re.DOTALL)
    strategies.append([_clean_prompt(s) for s in fenced if s.strip()])

    # 5. Blank-line-separated paragraphs, skipping headings/horizontal-rules
    chunks = re.split(r"\n\s*\n", text)
    paras = []
    for c in chunks:
        c = c.strip()
        if not c: continue
        if c.startswith("#"): continue            # markdown heading
        if re.match(r"^[-=*_]{3,}$", c): continue  # hr
        paras.append(_clean_prompt(c))
    strategies.append(paras)

    # Pick the strategy that gave the most prompts, preferring earlier
    # (more structural) ones when tied
    best = max(strategies, key=lambda s: (len(s), -strategies.index(s)))
    # Trim empty strings
    return [p for p in best if p and len(p) > 5]


def read_prompts() -> list:
    """Parse the default prompts MD file into individual prompt strings."""
    p = prompts_md_path()
    if not os.path.exists(p):
        return []
    text = Path(p).read_text(encoding="utf-8", errors="replace")
    return parse_prompts_md(text)
