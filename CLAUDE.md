# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Non-obvious context before you touch anything

- **The user has Claude.ai Pro, NOT an Anthropic API key.** `agent.py` (the "full" entry point) calls the Anthropic API directly and will not run as-is for this user. Day-to-day, the agent runs through `agent_phase1.py` (local Gemma + content pipelines) and the HybridRouter path that drives Claude.ai via browser automation. Never add code that assumes a paid Anthropic key is available.
- **Everything-in-browser is the architecture, not Selenium.** External services (Gemini/NB2, Drive, Grok, Claude.ai, Upwork) are driven by *the user's real signed-in Chrome*, attached via Chrome DevTools Protocol on `--remote-debugging-port=9222`. Playwright connects to the running Chrome instead of launching its own Chromium — this is what lets the bots reuse logged-in cookies (Google, X/Grok, Upwork) without re-authing each session. `browser.py` primitives + AppleScript/pyautogui/clipboard/OCR drive the legacy paths (Claude.ai search, OS-level focus). Selenium is deliberately avoided because it can't share a running Chrome's user-data-dir. If you're tempted to reach for `selenium`, `webdriver`, or `playwright.chromium.launch()` (bundled Chromium), stop — use `browser.py` primitives or the CDP-attach pattern in `gemini_bot.py` / `grok_video.py` / `upwork_agent.py` instead.
- **Target machine:** Mac Studio M1 Max, 32 GB. Gemma model (`gemma4:e4b`, ~5 GB) is sized to coexist with other work. Don't swap to larger models without asking.
- **There is a mother/child agent mesh.** The mother (`agent_phase1.py`) can run `agents.py` agents in-process, OR delegate them to a remote `lotus-child.py` over a token-authed WebSocket discovered via mDNS (`lotus_mdns.py`). When you see Parser/Renderer/Review/Archiver references, assume work *may* be remote — never hardcode local paths inside an agent's `run()`.
- **The pipeline has a human review gate.** Parsed prompts land in `~/.lotus_auth/prompts.db` as `pending`. Image generation only fires for rows the user `approves` in the dashboard. Don't add a code path that bypasses `lotus_prompts_db.approved_text_for()` to "speed things up" — the gate is the product.

## Running and developing

Project root is `~/LotusAgent` with a local `venv/`. Activate before any Python work:

```bash
cd ~/LotusAgent && source venv/bin/activate
```

### Canonical startup procedure

Browser-driven agents (`gemini_bot`, `grok_video`, `upwork_agent`) need a CDP-attached Chrome. Order matters — Chrome first, then the agent with `LOTUS_CDP_URL` set:

```bash
# 1. Real Chrome with CDP enabled (signs you into Google / Grok / Upwork once;
#    the persistent profile at ~/.lotus_auth/chrome_cdp_profile/ remembers).
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.lotus_auth/chrome_cdp_profile" \
  https://gemini.google.com/app &

# 2. Boot the agent with the CDP URL.
cd ~/LotusAgent && source venv/bin/activate
LOTUS_CDP_URL=http://localhost:9222 python agent_phase1.py v
```

`lotus_preflight.py` codifies this — it discovers Chrome cross-platform (`darwin`/`win32`/Linux paths), launches it with the right flags if not already running, and waits up to 10 min for the user to sign in to Gemini before unblocking the agent. **Don't bypass it** by launching Chrome without `--remote-debugging-port` — the bots have no fallback path.

### Python entry points

| Entry point | Brain | Needs | When to run |
|---|---|---|---|
| `python agent_phase1.py [v\|t]` | Local Gemma via Ollama (`http://localhost:11434`) + content-pipeline orchestrator | Ollama + `gemma4:e4b` | Default daily driver |
| `python agent.py [v\|t]` | Anthropic API (`claude-sonnet-4-20250514`) + HybridRouter | `ANTHROPIC_API_KEY` env var | Only if user explicitly set a key |
| `python server.py` | WebSocket + HTTP dashboard server | websockets | Usually auto-started by the agents; run standalone only to debug the dashboard |
| `python enroll.py [name]` | Hands-free voiceprint enrollment | resemblyzer, pyaudio | Initial setup or re-enroll (workaround for macOS PyAudio segfault seen in `auth.py::enroll`) |
| `python lotus-child.py --port <p> --agents <list>` | Hosts a subset of the agent mesh as a standalone WebSocket worker | shared token at `~/.lotus_auth/token.txt`, reachable Ollama URL | Run on a second machine (or extra port locally) to delegate ParserAgent / RendererAgent / ReviewAgent / ArchiverAgent work off the mother |
| `python lotus-model.py <pull\|switch\|remove>` | Manage the mother's Ollama model | Ollama running | Switch Gemma variants without editing config by hand |

Voice-auth CLI on `agent.py` (legacy path — `enroll.py` is preferred):
```bash
python agent.py enroll <name>    # 5-phrase enrollment
python agent.py users            # list enrolled users
python agent.py remove <name>    # delete user
python agent.py reset            # wipe ~/.lotus_auth
```

### Requirements files (split by phase)

- `requirements_phase1.txt` — foundation (requests, SpeechRecognition, edge-tts, pyaudio, whisper)
- `requirements.txt` — full stack including `anthropic`, `pyautogui`, `resemblyzer`, `websockets`, Playwright, google-genai, mistune
- Install the narrower one unless the task needs more.

### Tests

All tests are scripts — there is no `pytest` runner or lint step. Run them directly and read stdout.

| Test | Needs | What it covers |
|---|---|---|
| `tests/test_pipeline_e2e.py` | mother running on `ws://localhost:8765`, Ollama up, token at `~/.lotus_auth/token.txt` | Real end-to-end — connects as the dashboard would and validates every pipeline stage |
| `tests/test_agents.py` | Ollama only | In-process agent-mesh smoke test (Parser/Renderer/Review/Archiver via `agents.py`) |
| `tests/corpus_regression.py` | nothing — pure parser test | Runs `prompts_parser` against `tests/corpus/{luxury,tech,medical}.md` and diffs against `expected_*.json`. Catches parser regressions when changing fast-paths or Gemma classification |
| `tests/test_luxury_mdfile.py` | nothing | Exercises `prompts_parser.py` against the 63 KB LUXURY_REBUILD markdown |

Run any of them as `venv/bin/python tests/<name>.py`.

### Subprojects (separate `npm` scopes)

- **`lotus-ui/`** — Vite + React 19 + Tailwind dashboard. `npm run dev` / `npm run build` / `npm run lint` (ESLint). TypeScript strict.
- **`lotus-desktop/`** — Electron LAN client for a remote mother. `npm start` for dev, `npm run build:mac|win|linux` via electron-builder.

### Dashboard

The Python `server.py` serves HTTP `localhost:8766/dashboard.html` + WebSocket `ws://localhost:8765`. Auto-started when either agent boots. The `lotus-ui/` React app is the newer dashboard — not yet the default; legacy `dashboard.html` is still what ships on agent start.

## Architecture

```
Voice → VoiceEngine (voice.py) → [VoiceAuth verify, auth.py] → HybridRouter.classify (router.py)
                                                                  │
                                                        ┌─────────┴─────────┐
                                              route="search"           route="task"
                                                        │                   │
                                       Drive Claude.ai browser       Anthropic API + TOOLS
                                       (browser.py / pyautogui)      loop in agent.py
                                                        │                   │
                                                        └─────── ToolExecutor (tools.py)
                                                                            │
                                                                  DashboardServer.broadcast

Content-pipeline path (agent_phase1.py orchestrates):
  prompts.md → prompts_parser (mistune AST + Gemma classification)
            → lotus_prompts_db.add_batch (status=pending, persisted in prompts.db)
            → user approves/edits/rejects rows in dashboard
            → lotus_prompts_db.approved_text_for(source_hash) returns gated set
            → _gen_image router → gemini_bot (Playwright) → gemini_api (key rotation)
                                                          → gemini (legacy OCR) [only if API worked this session]
            → saved under lotus_config folder layout (Techengine<MMDDYYYY>/...)
            → lotus_history records every frame in SQLite

Agent-mesh path (Phase D — agents.py + lotus-child.py + lotus_mdns.py):
  mother (agent_phase1) ─┬─ in-process agents (default)
                         └─ RemoteAgent → WebSocket → lotus-child.py
                                          (mDNS discovery on _lotus-agent._tcp.local.,
                                           token-hash fingerprint in TXT record)
```

### Cross-file relationships (read these before editing)

- **Tool definitions live in two places.** Adding a new tool in `agent.py` requires (1) an entry in the `TOOLS` list with JSON input schema, AND (2) a matching `tool_<name>(self, **params)` on `ToolExecutor` in `tools.py`. `ToolExecutor.execute` dispatches via `getattr(self, f"tool_{tool_name}")` — method names must exactly match tool-name strings.
- **HybridRouter classification is keyword-based, not LLM-based.** `router.py` has hardcoded `SEARCH_TRIGGERS`, `TASK_TRIGGERS` plus a question-word fallback. If routing looks wrong, adjust the lists — don't add a classifier.
- **Search routing is browser automation, not API.** `HybridRouter.send_to_claude_chat` locates a Claude.ai tab (AppleScript on macOS), pastes the query via clipboard, presses Enter. The response is **never** programmatically captured — the user reads it in the browser. Don't try to "fix" this.
- **`_gen_image` has a strict fallback order** (`agent_phase1.py`): (1) `gemini_bot` Playwright web-app, (2) `gemini_api` official API (skipped for the rest of the session once a plan-level 429 is seen — don't waste 30 s/frame retrying), (3) legacy `gemini.py` OCR path (intentionally not called now — returns None so the pipeline advances). Preserve this order and the sticky-skip behavior.
- **Voice auth verifies the *same* audio that contained the wake word** (`agent.py::run_voice_loop`). The raw `AudioData` from the wake-word utterance is passed to `VoiceAuth.verify`. Refactoring to re-listen after the wake word would let an attacker play a "Lotus" recording of the user and then speak commands themselves — keep the single-audio verification.
- **`agent_phase1.py` is the daily orchestrator, not a toy.** It imports `voice.py`, `lotus_config.py`, `lotus_history.py`, `lotus_recorder.py`, `prompts_parser.py`, `lotus_prompts_db.py`, and the gemini modules. It does **not** import `tools.py`, `router.py`, `auth.py`, or `server.py` — the two agents represent different trust/capability tiers. Don't DRY them together.
- **`browser.py` is the one true browser primitive module.** `focus_chrome_tab`, `open_url`, `screenshot`, `ocr_data`, `find_text`, `click`, `paste_text`, `press`, `wait_for_text`. Prefer composing these over hardcoded `pyautogui` coordinates — UI elements are located by OCR so automations survive Google UI refreshes.
- **`prompts_parser.py` uses Gemma as a classifier, not a parser.** mistune builds the AST deterministically; each candidate block is sent to Gemma for a single-prompt "cleaned prompt or reject" decision. `extract_candidates()` returns a list of dicts (text, slide_title, topic_number, slide_number, source_type, text_hash) — the dict shape is the contract that `lotus_prompts_db.add_batch` consumes, so don't strip fields when adding new ones, and don't replace either half (mistune ↔ Gemma) with the other.

- **`lotus_prompts_db.py` is a review gate, not a cache.** Its DB (`~/.lotus_auth/prompts.db`) is *separate* from history (`history.db`) on purpose — wiping prompt-review state must not affect generation history. Generation only consumes `approved_text_for(source_hash)`; rows stay `pending` until a user acts in the dashboard. A row's identity is `(source_hash, text_hash)` UNIQUE, so re-running the parser on the same MD file is idempotent — re-extraction won't lose prior approvals.

- **`lotus_file_classifier.py` is heuristic-first, Gemma-fallback.** `/api/upload` calls `classify_file(path)` after saving the upload. The heuristic scans the first 8 KB for known prompt-pack markers (`## SLIDE`, `## POST`, `## TOPIC`, `NB2 PROMPT`, `Production Bible`, `\d+ slides each`, `Instagram carousel`, fenced code blocks) and returns instantly with verdict 'yes'. Strong-NO patterns (XML/HTML headers, `import`/`def`/`function` at top) return 'no' instantly. Only when neither tier fires does it call Gemma. **The verdict is always advisory** — the dashboard's "Treat as prompts file anyway" link in `SourceDropzone` lets the user override 'no'/'uncertain'. 'yes' verdicts auto-load the file via `onPath`; everything else waits for explicit confirmation. Don't tighten this into a hard block.

- **`lotus_pipelines_db.py` snapshots the multi-pipeline registry.** Every call to `_pipeline_broadcast` upserts every live pipeline's metadata into `~/.lotus_auth/pipelines.db`. On agent boot, `LotusPhase1.__init__` calls `_rehydrate_pipelines_from_db` to restore non-terminal rows (i.e. `stage != 'done' AND cancelled = 0`) into `_pipelines`. **Render-loop threads do NOT come back automatically** — every rehydrated pipeline is force-paused with `pause_reason='restored from previous session'`; the user resumes via the UI. Terminal/cancelled rows stay in the DB for the A2 "All Pipelines" overview but never re-enter the live registry.

- **Agent mesh is a separate trust tier from the orchestrator.** `agents.py` defines `BaseAgent` (submit/status/health) and the four concrete agents (Parser/Renderer/Review/Archiver). Each `submit(task)` runs on a daemon thread so a blocking Playwright/OCR call inside an agent can't stall the mother's event loop. Agents are exposed remotely by `lotus-child.py` over a JSON-over-WebSocket protocol gated by the shared token in `~/.lotus_auth/token.txt`. `lotus_mdns.py` advertises children on `_lotus-agent._tcp.local.` with the SHA-256(token)[:16] fingerprint in the TXT record — the mother only auto-registers children whose fingerprint matches. The token itself never leaves the machine; mDNS is for discovery, not auth.

- **Children don't run their own Ollama.** `lotus-child.py::_build_agent_factories` sets `OLLAMA_URL` to the mother's reachable URL *before* importing `agents`, so ReviewAgent/ArchiverAgent reach back to the mother for Gemma calls. Don't refactor an agent to import Ollama URL from config at module-load time — the late-binding import is what makes remote children work.

- **`lotus_preflight.py` is cross-platform Chrome control.** Chrome binary discovery is a list of candidates per `sys.platform` (`darwin` / `win32` / Linux `which`). When adding new CDP-driven agents, route through preflight rather than re-implementing the Chrome launch — it's the only place that broadcasts `"Sign in to Gemini"` state to the dashboard while the user authenticates.

- **`grok_video.py` is image-to-video over CDP, not API.** Connects via `LOTUS_CDP_URL` to grok.com/imagine, uploads the source PNG, picks `Video` tab + quality + duration + aspect, types prompt, polls `<video>` for finished MP4, then fetches via Playwright's `APIRequestContext` (uses Chrome's cookie jar) — fall back to `<video>.src` direct fetch only if that fails. Selectors are **text-based** ("Video", "720p", "Aspect Ratio", "Submit") because Grok's CSS classes churn — keep them text-based when patching.

- **`upwork_agent.py` + `upwork_db.py` is a three-tier handoff.** Real Chrome via CDP scrapes the Best Matches feed (Upwork blocks bundled Playwright Chromium), local Gemma scores each job with reasons, optional Claude.ai (browser) drafts the proposal. Selectors fall back to semantic queries (`article`, `[data-test=...]`, role=heading) and text patterns because Upwork's class names are obfuscated. State persists in `upwork_db` (separate SQLite, not part of the prompts/history/pipelines DBs).

- **`blender.py` renders MP4 directly via Blender's built-in FFmpeg encoder.** Templated Blender Python script (`blender_jobs.py.tmpl`) is materialised per call with token replacements, then run via `Blender --background --python <script>`. **Do not call external `ffmpeg` from this path** — that conflicts with the user's "FFmpeg → Premiere Pro" preference (Premiere is for cinematic-join / final cut). Blender path candidates are an explicit list; 4.2 LTS is preferred because it's the only build that ships with FFmpeg.

- **`photoshop.py` is currently PAUSED** (see `SESSION_STATUS.md` A15). Module + `photoshop_jobs.jsx` template are written but not smoke-tested. If you resume it, ask the user for the watermark logo path and explicit OK to launch Photoshop before running anything — it drives the live PS app via JSX.

## Persistent state (lives outside the repo)

| Path | Written by | Contents |
|---|---|---|
| `~/.lotus_auth/voiceprints.npz` | `auth.py` / `enroll.py` | numpy-saved speaker embeddings per user |
| `~/.lotus_auth/config.json` | `auth.py` | enrollment metadata (role, timestamp) |
| `~/.lotus_auth/chrome_cdp_profile/` | Chrome itself (via `--user-data-dir`, launched by `lotus_preflight.py`) | Persistent Chrome profile — holds Google / Grok / Upwork login cookies indefinitely. The CDP-attached agents (`gemini_bot`, `grok_video`, `upwork_agent`) use whatever Chrome session is open on this profile |
| `~/.lotus_auth/gemini_api_key.txt` | user (manual) | one API key per line; `gemini_api.py` rotates round-robin, penalizes rate-limited keys |
| `~/.lotus_auth/history.db` | `lotus_history.py` | SQLite of pipeline runs + per-frame status |
| `~/.lotus_auth/prompts.db` | `lotus_prompts_db.py` | SQLite of parser-extracted prompts under review (pending/approved/rejected) — **separate from history.db on purpose** |
| `~/.lotus_auth/pipelines.db` | `lotus_pipelines_db.py` | SQLite metadata for the multi-pipeline registry (id, name, source_md, source_hash, stage, paused, cancelled). Snapshotted from `_pipeline_broadcast`; non-terminal rows rehydrated on agent boot |
| `~/.lotus_child_config.json` | `setup-child.py`, `lotus-child.py` | Per-machine child config (port, agents to host, mother's Ollama URL) |
| `~/.lotus_auth/recordings/` | `lotus_recorder.py` | rotated WAV files (every `ROTATE_SECONDS`) |
| `~/.lotus_auth/token.txt` | dashboard auth | token consumed by `test_pipeline_e2e.py` |
| `~/.lotus_config.json` | `lotus_config.py` | projects root, brand, folder layout for content pipeline |
| `~/.lotus_screenshots/` | `tools.py::tool_screenshot` | `screen_YYYYMMDD_HHMMSS.png` |
| `~/.lotus_notes.json` | `tools.py::tool_take_note` | list of `{title, content, created}` |

`~/.lotus_auth/` must survive across sessions — wiping forces re-enrollment and re-login of every Google service. `agent.py reset` is the only sanctioned way to clear it.

## Content-pipeline folder layout (locked 2026-04-18)

`lotus_config.py` owns this. Frame numbering is inferred from the filesystem so a restart mid-run resumes where it left off.

```
<projects_root>/                     default: ~/LotusAgent/Projects
└── <brand><MMDDYYYY>/               e.g. Techengine04182026
    ├── Post1/ ... Post5/            MULTI_FRAME_SECTIONS — FrameN.png inside
    ├── Reel/                        multi-frame
    ├── ReelCover.png                SINGLE_ITEM_SLOTS — file directly in parent
    ├── Story1.png ... Story3.png    single-item slots
    └── prompts.md                   Claude-generated NB2 prompts
```

## Tuning knobs (module constants, not env vars unless noted)

- `auth.py`: `SIMILARITY_THRESHOLD = 0.75`, `LOCKOUT_ATTEMPTS = 3`, `LOCKOUT_SECONDS = 60`, `ENROLLMENT_PHRASES` (5 fixed).
- `agent_phase1.py`: `MODEL = "gemma4:e4b"`, `WAKE_WORD = "lotus"`, `TTS_VOICE = "en-IN-PrabhatNeural"`.
- `agent.py`: `self.wake_word = "lotus"`, model `claude-sonnet-4-20250514`.
- `voice.py`: TTS voice options in `VoiceEngine.__init__` docstring.
- `gemini_api.py`: `LOTUS_GEMINI_IMAGE_MODEL` env var overrides default `gemini-3.1-flash-image-preview` (NB2).
- `lotus_config.py`: `SINGLE_ITEM_SLOTS`, `MULTI_FRAME_SECTIONS`, `parent_format`.
- `lotus_recorder.py`: `ROTATE_SECONDS` for WAV rotation.

## Productization & cross-platform

LOTUS is being built to ship as a paid Mac+Windows desktop product (see `PRODUCTIZATION_PLAN.md`). That has two practical consequences for any code change:

- **Cross-platform-first.** New Python should branch on `sys.platform` rather than assume macOS. `lotus_preflight.py`, `setup-mother.py`, `setup-child.py` are the existing examples — Chrome discovery, port checks, launcher generation all switch by platform. Avoid hardcoded `/Applications/...` paths or `brew` invocations outside install scripts. macOS-only behaviors (AppleScript focus in `browser.py`, `afplay` for TTS) are isolated and known — extend the same isolation pattern when you need OS-specific code.
- **Distribution surfaces:**
  - `lotus-desktop/` — Electron LAN client. Builds via `npm run build:mac|win|linux` (electron-builder, NSIS for Windows, dmg for Mac, AppImage for Linux). App ID `com.lotusalgebra.lotus-agent`.
  - `packaging/` — PyInstaller specs for the **child worker** installer (`packaging/lotus-child.spec` is the cross-platform source of truth). Per-platform builds via `packaging/windows/build.bat` (WiX MSI) or `packaging/mac/build.sh` (pkgbuild + productbuild). **Cross-compilation isn't supported** — each platform's installer must be built on that platform.
  - The mother bundle isn't packaged yet — that's Phase 1 of `PRODUCTIZATION_PLAN.md`.

## macOS-specific gotchas

- **Accessibility permission** must be granted to Terminal (or whichever terminal app runs Python) before `pyautogui` click/type/hotkey work. System Settings → Privacy & Security → Accessibility.
- **Microphone permission** same path, under Microphone.
- `tool_open_app` uses `open -a <name>` — app names must match `.app` bundle names (e.g. `"Google Chrome"`, not `"chrome"`). The Windows-style `app_map` in `tools.py` doesn't apply on darwin.
- edge-tts audio plays via `afplay`. No sound → verify `afplay` works standalone on a test mp3 first.
- `enroll.py` opens the mic stream ONCE — do not refactor into a re-open loop (caused PortAudio segfaults on macOS, which is why this file exists alongside `auth.py::enroll`).
- `launch.bat` is Windows-only; ignore.

## Safety behavior already in place (don't remove)

- `tools.py::tool_run_command` blocks a hardcoded destructive-command list (`rm -rf /`, `mkfs`, `dd if=`, fork bomb, etc.), enforces 30 s timeout + 3000-char output cap. Extend the blocklist if you find gaps; don't weaken it.
- `pyautogui.FAILSAFE = True` set in `tools.py::_init_screen` and `browser.py` — slamming the mouse into a screen corner aborts automation. Preserve this.
- `lotus_recorder.py` is the engine; pair any recording with a visible REC indicator in the UI — never hide recording from the user.
- Conversation history in `agent.py` auto-trims to last 30 messages after reaching 40 to keep token usage bounded.
