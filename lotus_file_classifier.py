"""Quick classifier for uploaded files.

Used by `/api/upload` to surface a verdict in the dashboard:
"does this file look like an image-prompt list, or is it something else?"

The verdict is *advisory* — the user can always override and load the file
anyway. Goal is to catch the obvious mistake (uploading the wrong file) and
let the user proceed without ceremony when the answer is clear.

Two-tier strategy:
  1. **Heuristic-first** — pattern-match the file head for known markers
     (## SLIDE / ## POST / ## TOPIC / fenced code blocks / "NB2 PROMPT"
     labels). When these fire, return verdict immediately — no Gemma call,
     so it's instant and reliable for the schemas this user actually ships.
  2. **Gemma fallback** — only when the heuristic is silent. One-shot yes/no
     classification with a short timeout. Falls back to 'uncertain' if
     Ollama is unreachable, the model returns garbage, or anything else
     goes wrong.

`classify_file(path)` returns:
  {
    'verdict':    'yes' | 'no' | 'uncertain',
    'confidence': float in [0.0, 1.0],
    'reason':     short human string,
    'source':     'heuristic' | 'gemma' | 'fallback',
  }
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = "gemma4:e4b"
MAX_HEAD_BYTES = 8192   # 8 KB head — large enough that medical-style files
                        # with a verbose metadata preamble still surface their
                        # first SLIDE/POST marker in the heuristic window.

# Strong-YES markers — if any of these appear in the head we're confident.
_YES_MARKERS = [
    re.compile(r"^##\s+SLIDE\s+\d+",         re.MULTILINE | re.IGNORECASE),
    re.compile(r"^##?\s+POST\s+\d+",         re.MULTILINE | re.IGNORECASE),
    re.compile(r"^##?\s+TOPIC\s+\d+",        re.MULTILINE | re.IGNORECASE),
    re.compile(r"^##?\s+FRAME\s+\d+",        re.MULTILINE | re.IGNORECASE),  # A14 — ASTRA-style reels
    re.compile(r"^##?\s+STORY\s+\d+",        re.MULTILINE | re.IGNORECASE),  # story-pack files
    re.compile(r"\bNB2\s+PROMPT\b",          re.IGNORECASE),
    re.compile(r"\bGROK\s+PROMPT\b",         re.IGNORECASE),
    re.compile(r"^PROMPT\s*\d*\s*:",         re.MULTILINE | re.IGNORECASE),
    # Production-pack metadata that always sits above slide markers in this
    # user's Claude-generated MDs. Each phrase is specific enough that a
    # random doc shouldn't trip them.
    re.compile(r"\bIG\s+carousel\b",         re.IGNORECASE),
    re.compile(r"\bInstagram\s+carousel\b",  re.IGNORECASE),
    re.compile(r"\bProduction\s+Bible\b",    re.IGNORECASE),
    re.compile(r"\b\d+\s+slides?\s+each\b",  re.IGNORECASE),
    # A14 — reel-pack signatures
    re.compile(r"\bREEL\s+PACKAGE\b",        re.IGNORECASE),
    re.compile(r"\bREEL\s+COMPLETE\b",       re.IGNORECASE),
    re.compile(r"\bBAKED\s+INTO\b",          re.IGNORECASE),
    re.compile(r"\bVertical\s+9\s*:\s*16\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+Frames?\b",         re.IGNORECASE),
]

# Strong-NO markers — clearly not a markdown prompt list.
_NO_MARKERS = [
    re.compile(r"^\s*<\?xml\b",           re.IGNORECASE),
    re.compile(r"^\s*<!doctype\s+html",   re.IGNORECASE),
    re.compile(r"^\s*<html\b",            re.IGNORECASE),
    # Python/JS source code at top
    re.compile(r"^\s*(import\s+\w|from\s+\w+\s+import|def\s+\w+\s*\()", re.MULTILINE),
    re.compile(r"^\s*(function\s+\w+\s*\(|class\s+\w+\s*\{|const\s+\w+\s*=)", re.MULTILINE),
]


def _read_head(path: str, max_bytes: int = MAX_HEAD_BYTES) -> str:
    with open(path, "rb") as f:
        data = f.read(max_bytes)
    return data.decode("utf-8", errors="replace")


def _heuristic_verdict(head: str) -> Optional[dict]:
    """Pattern-match the file head. Returns a verdict dict on a clear hit,
    or None when no pattern fires (in which case the caller falls through
    to Gemma)."""
    # Strong NO wins over strong YES — if the head looks like code or HTML
    # we treat it as NO regardless of stray prompt-y words further down.
    for rx in _NO_MARKERS:
        if rx.search(head):
            return {
                "verdict":    "no",
                "confidence": 0.95,
                "reason":     "head looks like code or markup, not markdown",
                "source":     "heuristic",
            }
    yes_hits = [rx for rx in _YES_MARKERS if rx.search(head)]
    fence_count = head.count("```")
    if yes_hits and fence_count >= 2:
        return {
            "verdict":    "yes",
            "confidence": 0.95,
            "reason":     f"matched prompt markers + {fence_count // 2} fenced block(s)",
            "source":     "heuristic",
        }
    if yes_hits:
        return {
            "verdict":    "yes",
            "confidence": 0.80,
            "reason":     "matched prompt markers in header",
            "source":     "heuristic",
        }
    return None


def _gemma_verdict(head: str, model: str, timeout: int) -> dict:
    """Tight one-shot Gemma call. JSON-only response. Bails to 'uncertain'
    on any error — Ollama down, parse failure, slow response, etc."""
    system = (
        "Decide whether the text below is a list of image-generation prompts "
        "(scene descriptions a model like Midjourney / Gemini / NB2 could render).\n"
        "Slide-by-slide markdown carousels with fenced code blocks containing "
        "scene descriptions = YES. Plain prose, code, config, or shopping lists = NO.\n\n"
        'Reply JSON ONLY: {"is_prompt_file": true|false, "confidence": 0.0-1.0, '
        '"reason": "<short>"}'
    )
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": head},
                ],
                "options": {"temperature": 0.0, "num_predict": 80},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        content = (r.json().get("message") or {}).get("content", "").strip()
        parsed = json.loads(content)
        is_prompt = bool(parsed.get("is_prompt_file"))
        conf = float(parsed.get("confidence", 0.5))
        reason = str(parsed.get("reason", ""))[:140] or (
            "Gemma classification" if is_prompt else "Gemma rejected as non-prompt"
        )
        return {
            "verdict":    "yes" if is_prompt else "no",
            "confidence": max(0.0, min(1.0, conf)),
            "reason":     reason,
            "source":     "gemma",
        }
    except Exception as e:
        return {
            "verdict":    "uncertain",
            "confidence": 0.0,
            "reason":     f"classifier unavailable ({type(e).__name__})",
            "source":     "fallback",
        }


def classify_file(path: str,
                  model: str = DEFAULT_MODEL,
                  timeout: int = 8) -> dict:
    """Top-level entry. See module docstring for the return shape."""
    if not path or not os.path.isfile(path):
        return {
            "verdict":    "uncertain",
            "confidence": 0.0,
            "reason":     "file not found",
            "source":     "fallback",
        }
    try:
        head = _read_head(path)
    except Exception as e:
        return {
            "verdict":    "uncertain",
            "confidence": 0.0,
            "reason":     f"could not read file ({type(e).__name__})",
            "source":     "fallback",
        }
    if not head.strip():
        return {
            "verdict":    "no",
            "confidence": 0.9,
            "reason":     "file is empty",
            "source":     "heuristic",
        }
    h = _heuristic_verdict(head)
    if h is not None:
        return h
    return _gemma_verdict(head, model=model, timeout=timeout)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python lotus_file_classifier.py <path>")
        sys.exit(1)
    print(json.dumps(classify_file(sys.argv[1]), indent=2))
