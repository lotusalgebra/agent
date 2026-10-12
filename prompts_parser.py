"""
Structured prompt extractor for LOTUS content pipelines.

Strategy (authorized by Somendra, 2026-04-19):
  1. Parse markdown into an AST using `mistune` — handles ALL standard
     markdown (headings, lists, code blocks, HTML, tables) correctly.
  2. Walk the AST to locate PROMPT CANDIDATES — chunks of content sitting
     under a heading that looks like a section ("Slide 01", "Prompt N",
     "GROK PROMPT:", etc.) or inside fenced code blocks labeled prompt.
  3. Each candidate is handed to Gemma for VALIDATION/CLEANUP — Gemma
     returns either a cleaned prompt string or "reject" for non-prompts.
  4. Caller gets the final list of validated prompt strings.

This replaces the earlier regex-only + single-Gemma-call approach that
couldn't handle the 63 KB LUXURY_REBUILD_CARS_1-4.md (40 slides).

Uses Gemma at the step where it's strong (single-prompt classification)
and a proper library at the step where deterministic behaviour matters
(structural markdown parsing).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import mistune

try:
    import frontmatter  # python-frontmatter
except ImportError:
    frontmatter = None


# ── AST walker ────────────────────────────────────────────────────────────

# Heading text patterns that indicate a section likely contains a prompt.
_PROMPT_SECTION_RE = re.compile(
    r"(slide|prompt|frame|image|post|story|reel|scene)\s*(\d+)?",
    re.IGNORECASE,
)
# Labels followed by a colon that introduce the actual prompt body.
# MULTILINE so it matches the start of any line.
# Match primary prompt labels only (not subsections like SCENE: or IMAGE:
# which appear inside a larger prompt body).
_PROMPT_LABEL_RE = re.compile(
    r"^\s*(GROK\s+PROMPT|NB2\s+PROMPT|IMAGE\s+PROMPT|PROMPT|IMAGINE)\s*[:—\-]",
    re.IGNORECASE | re.MULTILINE,
)
# Decorative section markers used in creative-brief docs, e.g.
# "━━━ SLIDE 01 — FULL-BLEED HERO [GROK] ━━━"
_BOX_SECTION_RE = re.compile(
    r"^[\s]*[━─═▬▀]{3,}\s+(.+?)\s+[━─═▬▀]{3,}\s*$",
    re.MULTILINE,
)


def _flatten_text(node) -> str:
    """Recursively extract plain text from a mistune AST node."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)

    t = node.get("type", "")
    children = node.get("children")
    raw = node.get("raw")

    if t == "text":
        return raw or ""
    if t in ("codespan", "code_span"):
        return raw or _flatten_text(children)
    if t == "linebreak":
        return "\n"
    if t == "softbreak":
        return " "

    if children is not None:
        return _flatten_text(children)
    return raw or ""


def _section_title_looks_prompty(title: str) -> bool:
    return bool(_PROMPT_SECTION_RE.search(title or ""))


# Stricter than _section_title_looks_prompty: a fenced code block should only
# be auto-treated as an image prompt when the heading above it explicitly
# names it as a prompt. Prevents PIN COMMENT / CAPTION / HASHTAGS / WORKFLOW
# code blocks (which are body text, not image prompts) from polluting the
# candidate list.
_CODEBLOCK_PROMPT_TITLE_RE = re.compile(
    r"\b(PROMPT|NB2|GROK|IMAGINE|MIDJOURNEY|STABLE\s*DIFFUSION)\b",
    re.IGNORECASE,
)


def _code_block_title_looks_prompty(title: str) -> bool:
    return bool(_CODEBLOCK_PROMPT_TITLE_RE.search(title or ""))


# ── Cinematic storyboard kinds (see CINEMATIC_FORMAT.md) ──────────────────
#
# A cinematic pack carries three kinds of block under PROMPT-bearing
# headings, and they must NOT all reach the image generator:
#
#   asset   — character / location / prop reference sheets. Rendered FIRST
#             so every later keyframe can re-describe the same subject and
#             stay visually consistent (NB2 has no memory between calls).
#   image   — the keyframe still for an act. Also every legacy SLIDE/STORY.
#   motion  — the ACT PROMPT: how the shot moves. Goes to Google Flow as
#             image→video, using that act's APPROVED keyframe as the source
#             image, so the clip matches the board the user signed off on.
#
# Motion blocks used to be discarded outright by a `"stitching" in title or
# text[:120]` filter, because the only consumer downstream was image
# generation. That filter was also a latent bug: a legitimate image prompt
# opening with the word "stitching" (leather, embroidery, couture — all
# common in this corpus) was silently dropped. Classifying by HEADING is
# both what the cinematic format needs and strictly more precise.
_MOTION_TITLE_RE = re.compile(
    r"\b(ACT\s+PROMPT|MOTION\s+PROMPT|VIDEO\s+PROMPT|STITCHING)\b",
    re.IGNORECASE,
)
_ASSET_TITLE_RE = re.compile(r"\bASSET\b", re.IGNORECASE)
_ACT_NUM_RE = re.compile(r"\bACT\s*0*(\d+)", re.IGNORECASE)


def classify_kind(title: str, text: str = "") -> str:
    """'asset' | 'image' | 'motion' for one candidate, from its heading path.

    Falls back to a body check for the literal phrase "stitching prompt"
    (not the bare word) to keep recognising legacy REEL packs that put the
    label in the block body rather than the heading.
    """
    t = title or ""
    if _MOTION_TITLE_RE.search(t):
        return "motion"
    if "stitching prompt" in (text or "")[:160].lower():
        return "motion"
    if _ASSET_TITLE_RE.search(t):
        return "asset"
    return "image"


def act_number(title: str) -> Optional[int]:
    """Act number from a heading path ('… · ACT 3 — Reveal · …' -> 3)."""
    m = _ACT_NUM_RE.search(title or "")
    return int(m.group(1)) if m else None


def _is_divider_heading(text: str) -> bool:
    """Treat heading lines that are visually dividers (mostly ASCII art —
    e.g. `# ================================================================`
    or `## ━━━━━━━`) as decoration, not section markers. Without this guard
    they overwrite the real informative heading at the same level (so
    `# TOPIC 01 …` followed by `# ====…` would lose the topic name)."""
    s = (text or "").strip()
    if not s:
        return True
    alnum = sum(1 for ch in s if ch.isalnum())
    return alnum < max(3, int(len(s) * 0.2))


# Split anchor: a story time label like `8:00 AM IST` or `11:00 AM IST`.
# mistune's AST flatten strips bold (`**`) markers + newlines, so by the
# time we see the section body it's one continuous string — pattern-split
# on the time itself rather than the bold.
_STORY_TIME_RE = re.compile(
    r"\d{1,2}:\d{2}\s*[APap][Mm]\s+(?:IST|EST|UTC|GMT|PT|PST|CT|ET)",
)


def _split_story_section(cand: dict) -> list:
    """If `cand` is a STORY-SEQUENCE-ish section with multiple time-label
    sub-stories embedded as bold paragraphs (post-flatten: just inline
    "8:00 AM IST — Title BG: ... Text: ..."), split each into its own
    candidate. Pre-filters on title containing "story" so spec lists with
    coincidental timestamps don't get sliced up."""
    title_path = (cand.get("title") or "")
    if "story" not in title_path.lower():
        return [cand]
    body = cand.get("text") or ""
    starts = [m.start() for m in _STORY_TIME_RE.finditer(body)]
    if len(starts) < 2:
        return [cand]
    out: list = []
    starts.append(len(body))
    for i in range(len(starts) - 1):
        chunk = body[starts[i]:starts[i + 1]].strip()
        if len(chunk) < 30:
            continue
        # First ~80 chars after the time label become the story's
        # human-readable label — used in the title path so each story
        # is distinguishable in the review-prompts UI.
        head = chunk[:80].split("BG:", 1)[0].split("Text:", 1)[0].strip()
        head = head.rstrip("-—–:· ").strip()
        out.append({
            "title":  f"{title_path} · {head}" if head else title_path,
            "text":   chunk,
            "source": "story_section",
        })
    return out if out else [cand]


def extract_candidates(md_text: str, include_motion: bool = False) -> list:
    """Walk the markdown AST and return a list of prompt-CANDIDATE dicts.

    Each candidate has:
      - 'title':   heading text above the content (or '' if from a code block)
      - 'text':    the raw content chunk (may include preamble, labels, etc.)
      - 'source':  where it came from ('section'|'code_block'|'label')
      - 'kind':    'asset' | 'image' | 'motion'  (see classify_kind)
      - 'act':     act number when the heading path names one, else None

    These candidates then go to Gemma for validation / cleanup.

    `include_motion` defaults to False so every existing caller keeps the
    historical behaviour of never seeing video/motion prompts — those would
    be rendered as PNGs by the image chain. The cinematic path opts in via
    `extract_storyboard()`.
    """
    # Strip YAML frontmatter if present so mistune doesn't choke
    if frontmatter is not None:
        try:
            post = frontmatter.loads(md_text)
            md_text = post.content
        except Exception:
            pass

    parser = mistune.create_markdown(renderer=None, plugins=["strikethrough", "table"])
    ast = parser(md_text)

    candidates: list = []

    # Pass 1: walk top-level blocks. When we see a heading, buffer content
    # until the next heading; emit (title, content) candidates whose title
    # looks prompt-related.
    #
    # We track the FULL heading hierarchy (level -> title) instead of just
    # the most recent heading. The composed `title_path` (e.g. "TOPIC 01 …
    # · SLIDE 01 — HOOK · 📸 NB2 PROMPT (ADVANCED)") lets downstream pick
    # out TOPIC and SLIDE numbers even though only the H3 is the immediate
    # parent of the code block.
    current_title = None
    current_buf: list = []
    heading_stack: dict[int, str] = {}
    # Pin the most recent heading that names a TOPIC NN — author-style docs
    # often place several H1 lines per topic ("# TOPIC 01 …", "# Post Date…",
    # "# Hook: …"); without pinning, the topic name gets shadowed by sibling
    # H1s at the same level and downstream loses the topic_number.
    topic_anchor: list[Optional[str]] = [None]
    _TOPIC_PATTERN = re.compile(r"\bTOPIC\s*\d+\b", re.IGNORECASE)

    def title_path() -> str:
        parts: list = []
        if topic_anchor[0]:
            parts.append(topic_anchor[0])
        for lvl in sorted(heading_stack):
            t = heading_stack[lvl]
            if t and t not in parts:
                parts.append(t)
        return " · ".join(parts)

    def flush(force: bool = False):
        if current_buf and (force or _section_title_looks_prompty(current_title or "")):
            body = _flatten_text(current_buf).strip()
            if body and len(body) > 30:   # arbitrary: skip tiny fragments
                candidates.append({
                    "title": title_path(),
                    "text": body,
                    "source": "section",
                })

    for block in ast:
        t = block.get("type", "")
        if t == "heading":
            # Flush previous section
            flush()
            level = (block.get("attrs") or {}).get("level", 1) or 1
            title = _flatten_text(block).strip()
            if _is_divider_heading(title):
                # Don't let visual dividers (`# ====…`, `## ━━━…`) overwrite
                # the last informative heading at this level.
                current_buf = []
                continue
            if _TOPIC_PATTERN.search(title):
                topic_anchor[0] = title
            current_title = title
            # Drop any deeper or sibling-level headings before recording.
            for k in [k for k in heading_stack if k >= level]:
                del heading_stack[k]
            heading_stack[level] = current_title
            current_buf = []
        elif t in ("block_code", "fenced_code", "code_block"):
            # Fenced code block — only treat as a prompt candidate when the
            # heading path above it explicitly names it (NB2 PROMPT, GROK
            # PROMPT, etc.). Otherwise the same fast-path used to ship body
            # copy (PIN COMMENT, CAPTION, HASHTAGS) to the image model.
            code_body = (block.get("raw") or _flatten_text(block)).strip()
            tpath = title_path()
            if (code_body and len(code_body) > 30
                and _code_block_title_looks_prompty(tpath)):
                candidates.append({
                    "title": tpath,
                    "text": code_body,
                    "source": "code_block",
                })
            current_buf.append(block)  # also keep in section buffer
        else:
            current_buf.append(block)
    flush()

    # Pass 2: text-level scans for PATTERNS that mistune's AST misses.

    # 2a. Box-drawing section markers: "━━━ SLIDE 01 ... ━━━" — creative-doc
    # convention. Everything between one marker and the next (or an all-box
    # line / EOF) is a prompt candidate.
    box_matches = list(_BOX_SECTION_RE.finditer(md_text))
    for i, m in enumerate(box_matches):
        title = m.group(1).strip()
        start = m.end()
        end = box_matches[i + 1].start() if i + 1 < len(box_matches) else len(md_text)
        body = md_text[start:end].strip()
        if body and len(body) > 30:
            candidates.append({
                "title": title,
                "text": body,
                "source": "box_section",
            })

    # 2b. "LABEL:" patterns (GROK PROMPT: ...) anywhere in the document.
    for m in _PROMPT_LABEL_RE.finditer(md_text):
        start = m.end()
        segment = md_text[start:start + 3000]
        stop = re.search(
            r"\n\s*(?:#{1,6}\s|[━─═]{3,}|\*{3,}|-{3,}|={3,}|\Z)",
            segment,
        )
        if stop:
            segment = segment[:stop.start()]
        segment = segment.strip()
        if len(segment) > 40:
            candidates.append({
                "title": m.group(0).strip(),
                "text": segment,
                "source": "label",
            })

    # A19 — Story-sequence split. Some files (carousel + day-content
    # packs) embed individual stories inside a single STORY SEQUENCE
    # section as `**HH:MM IST — Title**` bold-paragraphs followed by
    # bullets. The deterministic AST sees one big section; split into
    # per-story candidates so each is reviewable + renderable.
    expanded: list = []
    for c in candidates:
        if c.get("source") == "section":
            expanded.extend(_split_story_section(c))
        else:
            expanded.append(c)
    candidates = expanded

    # Tag every candidate with its kind + act number. Motion prompts (ACT
    # PROMPT / legacy GROK STITCHING PROMPT) describe how a shot MOVES —
    # they're instructions for the image→video stage and must never enter
    # the image-generation pipeline, so they're withheld unless the caller
    # explicitly asks for them.
    for c in candidates:
        c["kind"] = classify_kind(c.get("title") or "", c.get("text") or "")
        c["act"] = act_number(c.get("title") or "")
    if not include_motion:
        candidates = [c for c in candidates if c["kind"] != "motion"]

    # Smart de-dup: prefer 'label' candidates (just the prompt body) over
    # 'box_section' / 'section' candidates (whole slide with surrounding
    # metadata). Drop any non-label candidate whose content contains a
    # label candidate's prompt — the label covers it cleanly.
    labels = [c for c in candidates if c["source"] == "label"]
    others = [c for c in candidates if c["source"] != "label"]
    keep: list = list(labels)
    for c in others:
        full = re.sub(r"\s+", " ", c["text"].lower())
        absorbed = any(
            re.sub(r"\s+", " ", lbl["text"].lower())[:300] in full
            for lbl in labels
            if len(lbl["text"]) >= 60
        )
        if not absorbed:
            keep.append(c)
    # Final dedup by SHA1 of the full normalised text. Prefix-based dedup
    # collapsed legitimately-distinct prompts that shared a stock opening
    # (e.g. NB2 production-bible prompts that all begin with the same 200+
    # chars before diverging on subject). Hash on the whole body instead.
    import hashlib as _h
    seen_hashes: set = set()
    deduped: list = []
    for c in keep:
        norm = re.sub(r"\s+", " ", c["text"].lower()).strip()
        key = _h.sha1(norm.encode("utf-8", errors="replace")).hexdigest()
        if key in seen_hashes:
            continue
        seen_hashes.add(key)
        deduped.append(c)
    return deduped


# ── LLM validation per-candidate (Claude via lotus_llm, Gemma fallback) ───

# Remote children still reach the mother's Ollama: lotus_llm reads the
# OLLAMA_URL env var that `lotus-child.py` sets before importing agents.

# Fast yes/no classifier — the model only returns a boolean, we keep the
# parser's already-clean text. Cuts per-candidate time from ~30s to ~3s.
CLASSIFY_SYSTEM_PROMPT = (
    "Classify: is the text below an image-generation prompt (describes a "
    "scene / composition / visual that an image model like Midjourney, "
    "Stable Diffusion, Gemini, or NB2 could render)?\n\n"
    "A DESIGN RULES section, typography spec, list of facts, or header "
    "is NOT a prompt.\n"
    "A visual scene description, even with many details/labels, IS a prompt.\n\n"
    'Respond JSON ONLY: {"is_prompt": true} or {"is_prompt": false}'
)


def validate_with_gemma(candidate: dict, model: str = "gemma4:e4b",
                        timeout: int = 45) -> Optional[str]:
    """Fast yes/no classify. If yes, returns the candidate's text (already
    clean from the parser). If no, returns None.

    Fast-paths (skip Gemma entirely — they're almost certainly prompts and
    validation would waste time AND the local small model often returns
    empty content because it burns the budget on internal 'thinking'):
      - 'label'      source — GROK/NB2 PROMPT: labels already strong.
      - 'code_block' source — author explicitly put it in a fenced code
                              block, which is the canonical prompt
                              container in our .md conventions.
      - 'box_section' source — box-drawn ━━━ SLIDE NN ━━━ markers.
    """
    src = candidate.get("source")
    if src in ("label", "code_block", "box_section", "story_section"):
        text = (candidate.get("text") or "").strip()
        # Still refuse trivially short fragments that slipped through.
        return text if len(text) >= 50 else None

    # A13 fast-path — `section` candidates that contain an explicit prompt
    # label ("NB2 PROMPT", "GROK PROMPT", "PROMPT:") are author-authored
    # prompt blocks and shouldn't be subject to Gemma's drift. Without this,
    # Gemma was rejecting 5/10 valid stories in TODAY_10_STORIES.md just
    # because the body started with "HOOK:" before the prompt itself.
    section_text = (candidate.get("text") or "")
    section_low  = section_text.lower()
    if any(label in section_low for label in
           ("nb2 prompt", "grok prompt", "prompt:")):
        if len(section_text.strip()) >= 50:
            return section_text.strip()

    # A14 fast-path — visual-prompt keyword density. Reel-pack files
    # (ASTRA-style) put scene descriptions in section bodies under
    # `## FRAME N` / `## STORY N` headings without an NB2/GROK label.
    # These bodies are saturated with cinematography vocabulary, so a
    # density check correctly identifies them and avoids Gemma drift.
    _VISUAL_KW = (
        "cinematic", "photorealistic", "composition", "vertical 9:16",
        "9:16", "1080×", "1080x", "baked into", "lighting", "color palette",
        "colour palette", "drop shadow", "stencil", "vapor", "documentary",
        "dramatic", "ultra-", "frame composition", "hero shot",
    )
    hits = sum(1 for kw in _VISUAL_KW if kw in section_low)
    if hits >= 3 and len(section_text.strip()) >= 80:
        return section_text.strip()

    # 'section' candidates may include rule text or specs — ask the model.
    # lotus_llm routes this to Claude (task 'classify') when an API key is
    # set, else to local Gemma. `model` is kept for caller compatibility;
    # the Gemma variant now comes from `ollama.model` in config. Leave room
    # in max_tokens for the thinking pass, or the reply comes back empty.
    probe = candidate.get("text", "")[:1500]
    try:
        import lotus_llm
        content = lotus_llm.chat(
            [{"role": "user", "content": probe}],
            system=CLASSIFY_SYSTEM_PROMPT, task="classify", json_mode=True,
            timeout=timeout,
        )
        if not content:
            # Small model hit the thinking cap. Fall back to a heuristic:
            # if the text has visual-prompt keywords, keep it.
            text_lower = candidate.get("text", "").lower()
            keywords = ("composition", "cinematic", "aspect ratio", "shot",
                        "lighting", "colour", "color palette", "render",
                        "illustration", "image", "frame", "camera", "mood",
                        "style", "photograph")
            hits = sum(1 for k in keywords if k in text_lower)
            if hits >= 3:
                return candidate["text"].strip()
            return None
        data = json.loads(content)
        if data.get("is_prompt") is True:
            return candidate["text"].strip()
    except Exception as e:
        print(f"[validate_with_gemma] {e}")
        # Heuristic fallback on exception too.
        text_lower = candidate.get("text", "").lower()
        if any(k in text_lower for k in ("composition", "cinematic", "aspect ratio",
                                          "shot", "lighting", "render", "illustration")):
            return candidate["text"].strip()
    return None


# ── Top-level entry ───────────────────────────────────────────────────────

def extract_prompts(md_text: str,
                    model: str = "gemma4:e4b",
                    on_progress=None) -> list:
    """Full extraction pipeline — returns a list of cleaned prompt strings.

    `on_progress(i, n, candidate_title)` is optionally called before each
    Gemma validation so the dashboard can show live progress.
    """
    candidates = extract_candidates(md_text)
    if not candidates:
        return []

    results: list = []
    for i, cand in enumerate(candidates):
        if on_progress:
            try:
                on_progress(i + 1, len(candidates), cand.get("title", ""))
            except Exception:
                pass
        cleaned = validate_with_gemma(cand, model=model)
        if cleaned:
            results.append(cleaned)
    return results


def is_cinematic(md_text: str) -> bool:
    """True when the document uses the cinematic storyboard format — i.e. it
    has at least one motion prompt to hand to Flow. Files without one are
    ordinary image packs and take the existing path unchanged."""
    return any(c["kind"] == "motion"
               for c in extract_candidates(md_text, include_motion=True))


def extract_storyboard(md_text: str) -> dict:
    """Cinematic split of one document (see CINEMATIC_FORMAT.md).

    Returns {'assets': [...], 'keyframes': [...], 'motion': [...]} of raw
    candidate dicts — no Gemma. Render order is assets → keyframes → motion:
    assets first so keyframes can reference a rendered sheet, motion last
    because it costs Flow credits and needs an APPROVED keyframe to animate.

    Motion candidates carry 'act', which is how the video stage finds the
    keyframe to use as its source image. A motion prompt whose heading names
    no act can't be paired with a still and will fall back to text→video.
    """
    cands = extract_candidates(md_text, include_motion=True)
    out = {"assets": [], "keyframes": [], "motion": []}
    bucket = {"asset": "assets", "image": "keyframes", "motion": "motion"}
    for c in cands:
        out[bucket[c["kind"]]].append(c)
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python prompts_parser.py <file.md>")
        sys.exit(1)
    text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    cands = extract_candidates(text)
    print(f"Found {len(cands)} candidates before validation:")
    for i, c in enumerate(cands):
        print(f"  {i+1}. [{c['source']}] {c['title'][:60]}  ({len(c['text'])} chars)")
    print()
    print("Validating with Gemma…")
    for i, c in enumerate(cands):
        result = validate_with_gemma(c)
        if result:
            print(f"  [OK]  {c['title'][:40]}  → {result[:80]}…")
        else:
            print(f"  [--]  {c['title'][:40]}  rejected")
