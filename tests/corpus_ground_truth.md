# Parser corpus — ideal extraction counts

This is the human-verified target for `prompts_parser.extract_candidates()`.
The blessed `expected_*.json` files snapshot **today's** parser output (so
we detect regression). This file records the **ideal** output (so C2 knows
what to fix).

## Per-file ideals

| File | Posts/Topics | Slides each | Ideal candidates | Today | Delta |
|---|---|---|---|---|---|
| `luxury.md` | 7 posts | 10 | 70 | 70 | 0 ✅ |
| `tech.md` | 7 posts | 10 | 70 | 70 | 0 ✅ |
| `medical.md` | 5 topics | 10 | 50 | 57 | +7 false positives |

## Known issues

### medical.md — 7 false-positive checklist sections

The parser emits `section` candidates for "PRE-POST CHECKLIST" blocks under
each TOPIC heading. These are publishing checklists (`[ ] All 10 slides at
1080 × 1350...`), not image-generation prompts.

Root cause: `_section_title_looks_prompty()` returns true for any heading
containing the topic anchor (e.g. "🔥 TOPIC 01 — HEARING LOSS GENE
THERAPY · Hook: ... · PRE-POST CHECKLIST") because the topic name keyword
matches.

C2 fix idea: exclude sections whose body is dominated by checklist
markers (`[ ]`, `[x]`) or whose immediate heading contains "CHECKLIST",
"PRE-POST", "POST-PUBLISH", etc.

### medical.md — 50 correct candidates come from `code_block` source

The MEDICAL v2 ADVANCED format puts the prompt inside a fenced code block
under `### 📸 NB2 PROMPT (ADVANCED)`. All 50 are caught via the
`code_block` source. Good.

### luxury.md / tech.md — 70 correct candidates come from `section` source

These older files put the fenced code block immediately under
`## SLIDE N — TITLE` with no intermediate heading. The fenced block isn't
labelled as a prompt explicitly, so `_code_block_title_looks_prompty()`
returns false and the candidate is captured as `section` (the slide body)
instead. This works but is fragile — a code-block-aware path would be
cleaner.

## Reproducing

```bash
cd ~/LotusAgent
venv/bin/python tests/corpus_regression.py --summary   # counts only
venv/bin/python tests/corpus_regression.py             # diff vs blessed
venv/bin/python tests/corpus_regression.py --bless     # rewrite baselines
```

After C2 lands, the ideal baseline becomes 70 / 70 / 50 and we re-bless.
