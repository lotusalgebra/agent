# LOTUS Agent — Quick Start for Somendra

## What's in this folder

Everything you need for your voice AI assistant. 11 files. 2 documentation files.

## Simplest way to use this

### Option 1: Hand to Claude Code / ChatGPT / any AI
1. Open the AI assistant (Claude Code, ChatGPT, Cursor, whatever)
2. Point it at this folder
3. Tell it: **"Read README_FOR_AI.md and execute Phase 1"**
4. Approve each command as it runs
5. Done in 20 minutes

### Option 2: Do it yourself
Follow `SETUP.md` step by step.

### Option 3: Continue in Claude.ai chat
Just say "let's do Phase 1" and I'll guide you command-by-command.

## What Phase 1 gets you

- Ollama running Gemma 4 E4B (local AI brain)
- Voice engine working (speak and listen)
- Python environment ready
- All base code in place

**Runtime after Phase 1:** Talk to your Mac, Gemma responds.

## Phases overview

| Phase | Feature | When |
|---|---|---|
| 1 | Ollama + Gemma + Voice | **Today** |
| 2 | Screen control (click, type, screenshot) | Next session |
| 3 | Voice auth (only your voice works) | After Phase 2 |
| 4 | Content pipelines (NB2, Grok, FFmpeg) | Production ready |
| 5 | Premiere Pro + After Effects automation | Pro editing |
| 6 | Windows 365 Cloud PC worker | Multi-machine |

## If something breaks

Just paste the error into this Claude chat or your AI assistant.
Don't try to fix it yourself — ask, and you'll get the exact fix.

## Files you probably won't touch

- `dashboard.html` — just works
- `server.py` — just works  
- `auth.py` — used in Phase 3
- `launch.bat` — Windows only, ignore on Mac

## Files you MIGHT customize later

- `agent.py` — main brain behavior, system prompt
- `voice.py` — change TTS voice (Indian English default)
- `tools.py` — add custom tools you want
- `router.py` — adjust what goes to Claude.ai vs local

## Default settings

- **Wake word:** "Lotus"
- **TTS voice:** Indian English male (Prabhat)
- **Local model:** Gemma 4 E4B (~5GB, runs on ~6GB RAM)
- **Brain routing:** Search → Claude.ai, Tasks → local Gemma
- **Auth threshold:** 75% voiceprint match

All changeable in the respective files.

## Ready?

Hand this folder to your AI assistant and say:
> "Read README_FOR_AI.md and execute Phase 1"

Or come back to the Claude chat and say:
> "Let's start Phase 1"
