# LOTUS — Session checkpoint · 2026-04-27

Saved before system restart. Read this first when resuming.

---

## How to restart cleanly

```bash
# 1. Open real Chrome with CDP enabled (required by gemini_bot + grok_video).
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.lotus_auth/chrome_cdp_profile" \
  https://gemini.google.com/app &

# 2. Sign in to Google + Gemini in that Chrome window if prompted.
#    Open a second tab to https://grok.com/imagine and sign in too
#    (needed for grok_video to drive the Imagine UI).

# 3. Boot the agent in voice mode with the CDP URL set.
cd ~/LotusAgent && source venv/bin/activate
LOTUS_CDP_URL=http://localhost:9222 python agent_phase1.py v
```

Dashboard: <http://localhost:8766/>  ·  WS: `ws://localhost:8765`

---

## Key tooling pinned this session

- **Playwright 1.55.0** in venv (1.58 had a CDP `setDownloadBehavior` bug
  that broke gemini_bot via CDP — do NOT upgrade without testing).
- **Chrome 147** (whatever you have installed).
- **Tesseract 5.5.2** at `/opt/homebrew/bin/tesseract` — used by both
  `prompts_parser` (Gemma offload) and `photoshop.py` OCR.
- Ollama running with `gemma4:e4b`.

---

## Task list

| # | Task | Status | Notes |
|---|---|---|---|
| A1  | Pipeline list/tabs in UI | done | (was already there) |
| A2  | All-Pipelines overview screen | done | |
| A3  | Persist pipelines across restart | done | `~/.lotus_auth/pipelines.db` |
| A4  | "+ New Pipeline" button | done | |
| A5/A6/A6.1/A6.2 | Per-post checkbox batches + sync semantics + scope clobber | done | |
| A7  | Cross-pipeline Queue tab | done | |
| A8  | Delete-pipeline trash button | done | |
| A9  | Patch gemini_bot CDP downloads | done via A1 (Playwright downgrade) | |
| A10 | Per-pipeline folders (`<name>_<MMDDYYYY>/`) | done | |
| A11 | Backfill folder for legacy pipelines | done | |
| A12 | Save Batch / mark complete button | done | |
| A13 | Parser fast-path on `NB2 PROMPT` / `GROK PROMPT` labels | done | |
| A14 | Recognize FRAME/REEL/BAKED format (ASTRA-style files) | done | |
| A15 | **Photoshop integration** — OCR text removal + watermark | **PAUSED** | module written, **not smoke-tested**; needs watermark file path + OK to launch PS |
| A16 | (Grok image-gen tier-3 fallback) | reverted | wrong feature; left in code as commented-out reference |
| A17 | **Grok video gen (image-to-video)** | **IN PROGRESS** | `grok_video.py` built, needs end-to-end smoke test |
| A18 | Lightbox edit-prompt + regen | done | inline editor, sends `pipeline_regen_frame` with `{id, text}` |

---

## Files written / modified this session

**New:**
- `lotus_pipelines_db.py` — multi-pipeline metadata persistence
- `lotus_file_classifier.py` — heuristic + Gemma fallback for upload classification
- `tests/corpus_regression.py` + `tests/corpus/{luxury,tech,medical}.md` + `expected_*.json`
- `tests/corpus_ground_truth.md`
- `photoshop.py` — Photoshop integration backend (PAUSED)
- `photoshop_jobs.jsx` — JSX template for PS jobs (PAUSED)
- `grok_bot.py` — Grok image-gen bot (deprecated, kept for reference)
- `grok_video.py` — **active** — Grok image-to-video bot
- `SESSION_STATUS.md` — this file

**Modified:**
- `agent_phase1.py` — A3/A4/A6/A7/A8/A10/A11/A12/A13/A14/A18 hooks (Pipeline class, dispatchers, render path, dedupe)
- `server.py` — `/api/pipelines`, `/api/queue`, `pipeline_list` reconnect-replay caching
- `lotus_config.py` — `parent_folder_for_pipeline()` helper, `parent=` override on `next_frame_path` / `section_folder` / `single_item_path`
- `lotus_pipelines_db.py` — `folder_name` column added
- `prompts_parser.py` — A13/A14 fast-paths
- `CLAUDE.md` — updated cross-file relationship bullets for new modules
- `lotus-ui/src/App.tsx` — All Pipelines tab, Queue tab, per-post batch UI, Delete, Backfill, Save Batch, Lightbox edit, ~285 lines of dead code removed (D1)

---

## Live pipelines as of save

| id | name | stage | folder |
|---|---|---|---|
| 2fd4505d | **agentanytype** | review_frames | agentanytype_04272026 |
| a0184a81 | SugarDrug | done | sugardrug_04272026 |
| 2d806ebc | medical | done | medical_04262026 |
| 907bfe30 | mondayreel | done | mondayreel_04262026 |
| 1cb533fd | story_today | done | storytoday_04262026 |
| 295237d3 | FoodPart1 | done | foodpart1_04262026 |
| d68db27a | Defence | save | defence_04262026 |

`agentanytype` is the active one — only slide 2 approved, Frame2.png on disk + ghost Frame1.png (915 KB low-quality, can delete manually).

---

## Resume points (where we left off)

1. **A17 Grok video smoke test** — module is built, needs first end-to-end run.
   Awaiting from user:
   - source frame path
   - animation prompt
   - (optional) quality / duration / aspect overrides

   Then I run:
   ```bash
   LOTUS_CDP_URL=http://localhost:9222 python grok_video.py \
     "<frame.png>" "<animation prompt>" \
     --quality 720p --duration 10s --aspect 9:16
   ```
   Grok window opens visibly, ~3-5 min, MP4 saves to `<frame_dir>/Videos/<frame>.mp4`.

2. **A15 Photoshop integration** — paused per user. Resume by sending watermark logo path + confirming OK to launch Photoshop.

3. **Lightbox fixes (A18)** — shipped. Verify after restart by clicking any frame → tile enlarges → Edit / Approve / Deny / Regen / Save & Regen-with-Edited buttons should all work. Keyboard shortcuts A/D/R also work in any frame-relevant stage.

4. **Frame dedupe** — fixed. `_pipeline_generate_approved` now drops stale duplicates before rebuild. The agentanytype pipeline had 3 ghost f2 entries; restart wipes them.

---

## Known issues / not yet fixed

- **Render quality drop** — when Playwright's primary download path fails, gemini_bot falls back to canvas-capture which produces ~1 MB low-quality PNGs (vs 5-7 MB normal). Frame1.png in agentanytype is an example. Root cause: download race between Playwright's temp file and Chrome's cleanup. Patch deferred — happens rarely now that CDP is stable.

- **`npm run build` has 0 tsc errors** as of last build (D1 cleaned up 21 pre-existing). Stay clean — don't add unused imports / dead branches.

- **Pre-A10 pipelines** (`Defence`, `medical` originally) were force-paused on rehydration with `pause_reason='restored from previous session'`. Use the migrate link or just create fresh pipelines.

---

## Test commands you can run after restart

```bash
# Verify modules + parser regression
cd ~/LotusAgent && venv/bin/python tests/corpus_regression.py

# Live API checks (needs agent running)
curl -s http://localhost:8766/api/pipelines | venv/bin/python -m json.tool | head -30
curl -s http://localhost:8766/api/queue     | venv/bin/python -m json.tool | head -30

# Grok video smoke test (needs agent running + grok.com signed in)
LOTUS_CDP_URL=http://localhost:9222 venv/bin/python grok_video.py \
  "<frame_path>" "<animation prompt>" --quality 720p --duration 10s --aspect 9:16
```
