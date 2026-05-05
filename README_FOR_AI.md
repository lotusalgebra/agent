# LOTUS AGENT — Setup Instructions for AI Assistant

## Context for whoever reads this

You are being asked to set up **LOTUS Agent** on Somendra's Mac Studio M1 Max 32GB.
This is a voice-controlled AI assistant that runs locally.

The user (Somendra) has provided you this folder containing all source code.
Your job: install dependencies, configure the environment, and verify it runs.

**User profile:**
- Name: Somendra
- Mac: Mac Studio M1 Max 32GB RAM, 1TB storage
- Has: Claude.ai Pro subscription (NOT API key)
- Work: Content creator for @techengine.lab + full-stack developer
- Comfort level: Can run terminal commands but prefers step-by-step guidance

---

## IMPORTANT — What NOT to do

- ❌ Do NOT use Anthropic API — user has Pro subscription, not API key
- ❌ Do NOT install pyautogui or screen control libraries yet (Phase 2)
- ❌ Do NOT set up Google Drive OAuth yet (Phase 4)
- ❌ Do NOT configure Selenium for NB2/Grok yet (Phase 3)
- ❌ Do NOT ask for API keys, OpenAI keys, or cloud credentials

---

## PHASE 1 — Foundation Setup (do this now)

This is all we do today. Everything else comes in later phases.

### Goal
Install Ollama + Gemma 4 E4B model + Python environment, verify everything works.

### Steps (do in order, ask approval before each)

#### 1. Verify Mac environment
```bash
sw_vers                           # Should show macOS version
system_profiler SPHardwareDataType | grep -E "Chip|Memory"  # Verify M1 Max 32GB
python3 --version                 # Need Python 3.9+
```

Expected: macOS 13+, Apple M1 Max chip, 32 GB memory, Python 3.9 or higher.

#### 2. Install Homebrew (if missing)
```bash
# Check first
brew --version || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

If Homebrew installs, also run the `eval` command it prints at the end.

#### 3. Install Ollama
```bash
brew install ollama
```

#### 4. Start Ollama service
```bash
# Run in background
brew services start ollama

# Verify running
curl http://localhost:11434/api/tags
```

Should return JSON (maybe empty list if no models yet).

#### 5. Download Gemma 4 E4B (~5GB, 5-10 minutes)
```bash
ollama pull gemma4:e4b
```

#### 6. Test Gemma works
```bash
ollama run gemma4:e4b "Say hello in one sentence"
```

Should respond with something like "Hello! How are you today?"

Exit with `/bye`.

#### 7. Set up Python virtual environment
```bash
cd ~/LotusAgent                   # This folder
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
```

#### 8. Install Phase 1 dependencies only
```bash
pip install anthropic requests
pip install SpeechRecognition edge-tts openai-whisper
pip install pyperclip psutil
pip install websockets
```

For PyAudio (microphone input):
```bash
brew install portaudio
pip install pyaudio
```

#### 9. Test voice engine
Create a test file and run it:
```bash
cat > test_voice.py << 'EOF'
from voice import VoiceEngine
ve = VoiceEngine()
ve.speak("LOTUS Agent is online and ready, Somendra.")
print("Say something for 5 seconds...")
text = ve.listen(timeout=5)
print(f"Heard: {text}")
EOF

python test_voice.py
```

This tests both TTS (you should hear it speak) and STT (it transcribes what you say).

#### 10. Verify Ollama Python connection
```bash
cat > test_ollama.py << 'EOF'
import requests
r = requests.post(
    "http://localhost:11434/api/generate",
    json={"model": "gemma4:e4b", "prompt": "Say hi", "stream": False}
)
print(r.json()["response"])
EOF

python test_ollama.py
```

Should print a Gemma response.

### Phase 1 Complete When:
- [ ] Ollama runs, Gemma 4 E4B installed and responding
- [ ] Python venv created with all base deps
- [ ] Voice TTS speaks out loud
- [ ] Voice STT transcribes microphone input
- [ ] Ollama Python connection works

**At this point, stop and report success to Somendra.**
Do NOT proceed to Phase 2 until Somendra explicitly asks.

---

## PHASE 2 — Screen Control (only if Somendra asks)

Adds ability to control screen, open apps, take screenshots.

```bash
pip install pyautogui Pillow pytesseract
brew install tesseract
```

Grant Accessibility permissions in:
**System Settings → Privacy & Security → Accessibility → add Terminal**

Test with `agent.py` in text mode:
```bash
python agent.py
# Choose 't' for text mode
# Try: "take a screenshot"
```

---

## PHASE 3 — Voice Auth (only if Somendra asks)

```bash
pip install resemblyzer numpy
python agent.py enroll Somendra
```

Follow voice enrollment prompts (5 phrases).

---

## PHASE 4 — Content Pipelines (future, don't do now)

- NB2 browser automation (needs Selenium + ChromeDriver)
- Grok animation automation
- FFmpeg cinematic join
- Google Drive API upload
- Claude.ai browser automation for reel scoring

These require additional setup and testing. Somendra will ask when ready.

---

## File Structure (already provided in this folder)

```
LotusAgent/
├── README_FOR_AI.md       ← This file (your instructions)
├── SETUP.md               ← Human-readable setup guide
├── BLUEPRINT.md           ← Full architecture + pipeline blueprint
├── agent.py               ← Main brain + voice loop
├── voice.py               ← STT (Whisper/Google) + TTS (edge-tts)
├── tools.py               ← Screen control, file ops, apps (18 tools)
├── router.py              ← Hybrid router (Claude.ai vs local)
├── auth.py                ← Voiceprint authentication
├── server.py              ← WebSocket dashboard backend
├── dashboard.html         ← JARVIS-style live UI
├── requirements.txt       ← All Python deps
└── test/                  ← Quick test scripts (created by you)
```

---

## Architecture Summary (for context)

**Voice command flow:**
```
Speak "Lotus, ..." 
    ↓
[Voice Auth] → verify speaker
    ↓  
[Hybrid Router] → classify: search or task?
    ↓                         ↓
[Claude.ai browser]    [Gemma 4 E4B local]
 (Pro subscription)     (Ollama, free)
    ↓                         ↓
[Claude responds]      [Execute tool → respond]
    ↓                         ↓
[Voice TTS] ←──────────────────
```

**Today's goal: Phase 1 only.** Get the foundation solid. Voice in → Gemma brain → voice out. Everything else is added incrementally when requested.

---

## Troubleshooting Common Issues

**"brew: command not found"**
Run Homebrew installer, then the `eval "$(/opt/homebrew/bin/brew shellenv)"` command it suggests.

**"ollama: command not found" after brew install**
```bash
eval "$(/opt/homebrew/bin/brew shellenv)"
source ~/.zshrc
```

**Ollama can't find Gemma 4 E4B**
Check exact model name:
```bash
ollama list
# If different name shown, use that name in tests
```

**PyAudio install fails**
```bash
brew install portaudio
pip install --no-cache-dir pyaudio
```

**Microphone not detected**
**System Settings → Privacy & Security → Microphone → add Terminal**

**"edge-tts" no audio output**
```bash
brew install mpv
# or
brew install ffmpeg
```

---

## Summary Instruction for AI Assistant

1. Read this file completely
2. Execute Phase 1 only
3. Ask user approval before each command (they must type `y`)
4. Paste command output so user can see
5. Report success/failure after each step
6. When Phase 1 complete, STOP and wait for user to continue to Phase 2

**Do not improvise or add steps. Stick to what's written here.**
**Do not use Anthropic API — user has Pro subscription only.**
**Do not skip tests — each phase must verify before moving on.**

When Phase 1 is verified working, tell Somendra:

> ✅ Phase 1 complete. Gemma 4 E4B is running locally via Ollama.
> Voice engine tested: TTS speaking, STT listening.
> Ready for Phase 2 (screen control) whenever you want.

---

End of instructions.
