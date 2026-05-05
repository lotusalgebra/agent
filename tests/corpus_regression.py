"""Deterministic regression baseline for prompts_parser.

Runs `extract_candidates` (no Gemma) on every *.md in tests/corpus/ and
either:
  - writes baseline expected_<name>.json  (when called with --bless), or
  - diffs current output against expected_<name>.json (default).

Usage:
  venv/bin/python tests/corpus_regression.py            # diff vs baseline
  venv/bin/python tests/corpus_regression.py --bless    # rewrite baselines
  venv/bin/python tests/corpus_regression.py --summary  # just print counts

The deterministic layer is what we lock at 100%. Gemma-classifier output
drifts; tracking that needs separate, looser metrics.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

import prompts_parser as pp


CORPUS_DIR = ROOT / "corpus"


def _candidate_record(c: dict) -> dict:
    """Stable shape for diffing — text replaced by sha1 prefix + length so
    expected files stay compact and don't churn on whitespace edits we
    don't care about. Title is normalized."""
    text = c.get("text", "")
    norm = " ".join(text.split())
    h = hashlib.sha1(norm.encode("utf-8", errors="replace")).hexdigest()[:16]
    return {
        "source": c.get("source", ""),
        "title": (c.get("title") or "").strip(),
        "text_len": len(text),
        "text_sha1_16": h,
    }


def extract(md_path: Path) -> list[dict]:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    cands = pp.extract_candidates(text)
    return [_candidate_record(c) for c in cands]


def baseline_path(md_path: Path) -> Path:
    return md_path.parent / f"expected_{md_path.stem}.json"


def cmd_summary() -> int:
    files = sorted(CORPUS_DIR.glob("*.md"))
    print(f"Corpus: {len(files)} files in {CORPUS_DIR}")
    total = 0
    for f in files:
        recs = extract(f)
        by_source: dict[str, int] = {}
        for r in recs:
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items()))
        print(f"  {f.name:20s}  {len(recs):4d} candidates  ({breakdown})")
        total += len(recs)
    print(f"  {'TOTAL':20s}  {total:4d}")
    return 0


def cmd_bless() -> int:
    files = sorted(CORPUS_DIR.glob("*.md"))
    for f in files:
        recs = extract(f)
        bp = baseline_path(f)
        bp.write_text(json.dumps(recs, indent=2, ensure_ascii=False))
        print(f"  blessed {bp.name}: {len(recs)} candidates")
    return 0


def cmd_diff() -> int:
    files = sorted(CORPUS_DIR.glob("*.md"))
    fails = 0
    for f in files:
        bp = baseline_path(f)
        if not bp.exists():
            print(f"FAIL {f.name}: no baseline at {bp.name} — run with --bless first")
            fails += 1
            continue
        expected = json.loads(bp.read_text())
        actual = extract(f)
        if expected == actual:
            print(f"PASS {f.name}: {len(actual)} candidates match baseline")
            continue
        fails += 1
        # Concise diff summary: count and per-source delta
        e_src: dict[str, int] = {}
        a_src: dict[str, int] = {}
        for r in expected: e_src[r["source"]] = e_src.get(r["source"], 0) + 1
        for r in actual:   a_src[r["source"]] = a_src.get(r["source"], 0) + 1
        print(f"FAIL {f.name}: expected {len(expected)} got {len(actual)}")
        all_src = sorted(set(e_src) | set(a_src))
        for s in all_src:
            e, a = e_src.get(s, 0), a_src.get(s, 0)
            mark = "" if e == a else " ←"
            print(f"        {s:14s} expected={e:3d} actual={a:3d}{mark}")
    return 1 if fails else 0


def main(argv: list[str]) -> int:
    if "--bless" in argv:
        return cmd_bless()
    if "--summary" in argv:
        return cmd_summary()
    return cmd_diff()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
