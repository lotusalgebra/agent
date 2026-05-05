# LOTUS Agent — Setup Guide

## Quick Start (5 minutes)

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 2. PyAudio (voice input — platform-specific)
```bash
# Windows
pip install pipwin && pipwin install pyaudio

# macOS
brew install portaudio && pip install pyaudio

# Linux (Ubuntu/Debian)
sudo apt install python3-pyaudio portaudio19-dev
```

### 3. Set your API key
```bash
# Windows
set ANTHROPIC_API_KEY=sk-ant-...

# macOS/Linux
export ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Run
```bash
python agent.py
```
Choose `v` for voice mode or `t` for text mode.

---

## What It Does

| Feature | How |
|---------|-----|
| **Voice commands** | Say "Lotus" + command. Uses Whisper/Google STT |
| **Voice responses** | edge-tts (Microsoft neural voices) |
| **Screen control** | PyAutoGUI — click, type, scroll, screenshot |
| **Screen reading** | Tesseract OCR (optional) |
| **Open apps** | Chrome, VS Code, Blender, CapCut, etc. |
| **Run commands** | Shell/terminal execution |
| **File ops** | Read, write, list files |
| **Live dashboard** | Browser UI at localhost:8766 |

---

## Architecture

```
┌─────────────────────────────────────────┐
│              YOU (Voice/Text)            │
└──────────────┬──────────────────────────┘
               │
    ┌──────────▼──────────┐
    │    VoiceEngine       │  STT: Whisper/Google
    │    (voice.py)        │  TTS: edge-tts
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │    LotusAgent        │  Brain: Claude Sonnet
    │    (agent.py)        │  Tool routing + memory
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │    ToolExecutor      │  Screen: PyAutoGUI
    │    (tools.py)        │  Files, Apps, System
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │    DashboardServer   │  WebSocket broadcast
    │    (server.py)       │  → dashboard.html
    └─────────────────────┘
```

---

## Example Commands

**Screen control:**
- "Lotus, take a screenshot and tell me what's on screen"
- "Lotus, open Chrome and go to instagram.com"
- "Lotus, click on the search bar and type hello"

**Apps:**
- "Lotus, open VS Code"
- "Lotus, open Blender"
- "Lotus, open CapCut"

**System:**
- "Lotus, how much RAM am I using?"
- "Lotus, list files on my desktop"
- "Lotus, what's running on my system?"

**Tasks:**
- "Lotus, create a new file called todo.md with my tasks"
- "Lotus, take a note — meeting with client tomorrow at 3"
- "Lotus, what notes do I have?"

---

## Optional: Tesseract OCR (recommended)

Enables screen reading — the agent can find and read text on your screen.

```bash
# Windows — download installer:
# https://github.com/UB-Mannheim/tesseract/wiki

# macOS
brew install tesseract

# Linux
sudo apt install tesseract-ocr

# Then:
pip install pytesseract
```

---

## Customization

### Change wake word
In `agent.py`, line: `self.wake_word = "lotus"`

### Change voice
In `voice.py`, constructor parameter `tts_voice`:
- `en-IN-PrabhatNeural` — Indian English male (default)
- `en-IN-NeerjaNeural` — Indian English female
- `en-US-GuyNeural` — American male
- `en-GB-RyanNeural` — British male

### Change AI model
In `agent.py`, change `model="claude-sonnet-4-20250514"` to any Claude model.

### Add custom tools
In `tools.py`, add a method `tool_your_tool_name(self, **params)` and add the
matching tool definition in `TOOLS` list in `agent.py`.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| PyAudio install fails | Windows: use `pipwin`. macOS: `brew install portaudio` first |
| Mic not detected | Check system audio settings, ensure mic permissions |
| "No module named 'pyautogui'" | `pip install pyautogui Pillow` |
| OCR not working | Install Tesseract engine (not just pip package) |
| Dashboard blank | Ensure `pip install websockets` and check port 8765/8766 |
| Edge-TTS no sound | Install ffplay/mpv for audio playback |
