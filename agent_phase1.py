"""
LOTUS AGENT — Phase 1 (Ollama/Gemma local brain only)
Built by Somendra | Lotus Algebra

Phase 1 = voice in → Gemma brain → voice out
No API key required. Runs 100% local.
Upgrade to Phase 2+ adds tools, screen control, pipelines.
"""

import json
import os
import re
import requests
import sys
from datetime import datetime
from typing import Optional

import lotus_config
import lotus_prompts_db
import lotus_pipelines_db
from voice import VoiceEngine


# ── Image generation router ──────────────────────────────────────────
# Prefer the official google-genai API (fast, reliable, NB2). Fall back
# to the Chrome+OCR automation if no API key is configured.

_gen_api_plan_blocked: bool = False   # sticky once we've seen a 429-plan error

# Engine name → (module name, callable name). Each callable has the same
# signature (prompt, target, verbose=...) -> Optional[str]. Tiers run in
# the order chosen by `_gen_image_chain()` below.
_GEN_ENGINES = {
    "playwright": ("gemini_bot",       "create_image_and_download"),
    "human":      ("gemini_bot_human", "create_image_and_download"),
    "api":        ("gemini_api",       "create_image_and_download"),
}

def _resolve_pipeline_choice() -> str:
    """Read the runtime pipeline preference. Env var beats config file
    so an operator can force a choice without editing config. Validated
    against the known engine names; falls back to 'playwright' for any
    unknown value (preserves the old default behavior)."""
    env = os.environ.get("LOTUS_GEN_IMAGE_PIPELINE", "").strip().lower()
    if env in _GEN_ENGINES:
        return env
    try:
        import lotus_config as _lc
        choice = (_lc.load().get("image", {}).get("pipeline") or "").strip().lower()
        if choice in _GEN_ENGINES:
            return choice
    except Exception:
        pass
    return "playwright"


def _gen_image_chain(primary: str) -> list:
    """Return the engine name list, primary first then the rest in their
    natural fallback order. Keeps the original ordering for non-primary
    tiers so behavior matches what users have always seen."""
    natural = ["playwright", "human", "api"]
    chain = [primary] + [e for e in natural if e != primary]
    return chain


def _gen_image(prompt: str, target: str, verbose: bool = True) -> Optional[str]:
    """Single entry point for all image generation across the agent.

    Default priority: Playwright (gemini_bot) → human (gemini_bot_human) → API
    (gemini_api). The primary tier is configurable at runtime via:
      - dashboard / POST /api/config/pipeline   (preferred for live switching)
      - ~/.lotus_config.json `image.pipeline`   (persisted)
      - LOTUS_GEN_IMAGE_PIPELINE env var        (boot-time override)

    Each tier is tried in order; first success wins. The legacy `gemini`
    OCR path is intentionally not in the chain — same failure modes, adds
    minutes per failed frame.

    NOTE: grok_bot is NOT used as an image-gen fallback — Grok's role in
    this agent is image-to-VIDEO (see grok_video.py / A17), not image gen.
    """
    global _gen_api_plan_blocked

    primary = _resolve_pipeline_choice()
    chain = _gen_image_chain(primary)
    if verbose and primary != "playwright":
        print(f"[gen] pipeline override: {primary} (chain: {chain})")

    for engine in chain:
        # Skip the official API once we've seen a plan-level 429 — it'll
        # waste 30+s/frame retrying for the rest of the session.
        if engine == "api" and _gen_api_plan_blocked:
            continue
        mod_name, fn_name = _GEN_ENGINES[engine]
        try:
            mod = __import__(mod_name)
            fn = getattr(mod, fn_name)
            if engine == "api":
                # API path has an `is_available` gate to skip cleanly when
                # no key is configured — preserve that behavior.
                if hasattr(mod, "is_available") and not mod.is_available():
                    continue
            path = fn(prompt, target, verbose=verbose)
            if path:
                return path
            if verbose:
                print(f"[gen] {mod_name} returned no image")
            # If the API returned None, assume plan-quota and stop using it.
            if engine == "api":
                _gen_api_plan_blocked = True
                if verbose:
                    print("[gen] gemini_api plan-blocked — skipping it from now on")
        except Exception as e:
            if verbose:
                print(f"[gen] {mod_name} error: {e}")
            if engine == "api":
                _gen_api_plan_blocked = True

    if verbose:
        print("[gen] giving up on this frame — pipeline will continue")
    return None


# ═══════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Ollama model LOTUS uses for all local-brain calls. Read from
# ~/.lotus_config.json so `lotus-model switch` can change it without
# touching code. Env var OLLAMA_MODEL wins when set (used by the CLI
# for one-off overrides + by children pointed at a remote Ollama).
def _load_model() -> str:
    env = os.environ.get("OLLAMA_MODEL", "").strip()
    if env:
        return env
    try:
        import lotus_config as _lc
        return _lc.load().get("ollama", {}).get("model", "gemma4:e4b")
    except Exception:
        return "gemma4:e4b"
MODEL = _load_model()
WAKE_WORD = "lotus"
TTS_VOICE = "en-IN-PrabhatNeural"   # Indian English male

# Whisper mishears "Lotus" as various close words; accept any of these as the
# wake trigger. Google STT usually gets it right but we keep the list for
# when we fall back to Whisper or when the user speaks quickly/quietly.
WAKE_ALIASES = (
    "lotus", "lotas", "lotos", "lotis", "lottos",
    "notus", "notice", "noticed", "notes",
    "loadus", "load us", "let us", "lettuce", "lattice",
    "lotto", "lotus's",
)

IMAGE_PROJECT_DIR = "~/LotusAgent/Projects/voice"

# Keyword-based intent router. Ordered longest-first so "make an image" matches
# before "image" alone when we extract the residual prompt.
IMAGE_TRIGGERS = (
    "generate an image of",
    "generate image of",
    "create an image of",
    "create image of",
    "make an image of",
    "make image of",
    "draw a picture of",
    "picture of",
    "image of",
    "generate image",
    "create image",
    "make image",
    "draw me",
    "draw",
)


# STT sometimes runs action verbs into their targets (e.g. Google returns
# "opencloud" when user says "open cloud"). This pre-processor splits common
# compound forms before Gemma sees them, so it parses the intended action.
_STT_COMPOUND_RE = re.compile(
    r"\b(open|close|launch|start|create|take|display|show|view|quit|press|scroll|type)"
    r"([a-z]{3,20})\b",
    re.IGNORECASE,
)


_STT_COMPOUND_TARGETS = {
    "screenshot", "image", "window", "tab", "link", "url", "preview",
    "reel", "picture", "photo", "file", "folder", "app",
}


def _split_stt_compounds(text: str) -> str:
    def _sub(m):
        suffix = m.group(2).lower()
        if suffix in _STT_COMPOUND_TARGETS or suffix in _APP_ALIASES:
            return f"{m.group(1)} {m.group(2)}"
        return m.group(0)   # leave untouched (e.g. "opening", "closed")
    return _STT_COMPOUND_RE.sub(_sub, text)


# ── Tool intents (actionable commands that use tools.py) ─────────────────
# These bypass Gemma and call the ToolExecutor directly so the agent actually
# performs the action instead of just chatting about it.

_OPEN_RE = re.compile(
    r"\b(?:open|launch|start|run)\s+(?:the\s+)?(.+?)\s*(?:please|now|for\s+me)?\s*$",
    re.IGNORECASE,
)
# "take a screenshot" triggers capture. Order matters: this is checked BEFORE
# _SHOW_RE so "take a screenshot" doesn't route to show.
_SCREENSHOT_RE = re.compile(
    r"\b(?:take|capture|grab|make)\s+(?:a\s+|the\s+)?"
    r"(?:screenshots?|screen\s*shot|screen(?:\s+capture)?)\b",
    re.IGNORECASE,
)
# "show / display / view / preview / open the <media>" → open in default
# image viewer (Preview.app on macOS). Must be checked BEFORE _OPEN_RE so
# "open screenshot" doesn't try to launch a "screenshot" application.
_SHOW_MEDIA_RE = re.compile(
    r"\b(?:show|display|view|preview|open)\s+(?:me\s+)?(?:the\s+)?"
    r"(?:(?:last|latest|recent|newest|previous)\s+)?"
    r"(screenshots?|screen|images?|photos?|pictures?|post\s*\d+|post\d+|reel)\b",
    re.IGNORECASE,
)
# Close / quit — close the current window (Cmd+W) or quit the active app (Cmd+Q)
_CLOSE_RE = re.compile(
    r"\b(close|quit)\b\s*(window|tab|app|application|it|this)?",
    re.IGNORECASE,
)
# "type hello world" — types at current cursor
_TYPE_RE = re.compile(r"\btype\s+(.+)$", re.IGNORECASE)
# "press command c", "press enter", "hit return", "press cmd shift d"
_HOTKEY_RE = re.compile(
    r"\b(?:press|hit|tap)\s+((?:command|cmd|control|ctrl|option|opt|alt|shift|fn)"
    r"(?:\s+(?:command|cmd|control|ctrl|option|opt|alt|shift|fn|[a-z0-9]+))+|[a-z]+|return|enter|escape|esc|space|tab|delete|backspace)",
    re.IGNORECASE,
)
# "scroll up / down [by N]"
_SCROLL_RE = re.compile(r"\bscroll\s+(up|down)(?:\s+(?:by\s+)?(\d+))?", re.IGNORECASE)


# macOS app name resolution: voice-friendly → proper bundle name.
# `open -a chrome` fails because Apple doesn't fuzzy-match; we need
# "Google Chrome". Aliases map common spoken names to installed names.
_APP_ALIASES = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "browser": "Google Chrome",
    "safari": "Safari",
    "firefox": "Firefox",
    "edge": "Microsoft Edge",
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "code": "Visual Studio Code",
    "cursor": "Cursor",
    "claude": "Claude",
    "cloud": "Claude",          # Whisper/Google often mishears "Claude" as "cloud"
    "clawed": "Claude",
    "claude.ai": "Claude",
    "anthropic": "Claude",
    "chatgpt": "ChatGPT",
    "gpt": "ChatGPT",
    "chat gpt": "ChatGPT",
    "gemini app": "Gemini",
    "terminal": "Terminal",
    "iterm": "iTerm",
    "finder": "Finder",
    "notes": "Notes",
    "music": "Music",
    "spotify": "Spotify",
    "photos": "Photos",
    "mail": "Mail",
    "messages": "Messages",
    "facetime": "FaceTime",
    "calendar": "Calendar",
    "calculator": "Calculator",
    "slack": "Slack",
    "discord": "Discord",
    "zoom": "zoom.us",
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
    "figma": "Figma",
    "blender": "Blender",
    "capcut": "CapCut",
    "davinci": "DaVinci Resolve",
    "photoshop": "Adobe Photoshop",
    "premiere": "Adobe Premiere Pro",
    "after effects": "Adobe After Effects",
    "illustrator": "Adobe Illustrator",
    "settings": "System Settings",
    "system settings": "System Settings",
    "preferences": "System Settings",
    "system preferences": "System Settings",
    "activity monitor": "Activity Monitor",
    "preview": "Preview",
    "ollama": "Ollama",
}


def _chrome_last_profile() -> Optional[str]:
    """Return last-used Chrome profile directory name (e.g. 'Default' or
    'Profile 1'), so we can launch Chrome directly into it instead of
    showing the profile picker."""
    import json
    p = os.path.expanduser("~/Library/Application Support/Google/Chrome/Local State")
    try:
        with open(p, "r") as f:
            data = json.load(f)
        last = data.get("profile", {}).get("last_used")
        if last:
            return last
        cache = data.get("profile", {}).get("info_cache", {}) or {}
        if cache:
            return next(iter(cache.keys()))
    except Exception:
        pass
    return None


def _resolve_app_name(name: str) -> list:
    """Return candidate app names to try, in priority order."""
    raw = name.strip().lower()
    candidates: list = []
    if raw in _APP_ALIASES:
        candidates.append(_APP_ALIASES[raw])
    candidates.append(name.strip())                # as-spoken
    candidates.append(name.strip().title())        # Title Case
    # Glob fallback — e.g. "Adobe Premiere Pro 2024" when alias is "Adobe Premiere Pro"
    import glob as _glob
    for base in list(candidates):
        matches = sorted(_glob.glob(f"/Applications/{base}*.app"))
        for m in matches:
            app_name = os.path.splitext(os.path.basename(m))[0]
            if app_name not in candidates:
                candidates.append(app_name)
    return candidates


def _normalize_hotkey(phrase: str) -> list:
    """Map voice-friendly modifier names to pyautogui keys."""
    tokens = phrase.lower().split()
    aliases = {
        "command": "command", "cmd": "command",
        "control": "ctrl", "ctrl": "ctrl",
        "option": "alt", "opt": "alt", "alt": "alt",
        "shift": "shift",
        "return": "enter", "enter": "enter",
        "escape": "esc", "esc": "esc",
        "backspace": "backspace", "delete": "delete",
        "space": "space", "tab": "tab",
        "up": "up", "down": "down", "left": "left", "right": "right",
    }
    return [aliases.get(t, t) for t in tokens]
_SYSINFO_RE = re.compile(
    r"\b(system\s+info|how\s+much\s+(?:ram|memory|cpu|disk)|system\s+status)\b",
    re.IGNORECASE,
)
_OPEN_URL_RE = re.compile(
    r"\b(?:open|go\s+to|visit)\s+(https?://\S+|\S+\.(?:com|org|net|io|ai|co|in|dev)\S*)",
    re.IGNORECASE,
)


def _extract_tool_intent(text: str) -> Optional[tuple]:
    """Detect simple action commands. Returns (tool_name, params_dict) or None."""
    t = text.strip()

    m = _SCREENSHOT_RE.search(t)
    if m:
        return ("screenshot", {})

    m = _SHOW_MEDIA_RE.search(t)
    if m:
        return ("show_media", {"target": m.group(1).strip().lower().replace(" ", "")})

    m = _SYSINFO_RE.search(t)
    if m:
        return ("get_system_info", {})

    # "close window" / "quit" — route BEFORE open so 'close chrome' isn't opened
    m = _CLOSE_RE.search(t)
    if m:
        verb = m.group(1).lower()
        target = (m.group(2) or "").lower()
        if verb == "quit" or target in ("app", "application"):
            return ("hotkey", {"keys": "command+q"})
        # close window / tab / this
        return ("hotkey", {"keys": "command+w"})

    m = _SCROLL_RE.search(t)
    if m:
        direction = m.group(1).lower()
        amount = int(m.group(2)) if m.group(2) else 5
        return ("scroll", {"direction": direction, "amount": amount})

    m = _HOTKEY_RE.search(t)
    if m:
        phrase = m.group(1)
        keys = _normalize_hotkey(phrase)
        return ("hotkey", {"keys": "+".join(keys)})

    m = _OPEN_URL_RE.search(t)
    if m:
        return ("open_url", {"url": m.group(1)})

    m = _TYPE_RE.search(t)
    if m:
        # Only trigger if the word "type" is at the very start (user clearly
        # commanded typing, not describing something).
        if t.lower().lstrip().startswith("type "):
            return ("type_text", {"text": m.group(1).strip()})

    m = _OPEN_RE.search(t)
    if m:
        target = m.group(1).strip(" .,!?")
        if target and len(target.split()) <= 5:
            return ("open_app", {"app_name": target})

    return None


def _extract_image_prompt(text: str) -> Optional[str]:
    """If `text` is an image-generation request, return the prompt body.

    Returns the text that comes AFTER the matched trigger, or the full text
    when the trigger has no tail (e.g. user says just 'make image').
    Returns None when the text is not an image request.
    """
    low = text.lower()
    for trig in IMAGE_TRIGGERS:
        idx = low.find(trig)
        if idx >= 0:
            tail = text[idx + len(trig):].strip(" ,.:;-—")
            return tail if tail else text
    return None


# ── Image preferences ──────────────────────────────────────────────────────
# File-layout convention (see lotus_config.py):
#   ~/LotusAgent/Projects/MMDDYY/post1.png, post2.png, ..., reel.mp4
# Parent folder auto-rotates by date. Child numbering is filesystem-inferred.
# Most users never need to configure anything. Commands supported:
#   "show image settings"              → describe current layout
#   "reset image settings"             → back to defaults
#   "save projects in <path>"          → change the projects root
#   "call images <prefix>"             → rename 'post' → something else
#   "call reel <name>"                 → rename 'reel'

_CFG_SHOW_KEYWORDS = (
    "show image settings", "show image config",
    "image settings", "image config",
    "what are my image", "what is my image",
)
_CFG_RESET_KEYWORDS = (
    "reset image settings", "reset image config",
    "clear image settings", "forget image settings",
)

_CFG_ROOT_RE = re.compile(
    r"(?:save|put|store)\s+projects?\s+(?:in|to|at)\s+(\S+)",
    re.IGNORECASE,
)
_CFG_PREFIX_RE = re.compile(
    r"(?:call|rename|name)\s+images?\s+([a-zA-Z][\w\-]*)",
    re.IGNORECASE,
)
_CFG_REEL_RE = re.compile(
    r"(?:call|rename|name)\s+(?:the\s+)?reel\s+([a-zA-Z][\w\-]*)",
    re.IGNORECASE,
)


def _classify_config_intent(text: str) -> Optional[str]:
    t = text.lower()
    if any(k in t for k in _CFG_RESET_KEYWORDS):
        return "reset"
    if any(k in t for k in _CFG_SHOW_KEYWORDS):
        return "show"
    if _CFG_ROOT_RE.search(text) or _CFG_PREFIX_RE.search(text) or _CFG_REEL_RE.search(text):
        return "set"
    return None


SYSTEM_PROMPT = """You are LOTUS — a voice AI assistant for Somendra. LOTUS is the
front-desk interface; YOU (Gemma) are the brain that decides what to do.

YOUR JOB:
- For action commands, CALL TOOLS. Don't describe what you would do.
- For conversational / informational / creative questions, reply with text.
- After a tool returns a result, give a SHORT spoken confirmation
  (≤1 sentence, no markdown, no emojis).
- For multi-step requests, CHAIN tools: call one, wait for the result,
  then call the next. e.g. "open Claude and start a new chat" means:
  call open_app("Claude"), then click_on_text("New chat").

TOOL CHAINING EXAMPLES:
- "Open Chrome and go to instagram"
  → open_app("chrome") → open_url("instagram.com")
- "Open Claude and start a new chat"
  → open_app("Claude") → click_on_text("New chat")
- "Take a screenshot and show it"
  → take_screenshot() → show_media("screenshot")
- "Generate Post1 Frame1 of a mountain at sunset"
  → create_image(prompt="mountain at sunset", section="Post1", frame_number=1)
- "Switch to Post2 and generate a frame of a lotus flower"
  → set_active_section("Post2") → create_image(prompt="lotus flower")
- "Generate Story1 of Agni missile launch"
  → create_image(prompt="Agni missile launch", section="Story1")
- "Read the prompts file and generate all frames into Reel"
  → read_prompts() → generate_all_frames(section="Reel")

CONTENT PIPELINE (this user's @techengine.lab workflow):
Parent folder is auto-named <brand><MMDDYYYY> e.g. Techengine04182026.
Sections inside the parent:
  - Multi-frame subfolders: Post1, Post2, Post3, Post4, Post5, Reel
    (each contains Frame1.png, Frame2.png, …)
  - Single-file slots: ReelCover, Story1, Story2, Story3
  - prompts.md — Claude-generated list of NB2 image prompts
When the user says "Post 1 frame 2" parse section="Post1", frame_number=2.
When they say "story 3" parse section="Story3".
When they say "reel cover" parse section="ReelCover".

PIPELINE VOICE CONTROL (when a pipeline is active):
- "start pipeline <name>"              → start_pipeline(name="<name>")
- "approve frame 1" / "approve f1"      → pipeline_approve_frames(frame_ids=["f1"])
- "approve all" / "approve all frames"  → pipeline_approve_frames(frame_ids="all")
- "deny frame 2" / "reject frame 2"     → pipeline_deny_frames(frame_ids=["f2"])
- "regenerate frame 3"                   → pipeline_regenerate_frame(frame_id="f3")
- "save to Post1" / "save in Reel"       → pipeline_save(section="Post1")
- "cancel pipeline"                      → pipeline_cancel()  (graceful — waits for current frame)
- "emergency stop" / "kill all" / "stop everything" / "abort" / "panic" /
  "stop now" → emergency_stop()  (immediate — sets stop flag + closes the
  Gemini Chrome tab so any blocking call unblocks instantly)

PROMPT REVIEW is AUTOMATIC — Gemma auto-approves all prompts on load.
Only frame review and save destination require user voice input.

VOICE RECORDER:
- "start recording" / "record the room" / "record audio" → invoke the
  dashboard rec_start direct_action (you can't do this via tools; just
  acknowledge "recording started" — the user will tap the button, or
  surface intent in your reply).
- "stop recording" / "stop rec" → same pattern, acknowledge "stopped".
- "play last recording" / "play the recording" / "play what you heard" →
  say "playing the last recording" — the UI will handle playback.

RESEARCH / BRAINSTORM / ASK:
- "latest news" / "what's trending" / "news headlines" → get_news(topic="trending")
- "tech news" → get_news(topic="tech"), "sports news" → get_news(topic="sports")
- "analyze this news headline: \"X\"" / "analyse X" / "process this headline" /
  "more details on headline 3" / "explain this news" / "deep dive on X" /
  "break it down" / "what's the best take" → analyze_news(title=<exact
  headline>, url=<its url if supplied>, question=<user's extra ask if any>).
  The local Gemma brain returns SUMMARY / KEY FACTS / WHY IT MATTERS / MY
  TAKE. After the tool returns, read MY TAKE aloud and ask what the user
  wants next. If the dashboard sent a button-style command that contains
  "analyze" followed by a quoted title, ALWAYS route to analyze_news with
  that exact title — do not respond in plain text.
- "review posts" / "review my posts" / "walk me through the posts" /
  "review post" / "show me the posts" / "go through posts" →
  review_posts(). Posts come pre-trusted from Claude; there is NO prompt-
  approval step. The review is informational — analysis + my take.
  "next post" / "next one" / "skip this post" → next_post().
  "previous post" / "go back" → previous_post().
- "redo" / "redo this" / "improve this prompt" / "make it better" /
  "rewrite for better generation" / "refine the prompt" → improve_post_prompt().
  Gemma produces a stronger NB2 prompt as a proposal; side-by-side with
  the original. User can accept/reject/redo-again.
- "accept" / "use the improved one" / "yes swap it in" → accept_improved_prompt().
- "reject" / "keep the original" → reject_improved_prompt().
- "generate this" / "generate image" / "approve and generate" / "make the
  image" / "create it" / "go ahead" / "ship it" → generate_post_image().
  This IS the approval point — calling it means the user wants the current
  prompt (original or improved, whichever is active) rendered by create_image.
- "next news" / "next one" / "next news is good" / "next news good do
  research" / "skip this" / "move on to next" / "what about the next" /
  "process next" / "go to next" → next_news(). ALWAYS call next_news for
  any phrase that starts with "next" when a news list is active. Do NOT
  call get_news again unless the user asks for a FRESH batch or a
  different topic. If they say "next news is good, go deeper", call
  next_news(question="go deeper"). "previous" / "go back" → previous_news().
- Deep research, creative brainstorm, content ideas, essay help, comparisons,
  "give me reel suggestions", "what should I post about X", "research Y for me"
  → ask_claude(query="<the user's exact question>"). After the tool returns,
  read a one-sentence summary aloud AND ask the user what they think.
  Claude has a Pro subscription, so rely on it for anything heavier than a
  local lookup. The dashboard shows Claude's full reply with a 1-10 score.

RULES:
- Responses that will be spoken aloud: ≤1 sentence, plain prose.
- Never invent tool results; wait for the real tool output before replying.
- If the user's request is ambiguous, ask a single short clarifying question.
- App-name guessing is fine; open_app resolves aliases (chrome → Google
  Chrome, claude/cloud → Claude, vscode → Visual Studio Code).
- For UI interactions (clicking buttons, menus, sidebar items): use
  click_on_text with the VISIBLE label. Add a brief pause between opening
  an app and clicking inside it — the app needs a moment to render.

USER CONTEXT:
- Somendra runs @techengine.lab (defense/science content creator)
- Full-stack dev (.NET/C#, Umbraco, Kentico)
- Creative work in Adobe Premiere, After Effects, Blender
- Lives in Delhi, India"""


# ── Tool schemas exposed to Gemma via Ollama tool-calling ────────────────
# Gemma returns tool_calls; agent dispatches to python handlers below.

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "open_app",
        "description": "Open a macOS application by name. Accepts common names (chrome, vscode, whatsapp) — agent resolves to proper bundle name.",
        "parameters": {
            "type": "object",
            "properties": {"app_name": {"type": "string", "description": "Application name"}},
            "required": ["app_name"],
        },
    }},
    {"type": "function", "function": {
        "name": "open_url",
        "description": "Open a URL in the default browser.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    }},
    {"type": "function", "function": {
        "name": "take_screenshot",
        "description": "Capture the current screen. File is saved into today's project folder and a thumbnail shown in the dashboard.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "show_media",
        "description": "Open a previously-saved file in the default image viewer (Preview). Target can be 'screenshot' (latest), 'image' (latest of any kind), 'post1' / 'post2' etc., or 'reel'.",
        "parameters": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    }},
    {"type": "function", "function": {
        "name": "get_system_info",
        "description": "Get current CPU, RAM, disk usage, and top processes.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "press_keys",
        "description": "Press a keyboard shortcut (e.g. 'command+c', 'enter', 'command+w' to close window, 'command+q' to quit).",
        "parameters": {
            "type": "object",
            "properties": {"keys": {"type": "string", "description": "Keys joined with '+'"}},
            "required": ["keys"],
        },
    }},
    {"type": "function", "function": {
        "name": "scroll",
        "description": "Scroll the active window up or down.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "amount": {"type": "integer", "description": "Scroll clicks (default 5)"},
            },
            "required": ["direction"],
        },
    }},
    {"type": "function", "function": {
        "name": "type_text",
        "description": "Type text at the current cursor position in the focused window.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }},
    {"type": "function", "function": {
        "name": "create_image",
        "description": (
            "Generate a full-resolution image via Gemini (~45s). Saves into the "
            "parent folder (e.g. Techengine04182026). Section controls where: "
            "'Post1'..'Post5' or 'Reel' → saved as FrameN in that subfolder; "
            "'ReelCover' / 'Story1' / 'Story2' / 'Story3' → saved as single "
            "file in the parent. If section is omitted, uses active section. "
            "frame_number is optional (defaults to next available)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What to generate"},
                "section": {
                    "type": "string",
                    "description": "Post1, Post2, ..., Reel, ReelCover, Story1, Story2, Story3",
                },
                "frame_number": {
                    "type": "integer",
                    "description": "Explicit frame number; omit to use next available",
                },
            },
            "required": ["prompt"],
        },
    }},
    {"type": "function", "function": {
        "name": "set_active_section",
        "description": "Switch the default section new images go into (Post1, Post2, Reel, ...).",
        "parameters": {
            "type": "object",
            "properties": {"section": {"type": "string"}},
            "required": ["section"],
        },
    }},
    {"type": "function", "function": {
        "name": "read_prompts",
        "description": "Read the prompts.md file in today's parent folder and return it as a list of individual prompt strings.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "start_pipeline",
        "description": (
            "Start a named content pipeline. Prompts are auto-approved by the "
            "agent and generation begins immediately. Generated frames pause "
            "for voice approval, then user picks a save destination."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pipeline name, e.g. 'Agni Launch'"},
                "source_md": {"type": "string", "description": "Path to prompts.md (optional)"},
            },
            "required": [],
        },
    }},
    {"type": "function", "function": {
        "name": "pipeline_approve_frames",
        "description": "Approve one or more generated frames. Pass frame_ids ['f1','f2'] or 'all'.",
        "parameters": {
            "type": "object",
            "properties": {
                "frame_ids": {
                    "description": "List of frame ids (e.g. ['f1','f3']) or the string 'all'",
                },
            },
            "required": ["frame_ids"],
        },
    }},
    {"type": "function", "function": {
        "name": "pipeline_deny_frames",
        "description": "Deny one or more frames (marks them skipped; they won't be saved).",
        "parameters": {
            "type": "object",
            "properties": {"frame_ids": {"description": "List of ids or 'all'"}},
            "required": ["frame_ids"],
        },
    }},
    {"type": "function", "function": {
        "name": "pipeline_regenerate_frame",
        "description": "Re-generate a specific frame using its original prompt.",
        "parameters": {
            "type": "object",
            "properties": {"frame_id": {"type": "string"}},
            "required": ["frame_id"],
        },
    }},
    {"type": "function", "function": {
        "name": "pipeline_save",
        "description": "Save all approved frames to a named destination section (Post1..Post5, Reel, ReelCover, Story1..Story3).",
        "parameters": {
            "type": "object",
            "properties": {"section": {"type": "string"}},
            "required": ["section"],
        },
    }},
    {"type": "function", "function": {
        "name": "pipeline_cancel",
        "description": "Cancel the active pipeline. Cooperative — in-flight image generation finishes (~30-45s) then the loop exits. For immediate kill use emergency_stop.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "emergency_stop",
        "description": (
            "EMERGENCY KILL — stop every running pipeline task, kill the "
            "Gemini Chrome tab if image generation is mid-flight, and clear "
            "the active pipeline. Use for 'emergency stop', 'kill all', "
            "'stop everything now', 'abort', 'panic'. No confirmation needed "
            "— the user is saying this IS the confirmation."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_news",
        "description": "Fetch top trending news headlines from Google News RSS. Use for 'what's the news', 'latest news', 'trending news', 'show me news'. The dashboard shows them as cards AND you should read the top 3 aloud.",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "One of: trending, india, world, tech, business, sports. Default: trending."},
                "count":  {"type": "integer", "description": "How many headlines to show (1-10, default 5)."},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "review_posts",
        "description": (
            "Walk through today's posts (the prompts in prompts.md OR the "
            "active pipeline's prompts) one at a time, running a Gemma review "
            "on each. Use for 'review posts', 'review my posts', 'review "
            "post', 'show posts', 'let's review the posts', 'walk me through "
            "the posts'. Kicks off the walk; Gemma analyses the FIRST post "
            "immediately and reads aloud. Subsequent items: next_post()."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Optional: 'pipeline' or 'prompts_md'. Default auto-picks."},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "generate_post_image",
        "description": (
            "Approve a post and GENERATE the image from its current prompt. "
            "This is the image-gen approval point — prompts come pre-trusted "
            "from Claude, so no prompt-approval step is required; approval "
            "is implicit when the user asks to generate. Use for 'generate "
            "this', 'generate image', 'make the image', 'create it', 'go "
            "ahead', 'approve and generate'. Uses the CURRENT prompt in the "
            "review list (original OR improved, whichever is active)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "index":   {"type": "integer", "description": "Optional 1-based post index. Defaults to the cursor."},
                "section": {"type": "string",  "description": "Optional target section (Post1, Reel, Story1, …). Defaults to the active section."},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "improve_post_prompt",
        "description": (
            "Rewrite the CURRENT post prompt into a stronger NB2 / Gemini "
            "image-gen prompt. Use for 'redo this', 'redo the prompt', "
            "'improve this prompt', 'make it better', 'refine the prompt', "
            "'rewrite for better generation'. Gemma produces a richer prompt "
            "(camera + lighting + mood + specific details) and broadcasts a "
            "side-by-side card so the user can Accept / Reject / Regenerate. "
            "If accepted, the updated prompt becomes the active one for "
            "regeneration and further walks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Optional 1-based index to target a specific post. Defaults to the current cursor."},
                "focus": {"type": "string", "description": "Optional direction like 'more cinematic', 'tighter framing', 'sharper subject'."},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "accept_improved_prompt",
        "description": "Accept the last improved post prompt (swap it into the review list).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "reject_improved_prompt",
        "description": "Reject the last improved post prompt (keep the original).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "next_post",
        "description": (
            "Advance to the NEXT post in the review walk started by "
            "review_posts and run a fresh Gemma analysis. Use for 'next post', "
            "'next one', 'move to next', 'skip this post'."
        ),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "previous_post",
        "description": "Step back one post in the review walk.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "next_news",
        "description": (
            "Advance to the NEXT headline in the last news list (from get_news) "
            "and run the same deep analysis — fetches the article, runs Gemma, "
            "reads MY TAKE aloud, asks for opinion. Use for 'next news', 'next "
            "one', 'next news is good', 'skip this one', 'move on to next', "
            "'process the next headline', 'what about the next'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Optional focus question for the next item."},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "previous_news",
        "description": "Go back to the previous headline in the last news list and re-analyze it. Use for 'previous', 'go back', 'the one before'.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "analyze_news",
        "description": (
            "Process a news article with the LOCAL Gemma brain and return a "
            "structured analysis: SUMMARY, KEY FACTS, WHY IT MATTERS, and MY "
            "TAKE. Use for ALL of: 'analyze', 'analyse', 'analyze this news', "
            "'analyze this headline', 'deep analyze', 'process this news', "
            "'process this headline', 'more details on headline N', 'give "
            "me deep analysis on X', 'deep dive', 'explain this news', 'what "
            "does this mean', 'break it down', or anything that refers to a "
            "specific stored headline by title or number. If the user passes "
            "a URL, include it. The dashboard shows the analysis as a card."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title":    {"type": "string", "description": "Headline shown in the news card."},
                "url":      {"type": "string", "description": "Article URL (optional but preferred — enables full-text fetch)."},
                "question": {"type": "string", "description": "Optional extra question the user wants answered about this article."},
            },
            "required": ["title"],
        },
    }},
    {"type": "function", "function": {
        "name": "ask_claude",
        "description": (
            "Route a research, brainstorm, creative, or 'give me suggestions' question to the "
            "native Claude.app on macOS (user has a Pro subscription; the app is already signed "
            "in). Claude's answer is captured via screen OCR, scored by Gemma 1-10, displayed "
            "on the dashboard as a card. Read a short summary aloud then ASK the user what they "
            "think. Use for reel ideas, deep research, comparisons, essays, long explanations — "
            "anything beyond local tool use."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The full question to send to Claude."},
            },
            "required": ["query"],
        },
    }},
    {"type": "function", "function": {
        "name": "show_today",
        "description": "Show the user everything produced today: list files in today's parent folder (Techengine<MMDDYYYY>/) grouped by section, and refresh the dashboard's Today gallery. Use when the user asks for 'today', 'what did we make today', 'show output', 'gallery'.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "open_projects_folder",
        "description": "Open the LOTUS Projects root folder in macOS Finder so the user can browse saved frames. Use for 'open projects', 'show me the folder', 'open finder'.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "generate_all_frames",
        "description": "Generate an image for every prompt in prompts.md, saving sequentially as FrameN in the given section. Each image takes ~45s.",
        "parameters": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "Target section (Post1, Reel, ...). Defaults to active section."},
                "start_frame": {"type": "integer", "description": "Starting FrameN (default 1 or next available)"},
            },
            "required": [],
        },
    }},
    {"type": "function", "function": {
        "name": "set_preference",
        "description": "Change a saved user preference (projects root, image prefix, reel name). Pass raw natural-language text like 'save projects in ~/X' or 'call images shot'.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }},
    {"type": "function", "function": {
        "name": "show_preferences",
        "description": "Describe current image/project preferences.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "focus_app",
        "description": "Bring an already-running app to the foreground (does NOT launch it).",
        "parameters": {
            "type": "object",
            "properties": {"app_name": {"type": "string"}},
            "required": ["app_name"],
        },
    }},
    {"type": "function", "function": {
        "name": "click_on_text",
        "description": "Find visible on-screen text (menu item, button label, sidebar entry) via OCR and click its centre. Use the EXACT label as shown in the UI.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The visible text to click"},
            },
            "required": ["text"],
        },
    }},
    {"type": "function", "function": {
        "name": "wait",
        "description": "Pause briefly so a freshly-opened app has time to render before clicking inside it.",
        "parameters": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "description": "How long to wait (default 1.5)"}},
            "required": [],
        },
    }},
    {"type": "function", "function": {
        "name": "refresh_dashboard",
        "description": "Tell the dashboard to reload its HTML (picks up any UI changes). Use when user says 'refresh yourself', 'reload', 'update the dashboard'.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
]


_FOLDER_SANITIZE_RE = re.compile(r"[^a-z0-9]+")


def _sanitize_for_folder(s: str, max_len: int = 30) -> str:
    """Folder-safe slug: lowercase, strip everything except [a-z0-9],
    truncate. Used for the per-pipeline top-level folder name."""
    s = _FOLDER_SANITIZE_RE.sub("", (s or "").lower())
    return s[:max_len]


# Sentinel that distinguishes "caller didn't pass folder_name" (compute one
# from name+today) from "caller explicitly passed None" (legacy rehydrated
# row with no stored folder_name — keep as None and fall back to brand+date
# at save time). Without this, rehydration would silently migrate every
# pre-A10 pipeline to a new folder.
_FOLDER_NAME_UNSET = object()


class Pipeline:
    """In-memory state for the interactive content pipeline.

    Default behaviour: Gemma auto-approves all prompts — the user doesn't
    review prompts manually. After generation, frames DO wait for user
    approval (voice or dashboard). Save destination is also user-driven.
    """
    def __init__(self, pipeline_id: str, source_md: str, name: str = "",
                 folder_name=_FOLDER_NAME_UNSET):
        import uuid as _uuid
        import threading as _th
        import time as _time
        self.id = pipeline_id or _uuid.uuid4().hex[:8]
        raw_name = (name or "").strip()
        self.name = raw_name or f"Pipeline {self.id}"
        # Per-pipeline top-level folder name — `<sanitized>_<MMDDYYYY>` so
        # frames from different pipelines never overwrite each other (A10).
        # Computed once at creation; persists in pipelines.db. If a
        # rehydrated pipeline restored a folder_name from disk, use it
        # verbatim — don't recompute (the date inside is part of identity).
        # If folder_name is explicitly None (legacy DB row pre-A10), keep
        # it None so parent_folder() falls back to the brand+date folder.
        if folder_name is _FOLDER_NAME_UNSET:
            slug = _sanitize_for_folder(raw_name)
            today = datetime.now().strftime("%m%d%Y")
            if slug:
                self.folder_name: Optional[str] = f"{slug}_{today}"
            else:
                self.folder_name = f"pipeline{self.id}_{today}"
        else:
            self.folder_name = folder_name
        self.stage = "ask_location"
        self.source_md = source_md
        self.source_hash: Optional[str] = None  # SHA1 of source file — DB key
        self.prompts: list = []       # [{id, text, status}]
        self.frames: list = []        # [{id, prompt_id, prompt_text, temp_path, temp_url, status}]
        self.save_section: Optional[str] = None
        # Pause-on-failure state. The render loop blocks on _resume_event
        # after any post that had failed frames, so the user gets a chance
        # to resolve them (upload/skip) before the next post is sent.
        self.paused: bool = False
        self.pause_reason: Optional[str] = None
        self._resume_event = _th.Event()
        # Error log — every failure is appended here with a timestamp so
        # the dashboard can display a running list without digging through
        # mother.log. Bounded to last 500 entries.
        self.errors: list = []        # [{ts, frame_id, section, stage, message}]
        self.created_at = _time.time()
        # Multi-pipeline cancellation signal. Render loops check this each
        # iteration and bail out when True. Prefer this over identity checks
        # against `_active_pipeline` (which now shifts focus across many
        # running pipelines).
        self.cancelled: bool = False

    def parent_folder(self) -> str:
        """Absolute path to this pipeline's top-level project folder.
        Per-pipeline folder when `folder_name` is set (A10 pipelines).
        Falls back to brand+date when None — that's the legacy path used
        by pipelines that pre-date A10 (rehydrated with no stored
        folder_name) so we don't silently migrate their existing frames."""
        if self.folder_name:
            return lotus_config.parent_folder_for_pipeline(self.folder_name)
        return lotus_config.parent_folder_for()

    def as_event(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "folder_name": self.folder_name,
            "stage": self.stage,
            "source_md": self.source_md,
            "source_hash": self.source_hash,
            "prompts": self.prompts,
            "frames": self.frames,
            "save_section": self.save_section,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "errors": self.errors[-100:],   # last 100; UI paginates/filters
            "created_at": self.created_at,
            "cancelled": self.cancelled,
        }

    def summary(self) -> dict:
        """Lightweight representation for the pipeline-list broadcast —
        what the UI needs to render a tab label (id, name, stage, counts,
        is-paused, timestamp). Full state still goes through as_event()."""
        from collections import Counter as _Counter
        statuses = _Counter((f.get("status") or "") for f in self.frames)
        by_source = _Counter((f.get("source") or "") for f in self.frames)
        return {
            "id": self.id,
            "name": self.name,
            "stage": self.stage,
            "paused": self.paused,
            "cancelled": self.cancelled,
            "frame_count": len(self.frames),
            "prompt_count": len(self.prompts),
            "statuses": dict(statuses),
            "by_source": dict(by_source),
            "created_at": self.created_at,
            # source_md + source_hash are needed by lotus_pipelines_db.upsert
            # and by the A2 overview screen (basename for display, hash for
            # joining against prompts.db). Adding here keeps the broadcast
            # event self-contained.
            "source_md": self.source_md,
            "source_hash": self.source_hash,
            # A10 — per-pipeline folder name (renders save here instead of
            # the brand+date folder so multiple pipelines never overwrite).
            "folder_name": self.folder_name,
        }


class LotusPhase1:
    # Class-wide lock: Chrome automation (_gen_image)
    # clicks a specific tab. Two concurrent calls stomp on each other and
    # trigger FileNotFoundError when one's temp-file rename races the other.
    # Serialise.
    _gemini_lock = __import__("threading").Lock()

    def __init__(self, enable_dashboard: bool = True):
        self.voice = None
        self.history = []
        self._process_lock = __import__("threading").Lock()
        self._active_pipeline: Optional[Pipeline] = None
        # Multi-pipeline registry. `_active_pipeline` is the currently
        # focused one (what new commands target + what the main UI tab
        # shows). Others keep running on their own threads; the UI has a
        # tab per entry so you can switch focus at will.
        self._pipelines: dict = {}
        # Rehydrate any non-terminal pipelines from disk so they survive
        # restarts. Render-loop threads do NOT come back automatically —
        # rehydrated pipelines show as paused with a 'restored from previous
        # session' reason; the user resumes them explicitly via the UI.
        self._rehydrate_pipelines_from_db()

        # News cursor: walks through the last get_news() result so voice
        # commands like "next news" and "previous" advance through the feed.
        self._last_news_items: list = []
        self._last_news_topic: str  = "trending"
        self._news_cursor: int      = -1

        # Post-review cursor: mirrors news, but walks prompts.md / pipeline
        # prompts one at a time so Gemma can review each post individually.
        self._post_review_items: list = []
        self._post_review_source: str = "prompts_md"
        self._post_review_cursor: int = -1
        # Latest Gemma-rewritten prompt awaiting accept/reject. Kept in
        # memory only; accept splices it into _post_review_items.
        self._pending_improvement: Optional[dict] = None

        # Cooperative-cancel flag for any long-running generation loop.
        # Set by pipeline_cancel / emergency_stop, cleared when a new
        # pipeline starts. Gen loops check between frames; emergency_stop
        # also closes the Chrome tab running Gemini to interrupt mid-frame.
        import threading as _th
        self._stop_event: _th.Event = _th.Event()

        self._verify_ollama()

        # Voice authentication (resemblyzer voiceprint)
        self.auth = None
        # Grace-period session so a verified speaker doesn't need to re-auth
        # on every command. Set to 5 min; resets on each successful verify.
        self._auth_session_user: Optional[str] = None
        self._auth_session_until: float = 0.0
        self._auth_session_seconds: int = 300
        try:
            from auth import VoiceAuth
            self.auth = VoiceAuth()
            if not self.auth.is_enrolled:
                print("⚠️  No voiceprint enrolled — voice will run without auth.")
                print("    Enroll with: python enroll.py <name>")
            else:
                print(f"🔐 Voiceprint ready — authorized users: {', '.join(self.auth.enrolled_users)}")
        except Exception as e:
            print(f"⚠️  VoiceAuth unavailable ({e}); voice will run without auth.")

        # Dashboard (WebSocket + HTTP) for browser UI
        self.dashboard = None
        if enable_dashboard:
            from server import DashboardServer
            self.dashboard = DashboardServer()
            self.dashboard.on_command = self._on_dashboard_command
            self.dashboard.on_pipeline = self._on_dashboard_pipeline
            self.dashboard.on_direct_action = self._on_dashboard_direct_action
            self.dashboard.on_frame_upload = self._pipeline_frame_upload
            # /api/queue snapshot — gives the Queue tab live access to
            # frames + pending-approved prompts across every pipeline.
            self.dashboard.queue_snapshot_fn = self._build_queue_snapshot
            self.dashboard.start()
            # Initial broadcast so the dashboard's pipeline_list cache
            # is primed with whatever was rehydrated from disk. Without
            # this, the All-Pipelines overview cards stay non-clickable
            # after a fresh boot until something else triggers a state
            # change.
            try:
                self._pipeline_broadcast()
            except Exception as e:
                print(f"[boot] initial pipeline broadcast failed: {e}",
                      flush=True)

        # Agent mesh — specialists that LOTUS orchestrates. ReviewAgent
        # auto-runs after every saved frame; up to 3 auto-retries on reject.
        # ArchiverAgent auto-runs on stage=done to freeze the run as a report.
        try:
            import agents as _agents_module
            self._agents = _agents_module.default_registry
            print(f"[boot] agent mesh loaded: "
                  f"{[a['name'] for a in self._agents.health_snapshot()]}")
        except Exception as e:
            self._agents = None
            print(f"[boot] agent mesh unavailable ({e}) — pipeline will run without ReviewAgent")

        # Register remote children (Phase D2). Env var is a comma-separated
        # list of child URLs with an `agents=` query parameter listing which
        # agents that child hosts. Example:
        #   LOTUS_CHILDREN="ws://localhost:8770?agents=ReviewAgent"
        #   LOTUS_CHILDREN="ws://mac-2.local:8770?agents=ReviewAgent,ParserAgent"
        if self._agents is not None:
            self._link_configured_children()
            # Phase D3: mDNS auto-discovery. Env var takes precedence —
            # anything advertised via mDNS is registered alongside it.
            self._mdns_browser = None
            self._start_mdns_browser()

        # Controller heartbeat — supervisor thread that watches the mesh for
        # trouble and broadcasts controller_alert events to the dashboard.
        # Starts only if the mesh is available.
        if self._agents is not None:
            import threading as _th
            self._controller_stop = _th.Event()
            self._controller_alert_dedup: dict = {}   # key → last-emit ts
            _th.Thread(
                target=self._controller_loop,
                daemon=True,
                name="lotus-controller",
            ).start()
            print(f"[boot] controller heartbeat started (10s poll)")

    def _broadcast(self, **fields) -> None:
        if self.dashboard:
            fields.setdefault("timestamp", datetime.now().isoformat())
            self.dashboard.broadcast(fields)

    def _on_dashboard_command(self, text: str) -> None:
        """Called (on a worker thread) when dashboard sends a user_command.
        Dashboard-driven replies also speak through edge-tts so Somendra
        hears them regardless of whether he typed or spoke the command."""
        try:
            # Deterministic voice-command router for the audio recorder —
            # these need to fire a direct action (not bounce through Gemma)
            # so the REC indicator flips immediately.
            low = (text or "").strip().lower()
            REC_START_HITS = ("start recording", "start rec", "record the room",
                              "record audio", "begin recording")
            REC_STOP_HITS  = ("stop recording", "stop rec", "end recording")
            REC_PLAY_HITS  = ("play last recording", "play the recording",
                              "play recording", "play what you heard",
                              "play last record", "play the record")
            if any(h in low for h in REC_START_HITS):
                self._on_dashboard_direct_action("rec_start", {})
                try:
                    if self.voice: self.voice.speak("Recording started.")
                except Exception: pass
                return
            if any(h in low for h in REC_STOP_HITS):
                self._on_dashboard_direct_action("rec_stop", {})
                try:
                    if self.voice: self.voice.speak("Recording stopped.")
                except Exception: pass
                return
            if any(h in low for h in REC_PLAY_HITS):
                self._on_dashboard_direct_action("rec_play", {"name": "last"})
                try:
                    if self.voice: self.voice.speak("Playing the last recording.")
                except Exception: pass
                return
            response = self.process(text)
            if response:
                print(f"🌐 [dashboard] Lotus: {response}\n")
                try:
                    if getattr(self, "voice", None) is not None:
                        self.voice.speak(response)
                except Exception as e:
                    print(f"(dashboard TTS failed: {e})")
        except Exception as e:
            self._broadcast(type="agent_response", text=f"Error: {e}")

    def _on_dashboard_direct_action(self, action: str, payload: dict) -> None:
        """Route UI button clicks straight to their handlers — no Gemma
        tool-routing in the middle. Gemma is still used INSIDE each handler
        (keypoint extraction, analysis, scoring) so the 'local brain' work
        is preserved; only the dispatch is deterministic."""
        print(f"[direct] action={action!r} payload_keys={list(payload.keys())}", flush=True)
        try:
            if action == "analyze_news":
                result = self._tool_analyze_news(
                    title=payload.get("title", ""),
                    url=payload.get("url", ""),
                    question=payload.get("question", ""),
                )
            elif action == "next_news":
                result = self._tool_next_news(question=payload.get("question", ""))
            elif action == "previous_news":
                result = self._tool_previous_news()
            elif action == "child_ping":
                # Manual re-ping triggered from the UI's child drawer.
                # host_port is "ip:port" identifying the child. Pings every
                # RemoteAgent registered at that host, updates their ping
                # state, broadcasts the refreshed mesh snapshot.
                host_port = (payload.get("host_port") or "").strip()
                result = self._child_ping(host_port)
            elif action == "child_disconnect":
                # User-initiated disconnect — remove every RemoteAgent whose
                # ws_url matches the host. Parallel to an auto-deregister
                # after 3 failed probes, but instant.
                host_port = (payload.get("host_port") or "").strip()
                result = self._child_disconnect(host_port)
            elif action == "pipeline_cancel":
                result = self._tool_pipeline_cancel(payload.get("id") or payload.get("pipeline_id"))
            elif action == "pipeline_focus":
                # Multi-pipeline: UI sends {id} to switch which pipeline is
                # the subject of subsequent commands + shown in the main tab.
                fid = payload.get("id") or payload.get("pipeline_id")
                if fid and self._focus_pipeline(fid):
                    result = f"Focus switched to {fid}."
                else:
                    result = f"Could not focus pipeline {fid!r}."
            elif action == "start_pipeline":
                # Manual creation from the All-Pipelines overview button.
                # Name only — the new pipeline parks at ask_location, user
                # supplies the source MD via the existing SourceDropzone.
                nm = (payload.get("name") or "").strip()
                if not nm:
                    result = "start_pipeline: name required"
                else:
                    result = self._tool_start_pipeline(name=nm)
            elif action == "backfill_pipeline_folder":
                # A11 — give a legacy pipeline (no folder_name) a per-pipeline
                # folder going forward. Past renders stay in <brand><MMDDYYYY>;
                # only new renders get isolated. Uses the pipeline's
                # created_at date so the slug reflects when the pipeline
                # was made, not today.
                pid = payload.get("id") or payload.get("pipeline_id")
                if not pid or not hasattr(self, "_pipelines"):
                    result = "backfill_pipeline_folder: id required"
                else:
                    target = self._pipelines.get(pid)
                    if not target:
                        result = f"Pipeline {pid} not in live registry."
                    elif target.folder_name:
                        result = (f"Pipeline {pid} already has folder_name "
                                  f"{target.folder_name!r} — nothing to backfill.")
                    else:
                        slug = _sanitize_for_folder(target.name)
                        when = datetime.fromtimestamp(target.created_at) \
                               if target.created_at else datetime.now()
                        date_str = when.strftime("%m%d%Y")
                        target.folder_name = (f"{slug}_{date_str}" if slug
                                              else f"pipeline{target.id}_{date_str}")
                        try:
                            lotus_pipelines_db.upsert(target.summary())
                            result = (f"Backfilled folder_name = "
                                      f"{target.folder_name!r}. "
                                      f"Future renders will land there.")
                        except Exception as e:
                            target.folder_name = None  # rollback in-memory
                            result = f"Backfill DB write failed: {e}"
                        self._pipeline_broadcast()
            elif action == "delete_pipeline":
                # Permanent removal from pipelines.db (the All-Pipelines
                # trash button). Auto-cancels if still in the live registry
                # so the in-memory render thread bails before we wipe the
                # row. prompts.db rows are NOT touched — those belong to
                # source content and may be reused by a future pipeline
                # that loads the same source_hash.
                pid = payload.get("id") or payload.get("pipeline_id")
                if not pid:
                    result = "delete_pipeline: id required"
                else:
                    if hasattr(self, "_pipelines") and pid in self._pipelines:
                        self._cancel_pipeline(pid)
                    try:
                        lotus_pipelines_db.delete(pid)
                        result = f"Pipeline {pid} deleted."
                    except Exception as e:
                        result = f"Delete failed: {e}"
                    # Re-broadcast so the dashboard's live list drops it.
                    self._pipeline_broadcast()
            elif action == "emergency_stop":
                result = self._tool_emergency_stop()
            elif action == "set_projects_root":
                path = (payload.get("path") or "").strip()
                if not path:
                    result = "set_projects_root: path required"
                else:
                    try:
                        cfg = lotus_config.set_projects_root(os.path.expanduser(path))
                        self._broadcast(type="config_updated", config=cfg.get("image", {}))
                        result = (
                            f"Projects root updated to {cfg['image']['projects_root']}. "
                            f"Next parent folder will be created there."
                        )
                    except Exception as e:
                        result = f"Failed to set projects root: {e}"
            elif action == "get_config":
                cfg = lotus_config.load()["image"]
                self._broadcast(type="config_updated", config=cfg)
                result = f"Config: projects_root={cfg.get('projects_root')}"
            elif action == "rec_start":
                try:
                    import lotus_recorder
                    st = lotus_recorder.start()
                    self._broadcast(type="recorder_status", **st)
                    result = (f"Recording started → {os.path.basename(st.get('file') or '')}"
                              if st.get('active') else "Recording failed to start")
                except Exception as e:
                    result = f"Recording start failed: {e}"
            elif action == "rec_stop":
                try:
                    import lotus_recorder
                    st = lotus_recorder.stop()
                    self._broadcast(type="recorder_status", **st)
                    result = f"Recording stopped ({st.get('bytes', 0)//1024} KB)"
                except Exception as e:
                    result = f"Recording stop failed: {e}"
            elif action == "rec_status":
                try:
                    import lotus_recorder
                    st = lotus_recorder.status()
                    self._broadcast(type="recorder_status", **st)
                    result = f"Recording: active={st.get('active')}"
                except Exception as e:
                    result = f"Recording status failed: {e}"
            elif action == "rec_delete":
                name = (payload.get("name") or "").replace("/", "").replace("..", "")
                if not name or not name.endswith(".wav"):
                    result = "rec_delete: bad filename"
                else:
                    try:
                        import lotus_recorder
                        p = os.path.join(str(lotus_recorder._RECORDINGS_DIR), name)
                        if os.path.exists(p):
                            os.remove(p)
                            result = f"Deleted {name}"
                        else:
                            result = f"Not found: {name}"
                        self._broadcast(type="recordings_updated")
                    except Exception as e:
                        result = f"Delete failed: {e}"
            elif action == "rec_play":
                # Play a specific recording locally via `afplay` (macOS).
                # Used by voice trigger "play last recording".
                name = (payload.get("name") or "").replace("/", "").replace("..", "")
                try:
                    import lotus_recorder, subprocess
                    if name == "last" or not name:
                        files = lotus_recorder.list_recordings(limit=1)
                        if not files:
                            result = "No recordings yet"
                        else:
                            path = files[0]["path"]
                            subprocess.Popen(["afplay", path])
                            result = f"Playing {os.path.basename(path)}"
                    else:
                        path = os.path.join(str(lotus_recorder._RECORDINGS_DIR), name)
                        if not os.path.exists(path):
                            result = f"Not found: {name}"
                        else:
                            subprocess.Popen(["afplay", path])
                            result = f"Playing {name}"
                except Exception as e:
                    result = f"Play failed: {e}"
            elif action == "recover_history":
                save_dir = (payload.get("save_dir")
                            or os.path.join(lotus_config.parent_folder_for(), "_recovered"))
                max_n = int(payload.get("max") or 60)
                try:
                    import gemini_bot
                    out = gemini_bot.recover_from_history(save_dir,
                                                          max_recover=max_n,
                                                          verbose=True)
                    self._broadcast(type="today_refresh")
                    result = (f"Recovery: saved {len(out)} image(s) to {save_dir}"
                              if out else
                              f"Recovery: no images found in Gemini history")
                except Exception as e:
                    result = f"Recovery failed: {e}"
            elif action == "gemini_status":
                try:
                    import gemini_api
                    st = gemini_api.status()
                except Exception as e:
                    st = {"error": str(e)}
                self._broadcast(type="gemini_status", **st)
                result = f"Gemini: {st.get('api_keys_found', 0)} key(s) configured."
            elif action == "gemini_add_key":
                k = (payload.get("key") or "").strip()
                if not k:
                    result = "gemini_add_key: no key provided."
                else:
                    try:
                        import gemini_api
                        gemini_api.save_api_key(k, append=True)
                        st = gemini_api.status()
                        self._broadcast(type="gemini_status", **st)
                        result = f"Key added. Total keys: {st.get('api_keys_found', 0)}."
                    except Exception as e:
                        result = f"Failed to save key: {e}"
            elif action == "gemini_remove_key":
                target = (payload.get("key") or payload.get("preview") or "").strip()
                try:
                    import gemini_api
                    n = gemini_api.remove_api_key(target)
                    st = gemini_api.status()
                    self._broadcast(type="gemini_status", **st)
                    result = f"Removed {n} key(s). Remaining: {st.get('api_keys_found', 0)}."
                except Exception as e:
                    result = f"Failed to remove key: {e}"
            elif action == "gemini_test_key":
                k = (payload.get("key") or "").strip()
                try:
                    import gemini_api
                    if not k:
                        # Test the first active key instead
                        keys = gemini_api._resolve_api_keys()
                        if not keys: result = "No keys configured.";
                        else:
                            res = gemini_api.test_key(keys[0])
                            self._broadcast(type="gemini_test_result",
                                            preview=keys[0][:6]+'…'+keys[0][-4:],
                                            **res)
                            result = f"Test: {'OK' if res.get('ok') else 'FAIL — ' + res.get('error','')[:120]}"
                    else:
                        res = gemini_api.test_key(k)
                        self._broadcast(type="gemini_test_result",
                                        preview=k[:6]+'…'+k[-4:] if len(k)>14 else '(short)',
                                        **res)
                        result = f"Test: {'OK' if res.get('ok') else 'FAIL — ' + res.get('error','')[:120]}"
                except Exception as e:
                    result = f"Test failed: {e}"
            elif action == "review_posts":
                result = self._tool_review_posts(source=payload.get("source", ""))
            elif action == "next_post":
                result = self._tool_next_post()
            elif action == "previous_post":
                result = self._tool_previous_post()
            elif action == "improve_post_prompt":
                result = self._tool_improve_post_prompt(
                    index=payload.get("index"),
                    focus=payload.get("focus", ""),
                )
            elif action == "generate_post_image":
                result = self._tool_generate_post_image(
                    index=payload.get("index"),
                    section=payload.get("section"),
                )
            elif action == "accept_improved_prompt":
                result = self._tool_accept_improved_prompt()
            elif action == "reject_improved_prompt":
                result = self._tool_reject_improved_prompt()
            elif action == "get_news":
                result = self._tool_get_news(
                    topic=payload.get("topic") or "trending",
                    count=int(payload.get("count") or 5),
                )
            elif action == "show_today":
                result = self._tool_show_today()
            elif action == "open_projects_folder":
                result = self._tool_open_projects_folder()
            elif action == "ask_claude":
                result = self._tool_ask_claude(payload.get("query", ""))
            else:
                result = f"Unknown direct action: {action}"
            # Surface the outcome on the activity feed (no Gemma think loop).
            self._broadcast(type="agent_response", text=result)
            try:
                if getattr(self, "voice", None) is not None and result:
                    self.voice.speak(result)
            except Exception as e:
                print(f"(direct TTS failed: {e})")
        except Exception as e:
            self._broadcast(type="agent_response", text=f"Direct action error: {e}")

    def _on_dashboard_pipeline(self, action: str, payload: dict) -> None:
        """Dashboard-driven pipeline control (approve/deny/regen/save)."""
        print(f"[pipeline-msg] action={action!r} payload={payload}", flush=True)
        try:
            if action == "set_source":
                path = os.path.expanduser((payload.get("path") or "").strip())
                print(f"[pipeline] resolving path: {path!r}  exists={os.path.exists(path)}", flush=True)
                # Race guard: dashboard may click LOAD before start_pipeline
                # has finished in Gemma's tool-call loop. Wait briefly.
                import time as _t
                _deadline = _t.time() + 8
                while not self._active_pipeline and _t.time() < _deadline:
                    _t.sleep(0.2)
                if not self._active_pipeline:
                    print("[pipeline] no active pipeline after 8s — ignoring set_source")
                    return
                # Live progress: tell dashboard we're processing this file
                pipeline = self._active_pipeline
                pipeline.source_md = path
                self._pipeline_broadcast({
                    "processing": True,
                    "processing_label": f"Gemma is reading {os.path.basename(path)}…",
                })
                ok, msg = self._pipeline_load_md(path)
                print(f"[pipeline] load result: ok={ok}  msg={msg!r}", flush=True)
                if ok:
                    self._pipeline_advance_after_load()
                else:
                    # Clear the processing banner and show the error
                    self._pipeline_broadcast({"error": msg, "processing": False})
            elif action == "approve_prompt":
                self._pipeline_set_prompt_status(payload.get("id"), "approved")
            elif action == "deny_prompt":
                self._pipeline_set_prompt_status(payload.get("id"), "denied")
            elif action == "approve_all_prompts":
                self._pipeline_approve_all_prompts()
            elif action == "approve_subset_prompts":
                ids = payload.get("ids") or []
                if not isinstance(ids, list):
                    ids = []
                # Optional topic_number scope — when present, sync only that
                # post's prompts; everything else stays untouched. Per-post
                # "Run Batch" buttons set this; the master "Run All Batches"
                # leaves it None (full sync across all posts).
                raw_topic = payload.get("topic_number")
                scope_topic: Optional[int] = None
                if raw_topic is not None:
                    try:
                        scope_topic = int(raw_topic)
                    except (TypeError, ValueError):
                        scope_topic = None
                msg = self._pipeline_approve_subset_and_run(ids, scope_topic=scope_topic)
                import time as _t
                self._broadcast(type="controller_alert",
                                ts=int(_t.time()), level="info",
                                agent="LOTUS", message=msg)
            elif action == "edit_prompt":
                self._pipeline_edit_prompt(
                    payload.get("id"),
                    payload.get("text", "") or payload.get("new_text", ""),
                )
            elif action == "start_generation":
                # Run generation on a worker thread so the WS handler returns
                # immediately and the dashboard stays responsive.
                import threading as _th
                _th.Thread(
                    target=self._pipeline_generate_approved,
                    daemon=True,
                ).start()
            elif action == "approve_frame":
                self._pipeline_set_frame_status(payload.get("id"), "approved")
            elif action == "deny_frame":
                self._pipeline_set_frame_status(payload.get("id"), "denied")
            elif action == "approve_frames":   # voice-tool name (plural)
                self._tool_pipeline_approve(payload.get("frame_ids"))
            elif action == "deny_frames":
                self._tool_pipeline_deny(payload.get("frame_ids"))
            elif action == "regen_frame":
                self._pipeline_regen_frame(
                    payload.get("id"),
                    new_text=(payload.get("text")
                              or payload.get("new_text")),
                )
            elif action == "save_here":
                self._pipeline_save(payload.get("section", "Post1"))
            elif action == "save":            # alias
                self._pipeline_save(payload.get("section", "Post1"))
            elif action == "mark_complete":
                # A12 — finalize from review_frames. New direct-render
                # pipelines (A10+) already have frames on disk at
                # <pipeline_folder>/Post{N}/Frame{M}.png — this is
                # metadata-only: backfill any missing final_url, advance
                # stage to 'done', fire archive. No file move.
                p = self._active_pipeline
                if p:
                    approved = [f for f in p.frames
                                if f.get("status") == "approved" or f.get("final_url")]
                    if approved:
                        root = os.path.expanduser(
                            lotus_config.load()["image"].get(
                                "projects_root", "~/LotusAgent/Projects"))
                        for fr in approved:
                            fp = fr.get("final_path")
                            if fp and not fr.get("final_url"):
                                try:
                                    rel = os.path.relpath(fp, root).replace(os.sep, "/")
                                    fr["final_url"] = "/projects/" + rel
                                except Exception: pass
                        p.stage = "done"
                        try:
                            if getattr(p, "history_run_id", None):
                                import lotus_history
                                lotus_history.end_run(p.history_run_id, status="completed")
                        except Exception: pass
                        try:
                            self._submit_pipeline_for_archive()
                        except Exception as e:
                            print(f"[pipeline] archive submit failed: {e}", flush=True)
                        self._pipeline_broadcast()
                        print(f"[pipeline] mark_complete: {len(approved)} frames, stage=done",
                              flush=True)
                    else:
                        self._pipeline_broadcast({"error":
                            "No approved frames — approve at least one before marking complete."})
                else:
                    self._pipeline_broadcast({"error": "No active pipeline."})
            elif action == "cancel":
                self._tool_pipeline_cancel(payload.get("id") or payload.get("pipeline_id"))
            elif action == "focus":
                fid = payload.get("id") or payload.get("pipeline_id")
                if fid: self._focus_pipeline(fid)
            elif action == "emergency_stop":
                self._tool_emergency_stop()
            elif action == "frame_skip":
                # User tells the pipeline this failed frame will stay empty.
                # Marks the frame as 'skipped' with source='skipped' so it
                # doesn't count against the LOTUS success tally and doesn't
                # block any downstream "all frames terminal" checks.
                fid = payload.get("id") or payload.get("frame_id")
                if fid and self._active_pipeline:
                    for fr in self._active_pipeline.frames:
                        if fr["id"] == fid:
                            fr["status"] = "skipped"
                            fr["source"] = "skipped"
                            fr["error"]  = None
                            print(f"[pipeline] {fid} skipped by user")
                            break
                    self._pipeline_broadcast()
            elif action == "resume":
                # User has resolved (uploaded/skipped) the failed frames and
                # wants the render loop to continue to the next post.
                p = self._active_pipeline
                if p and p.paused:
                    # Rehydrated pipelines have no live render thread — the
                    # worker died with the previous Python process. Setting
                    # _resume_event would hang forever (nothing's waiting on
                    # it), so re-spawn the render loop from the saved stage
                    # instead. _pipeline_generate_approved is idempotent on
                    # already-rendered frames (target-file existence check
                    # marks them pending_review without re-rendering).
                    if p.pause_reason == "restored from previous session":
                        p.paused = False
                        p.pause_reason = None
                        print(f"[pipeline] ▶ Resume — re-spawning render loop "
                              f"for rehydrated pipeline {p.id}")
                        import threading as _th
                        _th.Thread(
                            target=self._pipeline_generate_approved,
                            daemon=True,
                            name=f"render-{p.id}",
                        ).start()
                        self._pipeline_broadcast()
                    else:
                        p._resume_event.set()
                        print(f"[pipeline] ▶ Resume requested by dashboard")
                elif p:
                    print(f"[pipeline] Resume ignored — pipeline not paused")
            elif action == "archive":
                # UI-triggered archive. Can be fired at any pipeline stage;
                # ArchiverAgent writes the current snapshot.
                self._submit_pipeline_for_archive()
            elif action == "frame_retry":
                # User clicks ↻ on a failed frame — re-run this single prompt
                # through the bot via the single-image API (not the burst).
                # Runs in a thread so we don't block the WS handler, and so
                # concurrent retries are serialised by gemini_bot's own lock.
                fid = payload.get("id") or payload.get("frame_id")
                if fid and self._active_pipeline:
                    import threading as _th
                    _th.Thread(
                        target=self._pipeline_frame_retry,
                        args=(fid,),
                        daemon=True,
                    ).start()
        except Exception as e:
            self._broadcast(type="agent_response", text=f"Pipeline error: {e}")

    def _verify_ollama(self):
        """Check Ollama is running and model is available."""
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
            models = [m["name"] for m in r.json().get("models", [])]
            if not any(MODEL in m for m in models):
                print(f"❌ Model '{MODEL}' not found in Ollama.")
                print(f"   Available: {models}")
                print(f"   Run: ollama pull {MODEL}")
                sys.exit(1)
            print(f"✅ Ollama connected — {MODEL} ready")
        except requests.ConnectionError:
            print("❌ Ollama not running. Start it with:")
            print("   brew services start ollama")
            print("   (or)  ollama serve &")
            sys.exit(1)

    def _pipeline_state_for_gemma(self) -> str:
        """One-line summary of the active pipeline so Gemma can validate
        review-stage decisions (regenerate vs proceed) without guessing."""
        p = self._active_pipeline
        if not p:
            return ""
        try:
            approved = [f for f in p.frames if f.get("status") == "approved" or f.get("final_url")]
            denied   = [f for f in p.frames if f.get("status") == "denied"]
            pending  = [f for f in p.frames if f.get("status") == "pending_review"]
            denied_ids = [f.get("id") for f in denied if f.get("id")]
            return (
                f"stage={p.stage} prompts={len(p.prompts)} frames={len(p.frames)} "
                f"approved={len(approved)} denied={len(denied)} pending={len(pending)} "
                f"denied_ids={denied_ids!r} save_section={p.save_section or 'unset'}"
            )
        except Exception:
            return f"stage={p.stage}"

    def think(self, user_input: str) -> str:
        """Route through Gemma with tool-calling. Gemma decides whether to
        call a tool or reply with text. Tool results feed back in a loop
        until Gemma produces a final text response (max 5 iterations)."""
        # Inject live pipeline state so Gemma validates before acting.
        ctx = self._pipeline_state_for_gemma()
        if ctx:
            user_input = f"[pipeline] {ctx}\n{user_input}"
        self.history.append({"role": "user", "content": user_input})
        # Cap history to last 20 messages to keep token usage sane
        if len(self.history) > 20:
            self.history = self.history[-20:]

        for _ in range(5):
            try:
                r = requests.post(
                    f"{OLLAMA_URL}/api/chat",
                    json={
                        "model": MODEL,
                        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *self.history],
                        "tools": TOOL_SCHEMAS,
                        "stream": False,
                        "options": {"temperature": 0.3, "num_predict": 400},
                    },
                    timeout=60,
                )
                msg = r.json().get("message", {}) or {}
            except Exception as e:
                return f"Brain error: {e}"

            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                # Append Gemma's tool-call message to history
                self.history.append({
                    "role": "assistant",
                    "content": msg.get("content", "") or "",
                    "tool_calls": tool_calls,
                })
                # Execute each tool and append results
                for tc in tool_calls:
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name", "")
                    args = fn.get("arguments", {}) or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    tool_result = self._dispatch_tool(name, args)
                    self.history.append({
                        "role": "tool",
                        "content": tool_result,
                        "name": name,
                    })
                continue  # loop back to Gemma for a final reply

            # No tool calls — this is the final text reply
            text = (msg.get("content") or "").strip()
            self.history.append({"role": "assistant", "content": text})
            return text

        return "I processed the request but couldn't summarize it in time."

    def _dispatch_tool(self, name: str, args: dict) -> str:
        """Execute a Gemma-requested tool, return a string result for Gemma
        to see (it'll summarize for the user)."""
        self._broadcast(type="tool_call", tool=name, input=args)
        try:
            if name == "open_app":
                return self._tool_open_app(args.get("app_name", ""))
            if name == "open_url":
                return self._tool_open_url(args.get("url", ""))
            if name == "take_screenshot":
                return self._tool_screenshot()
            if name == "show_media":
                return self._tool_show_media(args.get("target", ""))
            if name == "get_system_info":
                return self._tool_system_info()
            if name == "press_keys":
                return self._tool_hotkey(args.get("keys", ""))
            if name == "scroll":
                return self._tool_scroll(args.get("direction", "down"), int(args.get("amount") or 5))
            if name == "type_text":
                return self._tool_type(args.get("text", ""))
            if name == "create_image":
                return self._tool_create_image(
                    args.get("prompt", ""),
                    section=args.get("section"),
                    frame_number=args.get("frame_number"),
                )
            if name == "set_active_section":
                return self._tool_set_section(args.get("section", ""))
            if name == "read_prompts":
                return self._tool_read_prompts()
            if name == "start_pipeline":
                return self._tool_start_pipeline(
                    source_md=args.get("source_md"),
                    name=args.get("name"),
                )
            if name == "pipeline_approve_frames":
                return self._tool_pipeline_approve(args.get("frame_ids"))
            if name == "pipeline_deny_frames":
                return self._tool_pipeline_deny(args.get("frame_ids"))
            if name == "pipeline_regenerate_frame":
                fid = args.get("frame_id", "")
                self._pipeline_regen_frame(fid)
                return f"Regenerating {fid}"
            if name == "pipeline_save":
                self._pipeline_save(args.get("section", "Post1"))
                return f"Saved to {args.get('section','Post1')}"
            if name == "pipeline_cancel":
                return self._tool_pipeline_cancel()
            if name == "emergency_stop":
                return self._tool_emergency_stop()
            if name == "generate_all_frames":
                return self._tool_generate_all_frames(
                    section=args.get("section"),
                    start_frame=args.get("start_frame"),
                )
            if name == "set_preference":
                return self._tool_set_pref(args.get("command", ""))
            if name == "show_preferences":
                return lotus_config.describe_image_settings()
            if name == "focus_app":
                return self._tool_focus_app(args.get("app_name", ""))
            if name == "click_on_text":
                return self._tool_click_on_text(args.get("text", ""))
            if name == "wait":
                import time as _t
                _t.sleep(float(args.get("seconds") or 1.5))
                return "Waited."
            if name == "refresh_dashboard":
                self._broadcast(type="reload")
                return "Dashboard refresh signal sent."
            if name == "show_today":
                return self._tool_show_today()
            if name == "open_projects_folder":
                return self._tool_open_projects_folder()
            if name == "get_news":
                return self._tool_get_news(
                    topic=args.get("topic") or "trending",
                    count=int(args.get("count") or 5),
                )
            if name == "analyze_news":
                return self._tool_analyze_news(
                    title=args.get("title", ""),
                    url=args.get("url", ""),
                    question=args.get("question", ""),
                )
            if name == "next_news":
                return self._tool_next_news(question=args.get("question", ""))
            if name == "previous_news":
                return self._tool_previous_news()
            if name == "review_posts":
                return self._tool_review_posts(source=args.get("source", ""))
            if name == "next_post":
                return self._tool_next_post()
            if name == "previous_post":
                return self._tool_previous_post()
            if name == "improve_post_prompt":
                return self._tool_improve_post_prompt(
                    index=args.get("index"),
                    focus=args.get("focus", ""),
                )
            if name == "generate_post_image":
                return self._tool_generate_post_image(
                    index=args.get("index"),
                    section=args.get("section"),
                )
            if name == "accept_improved_prompt":
                return self._tool_accept_improved_prompt()
            if name == "reject_improved_prompt":
                return self._tool_reject_improved_prompt()
            if name == "ask_claude":
                return self._tool_ask_claude(args.get("query", ""))
            return f"Unknown tool: {name}"
        except Exception as e:
            return f"Tool {name} failed: {e}"

    def _handle_image(self, prompt: str) -> str:
        """Run the Gemini image-gen pipeline and return a short voice reply.

        Target filename follows the parent-child layout: today's MMDDYY
        folder under the projects root, named post1.png / post2.png / ...
        (configurable via voice commands). Counter is inferred from the
        filesystem, so a new day resets naming automatically.
        """
        try:
            import gemini  # lazy — keeps Gemma-only mode importable
        except Exception as e:
            return f"Image backend unavailable: {e}"

        target_path = lotus_config.next_child_path("image")
        parent = os.path.dirname(target_path)
        parent_name = os.path.basename(parent)
        fname = os.path.basename(target_path)

        self._broadcast(type="tool_call", tool="gemini",
                        input={"prompt": prompt, "target": fname, "folder": parent_name})
        if self.voice:
            self.voice.speak(f"Generating {fname}. One moment.")
        print(f"  🎨 Gemini: generating {prompt!r} → {target_path}")

        with LotusPhase1._gemini_lock:
            path = _gen_image(prompt, target_path, verbose=True)
        if not path:
            self._broadcast(type="tool_result", tool="gemini",
                            result="Image generation failed.")
            return "Image generation failed. Check the Gemini tab."

        # Build a URL the dashboard can render (served under /projects/)
        rel = os.path.relpath(path, os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects")))
        image_url = "/projects/" + rel.replace(os.sep, "/")
        self._broadcast(type="tool_result", tool="gemini",
                        result=f"Saved {os.path.basename(path)}",
                        image_url=image_url,
                        image_path=path,
                        prompt=prompt)
        return f"Image saved as {os.path.basename(path)} in {parent_name} folder."

    # ── Tool implementations (called by _dispatch_tool) ───────────────────
    def _ensure_tools(self):
        if not hasattr(self, "_tools"):
            from tools import ToolExecutor
            self._tools = ToolExecutor()

    def _tool_open_app(self, app_name: str) -> str:
        import subprocess
        candidates = _resolve_app_name(app_name) if sys.platform == "darwin" else [app_name]
        used = None; err = ""
        for c in candidates:
            if sys.platform == "darwin":
                cmd = ["open", "-a", c]
                # Chrome: skip the profile picker by specifying last-used profile
                if c == "Google Chrome":
                    profile = _chrome_last_profile()
                    if profile:
                        cmd += ["--args", f"--profile-directory={profile}"]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0:
                    used = c; break
                err = (r.stderr or r.stdout or "").strip()
            else:
                try:
                    subprocess.Popen([c], shell=True); used = c; break
                except Exception as e:
                    err = str(e)
        result = f"Opened {used}" if used else f"Could not find app '{app_name}'. {err}"
        self._broadcast(type="tool_result", tool="open_app", result=result)
        return result

    def _tool_open_url(self, url: str) -> str:
        """Open URL in Chrome specifically (with logged-in profile) — per user
        preference, Chrome is always the default browser LOTUS uses."""
        import subprocess
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if sys.platform == "darwin":
            profile = _chrome_last_profile()
            if profile:
                cmd = ["open", "-a", "Google Chrome", url, "--args",
                       f"--profile-directory={profile}"]
            else:
                cmd = ["open", "-a", "Google Chrome", url]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                result = f"Opened {url} in Chrome"
            else:
                # Fallback to default browser
                self._ensure_tools()
                result = str(self._tools.execute("open_url", {"url": url}))
        else:
            self._ensure_tools()
            result = str(self._tools.execute("open_url", {"url": url}))
        self._broadcast(type="tool_result", tool="open_url", result=result)
        return result

    def _tool_screenshot(self) -> str:
        import pyautogui
        parent = lotus_config.parent_folder_for()
        ts = datetime.now().strftime("%H%M%S")
        name = f"screen_{ts}.png"
        path = os.path.join(parent, name)
        pyautogui.screenshot().save(path)
        rel = os.path.relpath(path, os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects")))
        url = "/projects/" + rel.replace(os.sep, "/")
        self._broadcast(type="tool_result", tool="take_screenshot",
                        result=f"Saved {name}", image_url=url, image_path=path)
        return f"Screenshot saved to {path}"

    def _tool_show_media(self, target: str) -> str:
        import subprocess
        target = target.strip().lower().replace(" ", "")
        parent = lotus_config.parent_folder_for()
        cfg = lotus_config.load()["image"]
        files = os.listdir(parent) if os.path.isdir(parent) else []
        chosen = None
        if target in ("screenshot", "screenshots", "screen"):
            cs = sorted(f for f in files if f.startswith("screen_") and f.endswith(".png"))
            if cs: chosen = cs[-1]
        elif target in ("image", "images", "photo", "photos", "picture", "pictures"):
            cs = [f for f in files if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
            if cs: chosen = max(cs, key=lambda f: os.path.getmtime(os.path.join(parent, f)))
        elif target.startswith("post"):
            for f in files:
                if f.lower().startswith(target) and f.lower().endswith((".png", ".jpg", ".jpeg")):
                    chosen = f; break
        elif target == "reel":
            cand = cfg["reel_name"] + cfg["reel_ext"]
            if cand in files: chosen = cand
        if not chosen:
            msg = f"No {target} in today's folder"
            self._broadcast(type="tool_result", tool="show_media", result=msg)
            return msg
        full = os.path.join(parent, chosen)
        if sys.platform == "darwin":
            subprocess.Popen(["open", full])
        elif sys.platform == "win32":
            os.startfile(full)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", full])
        rel = os.path.relpath(full, os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects")))
        url = "/projects/" + rel.replace(os.sep, "/")
        self._broadcast(type="tool_result", tool="show_media",
                        result=f"Opened {chosen}", image_url=url, image_path=full)
        return f"Opened {chosen} in Preview"

    def _tool_system_info(self) -> str:
        self._ensure_tools()
        res = self._tools.execute("get_system_info", {})
        self._broadcast(type="tool_result", tool="get_system_info", result=str(res)[:500])
        return str(res)

    def _tool_hotkey(self, keys: str) -> str:
        self._ensure_tools()
        res = self._tools.execute("hotkey", {"keys": keys})
        self._broadcast(type="tool_result", tool="press_keys", result=str(res))
        return str(res)

    def _tool_scroll(self, direction: str, amount: int) -> str:
        self._ensure_tools()
        res = self._tools.execute("scroll", {"direction": direction, "amount": amount})
        self._broadcast(type="tool_result", tool="scroll", result=str(res))
        return str(res)

    def _tool_type(self, text: str) -> str:
        self._ensure_tools()
        res = self._tools.execute("type_text", {"text": text})
        self._broadcast(type="tool_result", tool="type_text", result=str(res))
        return str(res)

    def _tool_create_image(self, prompt: str,
                           section: Optional[str] = None,
                           frame_number: Optional[int] = None) -> str:
        try:
            import gemini
        except Exception as e:
            return f"Gemini unavailable: {e}"
        try:
            target = lotus_config.next_frame_path(section, frame_number)
        except Exception as e:
            return f"Bad section: {e}"
        self._broadcast(type="tool_call", tool="gemini",
                        input={
                            "prompt": prompt,
                            "target": os.path.basename(target),
                            "folder": os.path.basename(os.path.dirname(target)),
                        })
        with LotusPhase1._gemini_lock:
            path = _gen_image(prompt, target, verbose=True)
        if not path:
            self._broadcast(type="tool_result", tool="gemini",
                            result="Image generation failed")
            return "Image generation failed"
        rel = os.path.relpath(path, os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects")))
        url = "/projects/" + rel.replace(os.sep, "/")
        self._broadcast(type="tool_result", tool="gemini",
                        result=f"Saved {os.path.basename(path)}",
                        image_url=url, image_path=path, prompt=prompt)
        return f"Saved to {path}"

    def _tool_set_section(self, section: str) -> str:
        try:
            lotus_config.set_active_section(section)
        except Exception as e:
            return f"Invalid section: {e}"
        res = lotus_config.describe_image_settings()
        self._broadcast(type="tool_result", tool="set_active_section", result=res)
        return res

    def _tool_read_prompts(self) -> str:
        prompts = lotus_config.read_prompts()
        if not prompts:
            msg = "No prompts.md found in today's parent folder."
            self._broadcast(type="tool_result", tool="read_prompts", result=msg)
            return msg
        summary = f"Found {len(prompts)} prompts: " + " | ".join(
            f"{i+1}. {p[:60]}" for i, p in enumerate(prompts[:5])
        )
        if len(prompts) > 5:
            summary += f" | ... and {len(prompts)-5} more"
        self._broadcast(type="tool_result", tool="read_prompts",
                        result=summary, prompts=prompts)
        return summary

    def _tool_generate_all_frames(self, section: Optional[str] = None,
                                  start_frame: Optional[int] = None) -> str:
        try:
            import gemini
        except Exception as e:
            return f"Gemini unavailable: {e}"
        prompts = lotus_config.read_prompts()
        if not prompts:
            return "No prompts.md to process."
        section = section or "Post1"
        n = start_frame or 1
        results = []
        # Fresh run — clear any stale stop signal.
        self._stop_event.clear()
        for i, prompt in enumerate(prompts):
            if self._stop_event.is_set():
                results.append(f"Stopped before frame {n+i}")
                break
            try:
                target = lotus_config.next_frame_path(section, n + i)
            except Exception as e:
                results.append(f"Skipped #{i+1}: {e}")
                continue
            self._broadcast(type="tool_call", tool="gemini", input={
                "prompt": prompt, "target": os.path.basename(target),
                "folder": os.path.basename(os.path.dirname(target)),
            })
            with LotusPhase1._gemini_lock:
                if self._stop_event.is_set():
                    results.append(f"Stopped before frame {n+i}")
                    break
                path = _gen_image(prompt, target, verbose=True)
            if not path:
                results.append(f"Frame {n+i} failed")
                continue
            rel = os.path.relpath(path, os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects")))
            url = "/projects/" + rel.replace(os.sep, "/")
            self._broadcast(type="tool_result", tool="gemini",
                            result=f"Saved {os.path.basename(path)}",
                            image_url=url, image_path=path, prompt=prompt)
            results.append(f"Frame {n+i}: {os.path.basename(path)}")
        return f"Done — {len(results)} frames: " + "; ".join(results[:10])

    def _tool_pipeline_cancel(self, pipeline_id: Optional[str] = None) -> str:
        """Graceful cancel — signal the target pipeline's render loop to stop
        after the current frame. Multi-pipeline safe: cancels ONE specific
        pipeline only (by id or focused). Other pipelines keep running.
        For blast-everything (kill all pipelines + Chrome), use
        `_tool_emergency_stop`.
        """
        target = pipeline_id
        if target is None:
            p = self._active_pipeline
            if not p:
                return "No active pipeline to cancel."
            target = p.id
        if target in self._pipelines:
            self._cancel_pipeline(target)
            self._broadcast(type="pipeline_cancel", reason="user_cancel", id=target)
            return f"Pipeline {target} cancel signalled. Any in-flight frame will finish before the loop exits."
        return f"Pipeline {target} not found."

    def _tool_emergency_stop(self) -> str:
        """Emergency kill — blast EVERY running pipeline. Sets the global
        stop flag AND marks every pipeline's cancelled=True AND closes the
        Gemini Chrome tab so any mid-flight render unblocks immediately.
        Use only when you want all work to halt, not per-pipeline cancel."""
        self._stop_event.set()
        # Multi-pipeline: cancel all registered pipelines, not just focus.
        for pid in list(getattr(self, "_pipelines", {}).keys()):
            try: self._cancel_pipeline(pid)
            except Exception: pass
        self._active_pipeline = None
        import subprocess as _sp
        closed = False
        try:
            script = '''
            tell application "Google Chrome"
                set killed to false
                repeat with w in windows
                    set i to 0
                    set toClose to {}
                    repeat with t in tabs of w
                        set i to i + 1
                        set u to URL of t
                        if u contains "gemini.google.com" or u contains "aistudio" then
                            set end of toClose to t
                            set killed to true
                        end if
                    end repeat
                    repeat with t in toClose
                        try
                            close t
                        end try
                    end repeat
                end repeat
                return killed
            end tell
            '''
            r = _sp.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
            closed = "true" in (r.stdout or "").lower()
        except Exception as e:
            print(f"(emergency_stop: couldn't close Gemini tab: {e})")
        self._broadcast(type="pipeline_cancel", reason="emergency_stop",
                        gemini_tab_closed=closed)
        self._broadcast(type="emergency_stop", gemini_tab_closed=closed)
        return (
            f"EMERGENCY STOP. Stop flag set, pipeline cleared, "
            f"Gemini tab {'closed' if closed else 'not found'}. "
            f"All generation loops will exit at the next checkpoint."
        )

    # ── News ─────────────────────────────────────────────────────────────
    _NEWS_FEEDS = {
        "trending": "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
        "india":    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
        "world":    "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en",
        "tech":     "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-IN&gl=IN&ceid=IN:en",
        "business": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-IN&gl=IN&ceid=IN:en",
        "sports":   "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=en-IN&gl=IN&ceid=IN:en",
    }

    def _tool_get_news(self, topic: str = "trending", count: int = 5) -> str:
        """Fetch top headlines from Google News RSS (no API key). Broadcasts a
        `news_headlines` event to the dashboard and returns a spoken summary."""
        import urllib.request, urllib.error, re as _re
        url = self._NEWS_FEEDS.get((topic or "trending").lower(), self._NEWS_FEEDS["trending"])
        count = max(1, min(10, int(count or 5)))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 LOTUS"})
            with urllib.request.urlopen(req, timeout=8) as r:
                xml = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            msg = f"Could not fetch news: {e}"
            self._broadcast(type="news_headlines", topic=topic, items=[], error=str(e))
            return msg

        def _strip_cdata(s: str) -> str:
            m = _re.search(r"<!\[CDATA\[(.*?)\]\]>", s or "", _re.DOTALL)
            return (m.group(1) if m else (s or "")).strip()

        items_xml = _re.findall(r"<item>(.*?)</item>", xml, _re.DOTALL)[:count]
        items = []
        for it in items_xml:
            t = _re.search(r"<title>(.*?)</title>", it, _re.DOTALL)
            l = _re.search(r"<link>(.*?)</link>", it, _re.DOTALL)
            s = _re.search(r"<source[^>]*>(.*?)</source>", it, _re.DOTALL)
            d = _re.search(r"<pubDate>(.*?)</pubDate>", it, _re.DOTALL)
            items.append({
                "title": _strip_cdata(t.group(1)) if t else "",
                "url":   _strip_cdata(l.group(1)) if l else "",
                "source": _strip_cdata(s.group(1)) if s else "",
                "published": (d.group(1).strip() if d else ""),
            })

        # Remember the list + reset cursor so "next news" can walk through.
        self._last_news_items = items
        self._last_news_topic = topic
        self._news_cursor     = -1   # first analyze_news sets it to the matching index

        self._broadcast(type="news_headlines", topic=topic, items=items,
                        cursor=self._news_cursor)
        if not items:
            return f"No {topic} headlines returned."
        # Gemma will speak whatever we return; keep it short + readable.
        top3 = [h["title"] for h in items[:3] if h["title"]]
        return (
            f"Top {len(items)} {topic} headlines posted to the dashboard. "
            f"Reading the top three: "
            + " · ".join(f"{i+1}. {t}" for i, t in enumerate(top3))
        )

    @staticmethod
    def _find_section_body(text: str, label: str) -> str:
        """Return the body paragraph under a named label, or empty string."""
        import re as _re
        if not text: return ""
        m = _re.search(
            rf"(?:^|\n)\s*{_re.escape(label)}\s*[:\-–]?\s*\n?([\s\S]*?)(?=\n\s*(?:SUMMARY|KEY FACTS|WHY IT MATTERS|MY TAKE|BOTTOM LINE|OVERVIEW|CONCLUSION)\b|\Z)",
            text, _re.IGNORECASE,
        )
        return (m.group(1).strip() if m else "")

    # ── News analysis via local Gemma ────────────────────────────────────
    def _fetch_article_text(self, url: str) -> str:
        """Light article scraper. Google News `/rss/articles/CBMi...` URLs
        resolve to a JS app shell (not the real article), so skip those —
        Gemma reasons from the headline + source instead. Non-Google URLs
        go through a basic <article>/<main> extractor."""
        if not url:
            return ""
        if "news.google.com/rss/articles/" in url or "news.google.com/articles/" in url:
            return ""   # JS app shell — useless, defer to Gemma reasoning
        import urllib.request, urllib.error, re as _re
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/605 LOTUS",
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                html = r.read().decode("utf-8", errors="replace")[:300_000]
        except Exception:
            return ""
        focus = _re.search(
            r"<(?:article|main)[^>]*>(.*?)</(?:article|main)>",
            html, _re.DOTALL | _re.IGNORECASE,
        )
        body = focus.group(1) if focus else html
        body = _re.sub(r"<script[^>]*>.*?</script>", " ", body, flags=_re.DOTALL | _re.IGNORECASE)
        body = _re.sub(r"<style[^>]*>.*?</style>",  " ", body, flags=_re.DOTALL | _re.IGNORECASE)
        body = _re.sub(r"<noscript[^>]*>.*?</noscript>", " ", body, flags=_re.DOTALL | _re.IGNORECASE)
        text = _re.sub(r"<[^>]+>", " ", body)
        text = _re.sub(r"\s+", " ", text).strip()
        for kill in ("Sign in", "Subscribe", "Cookie", "privacy policy"):
            text = text.replace(kill, " ")
        low = text.lower()
        # Some pages return a generic error/loader — reject anything too short
        # or that looks like a consent/cookie wall.
        if len(text) < 300: return ""
        if "google news" in low and len(text) < 2000: return ""
        return text[:6000]

    def _gemma_keypoints(self, title: str, source: str = "", context_text: str = "") -> list:
        """Pass 1 — extract the most likely entities and angles from a headline.
        Returns a short list of keyword strings to seed the deeper analysis."""
        prompt = (
            "Read this news headline. Identify the 3-6 most important factual "
            "entities and angles: people, places, organisations, numbers, dates, "
            "actions. One per line. No commentary, no bullets, just the phrases.\n\n"
            f"Headline: {title}\n"
            + (f"Source: {source}\n" if source else "")
            + (f"Context: {context_text[:1500]}\n" if context_text else "")
            + "\nEntities / angles:"
        )
        raw = self._gemma_oneshot(prompt, max_tokens=180)
        items = [ln.strip(" -•*\t") for ln in (raw or "").splitlines() if ln.strip()]
        # Drop anything that looks like a prose sentence (> 80 chars or ends with .)
        items = [i for i in items if len(i) <= 80][:8]
        return items

    def _gemma_oneshot(self, prompt: str, max_tokens: int = 600) -> str:
        """Single-turn Gemma call for structured analysis — no tool calls,
        no history coupling, small enough to stay responsive."""
        try:
            r = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.35, "num_predict": max_tokens},
                },
                timeout=90,
            )
            return (r.json().get("message", {}) or {}).get("content", "").strip()
        except Exception as e:
            return f"(Gemma failed: {e})"

    def _tool_analyze_news(self, title: str, url: str = "", question: str = "") -> str:
        """Local-Gemma deep analysis pipeline. Google News redirect URLs are
        opaque, so we stop trying to scrape them and instead use a two-pass
        Gemma reasoning chain on the headline + source + topic + any
        follow-up question the user asks."""
        title = (title or "").strip()
        if not title:
            return "analyze_news: no title given."

        # Keep the cursor in sync with whatever title we're analyzing.
        # Gemma sometimes paraphrases or truncates titles, so the match is
        # tolerant: exact, prefix, or token-overlap fallbacks.
        items = getattr(self, "_last_news_items", [])
        matched_item = None
        matched_idx  = None
        def _norm(s: str) -> str:
            import re as _re
            return _re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()
        tnorm = _norm(title)
        # 1) exact
        for i, it in enumerate(items):
            if (it.get("title") or "").strip() == title:
                matched_idx, matched_item = i, it; break
        # 2) normalized prefix (first 40 chars)
        if matched_idx is None and tnorm:
            for i, it in enumerate(items):
                inorm = _norm(it.get("title") or "")
                if inorm.startswith(tnorm[:40]) or tnorm.startswith(inorm[:40]):
                    matched_idx, matched_item = i, it; break
        # 3) token-overlap — choose the item sharing the most 4+ char words
        if matched_idx is None and tnorm:
            tset = {w for w in tnorm.split() if len(w) >= 4}
            best = (-1, 0)   # (index, score)
            for i, it in enumerate(items):
                iset = {w for w in _norm(it.get("title") or "").split() if len(w) >= 4}
                score = len(tset & iset)
                if score > best[1]:
                    best = (i, score)
            if best[1] >= 2:
                matched_idx = best[0]
                matched_item = items[best[0]]
        if matched_idx is not None:
            self._news_cursor = matched_idx
            if not url:
                url = (matched_item or {}).get("url") or ""
        source    = (matched_item or {}).get("source", "")
        published = (matched_item or {}).get("published", "")
        topic     = getattr(self, "_last_news_topic", "trending")

        self._broadcast(
            type="news_analysis", title=title, url=url,
            analysis="", status="working",
            cursor=getattr(self, "_news_cursor", -1),
            total=len(items),
        )

        # Pass A — article scrape (only useful for non-Google URLs).
        article = self._fetch_article_text(url) if url else ""

        # Pass B — extract entities/angles locally so the deep analyser has
        # structured anchors even when the article is unreachable.
        entities = self._gemma_keypoints(title, source=source, context_text=article)

        # Pass C — one combined Gemma call, but with a concrete few-shot
        # example. Small local models nail structure far more reliably when
        # they can pattern-match a worked example over following abstract
        # instructions.
        few_shot = (
            "Example:\n"
            "Headline: RBI raises repo rate by 25 bps to 6.75% in surprise move\n"
            "Source: Reuters\n\n"
            "SUMMARY\n"
            "The Reserve Bank of India unexpectedly hiked its policy rate. "
            "The 25 basis-point move lifts the repo rate to 6.75%. "
            "Markets had priced in no change at this meeting.\n\n"
            "KEY FACTS\n"
            "- Reserve Bank of India\n"
            "- Repo rate now 6.75%\n"
            "- 25 basis-point hike\n"
            "- Decision was unexpected\n\n"
            "WHY IT MATTERS\n"
            "Higher borrowing costs will dampen credit demand and weigh on "
            "equity valuations. The surprise element reprices near-term "
            "rate-cut expectations and strengthens the rupee short-term.\n\n"
            "MY TAKE\n"
            "The RBI is signalling that inflation fight is not done, and "
            "markets should expect another hold-or-hike at the next meeting.\n"
            "\n---\n\n"
        )
        user_lines = [f"Headline: {title}"]
        if source:    user_lines.append(f"Source: {source}")
        if published: user_lines.append(f"Published: {published}")
        if question:  user_lines.append(f"User focus: {question}")
        if article:   user_lines.append(f"Article excerpt:\n{article[:3000]}")
        user_block = "\n".join(user_lines)

        prompt = (
            "You are LOTUS's news analyst. Produce the analysis for the input "
            "below using the SAME format as the example — four sections with "
            "ALL CAPS labels on their own lines. Keep SUMMARY factual, KEY "
            "FACTS as dash-prefixed bullets, WHY IT MATTERS as two sentences, "
            "MY TAKE as exactly one forward-looking sentence. Do not restate "
            "the example.\n\n"
            + few_shot +
            user_block + "\n\nNow produce the four labelled sections:\n"
        )

        analysis = self._gemma_oneshot(prompt, max_tokens=1200)
        my_take, summary = self._extract_sections(analysis)
        # Single retry with terse reinforcement if the format slipped.
        if not my_take or not summary:
            retry_prompt = (
                prompt
                + "\n(Your previous attempt did not use the ALL CAPS labels. "
                "Reproduce exactly the four labels from the example.)\n"
            )
            retry = self._gemma_oneshot(retry_prompt, max_tokens=1200)
            mt2, sm2 = self._extract_sections(retry)
            if mt2 or sm2:
                analysis, my_take, summary = retry, mt2, sm2

        # Guard against MY TAKE being truncated mid-sentence (no end
        # punctuation, or too short) — top it up with a focused one-liner.
        def _looks_truncated(t: str) -> bool:
            t = (t or "").strip()
            if len(t) < 25:                return True
            if t[-1] not in ".!?\"'":      return True
            return False
        if _looks_truncated(my_take):
            top_up_prompt = (
                f"News headline: {title}\n"
                + (f"Summary so far: {summary}\n" if summary else "")
                + "Give ONE complete sentence as your forward-looking take. "
                  "Start with the assertion. End with a period. No labels.\n"
                  "Take:"
            )
            fresh_take = self._gemma_oneshot(top_up_prompt, max_tokens=120).strip().strip('"')
            # First sentence only.
            import re as _re
            m = _re.match(r"(.+?[.!?])(\s|$)", fresh_take, _re.DOTALL)
            fresh_take = (m.group(1) if m else fresh_take).strip()
            if fresh_take and len(fresh_take) >= 20:
                my_take = fresh_take
                # Splice the fresh take back into the analysis text.
                import re as _re2
                analysis = _re2.sub(
                    r"(?is)(^|\n)\s*MY\s*TAKE\s*[:\-–]?\s*\n?[\s\S]*$",
                    f"\\nMY TAKE\\n{fresh_take}",
                    analysis,
                )
                if "MY TAKE" not in analysis.upper():
                    analysis = analysis.rstrip() + f"\n\nMY TAKE\n{fresh_take}"

        # Targeted MY TAKE recovery — a focused one-sentence ask that small
        # models reliably complete even when the full format slipped.
        def _clean_take(raw: str) -> str:
            raw = (raw or "").strip().strip('"').strip("'")
            # Kill common preamble Gemma sometimes emits.
            for pre in ("my take:", "my take is", "here's my take:",
                        "bottom line:", "in summary,", "in short,"):
                if raw.lower().startswith(pre):
                    raw = raw[len(pre):].strip()
            # First sentence only.
            import re as _re
            m = _re.match(r"(.+?[.!?])(\s|$)", raw, _re.DOTALL)
            return (m.group(1) if m else raw).strip()

        if not my_take:
            take_prompt = (
                "Give ONE sentence stating your best-read conclusion about this "
                "news. No preamble like 'my take is'. No labels. Start with the "
                "assertion directly, end with a period.\n\n"
                f"Headline: {title}\n"
                + (f"Source: {source}\n" if source else "")
                + (f"Summary so far: {summary}\n" if summary else "")
                + (f"Anchors: {', '.join(entities)}\n" if entities else "")
                + "\nOne-sentence take:"
            )
            my_take = _clean_take(self._gemma_oneshot(take_prompt, max_tokens=80))

        # If Gemma still balked, compose a useful take locally from whatever
        # signal we do have — anchors + headline beats a placeholder.
        if not my_take:
            anchor_str = ", ".join(entities[:3]) if entities else ""
            head = title.rstrip(".!? ")
            if anchor_str:
                my_take = (f"{head} — this story turns on {anchor_str}; "
                           f"the short-term read is that the named actors will drive the next move.")
            else:
                my_take = f"{head} — watch for follow-up reporting before acting on this."

        # If SUMMARY is still blank but we DO have raw analysis text, promote
        # the first non-label paragraph rather than a placeholder.
        if not summary:
            paras = [p.strip() for p in (analysis or "").split("\n\n") if p.strip()]
            for p in paras:
                if 40 < len(p) < 600 and p.upper() != p:
                    summary = p; break
            if not summary:
                summary = (f"Gemma's structured pass was thin. Headline only: {title}")

        # Rebuild the analysis field so the overlay always has both sections.
        blocks = [("SUMMARY", summary)]
        kf_match = self._find_section_body(analysis, "KEY FACTS")
        if kf_match:
            blocks.append(("KEY FACTS", kf_match))
        why_match = self._find_section_body(analysis, "WHY IT MATTERS")
        if why_match:
            blocks.append(("WHY IT MATTERS", why_match))
        blocks.append(("MY TAKE", my_take))
        analysis = "\n\n".join(f"{lbl}\n{body.strip()}" for lbl, body in blocks)

        self._broadcast(
            type="news_analysis", title=title, url=url,
            analysis=analysis, status="done",
            has_article=bool(article),
            entities=entities,
            source=source, published=published, topic=topic,
            cursor=getattr(self, "_news_cursor", -1),
            total=len(items),
        )

        # Speech is handled by the caller (either run_voice or
        # _on_dashboard_direct_action / _on_dashboard_command) AFTER this
        # tool fully returns — that guarantees the UI card is already on
        # screen before audio starts and avoids double-speaking.
        my_take, summary = self._extract_sections(analysis)
        take_for_voice = (my_take or summary or "headline-level signal only")[:260]
        source_line = f" The source is {source}." if source else ""
        return (
            f"Here's my take on {title[:110]}.{source_line} {take_for_voice} "
            f"What's your call, Somendra — do you want me to go deeper, look "
            f"at the opposite view, or move on to the next story?"
        )

    # ── Post-review walk (prompts.md or pipeline prompts) ───────────────
    def _tool_review_posts(self, source: str = "") -> str:
        """Load today's posts into the review cursor and analyse the first."""
        items = []
        chosen = (source or "").strip().lower() or "auto"

        # Prefer the ACTIVE pipeline's prompts — they're the current batch.
        if chosen in ("auto", "pipeline") and self._active_pipeline:
            try:
                items = [{
                    "id":   p.get("id", f"p{i}"),
                    "text": (p.get("text") or "").strip(),
                    "status": p.get("status", "pending"),
                    "section": self._active_pipeline.name or "pipeline",
                } for i, p in enumerate(self._active_pipeline.prompts or [])
                  if (p.get("text") or "").strip()]
                if items:
                    chosen = "pipeline"
            except Exception:
                items = []

        # Otherwise fall back to prompts.md on disk.
        if not items and chosen in ("auto", "prompts_md"):
            try:
                raw = lotus_config.read_prompts() or []
            except Exception:
                raw = []
            items = [{
                "id": f"m{i}", "text": p.strip(),
                "status": "pending",
                "section": os.path.basename(lotus_config.parent_folder_for()),
            } for i, p in enumerate(raw) if p.strip()]
            if items: chosen = "prompts_md"

        if not items:
            return ("I don't see any posts to review — no active pipeline "
                    "and no prompts.md in today's folder. Drop your prompt "
                    "list on the Source card first.")

        self._post_review_items  = items
        self._post_review_source = chosen
        self._post_review_cursor = -1
        self._broadcast(type="post_review_list",
                        source=chosen, items=items,
                        folder=items[0].get("section", ""))
        return self._tool_next_post()

    def _tool_next_post(self) -> str:
        items = getattr(self, "_post_review_items", [])
        if not items:
            return ("No post review in progress. Say 'review posts' to "
                    "start walking through today's prompt list.")
        idx = getattr(self, "_post_review_cursor", -1) + 1
        if idx >= len(items):
            return (f"That was the last of the {len(items)} posts. Want me "
                    f"to start from the top again, or generate them all?")
        self._post_review_cursor = idx
        return self._analyse_post_at_cursor()

    def _tool_previous_post(self) -> str:
        items = getattr(self, "_post_review_items", [])
        if not items:
            return "No post review in progress."
        idx = getattr(self, "_post_review_cursor", 0) - 1
        if idx < 0:
            return "You're already on the first post."
        self._post_review_cursor = idx
        return self._analyse_post_at_cursor()

    def _analyse_post_at_cursor(self) -> str:
        """Gemma-driven review of the post the cursor currently points at.
        Produces SUMMARY / KEY ELEMENTS / STRENGTHS / MY TAKE, broadcasts
        a `post_review` card, and returns a conversational line for TTS."""
        items = self._post_review_items
        idx   = self._post_review_cursor
        item  = items[idx] if 0 <= idx < len(items) else {}
        text  = (item.get("text") or "").strip()
        if not text:
            return "That post is empty — skipping. Say 'next post' to continue."

        self._broadcast(
            type="post_review", status="working",
            index=idx, total=len(items),
            id=item.get("id", ""), section=item.get("section", ""),
            prompt_text=text, source=self._post_review_source,
        )

        few_shot = (
            "Example:\n"
            "Post prompt: cinematic shot of a stealth fighter breaking the sound barrier over a Himalayan pass at dusk, volumetric light, Nolan-style colour grading\n\n"
            "SUMMARY\n"
            "A high-cinema aerial shot featuring a stealth jet in a dramatic "
            "mountain setting at golden hour.\n\n"
            "KEY ELEMENTS\n"
            "- Subject: stealth fighter jet\n"
            "- Setting: Himalayan pass, dusk\n"
            "- Lighting: volumetric, golden-hour\n"
            "- Mood: Nolan-grade, cinematic\n\n"
            "STRENGTHS\n"
            "Strong subject recognition cues, evocative setting, lighting "
            "language that generators handle well. Easy to produce a hero "
            "frame without ambiguity.\n\n"
            "MY TAKE\n"
            "This is a scroll-stopping hero for a defence reel — lead with "
            "it, then cut to detail shots for the carousel.\n"
            "\n---\n\n"
        )
        prompt = (
            "You are LOTUS's content reviewer for @techengine.lab — a defence/"
            "science creator in India. Review the post prompt below using the "
            "SAME four labels from the example. Plain prose, no emoji, no "
            "markdown. SUMMARY is one tight sentence. KEY ELEMENTS is dash-"
            "prefixed bullets. STRENGTHS is two sentences. MY TAKE is exactly "
            "one forward-looking sentence with your view on whether this post "
            "will perform. Do not restate the example.\n\n"
            + few_shot +
            f"Post prompt: {text[:2000]}\n\n"
            f"Position: {idx+1} of {len(items)}"
            + (f"\nSection: {item.get('section')}" if item.get("section") else "")
            + "\n\nNow produce the four sections:\n"
        )
        raw = self._gemma_oneshot(prompt, max_tokens=900)
        # Label-strict: only trust MY TAKE / SUMMARY that were actually
        # labelled by Gemma. Unlabelled paragraphs can silently pollute the
        # take (e.g. pick up STRENGTHS content), so we don't fall back to
        # paragraph heuristics here.
        my_take  = self._find_section_body(raw, "MY TAKE")
        summary  = self._find_section_body(raw, "SUMMARY")

        # Retry once if structure slipped.
        if not my_take or not summary:
            raw2 = self._gemma_oneshot(
                prompt + "\n(Your last attempt missed a label — reproduce all "
                         "four ALL-CAPS labels exactly.)\n",
                max_tokens=900,
            )
            mt2 = self._find_section_body(raw2, "MY TAKE")
            sm2 = self._find_section_body(raw2, "SUMMARY")
            if mt2 or sm2:
                raw     = raw2
                my_take = mt2 or my_take
                summary = sm2 or summary

        # Targeted MY TAKE top-up if still missing OR truncated mid-sentence.
        def _take_ok(t: str) -> bool:
            t = (t or "").strip()
            return bool(t) and len(t) >= 25 and t[-1] in '.!?"\''
        if not _take_ok(my_take):
            fresh = self._gemma_oneshot(
                "In ONE complete sentence, give your take on whether this "
                "post will perform for a defence/science creator in India. "
                "Start with the verdict (will / won't / might). End with a "
                "period. No labels, no preamble.\n\n"
                f"Post prompt: {text[:800]}\n"
                + (f"Summary: {summary}\n" if summary else "")
                + "\nTake:",
                max_tokens=120,
            ).strip().strip('"')
            import re as _re
            m = _re.match(r"(.+?[.!?])(\s|$)", fresh, _re.DOTALL)
            fresh = (m.group(1) if m else fresh).strip()
            if _take_ok(fresh):
                my_take = fresh

        if not summary:
            summary = f"Post prompt: {text[:240]}"
        if not my_take:
            # Final hard fallback: a generic but specific-sounding take that
            # references the prompt so TTS isn't left silent.
            my_take = (
                f"This post is production-ready; ship it as-is and see how "
                f"the hook lands with the audience."
            )

        analysis = (
            f"SUMMARY\n{summary}\n\n"
            + (f"KEY ELEMENTS\n{self._find_section_body(raw, 'KEY ELEMENTS') or '- ' + text[:160]}\n\n")
            + (f"STRENGTHS\n{self._find_section_body(raw, 'STRENGTHS') or 'Structurally clean; Gemma could expand on hooks once produced.'}\n\n")
            + f"MY TAKE\n{my_take}"
        )

        self._broadcast(
            type="post_review", status="done",
            index=idx, total=len(items),
            id=item.get("id", ""), section=item.get("section", ""),
            prompt_text=text, analysis=analysis,
            summary=summary, my_take=my_take,
            source=self._post_review_source,
        )

        src_line = f"Section {item.get('section')}." if item.get("section") else ""
        return (
            f"Post {idx+1} of {len(items)}. {src_line} {my_take} "
            f"Want me to approve it for the batch, move to the next, "
            f"or regenerate with tweaks?"
        )

    def _tool_generate_post_image(self, index=None, section=None) -> str:
        """Generate one image from the current (original or improved) post
        prompt via create_image. Prompts from Claude are pre-trusted, so
        this call IS the user approval — there's no separate prompt-approve
        step any more."""
        items = getattr(self, "_post_review_items", [])
        if not items:
            return ("No active post review. Say 'review posts' first so I "
                    "know which prompt to generate.")
        if index is None:
            idx = getattr(self, "_post_review_cursor", 0)
            if idx < 0: idx = 0
        else:
            try: idx = int(index) - 1
            except Exception: idx = getattr(self, "_post_review_cursor", 0)
            idx = max(0, min(len(items) - 1, idx))

        item = items[idx] or {}
        prompt_text = (item.get("text") or "").strip()
        if not prompt_text:
            return "That post has no prompt — nothing to generate."

        target_section = (section or item.get("section") or "").strip() or None
        # If the post's stored section is the folder name (e.g. "Techengine..."),
        # that's the PARENT folder — a multi-frame section or slot is required.
        # Default to Post1 so the user sees something predictable.
        if target_section and target_section.startswith(lotus_config.load()["image"].get("brand", "Techengine")):
            target_section = None
        improved = bool(item.get("improved"))

        # Predict where the image will land so the "working" card can show
        # the target path before Gemini finishes.
        try:
            expected_path = lotus_config.next_frame_path(target_section)
        except Exception:
            expected_path = ""
        projects_root = os.path.expanduser(
            lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects")
        )
        expected_url = ""
        expected_folder = ""
        if expected_path:
            rel = os.path.relpath(expected_path, projects_root)
            expected_url = "/projects/" + rel.replace(os.sep, "/")
            expected_folder = os.path.basename(os.path.dirname(expected_path)) or ""

        self._broadcast(
            type="post_image_gen", status="working",
            index=idx, total=len(items), id=item.get("id", ""),
            prompt=prompt_text, section=target_section or "(active)",
            improved=improved,
            expected_path=expected_path, expected_url=expected_url,
            expected_folder=expected_folder,
        )

        # Delegate to the existing image-gen handler (Gemini via Chrome).
        try:
            msg = self._tool_create_image(prompt_text, section=target_section)
        except Exception as e:
            msg = f"Image generation failed: {e}"

        # Extract the actual saved path from the "Saved to <path>" return.
        import re as _re
        m = _re.search(r"Saved to\s+(\S.+)$", (msg or "").strip())
        saved_path = m.group(1).strip() if m else ""
        image_url = ""
        folder = ""
        filename = ""
        if saved_path and os.path.exists(saved_path):
            rel = os.path.relpath(saved_path, projects_root)
            image_url = "/projects/" + rel.replace(os.sep, "/")
            folder    = os.path.basename(os.path.dirname(saved_path))
            filename  = os.path.basename(saved_path)

        success = bool(image_url)

        self._broadcast(
            type="post_image_gen",
            status="done" if success else "error",
            index=idx, total=len(items), id=item.get("id", ""),
            prompt=prompt_text, section=target_section or "(active)",
            improved=improved, result=msg,
            image_url=image_url, image_path=saved_path,
            folder=folder, filename=filename,
            parent_folder=os.path.basename(lotus_config.parent_folder_for()),
        )
        label = "improved" if improved else "original"
        return (
            f"Approved and generating post {idx+1} with the {label} prompt. "
            f"{msg} Say 'next post' to keep reviewing, or 'redo this' if "
            f"you want me to tighten the prompt further."
        )

    def _tool_improve_post_prompt(self, index=None, focus: str = "") -> str:
        """Ask Gemma to rewrite the current post prompt as a stronger NB2 /
        Gemini image-gen prompt, broadcast original-vs-improved card, hold
        the result in _pending_improvement for accept/reject."""
        items = getattr(self, "_post_review_items", [])
        if not items:
            return ("No active post review. Say 'review posts' first so I "
                    "know which prompt to rewrite.")
        # Resolve target index — explicit (1-based) or current cursor.
        if index is None:
            idx = getattr(self, "_post_review_cursor", 0)
            if idx < 0: idx = 0
        else:
            try: idx = int(index) - 1
            except Exception: idx = getattr(self, "_post_review_cursor", 0)
            idx = max(0, min(len(items) - 1, idx))

        item = items[idx] or {}
        original = (item.get("text") or "").strip()
        if not original:
            return "That post is empty — nothing to rewrite."

        self._broadcast(
            type="post_improvement", status="working",
            index=idx, total=len(items), id=item.get("id", ""),
            original=original, improved="", changes=[], focus=focus,
        )

        # Gemma: NB2-aware rewriter. Few-shot gives it the exact shape.
        few_shot = (
            "Original prompt: mountain at sunset\n"
            "Improved prompt: Cinematic wide-angle photograph of a jagged "
            "Himalayan peak at golden-hour sunset, low-angle shot with the "
            "sun just below the summit casting volumetric god-rays through "
            "drifting alpine mist, warm orange-to-deep-violet gradient sky, "
            "crisp snow-line detail, shot on a 24mm lens, National "
            "Geographic photojournalism style, high dynamic range.\n"
            "Key changes:\n"
            "- Specific subject (Himalayan peak) and time (golden hour)\n"
            "- Camera direction (wide-angle, low-angle, 24mm lens)\n"
            "- Lighting anchors (volumetric god-rays, mist, HDR)\n"
            "- Colour palette (orange-to-violet gradient)\n"
            "- Style reference (National Geographic)\n"
            "\n---\n\n"
        )
        prompt = (
            "You are a prompt engineer specialised in NB2 / Gemini image "
            "generation for @techengine.lab (defence/science content). "
            "Rewrite the ORIGINAL prompt below into a richer, more specific "
            "prompt that will produce a noticeably stronger image. Keep the "
            "original INTENT; add clear subject anchors, camera/framing "
            "direction, lighting language, colour palette, mood, and a "
            "concrete style reference. Do not pad with marketing fluff.\n\n"
            "Output EXACTLY this format — two labelled blocks, no extra text:\n"
            "Improved prompt: <one dense sentence, 40-80 words>\n"
            "Key changes:\n"
            "- <change one>\n"
            "- <change two>\n"
            "- <change three>\n"
            + (f"- <change four — keep with focus: {focus}>\n" if focus else "")
            + "\n"
            + few_shot
            + f"Original prompt: {original[:1500]}\n"
            + (f"User focus: {focus}\n" if focus else "")
            + "\nNow produce the two blocks:\n"
        )
        raw = self._gemma_oneshot(prompt, max_tokens=700)

        # Parse the two blocks with tolerant regex.
        import re as _re
        imp_m = _re.search(
            r"Improved\s*prompt\s*[:\-]?\s*(.+?)(?:\n\s*Key\s*changes|\n\n|\Z)",
            raw, _re.IGNORECASE | _re.DOTALL,
        )
        improved = imp_m.group(1).strip().strip('"').strip() if imp_m else ""
        ch_m = _re.search(
            r"Key\s*changes\s*[:\-]?\s*\n([\s\S]+?)(?:\n\s*\n|\Z)",
            raw, _re.IGNORECASE,
        )
        changes = []
        if ch_m:
            for ln in ch_m.group(1).splitlines():
                ln = ln.strip().lstrip("-•*").strip()
                if ln and len(ln) > 3:
                    changes.append(ln)

        # Top-up if improved prompt is empty or suspiciously short/truncated.
        if not improved or len(improved) < 50 or improved[-1] not in '.!?"':
            fresh = self._gemma_oneshot(
                "Rewrite this NB2 prompt into ONE dense sentence (40-80 "
                "words) with specific subject, camera, lighting, colour "
                "palette, and style anchors. No labels, no preamble, one "
                "sentence ending with a period.\n\n"
                f"Original: {original[:1200]}\n"
                + (f"Focus: {focus}\n" if focus else "")
                + "\nImproved prompt:",
                max_tokens=240,
            ).strip().strip('"')
            m = _re.match(r"(.+?[.!?])(\s|$)", fresh, _re.DOTALL)
            fresh = (m.group(1) if m else fresh).strip()
            if fresh and len(fresh) >= 50:
                improved = fresh
        if not changes:
            changes = [
                "Added concrete subject and scene anchors",
                "Specified camera framing and lens",
                "Added lighting language and colour palette",
            ]
        if not improved:
            # Last-ditch: mechanically append style anchors to the original.
            improved = (original.rstrip(".") + ", cinematic composition, "
                        "volumetric lighting, shot on anamorphic lens, "
                        "high dynamic range, National Geographic style.")

        # Hold pending so accept/reject can act on it.
        self._pending_improvement = {
            "index": idx, "original": original, "improved": improved,
            "changes": changes, "focus": focus,
        }

        self._broadcast(
            type="post_improvement", status="done",
            index=idx, total=len(items), id=item.get("id", ""),
            original=original, improved=improved, changes=changes, focus=focus,
        )
        return (
            f"Rewritten prompt for post {idx+1}. Key changes: "
            f"{'; '.join(changes[:3])}. "
            f"Say 'accept' to swap it in, 'reject' to keep the original, "
            f"or 'regenerate' to produce a new image with the stronger prompt."
        )

    def _tool_accept_improved_prompt(self) -> str:
        pend = getattr(self, "_pending_improvement", None)
        if not pend:
            return "No improved prompt is waiting for acceptance."
        idx = pend["index"]
        items = self._post_review_items
        if 0 <= idx < len(items):
            items[idx]["text"] = pend["improved"]
            items[idx]["improved"] = True
        self._broadcast(
            type="post_improvement", status="accepted",
            index=idx, total=len(items),
            original=pend["original"], improved=pend["improved"],
            changes=pend["changes"],
        )
        self._pending_improvement = None
        return (
            f"Accepted. The rewritten prompt is now live for post {idx+1}. "
            f"Say 'regenerate' if you'd like me to produce a fresh image "
            f"from it, or 'next post' to keep reviewing."
        )

    def _tool_reject_improved_prompt(self) -> str:
        pend = getattr(self, "_pending_improvement", None)
        if not pend:
            return "No improved prompt is waiting to be rejected."
        idx = pend["index"]
        self._broadcast(
            type="post_improvement", status="rejected",
            index=idx, total=len(self._post_review_items),
            original=pend["original"], improved=pend["improved"],
            changes=pend["changes"],
        )
        self._pending_improvement = None
        return "Kept the original prompt. Say 'redo' to try a different angle."

    def _tool_next_news(self, question: str = "") -> str:
        """Advance the cursor one step and analyse that headline."""
        items = getattr(self, "_last_news_items", [])
        if not items:
            return ("No news list loaded yet. Ask me to 'show latest news' first, "
                    "then I can walk through them one by one.")
        idx = getattr(self, "_news_cursor", -1) + 1
        if idx >= len(items):
            return (f"That was the last of the {len(items)} headlines I had. "
                    f"Want me to pull a fresh batch?")
        self._news_cursor = idx
        item = items[idx] or {}
        title = (item.get("title") or "").strip()
        url   = (item.get("url")   or "").strip()
        if not title:
            return "Next item has no title — skipping. Say 'next' again."
        # Delegate to analyze_news, which handles fetch + Gemma + broadcast + TTS.
        return self._tool_analyze_news(title=title, url=url, question=question)

    def _tool_previous_news(self) -> str:
        """Step the cursor back and re-analyse."""
        items = getattr(self, "_last_news_items", [])
        if not items:
            return "No news list loaded yet."
        idx = getattr(self, "_news_cursor", 0) - 1
        if idx < 0:
            return "You're already on the first headline."
        self._news_cursor = idx
        item = items[idx] or {}
        return self._tool_analyze_news(
            title=(item.get("title") or "").strip(),
            url=(item.get("url")   or "").strip(),
        )

    @staticmethod
    def _extract_sections(analysis: str) -> tuple:
        """Pull SUMMARY and MY TAKE paragraphs out of whatever shape Gemma
        produced. Tolerates:
          - label on its own line (what we ask for)
          - label followed by ':' and inline content
          - variant label text (MY BOTTOM LINE, ASSESSMENT, CONCLUSION, etc.)
        If no labels are found, fall back to heuristics so the pipeline never
        returns fully empty."""
        import re as _re
        if not analysis:
            return "", ""

        label_map = {
            "SUMMARY":        ("SUMMARY", "OVERVIEW", "WHAT HAPPENED"),
            "KEY FACTS":      ("KEY FACTS", "FACTS", "KEY POINTS", "KEY DETAILS"),
            "WHY IT MATTERS": ("WHY IT MATTERS", "IMPACT", "WHY THIS MATTERS", "SIGNIFICANCE"),
            "MY TAKE":        ("MY TAKE", "TAKE", "MY BOTTOM LINE", "BOTTOM LINE",
                               "MY VIEW", "ASSESSMENT", "CONCLUSION", "VERDICT"),
        }
        # Build alternation regex that matches either the label at a line start
        # OR inline at the start of a sentence.
        all_labels = [lab for variants in label_map.values() for lab in variants]
        pattern = r"(?:^|\n)\s*(" + "|".join(_re.escape(l) for l in all_labels) + r")\s*[:\-–]?\s*"
        matches = list(_re.finditer(pattern, analysis, _re.IGNORECASE))
        sections: dict = {}
        for idx, m in enumerate(matches):
            raw_label = m.group(1).upper()
            canonical = next((k for k, vars in label_map.items() if raw_label in vars), raw_label)
            start = m.end()
            end = matches[idx+1].start() if idx+1 < len(matches) else len(analysis)
            sections[canonical] = analysis[start:end].strip()

        summary = sections.get("SUMMARY", "").strip()
        my_take = sections.get("MY TAKE", "").strip()

        # Heuristic fallbacks so we never return fully empty.
        if not summary:
            paras = [p.strip() for p in _re.split(r"\n\s*\n", analysis) if p.strip()]
            for p in paras:
                if 40 < len(p) < 600 and p.upper() != p:  # not an all-caps label line
                    summary = p; break
        if not my_take:
            # Last short paragraph often IS the take.
            paras = [p.strip() for p in _re.split(r"\n\s*\n", analysis) if p.strip()]
            for p in reversed(paras):
                if 20 < len(p) < 400 and p.upper() != p:
                    my_take = p; break
        return my_take, summary

    @staticmethod
    def _build_analysis_voice_line(title: str, summary: str, my_take: str, has_article: bool) -> str:
        """Compose one flowing sentence for edge-tts."""
        parts = [f"Here's my take on {title}."]
        if not has_article:
            parts.append("I only had the headline to work with.")
        first_line = ""
        if summary:
            first_line = summary.splitlines()[0].strip()
        if first_line:
            parts.append(first_line)
        if my_take:
            parts.append(f"My bottom line: {my_take}")
        parts.append("What's your call — do you want me to dig deeper, look at the opposite view, or skip it?")
        return " ".join(parts)

    # ── Research via the native Claude.app ──────────────────────────────
    def _tool_ask_claude(self, query: str) -> str:
        """Drive the logged-in Claude.app on macOS: activate, start a new
        chat, paste the query, press Return, wait for the reply to finish,
        capture via screenshot + OCR, let Gemma score it 1-10, broadcast
        to the dashboard."""
        import subprocess, time as _t, tempfile, os as _os
        q = (query or "").strip()
        if not q:
            return "ask_claude: empty query."

        self._broadcast(type="claude_suggestion",
                        query=q, response="", score=0, status="working")

        # 1. Activate Claude.app (launches if not running). Start a fresh
        # chat via Cmd+N so we don't append to an unrelated thread.
        subprocess.run(["open", "-a", "Claude"], capture_output=True, text=True)
        _t.sleep(2.5)
        subprocess.run(["osascript", "-e",
            'tell application "Claude" to activate'], capture_output=True, text=True)
        _t.sleep(0.6)
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to keystroke "n" using command down'],
            capture_output=True, text=True)
        _t.sleep(1.2)

        # 2. Put the query on the clipboard, paste it, press Return.
        try:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(q.encode("utf-8"))
        except Exception as e:
            return f"clipboard copy failed: {e}"
        _t.sleep(0.3)
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to keystroke "v" using command down'],
            capture_output=True, text=True)
        _t.sleep(0.4)
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to key code 36'],   # Return
            capture_output=True, text=True)

        # 3. Give Claude.app time to produce the answer. No programmatic
        # streaming-done signal is available for a native app, so use a
        # conservative time budget with a shorter first pass.
        _t.sleep(22)

        # 4. Capture the visible Claude window via screenshot + OCR, strip
        # our query echo, return the remainder as the response.
        response = self._capture_claude_app_response(q)

        if not response:
            response = ("(Sent to Claude.app. Automatic capture came back "
                        "empty — the reply is visible in the Claude window.)")

        # 5. Rate with Gemma and broadcast.
        score = self._gemma_score(q, response)
        self._broadcast(type="claude_suggestion", query=q,
                        response=response, score=score, status="done")

        preview = response[:220].replace("\n", " ")
        return (
            f"Claude replied in the Claude app (quality {score}/10). "
            f"Here's the gist: {preview}. "
            f"What do you think, Somendra — should we use it, or try a different angle?"
        )

    def _capture_claude_app_response(self, query: str) -> str:
        """Screencap the Claude.app window, OCR it, and return whatever comes
        after the echoed query. Best-effort — OCR is noisy, so also strip
        common Claude chrome. Kept simple on purpose — the dashboard card is
        a preview; the real copy lives in Claude.app for the user to read."""
        import subprocess, tempfile, os as _os
        try:
            from PIL import Image
            import pytesseract
        except Exception:
            return ""

        bounds_script = (
            'tell application "System Events"\n'
            '  tell process "Claude"\n'
            '    if (count of windows) > 0 then\n'
            '      set p to position of window 1\n'
            '      set s to size of window 1\n'
            '      return (item 1 of p as integer) & "," & (item 2 of p as integer) & "," '
            '        & (item 1 of s as integer) & "," & (item 2 of s as integer)\n'
            '    end if\n'
            '  end tell\n'
            'end tell\n'
        )
        r = subprocess.run(["osascript", "-e", bounds_script],
                           capture_output=True, text=True)
        bounds = (r.stdout or "").strip().replace(", ", ",")

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False); tmp.close()
        try:
            if bounds and bounds.count(",") == 3:
                x, y, w, h = [s.strip() for s in bounds.split(",")]
                # Crop 18% off the top (title bar + nav) and 80px off the
                # bottom (input box) so OCR only sees the conversation.
                try:
                    iy = int(y) + int(int(h) * 0.18)
                    ih = int(h) - int(int(h) * 0.18) - 80
                    subprocess.run(
                        ["screencapture", "-x", "-R", f"{x},{iy},{w},{ih}", tmp.name],
                        capture_output=True,
                    )
                except Exception:
                    subprocess.run(["screencapture", "-x", "-R", f"{x},{y},{w},{h}", tmp.name],
                                   capture_output=True)
            else:
                subprocess.run(["screencapture", "-x", tmp.name], capture_output=True)
            try:
                img = Image.open(tmp.name)
                text = pytesseract.image_to_string(img) or ""
            except Exception:
                text = ""
        finally:
            try: _os.unlink(tmp.name)
            except Exception: pass

        # Clean-up: collapse runs of whitespace, drop empty lines.
        import re as _re
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # Remove the query echo (first occurrence — may span lines).
        qwords = [w.lower() for w in _re.findall(r"\w+", query) if len(w) >= 4]
        out, skip_until_found = [], bool(qwords)
        for ln in lines:
            low = ln.lower()
            if skip_until_found and any(w in low for w in qwords[:4]):
                skip_until_found = False
                continue
            if skip_until_found:
                continue
            # Strip common Claude.app chrome lines.
            if _re.match(r"^(Claude|You|New chat|Copy|Regenerate|Retry)$", ln):
                continue
            if len(ln) <= 3:
                continue
            out.append(ln)
        cleaned = "\n".join(out).strip()
        # Keep it readable — cap to ~3 KB so the TTS summary stays bearable.
        return cleaned[:3000]

    def _gemma_score(self, query: str, response: str) -> float:
        """Cheap one-shot rating call. Temperature is low and num_predict small."""
        import re as _re
        try:
            r = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": (
                        "Rate the answer below from 1 to 10 based on how well it "
                        "addresses the question (accuracy, specificity, usefulness). "
                        "Respond with ONLY the number, nothing else.\n\n"
                        f"Question: {query}\n\nAnswer: {response[:1500]}\n\nScore (1-10):"
                    )}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 8},
                },
                timeout=15,
            )
            txt = (r.json().get("message", {}) or {}).get("content", "").strip()
            m = _re.search(r"(\d+(?:\.\d+)?)", txt)
            val = float(m.group(1)) if m else 7.0
            return max(1.0, min(10.0, val))
        except Exception:
            return 7.0

    def _tool_show_today(self) -> str:
        """Summarize today's parent folder + nudge the dashboard to refresh."""
        try:
            parent = lotus_config.parent_folder_for()
        except Exception as e:
            return f"Could not resolve today's folder: {e}"
        if not os.path.isdir(parent):
            return f"No output today yet. Folder {os.path.basename(parent)} will be created on first save."
        by_section: dict = {}
        total = 0
        for root, _, files in os.walk(parent):
            for f in sorted(files):
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    section = os.path.relpath(root, parent) or "(root)"
                    by_section.setdefault(section, []).append(f)
                    total += 1
        self._broadcast(type="today_refresh")
        if total == 0:
            return f"{os.path.basename(parent)} exists but is empty — no images saved yet today."
        parts = [f"{s}: {len(files)}" for s, files in sorted(by_section.items())]
        return (
            f"Today ({os.path.basename(parent)}): {total} files across "
            f"{len(by_section)} section(s) — {', '.join(parts)}. Dashboard gallery refreshed."
        )

    def _tool_open_projects_folder(self) -> str:
        """Open the LOTUS Projects root in macOS Finder."""
        import subprocess
        try:
            cfg = lotus_config.load()["image"]
            root = os.path.expanduser(cfg.get("projects_root", "~/LotusAgent/Projects"))
        except Exception:
            root = os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects"))
        os.makedirs(root, exist_ok=True)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", root])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", root])
            else:
                subprocess.Popen(["xdg-open", root])
            self._broadcast(type="tool_result", tool="open_projects_folder", result=root)
            return f"Opened {root} in Finder."
        except Exception as e:
            return f"Could not open projects folder: {e}"

    def _tool_focus_app(self, app_name: str) -> str:
        """Bring an already-running app to the foreground via AppleScript."""
        import subprocess
        if sys.platform != "darwin":
            return self._tool_open_app(app_name)
        candidates = _resolve_app_name(app_name)
        for c in candidates:
            r = subprocess.run(
                ["osascript", "-e", f'tell application "{c}" to activate'],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                self._broadcast(type="tool_result", tool="focus_app", result=f"Focused {c}")
                return f"Focused {c}"
        msg = f"Could not focus {app_name}"
        self._broadcast(type="tool_result", tool="focus_app", result=msg)
        return msg

    def _tool_click_on_text(self, text: str) -> str:
        """Find the visible text on screen via OCR and click its centre.
        Handles Retina scaling so the click lands correctly."""
        if not text.strip():
            return "No text given"
        try:
            import pyautogui
            import pytesseract
        except Exception as e:
            return f"OCR unavailable: {e}"
        img = pyautogui.screenshot()
        try:
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        except Exception as e:
            return f"OCR failed: {e}"

        query = text.lower().strip()
        logical_w, logical_h = pyautogui.size()
        phys_w, phys_h = img.size
        sx = phys_w / logical_w if logical_w else 1.0
        sy = phys_h / logical_h if logical_h else 1.0

        # Try whole-word contains first, then fuzzy (case-insensitive substring)
        candidates = []
        for i, tok in enumerate(data["text"]):
            tok = (tok or "").strip()
            if not tok:
                continue
            try:
                conf = int(data["conf"][i])
            except (ValueError, TypeError):
                conf = 0
            if conf < 40:
                continue
            if query in tok.lower() or tok.lower() in query:
                cx_phys = data["left"][i] + data["width"][i] // 2
                cy_phys = data["top"][i] + data["height"][i] // 2
                candidates.append((conf, tok, cx_phys, cy_phys))
        if not candidates:
            msg = f"Could not find '{text}' on screen"
            self._broadcast(type="tool_result", tool="click_on_text", result=msg)
            return msg
        candidates.sort(key=lambda c: -c[0])   # highest confidence first
        _, found, cx, cy = candidates[0]
        lx, ly = int(cx / sx), int(cy / sy)
        pyautogui.click(lx, ly)
        result = f"Clicked '{found}' at ({lx}, {ly})"
        self._broadcast(type="tool_result", tool="click_on_text", result=result)
        return result

    # ── Interactive pipeline ──────────────────────────────────────────────
    def _build_queue_snapshot(self) -> dict:
        """Cross-pipeline queue view consumed by the dashboard's Queue tab.

        For each live pipeline we expose:
          - identity (id, name, stage, paused, cancelled)
          - every frame currently in `pipeline.frames` (ordered as the
            render loop sees them — each frame's status tells you whether
            it's already done, mid-flight, awaiting review, or failed)
          - the count of approved prompts that haven't materialised into
            frames yet ('upcoming' work on this pipeline's queue)

        The data is intentionally read-only — the Queue tab only displays;
        any actions (approve/deny/regen) still go through the existing
        per-frame WS handlers. Returning a single dict keeps the HTTP
        endpoint simple."""
        pipelines = []
        for p in getattr(self, "_pipelines", {}).values():
            frames = []
            for f in (p.frames or []):
                frames.append({
                    "id":          f.get("id"),
                    "prompt_text": (f.get("prompt_text") or "")[:200],
                    "section":     f.get("section"),
                    "slide_idx":   f.get("slide_idx"),
                    "status":      f.get("status"),
                    "source":      f.get("source"),
                    "error":       f.get("error"),
                    "retry_count": f.get("retry_count"),
                    "final_url":   f.get("final_url"),
                    "temp_url":    f.get("temp_url"),
                })
            # Approved prompts that don't yet have a corresponding frame
            # entry — these are queued, waiting for the render loop.
            frame_prompt_ids = {str(f.get("prompt_id")) for f in (p.frames or [])
                                if f.get("prompt_id") is not None}
            upcoming = [
                {
                    "id":           str(pr.get("id")),
                    "title":        pr.get("title"),
                    "topic_number": pr.get("topic_number"),
                    "slide_number": pr.get("slide_number"),
                    "text":         (pr.get("text") or "")[:200],
                }
                for pr in (p.prompts or [])
                if pr.get("status") == "approved"
                   and str(pr.get("id")) not in frame_prompt_ids
            ]
            pipelines.append({
                "id":         p.id,
                "name":       p.name,
                "stage":      p.stage,
                "paused":     p.paused,
                "cancelled":  p.cancelled,
                "frame_count":  len(frames),
                "upcoming_count": len(upcoming),
                "frames":      frames,
                "upcoming":    upcoming,
            })
        return {"pipelines": pipelines}

    def _rehydrate_pipelines_from_db(self) -> None:
        """Load non-terminal pipelines from `lotus_pipelines_db` into the
        in-memory registry on agent boot. Render-loop threads from previous
        sessions are gone — rehydrated pipelines come back paused with
        `pause_reason='restored from previous session'` so the user knows
        they need to take an action (cancel, or resume by reloading the
        source MD which kicks generation again)."""
        try:
            rows = lotus_pipelines_db.list_active()
        except Exception as e:
            print(f"[boot] pipeline rehydrate skipped ({e})", flush=True)
            return
        if not rows:
            return
        for row in rows:
            try:
                # Explicit folder_name keyword: legacy rows have None →
                # pipeline keeps None and parent_folder() falls back to
                # brand+date. Never recompute on rehydrate (would migrate
                # existing pipelines to a new folder, breaking continuity).
                p = Pipeline(row["id"], row.get("source_md") or "",
                             name=row.get("name") or "",
                             folder_name=row.get("folder_name"))
                p.stage = row.get("stage") or "ask_location"
                p.source_hash = row.get("source_hash")
                p.created_at = float(row.get("created_at") or p.created_at)
                p.cancelled = bool(row.get("cancelled"))
                # Force-pause every restored pipeline — the original render
                # thread died with the previous process. The user decides
                # whether to resume or cancel.
                p.paused = True
                p.pause_reason = "restored from previous session"
                # If the source file was tracked, reload its prompts from
                # the review-gate DB so the UI has something to show.
                if p.source_hash:
                    try:
                        rs = lotus_prompts_db.list_for_source(p.source_hash)
                        p.prompts = [
                            {
                                "id":           str(r["id"]),
                                "db_id":        r["id"],
                                "title":        r["slide_title"],
                                "topic_number": r["topic_number"],
                                "slide_number": r["slide_number"],
                                "text":         r["edited_text"] or r["text"],
                                "edited":       bool(r["edited_text"]),
                                "status":       r["status"],
                            }
                            for r in rs
                        ]
                    except Exception as e:
                        print(f"[boot] prompt reload for {p.id} failed ({e})",
                              flush=True)
                self._pipelines[p.id] = p
            except Exception as e:
                print(f"[boot] failed to rehydrate {row.get('id')}: {e}",
                      flush=True)
        print(f"[boot] rehydrated {len(self._pipelines)} pipeline(s) from disk",
              flush=True)

    def _pipeline_broadcast(self, extra: Optional[dict] = None) -> None:
        # First broadcast the full state of the focused pipeline (backward
        # compat — existing dashboard code reads `pipeline_state`).
        if self._active_pipeline:
            payload = {"type": "pipeline_state", **self._active_pipeline.as_event()}
            # Attach the agent-mesh health snapshot so the dashboard's Agents
            # strip updates alongside every pipeline-state change.
            try:
                if getattr(self, "_agents", None):
                    payload["agents"] = self._agents.health_snapshot()
            except Exception: pass
            if extra:
                payload.update(extra)
            self._broadcast(**payload)
        # Always emit the list too — the dashboard's pipeline tabs show
        # EVERY registered pipeline, not just the focused one. Summaries
        # keep this event small even when 10+ pipelines are in memory.
        try:
            plist = [p.summary() for p in getattr(self, "_pipelines", {}).values()]
            self._broadcast(
                type="pipeline_list",
                pipelines=plist,
                focus_id=self._active_pipeline.id if self._active_pipeline else None,
            )
        except Exception: pass
        # Persist metadata for every live pipeline so the A2 overview screen
        # and post-restart rehydration have current state. Writes are cheap
        # (UPSERT on PK) and N is small.
        try:
            for p in getattr(self, "_pipelines", {}).values():
                lotus_pipelines_db.upsert(p.summary())
        except Exception as e:
            print(f"[pipeline] persist upsert failed: {e}", flush=True)

    def _set_pipeline(self, value, reason: str) -> None:
        """Helper so every state transition is logged — catches spurious resets.

        Multi-pipeline behaviour:
          - Setting to a new Pipeline: add it to _pipelines registry (if not
            already present) and make it the focus. Existing pipelines keep
            running on their own threads; their internal loops check
            `p.cancelled` rather than identity against `_active_pipeline`.
          - Setting to None: clear the focus. Does NOT remove anything from
            the registry — use `_cancel_pipeline(id)` for that."""
        # Lazy-init the registry on first use so older call sites work.
        if not hasattr(self, "_pipelines") or self._pipelines is None:
            self._pipelines = {}
        old = self._active_pipeline
        if value is not None:
            self._pipelines[value.id] = value
        self._active_pipeline = value
        if old is not value:
            print(f"[pipeline] _active_pipeline (focus): "
                  f"{old.id if old else 'None'} → "
                  f"{value.id if value else 'None'}  ({reason})", flush=True)

    def _focus_pipeline(self, pipeline_id: str) -> bool:
        """Switch focus to an existing pipeline. Used by the UI when the
        user clicks a different tab. Returns True if the focus actually
        changed."""
        if not hasattr(self, "_pipelines"):
            self._pipelines = {}
        p = self._pipelines.get(pipeline_id)
        if not p:
            print(f"[pipeline] focus: {pipeline_id} not in registry")
            return False
        if self._active_pipeline is p:
            return False
        self._active_pipeline = p
        print(f"[pipeline] focus → {p.id}", flush=True)
        self._pipeline_broadcast()
        return True

    def _cancel_pipeline(self, pipeline_id: Optional[str] = None) -> None:
        """Cancel ONE pipeline by id (or the focused one if id omitted).
        Sets cancelled=True so its render loop bails, then removes it from
        the registry. Emergency stop is separate (stops ALL pipelines)."""
        if not hasattr(self, "_pipelines"):
            self._pipelines = {}
        if pipeline_id is None:
            p = self._active_pipeline
            if not p: return
            pipeline_id = p.id
        p = self._pipelines.pop(pipeline_id, None)
        if not p:
            return
        p.cancelled = True
        try:
            p._resume_event.set()   # unblock any pause waiters so they can bail
        except Exception: pass
        try:
            lotus_pipelines_db.mark_cancelled(pipeline_id)
        except Exception as e:
            print(f"[pipeline] persist mark_cancelled failed: {e}", flush=True)
        if self._active_pipeline is p:
            # Promote another registered pipeline if any; else None.
            next_p = next(iter(self._pipelines.values()), None)
            self._active_pipeline = next_p
            if next_p:
                print(f"[pipeline] cancel {pipeline_id} → focus now {next_p.id}")
            else:
                print(f"[pipeline] cancel {pipeline_id} → no focus")
        self._pipeline_broadcast()

    def _tool_start_pipeline(self, source_md: Optional[str] = None,
                             name: Optional[str] = None) -> str:
        """Start the interactive content pipeline.

        Flow (new default — Gemma auto-approves prompts):
          ask_location → (load md) → auto-approve all prompts → generate
          → pause at review_frames (voice-driven) → save (voice-driven) → done

        `name` is shown in the dashboard tab so multiple pipelines are
        distinguishable (e.g. "Agni Launch").
        """
        import uuid as _uuid
        pipeline = Pipeline(_uuid.uuid4().hex[:8], "", name=name or "")
        # Clear any stale stop signal from a previously cancelled run so
        # this fresh pipeline isn't aborted the moment it starts.
        self._stop_event.clear()
        self._set_pipeline(pipeline, f"start_pipeline name={name!r}")

        if source_md:
            ok, msg = self._pipeline_load_md(os.path.expanduser(source_md))
            if not ok:
                pipeline.stage = "ask_location"
                pipeline.source_md = os.path.expanduser(source_md)
                self._pipeline_broadcast({"error": msg})
                return f"Couldn't read that file — {msg}. Dashboard will ask."
            # Auto-approve and kick off generation immediately
            self._pipeline_approve_all_prompts()
            import threading as _th
            _th.Thread(target=self._pipeline_generate_approved, daemon=True).start()
            return f"{pipeline.name} started. Auto-approving {len(pipeline.prompts)} prompts and generating."

        pipeline.stage = "ask_location"
        pipeline.source_md = lotus_config.prompts_md_path()   # suggested default
        self._pipeline_broadcast()
        if self.voice:
            self.voice.speak(f"{pipeline.name} ready. Where's the prompts file?")
        return (f"{pipeline.name} started. Pick the prompts file on the dashboard, "
                "or say the path.")

    def _pipeline_advance_after_load(self) -> None:
        """After load, hold at review_prompts so the user can verify each NB2
        prompt before any Gemini calls. The user explicitly clicks Start
        Generation in the UI (sends action='start_generation') to advance.

        The voice/tool path (`_tool_start_pipeline`) still auto-approves and
        kicks off generation — that's a different code path used when the
        user issues a single voice command for an unattended run.
        """
        p = self._active_pipeline
        if not p:
            return
        print(f"[pipeline] {len(p.prompts)} prompt(s) loaded — waiting for "
              "user to approve and click Start Generation", flush=True)
        self._pipeline_broadcast()

    def _detect_chunks(self, text: str) -> list:
        """Find obvious section boundaries in the document and split the text
        into chunks — one per section. If no clear dividers, return a single
        chunk (the whole text).

        Dividers we recognize (in priority order):
          1. Horizontal lines made of box-drawing chars (━━━, ━, ─) — often
             used around slide/section titles in creative docs.
          2. Markdown H1/H2 headings (# or ##) that repeat 3+ times.
          3. Horizontal rules (---, ===, ***) repeated 3+ times.
        """
        import re as _re
        lines = text.split("\n")

        def find_lines(pattern: str) -> list:
            return [i for i, ln in enumerate(lines) if _re.match(pattern, ln)]

        # Strategy 1: box-drawing / heavy-line section markers.
        # Includes lines like "━━━ SLIDE 01 ..." where 3 box chars introduce
        # a section title, as well as full rules "═══════".
        box = find_lines(r"^\s*[━─═▬▀]{3,}")
        if len(box) >= 3:
            return self._chunks_at(lines, box)

        # Strategy 2: repeated H1/H2 headings
        h12 = find_lines(r"^#{1,2}\s+")
        if len(h12) >= 3:
            return self._chunks_at(lines, h12)

        # Strategy 3: horizontal rules
        hr = find_lines(r"^[\-=*_]{3,}\s*$")
        if len(hr) >= 3:
            return self._chunks_at(lines, hr)

        # No clear dividers — return the whole text as one chunk
        return [text]

    def _chunks_at(self, lines: list, split_line_indices: list) -> list:
        """Split `lines` into chunks using the given 0-based line indices as
        starts. Chunks 10+ lines each, else merged into neighbour."""
        out = []
        for i, idx in enumerate(split_line_indices):
            end = split_line_indices[i + 1] if i + 1 < len(split_line_indices) else len(lines)
            chunk = "\n".join(lines[idx:end]).strip()
            if chunk:
                out.append(chunk)
        # Merge any chunks shorter than 200 chars into the next one
        merged = []
        buf = ""
        for c in out:
            if len(buf) < 200:
                buf = (buf + "\n\n" + c).strip() if buf else c
            else:
                merged.append(buf)
                buf = c
        if buf:
            merged.append(buf)
        return merged if merged else out

    def _extract_prompts_with_gemma(self, md_text: str) -> list:
        """Extract prompts from markdown. Returns a list of dicts:
            [{"title": "SLIDE 1 — HOOK · …", "text": "Generate an image…"}, …]
        Titles are preserved through the pipeline so post-slot inference
        can trust the `SLIDE N` heading (vs. guessing from text body)."""
        # Tell the dashboard we're starting parser work — this is the
        # "doing prompts analysis" indicator the user expects right after
        # an upload lands.
        try:
            self._broadcast(type="parsing_started",
                            chars=len(md_text),
                            message="Analyzing prompts…")
        except Exception:
            pass

        from prompts_parser import extract_candidates, validate_with_gemma
        candidates = extract_candidates(md_text)
        print(f"[parse] extracted {len(candidates)} prompt-candidate(s)", flush=True)

        if not candidates:
            try:
                self._broadcast(type="parsing_done", count=0,
                                message="No prompts found.")
            except Exception:
                pass
            return []

        out: list = []
        p = self._active_pipeline
        for i, cand in enumerate(candidates):
            if p:
                self._pipeline_broadcast({
                    "processing": True,
                    "processing_label": f"Gemma validating {i+1}/{len(candidates)}: {cand.get('title','')[:50]}",
                })
            cleaned = validate_with_gemma(cand, model=MODEL)
            if cleaned:
                out.append({"title": cand.get("title", ""), "text": cleaned})
                print(f"[parse] {i+1}/{len(candidates)} OK — {cleaned[:70]}…", flush=True)
            else:
                print(f"[parse] {i+1}/{len(candidates)} rejected ({cand.get('title','')[:40]})", flush=True)
        # Full-text SHA1 dedup. Legit distinct slides often share an opening
        # sentence ("Ultra-photorealistic technical engineering cutaway…"),
        # so dedup on FULL text, not first-100-chars.
        import hashlib as _h
        seen = set()
        deduped = []
        for item in out:
            key = _h.sha1(item["text"].strip().encode("utf-8", errors="replace")).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        try:
            self._broadcast(type="parsing_done",
                            count=len(deduped),
                            extracted=len(candidates),
                            message=f"Found {len(deduped)} prompts.")
        except Exception:
            pass
        return deduped

    def _extract_prompts_single_call(self, md_text: str) -> list:
        """One Gemma call for a manageably-sized chunk."""
        print(f"[gemma-extract] asking Gemma to parse {len(md_text)} chars…", flush=True)
        try:
            # Stream the JSON reply so connection stays alive on slow runs
            with requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": MODEL,
                    "format": "json",
                    "stream": True,
                    # Bigger output budget — a 63 KB doc with 4 long prompts
                    # verbatim is ~4K chars; larger real files may need 16K+
                    "options": {"temperature": 0.1, "num_predict": 32000,
                                "num_ctx": 65536},
                    "messages": [
                        {"role": "system", "content": (
                            "You are a COMPLETE-EXTRACTION parser. The user will "
                            "paste a markdown document. Your job is to extract "
                            "EVERY image-generation prompt in it — do not stop "
                            "after a few; keep going until you have processed "
                            "the entire document.\n\n"
                            "Prompts appear under labels like 'GROK PROMPT:', "
                            "'NB2 PROMPT:', 'Prompt N:', inside headings, in "
                            "numbered or bulleted lists, or inside fenced code "
                            "blocks.\n\n"
                            "For each prompt, capture the DESCRIPTIVE TEXT that "
                            "an image model would need — drop labels, numbering, "
                            "and decorative markdown. If a prompt spans multiple "
                            "paragraphs, concatenate them into one string. Be "
                            "THOROUGH: if the document mentions N slides or N "
                            "prompts, your output array must contain N strings.\n\n"
                            'Respond strictly as JSON: {"prompts": ["...", "...", ...]}'
                        )},
                        {"role": "user", "content": md_text},
                    ],
                },
                stream=True,
                timeout=(10, 600),       # 10s connect, 10min read
            ) as r:
                r.raise_for_status()
                content_parts = []
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    piece = (chunk.get("message") or {}).get("content", "")
                    if piece:
                        content_parts.append(piece)
                    if chunk.get("done"):
                        break
                content = "".join(content_parts)
            print(f"[gemma-extract] raw reply ({len(content)} chars): {content[:300]!r}", flush=True)
            data = None
            try:
                data = json.loads(content)
            except json.JSONDecodeError as e:
                # Partial-JSON recovery: Gemma ran out of tokens mid-array.
                # Find the last complete string in "prompts": [ ... ] and close
                # the JSON so we salvage what we have.
                print(f"[gemma-extract] JSON truncated ({e}); attempting recovery…", flush=True)
                import re as _re
                arr_match = _re.search(r'"prompts"\s*:\s*\[(.*)', content, _re.DOTALL)
                if arr_match:
                    body = arr_match.group(1)
                    strings = _re.findall(r'"((?:[^"\\]|\\.)*)"', body)
                    if strings:
                        recovered = [s.replace('\\"', '"').replace('\\n', '\n')
                                     for s in strings]
                        print(f"[gemma-extract] recovered {len(recovered)} complete prompt strings", flush=True)
                        data = {"prompts": recovered}
            if data is None:
                return []
            if isinstance(data, dict):
                arr = data.get("prompts") or data.get("items") or data.get("list") or []
            elif isinstance(data, list):
                arr = data
            else:
                arr = []
            out = []
            for item in arr:
                if isinstance(item, str):
                    s = item.strip()
                elif isinstance(item, dict):
                    s = (item.get("prompt") or item.get("text") or item.get("description")
                         or item.get("title") or "")
                    s = str(s).strip()
                else:
                    continue
                if s and len(s) > 5:
                    out.append(s)
            return out
        except Exception as e:
            print(f"[gemma-extract] {e}")
            return []

    def _pipeline_load_md(self, path: str) -> tuple:
        """Load prompts from `path` into the active pipeline. Returns (ok, msg).

        Extracted prompts are written to the prompts DB so the review state
        survives mother restarts. Re-uploading the same file is idempotent
        (UNIQUE on source_hash + text_hash). The pipeline's in-memory list
        is rebuilt from the DB so prompt ids match DB row ids — the UI uses
        these ids when sending approve/reject/edit actions.
        """
        p = self._active_pipeline
        if not p:
            return False, "no active pipeline"
        if not path or not os.path.exists(path):
            return False, f"not found: {path}"
        try:
            text = open(path, "r", encoding="utf-8", errors="replace").read()
        except Exception as e:
            return False, f"read error: {e}"

        # Gemma is the only parser — no regex fallback, no custom processing.
        prompts = self._extract_prompts_with_gemma(text)
        if not prompts:
            return False, "Gemma could not extract prompts from the file"
        # Accept either the new dict shape {title, text} or the older bare
        # string shape so older callers/fallback code still works.
        normalised: list = []
        for pr in prompts:
            if isinstance(pr, dict):
                normalised.append({
                    "title":  pr.get("title", ""),
                    "text":   pr.get("text", ""),
                    "source": pr.get("source", "validated"),
                })
            else:
                normalised.append({"title": "", "text": pr,
                                   "source": "validated"})
        print(f"[pipeline] Gemma extracted {len(normalised)} prompt(s) "
              f"from {os.path.basename(path)}")

        # Persist to the prompts DB. UNIQUE on (source_hash, text_hash) so a
        # second upload of the same file dedupes against the existing rows
        # (preserving any prior approve/reject/edit state).
        source_hash = lotus_prompts_db.file_hash(path)
        rows = lotus_prompts_db.add_batch(path, source_hash, normalised)
        print(f"[pipeline] DB now has {len(rows)} prompt(s) for this source "
              f"(hash={source_hash[:12]})")

        p.source_md = path
        p.source_hash = source_hash
        p.prompts = [
            {
                "id":           str(r["id"]),
                "db_id":        r["id"],
                "title":        r["slide_title"],
                "topic_number": r["topic_number"],
                "slide_number": r["slide_number"],
                "text":         r["edited_text"] or r["text"],
                "edited":       bool(r["edited_text"]),
                "status":       r["status"],
            }
            for r in rows
        ]
        p.stage = "review_prompts"
        self._pipeline_broadcast()
        counts = lotus_prompts_db.count_by_status(source_hash)
        return True, (f"Loaded {len(rows)} prompts from "
                      f"{os.path.basename(path)} "
                      f"(approved={counts['approved']}, "
                      f"pending={counts['pending']}, "
                      f"rejected={counts['rejected']}). Please review.")

    def _pipeline_set_prompt_status(self, prompt_id: str, status: str) -> None:
        p = self._active_pipeline
        if not p: return
        # Normalise the status spelling ('denied' is the legacy in-memory
        # value; the DB schema uses 'rejected').
        db_status = "rejected" if status in ("denied", "rejected") else status
        for item in p.prompts:
            if str(item["id"]) == str(prompt_id):
                item["status"] = db_status
                db_id = item.get("db_id")
                if db_id is not None:
                    try:
                        lotus_prompts_db.set_status(int(db_id), db_status)
                    except Exception as e:
                        print(f"[prompts-db] set_status({db_id}, "
                              f"{db_status}): {e}", flush=True)
                break
        self._pipeline_broadcast()

    def _pipeline_approve_all_prompts(self) -> None:
        p = self._active_pipeline
        if not p: return
        for item in p.prompts:
            if item["status"] == "pending":
                item["status"] = "approved"
        # Mirror to DB — only flip rows that are still pending so a
        # previously-rejected prompt isn't silently re-approved.
        if p.source_hash:
            try:
                with lotus_prompts_db._lock, lotus_prompts_db._conn() as c:
                    c.execute(
                        "UPDATE prompts SET status='approved', "
                        "updated_at=? WHERE source_hash=? AND status='pending'",
                        (datetime.utcnow().isoformat(), p.source_hash),
                    )
            except Exception as e:
                print(f"[prompts-db] approve_all: {e}", flush=True)
        self._pipeline_broadcast()

    def _pipeline_approve_subset_and_run(self, prompt_ids: list,
                                         scope_topic: Optional[int] = None) -> str:
        """Sync prompt status to a checkbox selection, then kick generation.

        UI semantics: the checkbox state IS the next batch. So we:
          - Within `scope_topic` (a single post number) — or all prompts
            when scope is None — set every prompt's status to match the
            selection: listed → 'approved', unlisted-currently-approved →
            'pending'. Rejected rows are NEVER touched (those are explicit
            user vetoes; un-rejecting must be a deliberate action).
          - Then start the existing render loop. It picks up only what's
            currently approved, so this naturally runs ONLY the checked
            subset.

        Reject (option b) if generation is already running.

        `prompt_ids` accepts both string and int ids; `db_id` is int.
        """
        p = self._active_pipeline
        if not p:
            return "No active pipeline."
        if p.stage == "generating":
            return ("Generation already running for this pipeline — "
                    "finish or cancel the current batch first.")
        if not prompt_ids:
            return "No prompts selected."

        wanted = {str(i) for i in prompt_ids}
        approved_db: list = []
        reverted_db: list = []
        for item in p.prompts:
            cur = item.get("status")
            sid = str(item.get("id"))
            if cur == "rejected" or cur == "denied":
                continue   # never auto-touch explicit rejections
            in_scope = (scope_topic is None
                        or item.get("topic_number") == scope_topic)
            if in_scope:
                # Within scope: ticked → approved, unticked-currently-approved
                # → pending. This is the post the user is operating on.
                if sid in wanted:
                    if cur != "approved":
                        item["status"] = "approved"
                        if item.get("db_id") is not None:
                            approved_db.append(item["db_id"])
                else:
                    if cur == "approved":
                        item["status"] = "pending"
                        if item.get("db_id") is not None:
                            reverted_db.append(item["db_id"])
            else:
                # OUT of scope: per-post Run Batch means "this post is the
                # ENTIRE next batch" — revert any other-post approvals to
                # pending so they don't sneak into this generation. The
                # master "Run All Batches" passes scope_topic=None and
                # never enters this branch (in_scope is always True).
                if cur == "approved":
                    item["status"] = "pending"
                    if item.get("db_id") is not None:
                        reverted_db.append(item["db_id"])

        if (approved_db or reverted_db) and p.source_hash:
            try:
                with lotus_prompts_db._lock, lotus_prompts_db._conn() as c:
                    now = datetime.utcnow().isoformat()
                    if approved_db:
                        ph = ",".join("?" * len(approved_db))
                        c.execute(
                            f"UPDATE prompts SET status='approved', updated_at=? "
                            f"WHERE id IN ({ph}) AND status != 'rejected' AND status != 'denied'",
                            (now, *approved_db),
                        )
                    if reverted_db:
                        ph = ",".join("?" * len(reverted_db))
                        c.execute(
                            f"UPDATE prompts SET status='pending', updated_at=? "
                            f"WHERE id IN ({ph}) AND status='approved'",
                            (now, *reverted_db),
                        )
            except Exception as e:
                print(f"[prompts-db] sync_subset: {e}", flush=True)

        # Run Batch is an explicit "go now" signal, so unpause whatever
        # state the pipeline is in. Without this, a rehydrated pipeline
        # (paused per A3 policy) or a pipeline paused on prior failures
        # would silently swallow the click.
        p.paused = False
        p.pause_reason = None
        try:
            p._resume_event.set()
        except Exception: pass
        self._stop_event.clear()
        # Kick generation on a worker thread.
        import threading as _th
        _th.Thread(target=self._pipeline_generate_approved,
                   daemon=True).start()
        self._pipeline_broadcast()
        scope_label = f"Post {scope_topic}" if scope_topic is not None else "all posts"
        return (f"{scope_label}: {len(approved_db)} newly approved, "
                f"{len(reverted_db)} reverted to pending; generation started.")

    def _pipeline_edit_prompt(self, prompt_id: str, new_text: str) -> None:
        """User edited an extracted prompt — persist to DB and revert status
        to pending so they re-approve after the change."""
        p = self._active_pipeline
        if not p: return
        new_text = (new_text or "").strip()
        if not new_text:
            return
        for item in p.prompts:
            if str(item["id"]) == str(prompt_id):
                item["text"]   = new_text
                item["edited"] = True
                item["status"] = "pending"
                db_id = item.get("db_id")
                if db_id is not None:
                    try:
                        lotus_prompts_db.update_text(int(db_id), new_text)
                    except Exception as e:
                        print(f"[prompts-db] update_text({db_id}): {e}",
                              flush=True)
                break
        self._pipeline_broadcast()

    @staticmethod
    def _infer_post_slots(prompts: list) -> list:
        """Given a list of prompt dicts, return a list of (post_index,
        frame_index) tuples — one per prompt. `frame_index` is the
        1-based slot inside the post (resets to 1 on every new post),
        used directly as the FrameN.png filename component.

        Strategy for detecting a NEW post (most reliable first):
          1. TITLE has "SLIDE N" — trust it (markdown H2 header).
          2. TEXT has "slide counter N / M" near end of prompt.
          3. Fall back to sequential.

        A new post begins whenever the detected slide-marker `n` drops
        from the previous (e.g. 10 → 1) or matches 1 after at least one
        slide has been emitted. Once `post_idx` increments, a per-post
        counter `frame_idx` resets to 1 — that's what writes to disk.
        Previously the code used `n` itself as the filename index, which
        caused Post3 to start at Frame2 when the first prompt of the new
        post had `n == 2` (typical for files where slide numbering isn't
        strictly per-post).
        """
        import re as _re
        results: list = []
        post_idx = 1
        last_n = 0
        frame_idx = 0   # per-post counter, resets on new post
        for i, pr in enumerate(prompts):
            title = (pr.get("title") or "").upper()
            text  = (pr.get("text") or pr.get("prompt_text") or "")

            n = None
            # 1. Title — most reliable.
            tm = _re.search(r"\bSLIDE\s*(\d+)\b", title)
            if tm:
                n = int(tm.group(1))
            # 2. End-of-text slide counter.
            if n is None:
                tail = text[-600:]
                cm = _re.search(
                    r'(?:slide\s*counter[^"]{0,80}|"\s*)(\d{1,2})\s*[/\-]\s*\d{1,2}',
                    tail, _re.IGNORECASE,
                )
                if cm:
                    n = int(cm.group(1))
            # 3. Fallback.
            if n is None:
                n = last_n + 1

            # New post: slide number drops from previous (e.g. 10 → 1 or 8 → 2)
            # OR matches 1 after at least one slide has been emitted.
            if last_n > 0 and n <= last_n:
                post_idx += 1
                frame_idx = 0  # reset per-post counter
            last_n = n
            frame_idx += 1
            results.append((post_idx, frame_idx))
        return results

    def _pipeline_generate_approved(self) -> None:
        """Loop through approved prompts generating each image DIRECTLY
        into the proper Post{N}/Frame{M}.png based on slide markers. No
        staging + manual Save required — frames land in the correct folder
        as they render. Review stage can still delete + regenerate bad
        ones in-place."""
        p = self._active_pipeline
        if not p: return
        p.stage = "generating"
        self._pipeline_broadcast()
        # A10 — render output goes into THIS pipeline's own folder so
        # frames from a parallel/earlier pipeline never overwrite. The
        # folder name was stamped at Pipeline.__init__ and persists in
        # pipelines.db.
        pipeline_parent = p.parent_folder()
        # Register this run in the history DB so the Gallery tab can
        # group generated frames by pipeline name + timestamp.
        try:
            import lotus_history
            p.history_run_id = lotus_history.start_run(
                name=p.name or p.id,
                source_md=getattr(p, "source_md", ""),
                parent_folder=os.path.basename(pipeline_parent),
            )
        except Exception as _e:
            print(f"[pipeline] history start_run failed: {_e}")
            p.history_run_id = None

        # Compute per-prompt (post_idx, slide_idx) so each frame lands in
        # Post{post_idx}/Frame{slide_idx}.png. If we can't infer, we fall
        # back to sequential Post1/FrameN — the next_frame_path() already
        # handles collisions by picking the next available integer.
        slots = self._infer_post_slots(p.prompts)
        root = os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects"))

        # Fresh run — clear any stale stop signal.
        self._stop_event.clear()

        # ── Pre-build every frame's target path + state ──
        # All frames are created up front so the dashboard shows the full
        # row count immediately. **Resume support**: any target file that
        # already exists on disk (from a previous run that got cancelled
        # or hit an emergency stop) is marked `pending_review` right away
        # and NOT re-queued — the pipeline picks up exactly where it left
        # off. To force a re-render of an existing frame the user hits
        # ↻ Regenerate on the review card.
        pipeline_frames: list = []
        batch_tasks:     list = []   # (prompt, target) pairs to actually render
        batch_frames:    list = []   # aligned list of frame dicts for those tasks
        resumed = 0
        # A18 dedupe — the IDs we're about to (re)build. Any existing
        # entries in p.frames with these IDs (from prior Run Batch clicks)
        # get DROPPED before we append fresh ones, otherwise the in-memory
        # frames list accumulates duplicates with the same id ("f2", "f2",
        # "f2") which makes the lightbox + per-frame actions ambiguous.
        about_to_build_ids = {
            f"f{i+1}" for i, prompt in enumerate(p.prompts)
            if prompt["status"] == "approved"
        }
        if about_to_build_ids:
            before = len(p.frames)
            p.frames = [f for f in p.frames
                        if f.get("id") not in about_to_build_ids]
            removed = before - len(p.frames)
            if removed:
                print(f"[pipeline] dedupe: dropped {removed} stale frame "
                      f"entr{'y' if removed == 1 else 'ies'} before rebuild",
                      flush=True)
        for i, prompt in enumerate(p.prompts):
            if prompt["status"] != "approved":
                continue
            post_idx, slide_idx = slots[i]
            section = f"Post{post_idx}"
            try:
                target_path = lotus_config.next_frame_path(
                    section, slide_idx, parent=pipeline_parent)
            except Exception as e:
                print(f"[pipeline] section {section} rejected ({e}) — using Post1/next")
                target_path = lotus_config.next_frame_path(
                    "Post1", parent=pipeline_parent)
                section = "Post1"
            rel = os.path.relpath(target_path, root).replace(os.sep, "/")
            url = "/projects/" + rel
            fid = f"f{i+1}"

            # RESUME CHECK — if the target file already exists (non-zero
            # size, not a broken tmp), treat it as already-done.
            already_done = False
            try:
                already_done = (
                    os.path.exists(target_path)
                    and os.path.getsize(target_path) > 2048  # reject empty / half-writes
                )
            except Exception:
                already_done = False

            frame = {
                "id": fid,
                "prompt_id": prompt["id"],
                "prompt_text": prompt["text"],
                "section": section,
                "slide_idx": slide_idx,
                "final_path": target_path,
                "final_url":  url if already_done else None,
                "temp_url":   url if already_done else None,
                "status":     "pending_review" if already_done else "generating",
                "resumed":    already_done,
                "source":     "lotus" if already_done else None,  # "lotus"|"manual"|"skipped"|None
                "error":      None,                               # populated when status=failed
                "retry_count":0,                                   # ReviewAgent auto-retry counter (max 3)
            }
            p.frames.append(frame)
            pipeline_frames.append(frame)
            if already_done:
                resumed += 1
                print(f"[pipeline] RESUME — {fid} already exists at "
                      f"{section}/{os.path.basename(target_path)}, skipping render")
            else:
                batch_tasks.append((prompt["text"], target_path))
                batch_frames.append(frame)
        if resumed:
            print(f"[pipeline] resume: {resumed} frame(s) already on disk; "
                  f"rendering the remaining {len(batch_tasks)}")
        self._pipeline_broadcast()

        # ── Parallel render via gemini_bot's 10-tab batch API ──
        try:
            import gemini_bot
        except Exception as e:
            print(f"[pipeline] gemini_bot unavailable ({e}) — aborting parallel mode")
            return

        # 5 posts in parallel — each tab drains ONE post's slides
        # sequentially in a single Gemini conversation. Keeps chat history
        # aligned per-post AND is gentler on Google's bot detection than
        # 10 concurrent new-chat requests. Bump with LOTUS_PIPELINE_POSTS_CONCURRENCY.
        POSTS_CONCURRENCY = int(
            os.environ.get("LOTUS_PIPELINE_POSTS_CONCURRENCY", "5"))

        # Group the rendering work by section (Post1, Post2, …).
        # batch_frames / batch_tasks are already aligned.
        post_groups: dict = {}
        for fr, tk in zip(batch_frames, batch_tasks):
            post_groups.setdefault(fr["section"], []).append((fr, tk))

        posts_ordered = sorted(post_groups.keys(),
                               key=lambda k: int(k.replace("Post", "") or 0))
        if not posts_ordered:
            print(f"[pipeline] nothing to render — all {len(pipeline_frames)} "
                  f"frames already on disk")

        def _build_on_done(window_start: int):
            """Returns an on_done closure for the current window of posts.
            `post_idx` in the callback is the index WITHIN the window;
            translate it to the absolute post via window_start."""
            def _cb(post_idx_in_window: int, slide_idx: int, path):
                if p.cancelled or self._stop_event.is_set():
                    return
                absolute = window_start + post_idx_in_window
                if absolute >= len(posts_ordered):
                    return
                section = posts_ordered[absolute]
                frames_tasks = post_groups[section]
                if slide_idx >= len(frames_tasks):
                    return
                fr, _ = frames_tasks[slide_idx]
                if path and os.path.exists(path):
                    rel2 = os.path.relpath(path, root).replace(os.sep, "/")
                    url2 = "/projects/" + rel2
                    fr["final_path"] = path
                    fr["final_url"]  = url2
                    fr["temp_url"]   = url2
                    fr["status"]     = "pending_review"
                    print(f"[pipeline] saved {fr['id']} → "
                          f"{section}/{os.path.basename(path)}")
                else:
                    fr["status"] = "failed"
                    print(f"[pipeline] {fr['id']} FAILED "
                          f"({section}/slide {fr['slide_idx']})")
                self._pipeline_broadcast()
            return _cb

        # BURST MODE: one post at a time, send-all-then-harvest.
        # Sidesteps the "second slide never sends" bug — we queue every
        # prompt in the chat first, then iterate the rendered <img> tags
        # in DOM order and save each. No per-slide detection dependency.
        for post_idx_abs, section in enumerate(posts_ordered):
            if self._stop_event.is_set() or p.cancelled:
                print(f"[pipeline] stopped at post {post_idx_abs+1}")
                return
            tasks = [tk for _, tk in post_groups[section]]
            frames = [fr for fr, _ in post_groups[section]]
            print(f"[pipeline] post {post_idx_abs+1}/{len(posts_ordered)} "
                  f"{section}: burst-sending {len(tasks)} slide(s)…")

            def _cb(_post_idx_ignored, slide_idx, path, frames=frames, section=section):
                if p.cancelled or self._stop_event.is_set():
                    return
                if slide_idx >= len(frames): return
                fr = frames[slide_idx]
                if path and os.path.exists(path):
                    rel2 = os.path.relpath(path, root).replace(os.sep, "/")
                    fr["final_path"] = path
                    fr["final_url"]  = "/projects/" + rel2
                    fr["temp_url"]   = fr["final_url"]
                    fr["status"]     = "pending_review"
                    fr["source"]     = "lotus"
                    fr["error"]      = None
                    print(f"[pipeline] saved {fr['id']} → "
                          f"{section}/{os.path.basename(path)}")
                    # Agent mesh: submit this freshly-saved frame to
                    # ReviewAgent in the background. The watcher thread
                    # auto-retries up to 3x on text-fidelity rejection.
                    self._submit_frame_for_review(fr)
                    # Persist to history DB for the Gallery tab.
                    try:
                        if getattr(p, "history_run_id", None):
                            import lotus_history
                            lotus_history.add_frame(
                                run_id=p.history_run_id,
                                post=section,
                                frame_num=slide_idx + 1,
                                path=path,
                            )
                    except Exception as _e:
                        print(f"[pipeline] history add_frame failed: {_e}")
                else:
                    fr["status"] = "failed"
                    # Tag error with the most recent bot-stderr hint if we can
                    # infer it; gemini_bot doesn't return a reason string, so
                    # for now we just label the stage. UI renders this on hover.
                    fr["error"] = fr.get("error") or "Image render or download failed (see mother log)"
                    print(f"[pipeline] {fr['id']} FAILED ({section})")
                    # Append to the pipeline's error log so the dashboard can
                    # show a chronological view without tailing mother.log.
                    try:
                        import time as _time
                        p.errors.append({
                            "ts":       _time.time(),
                            "frame_id": fr["id"],
                            "section":  section,
                            "stage":    "generate",
                            "message":  fr["error"],
                        })
                        if len(p.errors) > 500:
                            del p.errors[:len(p.errors) - 500]
                    except Exception: pass
                self._pipeline_broadcast()

            # Burst with one automatic retry on exception. Transient
            # failures (CDP blip, Chrome window churn, page-navigation
            # races) frequently succeed on the second attempt. The
            # _connect_cdp_with_retry helper inside gemini_bot already
            # absorbs short Chrome restarts; this outer retry covers the
            # case where the FIRST attempt got far enough to start a
            # burst but then died mid-flight.
            burst_attempts = 0
            while burst_attempts < 2:
                burst_attempts += 1
                try:
                    gemini_bot.create_post_burst(
                        tasks, verbose=True, on_done=_cb,
                    )
                    break
                except Exception as e:
                    print(f"[pipeline] burst error on {section} "
                          f"(attempt {burst_attempts}/2): {e}")
                    try:
                        import time as _time
                        p.errors.append({
                            "ts":       _time.time(),
                            "frame_id": "",
                            "section":  section,
                            "stage":    "burst",
                            "message":  f"burst error (attempt {burst_attempts}/2): {e}",
                        })
                    except Exception: pass
                    if burst_attempts < 2:
                        # Brief wait before retry — gives CDP / Chrome a
                        # chance to settle if the failure was a transient
                        # connection blip.
                        import time as _time
                        _time.sleep(3.0)

            # Sweep stuck frames. If create_post_burst raised before
            # processing every frame in `tasks`, those unfinished frames
            # are still in 'generating' status (the on_done callback only
            # fires for frames that reached either save-success or a
            # per-frame fail). Without this sweep they'd stay stuck
            # forever — the dashboard renders Approve/Deny only for
            # 'pending_review' and Retry/Skip only for 'failed', so
            # 'generating' is a dead state at the review stage.
            stuck = [fr for fr in frames if fr.get("status") == "generating"]
            if stuck:
                import time as _time
                stuck_ts = _time.time()
                for fr in stuck:
                    fr["status"] = "failed"
                    fr["error"]  = (
                        fr.get("error")
                        or "Render burst exited before this frame completed"
                    )
                    p.errors.append({
                        "ts":       stuck_ts,
                        "frame_id": fr.get("id", ""),
                        "section":  section,
                        "stage":    "burst",
                        "message":  "stuck-frame swept to failed (burst aborted)",
                    })
                print(f"[pipeline] {section} swept {len(stuck)} stuck frame(s) → failed",
                      flush=True)

            # Post-boundary status. Any frame in THIS post that ended as
            # 'failed' is logged for the dashboard's error feed and the
            # review_frames stage to pick up — but we DO NOT block the
            # render loop here. Pausing forever after every failed post
            # is a worse UX than letting the loop finish all approved
            # prompts and surfacing failures at the end (where the user
            # can upload/retry/skip in batch).
            if not p.cancelled and not self._stop_event.is_set():
                unresolved = [fr for fr in frames if fr.get("status") == "failed"]
                if unresolved:
                    print(f"[pipeline] {section} finished with "
                          f"{len(unresolved)} failed frame(s) — continuing "
                          f"to next post (resolve at review_frames stage)",
                          flush=True)
                    self._pipeline_broadcast()

        if not p.cancelled:
            p.stage = "review_frames"
            self._pipeline_broadcast()
        # Close out the history run regardless of whether it's still active.
        try:
            if getattr(p, "history_run_id", None):
                import lotus_history
                lotus_history.end_run(p.history_run_id, status="completed")
        except Exception: pass

    def _stage_pipeline_frame(self, pid: str, fname: str, src_path: str) -> tuple:
        """Move src into Projects/.pipeline/<pid>/<fname> so dashboard can render it
        via /projects/.pipeline/<pid>/<fname>. Returns (path, url)."""
        root = os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects"))
        staged_dir = os.path.join(root, ".pipeline", pid)
        os.makedirs(staged_dir, exist_ok=True)
        dst = os.path.join(staged_dir, fname)
        import shutil as _sh
        try:
            if src_path != dst:
                _sh.move(src_path, dst)
        except Exception:
            _sh.copy2(src_path, dst)
        rel = os.path.relpath(dst, root).replace(os.sep, "/")
        return (dst, "/projects/" + rel)

    def _pipeline_frame_upload(self, frame_id: str, src_path: str,
                                new_name: Optional[str] = None,
                                force: bool = False) -> tuple:
        """User-provided image file is now at `src_path`. Move it into the
        correct Post{N}/Frame{M}.png slot (or a renamed path if caller passed
        `new_name`). Marks the frame as status=pending_review, source=manual.

        Returns (ok, data) where data is a dict:
          - on success:   {"message": "saved to ...", "path": final_path}
          - on collision: {"collision": True, "existing_filename": "...",
                           "default_rename": "..._v2.ext"}
          - on error:     {"message": "..."}
        """
        p = self._active_pipeline
        if not p:
            return (False, {"message": "no active pipeline"})
        fr = next((f for f in p.frames if f["id"] == frame_id), None)
        if not fr:
            return (False, {"message": f"frame {frame_id} not found"})

        final_path = fr.get("final_path")
        if not final_path:
            return (False, {"message": f"frame {frame_id} has no target path"})

        # If user supplied a new_name, replace just the basename.
        if new_name:
            # Sanitise — no path separators, no empty, keep extension sensible.
            safe = new_name.replace("/", "_").replace("\\", "_").strip()
            if not safe:
                return (False, {"message": "new_name was empty after sanitising"})
            final_path = os.path.join(os.path.dirname(final_path), safe)

        # Collision check — only if user didn't force an overwrite AND isn't
        # renaming to a new filename. Returns a structured response so the
        # dashboard can pop a modal with Overwrite / Save-As / Cancel.
        if not force and not new_name and os.path.exists(final_path):
            base = os.path.basename(final_path)
            stem, ext = os.path.splitext(base)
            default_rename = f"{stem}_v2{ext}"
            return (False, {
                "collision":          True,
                "existing_filename":  base,
                "existing_path":      final_path,
                "default_rename":     default_rename,
                "message":            f"{base} already exists — pick Overwrite or Save-As",
            })

        try:
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            import shutil as _sh
            _sh.move(src_path, final_path)
        except Exception as e:
            return (False, {"message": f"move failed: {e}"})

        # Update the frame in place.
        root = os.path.expanduser(lotus_config.load()["image"].get(
            "projects_root", "~/LotusAgent/Projects"))
        rel = os.path.relpath(final_path, root).replace(os.sep, "/")
        fr["final_path"] = final_path
        fr["final_url"]  = "/projects/" + rel
        fr["temp_url"]   = fr["final_url"]
        fr["status"]     = "pending_review"
        fr["source"]     = "manual"
        fr["error"]      = None
        print(f"[pipeline] {frame_id} uploaded by user → {final_path}")

        # History DB — distinguish manual uploads from bot-rendered frames.
        try:
            if getattr(p, "history_run_id", None):
                import lotus_history
                lotus_history.add_frame(
                    run_id=p.history_run_id,
                    post=fr.get("section", ""),
                    frame_num=fr.get("slide_idx", 0) + 1,
                    path=final_path,
                )
        except Exception as _e:
            print(f"[pipeline] history add_frame (manual) failed: {_e}")

        self._pipeline_broadcast()
        return (True, {"message": f"saved to {final_path}", "path": final_path})

    # ── Child-process linking (Phase D2) ────────────────────────────────

    def _link_configured_children(self) -> None:
        """Parse LOTUS_CHILDREN and register a RemoteAgent for each
        (child_url, agent_name) pair. Registry supports multiple providers
        per agent name, so local+remote coexist and `pick_best()` picks
        the least-busy one at submit time.

        Format:
          LOTUS_CHILDREN="ws://host1:port1?agents=A,B;ws://host2:port2?agents=C"
        """
        raw = os.environ.get("LOTUS_CHILDREN", "").strip()
        if not raw:
            return
        token_path = os.path.expanduser("~/.lotus_auth/token.txt")
        try:
            token = open(token_path).read().strip()
        except Exception as e:
            print(f"[child-link] cannot read token ({e}) — skipping child links")
            return
        # Split on ';' for multiple children; ',' is used inside agents= list.
        import urllib.parse as _urllib
        import agents as _agents_mod
        for entry in raw.split(";"):
            entry = entry.strip()
            if not entry: continue
            parsed = _urllib.urlparse(entry)
            host_port = f"{parsed.hostname}:{parsed.port}"
            base_url  = f"{parsed.scheme}://{host_port}"
            qs = _urllib.parse_qs(parsed.query or "")
            agent_names = []
            for v in qs.get("agents", []):
                agent_names.extend(n.strip() for n in v.split(",") if n.strip())
            if not agent_names:
                print(f"[child-link] {entry}: no ?agents= specified — skipping")
                continue
            for name in agent_names:
                instance_key = f"{name}@{host_port}"
                remote = _agents_mod.RemoteAgent(
                    name=name,                       # base name (same as local)
                    remote_agent_name=name,          # what to call on the child
                    ws_url=base_url,
                    token=token,
                )
                self._agents.register(remote, instance_key=instance_key)
                print(f"[child-link] registered {instance_key}")

    # ── mDNS auto-discovery of children (Phase D3) ──────────────────────

    def _start_mdns_browser(self) -> None:
        """Browse the LAN for LOTUS children. Any discovered child whose
        token fingerprint matches ours gets its agents registered as
        RemoteAgents automatically. Children that go offline get their
        agents deregistered."""
        try:
            import lotus_mdns
        except Exception as e:
            print(f"[mdns] not available ({e}) — skipping auto-discovery")
            return
        token_path = os.path.expanduser("~/.lotus_auth/token.txt")
        try:
            token = open(token_path).read().strip()
        except Exception as e:
            print(f"[mdns] cannot read token ({e}) — auto-discovery disabled")
            return
        # Instance name → list of (instance_key, agent_name) that we
        # registered for it, so on_remove we can deregister exactly those.
        self._mdns_registered: dict = {}

        def _on_add(rec: dict) -> None:
            import agents as _agents_mod
            host, port = rec["host"], rec["port"]
            host_port = f"{host}:{port}"
            print(f"[mdns] ↓ discovered {rec['hostname']} at {host_port} "
                  f"hosting {rec['agents']}")
            keys: list = []
            for name in rec["agents"]:
                if not name: continue
                instance_key = f"{name}@{host_port}"
                # Skip if we already have this provider (env var might have
                # pre-registered it — mDNS shouldn't duplicate).
                if self._agents.get(instance_key) is not None:
                    if (self._agents.get(instance_key).name == name
                            and hasattr(self._agents.get(instance_key), "ws_url")):
                        continue
                remote = _agents_mod.RemoteAgent(
                    name=name,
                    remote_agent_name=name,
                    ws_url=f"ws://{host_port}",
                    token=token,
                )
                self._agents.register(remote, instance_key=instance_key)
                keys.append((instance_key, name))
                print(f"[mdns]   registered {instance_key}")
            self._mdns_registered[rec["instance"]] = keys
            self._emit_controller_alert(
                level="info", agent="LOTUS",
                message=f"Child {rec['hostname']} ({host_port}) joined · "
                        f"agents: {', '.join(rec['agents'])}",
            )

        def _on_remove(instance: str) -> None:
            keys = self._mdns_registered.pop(instance, [])
            for instance_key, name in keys:
                if self._agents.remove(instance_key):
                    print(f"[mdns] ✗ deregistered {instance_key} (child gone)")
            if keys:
                self._emit_controller_alert(
                    level="warning", agent="LOTUS",
                    message=f"Child {instance.split('.')[0]} went offline · "
                            f"removed {len(keys)} agent(s)",
                )

        try:
            self._mdns_browser = lotus_mdns.ChildBrowser(
                token=token, on_add=_on_add, on_remove=_on_remove,
            )
            print(f"[mdns] browsing for LOTUS children on the LAN…")
        except Exception as e:
            print(f"[mdns] browser start failed ({e}) — auto-discovery disabled")

    # ── Controller heartbeat (Phase 2 of the agent mesh) ────────────────

    def _controller_loop(self) -> None:
        """Supervisor: polls agent health every 10s, flags unhealthy agents,
        pings remote children for liveness (D5), and (if an active pipeline
        has errors) asks Gemma to spot patterns. All decisions are
        broadcast as `controller_alert` events."""
        import time as _time
        last_snap_sig = None
        last_liveness_probe = 0.0   # probe every ~30s, throttled by this
        MAX_PROBE_FAILS = 3         # deregister after this many consecutive misses
        while not getattr(self, "_controller_stop", None) or \
              not self._controller_stop.is_set():
            try:
                self._controller_stop.wait(timeout=10)
                if self._controller_stop.is_set():
                    return
                if not self._agents:
                    continue

                # D5 liveness probe — every 30s, ping every RemoteAgent's
                # health endpoint. After MAX_PROBE_FAILS consecutive misses
                # we deregister the agent (child crashed without sending
                # an mDNS goodbye, or LAN went away).
                now = _time.time()
                if now - last_liveness_probe > 30:
                    last_liveness_probe = now
                    self._probe_remote_agents(MAX_PROBE_FAILS)

                snap = self._agents.health_snapshot()
                # Skip if nothing changed — no point spamming the dashboard.
                sig = tuple(
                    (a["name"], a["busy"], a["success_count"], a["fail_count"])
                    for a in snap
                )
                if sig == last_snap_sig:
                    continue
                last_snap_sig = sig

                # Cheap heuristic: any agent with >= 3 total and fail_rate
                # >= 50% warrants a warning.
                for a in snap:
                    total = a["success_count"] + a["fail_count"]
                    if total < 3:
                        continue
                    fail_rate = a["fail_count"] / total
                    if fail_rate < 0.5:
                        continue
                    key = f"unhealthy:{a['name']}"
                    # Debounce: don't re-emit the same alert within 90s.
                    if _time.time() - self._controller_alert_dedup.get(key, 0) < 90:
                        continue
                    self._controller_alert_dedup[key] = _time.time()
                    self._emit_controller_alert(
                        level="warning",
                        agent=a["name"],
                        message=(
                            f"{a['name']} has failed {a['fail_count']}/{total} "
                            f"({int(fail_rate*100)}%). "
                            f"{'Last error: ' + a['last_error'] if a['last_error'] else ''}"
                        ).strip(),
                    )

                # Deeper Gemma pattern-spotting: look at recent errors in the
                # active pipeline. If 3+ errors in the last minute share the
                # same stage, ask Gemma to characterise the pattern.
                p = self._active_pipeline
                if p and p.errors:
                    now = _time.time()
                    recent = [e for e in p.errors if now - e.get("ts", 0) < 60]
                    if len(recent) >= 3:
                        stages = {e.get("stage", "") for e in recent}
                        key = f"pattern:{','.join(sorted(stages))}"
                        if _time.time() - self._controller_alert_dedup.get(key, 0) >= 120:
                            self._controller_alert_dedup[key] = _time.time()
                            insight = self._ask_gemma_for_pattern(recent)
                            if insight:
                                self._emit_controller_alert(
                                    level="info",
                                    agent="Gemma",
                                    message=insight,
                                )
            except Exception as e:
                # Never let the supervisor die — surface its own failure once.
                print(f"[controller] loop error: {e}")

    def _child_ping(self, host_port: str) -> str:
        """Immediately ping every RemoteAgent registered at `host_port`.
        Triggered from the UI's per-child drawer so users don't have to
        wait for the 30s probe cadence. Returns a short human message."""
        if not host_port or not self._agents:
            return "no child specified"
        matches = []
        for ikey, agent in list(self._agents._agents.items()):
            ws = getattr(agent, "ws_url", "") or ""
            if host_port in ws:
                matches.append((ikey, agent))
        if not matches:
            return f"no remote agents registered at {host_port}"
        ok_count = 0
        for ikey, agent in matches:
            if agent.ping():
                ok_count += 1
        # Broadcast so the UI refreshes ping timestamps.
        self._emit_controller_alert(
            level="info" if ok_count == len(matches) else "warning",
            agent="LOTUS",
            message=f"Manual ping of {host_port}: "
                    f"{ok_count}/{len(matches)} agents responded."
        )
        # Any active pipeline will rebroadcast on next state tick; also
        # surface an immediate agent snapshot by re-broadcasting any active
        # pipeline state.
        if self._active_pipeline:
            self._pipeline_broadcast()
        return f"pinged {len(matches)} agents at {host_port} → {ok_count} alive"

    def _child_disconnect(self, host_port: str) -> str:
        """Forcibly deregister every RemoteAgent at `host_port`. User
        action from the drawer — used when a child machine is being
        shut down or needs to be kicked. Child mDNS re-announcement will
        re-register it within ~30s if the child is still running; to
        keep it kicked, stop the child process too."""
        if not host_port or not self._agents:
            return "no child specified"
        removed: list = []
        for ikey, agent in list(self._agents._agents.items()):
            ws = getattr(agent, "ws_url", "") or ""
            if host_port in ws:
                if self._agents.remove(ikey):
                    removed.append(ikey)
        if not removed:
            return f"no remote agents registered at {host_port}"
        self._emit_controller_alert(
            level="warning", agent="LOTUS",
            message=f"Disconnected {host_port} — removed {len(removed)} agent(s) "
                    f"from registry. Stop the child process to prevent re-announce."
        )
        if self._active_pipeline:
            self._pipeline_broadcast()
        return f"disconnected {len(removed)} agent(s) at {host_port}"

    def _probe_remote_agents(self, max_fails: int) -> None:
        """Ping every RemoteAgent in the registry. After `max_fails`
        consecutive ping failures, deregister it + emit an alert. Called
        on the controller thread (~30s cadence)."""
        if not self._agents:
            return
        # Snapshot keys so we can mutate the registry safely during iteration.
        for ikey in list(self._agents._agents.keys()):
            agent = self._agents._agents.get(ikey)
            if not agent: continue
            # Only probe remote agents (the local ones don't need a ping).
            if not getattr(agent, "ws_url", None):
                continue
            before = agent.ping_fails
            ok = agent.ping()
            if ok and before > 0:
                print(f"[probe] ✓ {ikey} recovered")
            elif not ok:
                print(f"[probe] ✗ {ikey}  ping_fails={agent.ping_fails}")
                if agent.ping_fails >= max_fails:
                    if self._agents.remove(ikey):
                        print(f"[probe] deregistered {ikey} "
                              f"after {max_fails} failed pings")
                        self._emit_controller_alert(
                            level="warning", agent="LOTUS",
                            message=f"Remote agent {ikey} unreachable after "
                                    f"{max_fails} pings — deregistered."
                        )

    def _emit_controller_alert(self, level: str, agent: str, message: str) -> None:
        """Fire a controller_alert WS event to the dashboard."""
        import time as _time
        try:
            self._broadcast(
                type="controller_alert",
                level=level,
                agent=agent,
                message=message,
                ts=_time.time(),
            )
            print(f"[controller] {level.upper()} · {agent}: {message}")
        except Exception as e:
            print(f"[controller] broadcast failed: {e}")

    def _ask_gemma_for_pattern(self, errors: list) -> str:
        """Given a list of recent pipeline errors, ask Gemma to spot a
        short pattern. Returns a brief insight string, empty if none."""
        import requests
        bullets = "\n".join(
            f"- [{e.get('stage','')}] {e.get('section','')} {e.get('frame_id','')}: "
            f"{e.get('message','')}"
            for e in errors[-8:]
        )
        prompt = (
            "You are LOTUS Controller. These pipeline errors happened in the "
            "last minute. Identify ONE short pattern or give a single-sentence "
            "recommendation. If there's no clear pattern, reply 'none'.\n\n"
            f"Errors:\n{bullets}\n\n"
            "Respond with a single sentence (max 25 words), no preamble:"
        )
        try:
            r = requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": MODEL, "prompt": prompt, "stream": False,
                    "options": {"temperature": 0.2},
                },
                timeout=30,
            )
            r.raise_for_status()
            txt = (r.json().get("response") or "").strip()
            if txt.lower().startswith("none") or len(txt) < 12:
                return ""
            # Trim — dashboard strip has limited real estate.
            return txt[:240]
        except Exception:
            return ""

    def _pipeline_archive_rename(self, p) -> Optional[tuple]:
        """Rename today's parent folder so the completed run is preserved
        and the next pipeline gets a clean Techengine<date>/ to work in.

        Suffix = `<pipeline_name>_<HHMMSS>` — matches Somendra's existing
        archive naming (`_luxury_0920`, `_luxury_recovered`).

        Also rewrites each frame's `final_path` / `final_url` / `temp_url`
        in memory so the JSON report archive captures the new location
        AND the dashboard thumbnails keep working after the rename.

        Returns (old_name, new_name) on success, None on no-op/failure.
        """
        if not p:
            return None
        try:
            import time as _time
            suffix = f"{(p.name or 'run')}_{_time.strftime('%H%M%S')}"
            result = lotus_config.archive_parent_folder(suffix)
            if not result:
                return None
            old_path, new_path = result
            old_base = os.path.basename(old_path)
            new_base = os.path.basename(new_path)
            # Rewrite every frame's path/URL so they point to the renamed folder.
            for fr in p.frames:
                for key in ("final_path",):
                    v = fr.get(key)
                    if v and isinstance(v, str) and v.startswith(old_path):
                        fr[key] = new_path + v[len(old_path):]
                for key in ("final_url", "temp_url"):
                    v = fr.get(key)
                    if v and isinstance(v, str):
                        # URLs look like /projects/<folder_basename>/Post1/Frame1.png
                        fr[key] = v.replace(
                            f"/projects/{old_base}/",
                            f"/projects/{new_base}/",
                        )
            print(f"[pipeline] archive rename: {old_base} → {new_base}", flush=True)
            # Broadcast so the UI picks up the new paths immediately.
            self._pipeline_broadcast()
            # Surface a controller alert for dashboard visibility.
            self._emit_controller_alert(
                level="info",
                agent="LOTUS",
                message=(
                    f"Run complete. Folder archived as {new_base}. "
                    f"A fresh {old_base}/ will be created for the next pipeline."
                ),
            )
            return (old_base, new_base)
        except Exception as e:
            print(f"[pipeline] archive rename failed: {e}")
            return None

    def _submit_pipeline_for_archive(self) -> None:
        """Hand the current pipeline to ArchiverAgent. Safe to call when no
        pipeline is active (no-op). Result includes a written report path;
        we broadcast that path so the UI can link to it."""
        agents = getattr(self, "_agents", None)
        if not agents:
            return
        arc = agents.get("ArchiverAgent")
        if not arc:
            return
        p = self._active_pipeline
        if not p:
            return
        import threading as _th
        def _do():
            try:
                reports_dir = os.path.expanduser("~/LotusAgent/reports")
                wid = arc.submit({
                    "pipeline":     p.as_event(),
                    "reports_dir":  reports_dir,
                    "ask_gemma":    True,
                })
                # Wait for completion so we can broadcast the report path.
                import time as _time
                deadline = _time.time() + 120
                while _time.time() < deadline:
                    s = arc.status(wid)
                    if s["state"] in ("done", "failed"):
                        break
                    _time.sleep(0.3)
                s = arc.status(wid)
                if s["state"] == "done":
                    r = s.get("result") or {}
                    print(f"[archive] wrote report → {r.get('report_path')}")
                    self._emit_controller_alert(
                        level="info",
                        agent="ArchiverAgent",
                        message=(
                            f"Run archived. "
                            f"{r.get('counts',{}).get('by_lotus',0)} by LOTUS, "
                            f"{r.get('counts',{}).get('manual',0)} manual, "
                            f"{r.get('counts',{}).get('failed',0)} failed. "
                            f"Report: {os.path.basename(r.get('report_path',''))}"
                        ),
                    )
                else:
                    print(f"[archive] FAILED: {s.get('error')}")
            except Exception as e:
                print(f"[archive] exception: {e}")
        _th.Thread(target=_do, daemon=True, name="archive-worker").start()

    def _submit_frame_for_review(self, fr: dict) -> None:
        """Hand a just-saved frame to ReviewAgent and spawn a watcher thread
        that acts on the verdict. Pass → no-op. Fail & retry_count<3 →
        auto-retry. Fail & retry_count==3 → leave for manual resolution.
        Silently no-ops if the agent mesh isn't available."""
        agents = getattr(self, "_agents", None)
        if not agents:
            return
        agent = agents.get("ReviewAgent")
        if not agent:
            return
        img = fr.get("final_path")
        if not img or not os.path.exists(img):
            return
        fr.setdefault("retry_count", 0)
        work_id = agent.submit({
            "image_path":  img,
            "prompt_text": fr.get("prompt_text", ""),
        })
        import threading as _th
        _th.Thread(
            target=self._watch_review,
            args=(fr["id"], work_id),
            daemon=True,
            name=f"review-watch-{fr['id']}",
        ).start()

    def _watch_review(self, frame_id: str, work_id: str) -> None:
        """Poll ReviewAgent until it returns a verdict, then act on it.
        Runs on its own daemon thread so it doesn't block the render loop."""
        import time as _time
        agents = getattr(self, "_agents", None)
        if not agents: return
        agent = agents.get("ReviewAgent")
        if not agent: return

        p_ref = self._active_pipeline
        # Poll with a generous cap (OCR + Gemma can take ~60s).
        deadline = _time.time() + 180
        while _time.time() < deadline:
            s = agent.status(work_id)
            if s["state"] in ("done", "failed", "unknown"):
                break
            _time.sleep(0.5)
        s = agent.status(work_id)

        # Multi-pipeline world: bind to the pipeline we launched against,
        # not whatever is currently focused on the dashboard.
        p = p_ref
        if not p or p.cancelled:
            return
        fr = next((f for f in p.frames if f["id"] == frame_id), None)
        if not fr:
            return
        # Respect user intervention — if they've skipped/uploaded/denied,
        # don't overwrite that decision with a stale review verdict.
        if fr.get("status") != "pending_review":
            return

        if s["state"] != "done":
            # Review infrastructure itself failed — don't block shipping on
            # our own tool's error; leave the frame passed.
            err = s.get("error") or "review timeout"
            print(f"[review] infra error on {frame_id}: {err}")
            try:
                p.errors.append({
                    "ts":       _time.time(),
                    "frame_id": frame_id,
                    "section":  fr.get("section", ""),
                    "stage":    "review-infra",
                    "message":  err,
                })
            except Exception: pass
            return

        verdict = s.get("result") or {}
        if verdict.get("match"):
            # Gemma-vision approved — auto-promote pending_review → approved
            # so the user doesn't have to click approve for frames LOTUS has
            # already verified. User can still manually deny in the UI.
            if fr.get("status") == "pending_review":
                fr["status"] = "approved"
                self._pipeline_broadcast()
            print(f"[review] ✓ {frame_id} → approved  "
                  f"(ocr_chars={verdict.get('ocr_chars')})")
            return

        reason  = verdict.get("reason", "rejected")
        retry_n = fr.get("retry_count", 0)
        fr["status"] = "failed"
        if retry_n < 3:
            fr["error"] = f"Review rejected (attempt {retry_n+1}/3): {reason}"
            verb = "retrying"
        else:
            fr["error"] = f"Review rejected 3x — manual help needed: {reason}"
            verb = "giving up"
        try:
            p.errors.append({
                "ts":       _time.time(),
                "frame_id": frame_id,
                "section":  fr.get("section", ""),
                "stage":    "review",
                "message":  fr["error"],
            })
        except Exception: pass
        print(f"[review] ✗ {frame_id} — {verb}  ({reason})")
        self._pipeline_broadcast()

        # Auto-retry via the existing single-frame retry path, on its own
        # thread so this watcher can exit cleanly.
        if retry_n < 3:
            import threading as _th
            _th.Thread(
                target=self._pipeline_frame_retry,
                args=(frame_id,),
                daemon=True,
                name=f"auto-retry-{frame_id}",
            ).start()

    def _pipeline_frame_retry(self, frame_id: str) -> None:
        """Re-render a single failed frame via gemini_bot.create_image_and_download.
        Runs on a background thread. Updates the frame status live so the
        dashboard reflects generating → pending_review (or back to failed).
        """
        p = self._active_pipeline
        if not p:
            return
        fr = next((f for f in p.frames if f["id"] == frame_id), None)
        if not fr:
            print(f"[pipeline] retry: frame {frame_id} not found")
            return
        target = fr.get("final_path")
        prompt = fr.get("prompt_text") or ""
        if not target or not prompt.strip():
            print(f"[pipeline] retry: frame {frame_id} has no target or empty prompt")
            return
        # Reflect the in-flight state on the UI + bump the retry counter so
        # ReviewAgent's auto-retry loop respects the 3-attempt ceiling.
        fr["retry_count"] = fr.get("retry_count", 0) + 1
        fr["status"] = "generating"
        fr["error"]  = None
        self._pipeline_broadcast()
        print(f"[pipeline] ↻ retry {frame_id} (attempt {fr['retry_count']}) → "
              f"{target}  ({len(prompt)} chars)")

        import gemini_bot
        saved = None
        try:
            saved = gemini_bot.create_image_and_download(prompt, target, verbose=True)
        except Exception as e:
            print(f"[pipeline] retry {frame_id} EXCEPTION: {e}")

        # Re-check — user may have cancelled the pipeline mid-retry.
        if p.cancelled:
            return
        fr2 = next((f for f in p.frames if f["id"] == frame_id), None)
        if not fr2:
            return

        if saved and os.path.exists(saved):
            root = os.path.expanduser(lotus_config.load()["image"].get(
                "projects_root", "~/LotusAgent/Projects"))
            rel = os.path.relpath(saved, root).replace(os.sep, "/")
            fr2["final_path"] = saved
            fr2["final_url"]  = "/projects/" + rel
            fr2["temp_url"]   = fr2["final_url"]
            fr2["status"]     = "pending_review"
            fr2["source"]     = "lotus"
            fr2["error"]      = None
            print(f"[pipeline] retry saved {frame_id} → {saved}")
            # Re-submit to ReviewAgent — the retry's output is subject to the
            # same text-fidelity check as the original render.
            self._submit_frame_for_review(fr2)
        else:
            fr2["status"] = "failed"
            fr2["error"]  = "Retry failed — bot timed out or download path error"
            try:
                import time as _time
                p.errors.append({
                    "ts":       _time.time(),
                    "frame_id": frame_id,
                    "section":  fr2.get("section", ""),
                    "stage":    "retry",
                    "message":  fr2["error"],
                })
            except Exception: pass
            print(f"[pipeline] retry {frame_id} FAILED")
        self._pipeline_broadcast()

    def _pipeline_set_frame_status(self, frame_id: str, status: str) -> None:
        p = self._active_pipeline
        if not p: return
        for fr in p.frames:
            if fr["id"] == frame_id:
                fr["status"] = status
                # Auto-save path: frames now live at their final path from
                # generation time, so a DENIED frame means "this image is
                # bad, delete it from disk". The next regenerate re-creates
                # it at the same path; if the user never regenerates, the
                # Post folder stays clean (no bad image saved).
                if status == "denied":
                    fp = fr.get("final_path")
                    if fp and os.path.exists(fp):
                        try:
                            os.unlink(fp)
                            print(f"[pipeline] denied — removed {fp}")
                        except Exception as e:
                            print(f"[pipeline] could not delete denied frame {fp}: {e}")
                    fr["final_url"] = None
                    fr["temp_url"]  = None
                break
        # Auto-advance: since frames are already on disk at their final
        # path, "save" stage is a no-op for the auto-save flow. But we
        # still transition so the UI shows completion.
        if all(f["status"] in ("approved", "denied", "failed") for f in p.frames):
            approved = [f for f in p.frames if f["status"] == "approved"]
            if approved:
                p.stage = "save" if not all(f.get("final_path") for f in approved) else "done"
                p.save_section = p.save_section or "Post1"
        self._pipeline_broadcast()

    def _pipeline_regen_frame(self, frame_id: str,
                              new_text: Optional[str] = None) -> None:
        """Re-generate the image for a single frame in place.

        If `new_text` is provided, the frame's prompt is updated before
        regeneration (e.g. user edited the prompt in the lightbox and hit
        regen). The change is also persisted to prompts.db so the next
        run sees the edited version.

        If the frame has a `final_path` (auto-saved to Post{N}/Frame{M}.png),
        we DELETE the bad file and re-render to the SAME path so the folder
        stays clean. Cache-bust the URL so the dashboard thumbnail refreshes.
        Otherwise fall back to staging (legacy)."""
        p = self._active_pipeline
        if not p: return
        frame = next((f for f in p.frames if f["id"] == frame_id), None)
        if not frame:
            return
        # Apply the user's prompt edit BEFORE we kick the render.
        if new_text and isinstance(new_text, str) and new_text.strip():
            edited = new_text.strip()
            frame["prompt_text"] = edited
            # Mirror to prompts.db so the change persists across restarts.
            prompt_id = frame.get("prompt_id")
            if prompt_id is not None and p.source_hash:
                try:
                    lotus_prompts_db.update_text(int(prompt_id), edited)
                except Exception as e:
                    print(f"[pipeline] regen edit DB update failed: {e}",
                          flush=True)
            # Also reflect in the in-memory prompts list so the review
            # panel shows the edited text.
            for pr in p.prompts:
                if str(pr.get("id")) == str(prompt_id):
                    pr["text"] = edited
                    pr["edited"] = True
                    break
        frame["status"] = "generating"
        self._pipeline_broadcast()

        final_path = frame.get("final_path")
        if final_path:
            # Delete the bad file first so Gemini's fresh image overwrites
            # cleanly (also frees disk / prevents half-merge artefacts).
            try:
                if os.path.exists(final_path):
                    os.unlink(final_path)
                    print(f"[pipeline] removed bad frame: {final_path}")
            except Exception as e:
                print(f"[pipeline] could not delete {final_path}: {e}")
            with LotusPhase1._gemini_lock:
                path = _gen_image(frame["prompt_text"], final_path, verbose=True)
            if path and os.path.exists(path):
                # Cache-bust both URLs so the <img> re-fetches.
                import time as _t
                cb = f"?t={int(_t.time())}"
                frame["final_path"] = path
                root = os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects"))
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                frame["final_url"] = "/projects/" + rel + cb
                frame["temp_url"]  = "/projects/" + rel + cb
                frame["status"]    = "pending_review"
            else:
                frame["status"] = "failed"
            self._pipeline_broadcast()
            return

        # Legacy staging path (only for frames created by the old flow).
        import tempfile as _tf
        fd, tmp = _tf.mkstemp(suffix=".png", prefix=f"{frame_id}_regen_")
        os.close(fd)
        with LotusPhase1._gemini_lock:
            path = _gen_image(frame["prompt_text"], tmp, verbose=True)
        if path:
            staged = self._stage_pipeline_frame(p.id, os.path.basename(tmp), path)
            frame["temp_path"] = staged[0]
            frame["temp_url"] = staged[1] + "?t=" + str(int(__import__("time").time()))
            frame["status"] = "pending_review"
        else:
            frame["status"] = "failed"
        self._pipeline_broadcast()

    def _pipeline_save(self, section: str) -> None:
        """Move all approved frames from the staging folder into the final
        section with proper FrameN naming. Then switch pipeline to 'done'."""
        p = self._active_pipeline
        if not p: return
        p.save_section = section
        approved = [f for f in p.frames if f["status"] == "approved"]
        if not approved:
            self._pipeline_broadcast({"error": "No approved frames to save"})
            return
        try:
            _ = lotus_config.section_folder(section)
        except Exception as e:
            # Maybe single-item slot
            if lotus_config.is_single_item(section):
                # Save only first approved into the single slot
                src = approved[0]["temp_path"]
                dst = lotus_config.single_item_path(section)
                import shutil as _sh
                _sh.move(src, dst)
                approved[0]["final_path"] = dst
                root = os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects"))
                rel = os.path.relpath(dst, root).replace(os.sep, "/")
                approved[0]["final_url"] = "/projects/" + rel
                p.stage = "done"
                self._pipeline_broadcast()
                # Auto-rename the parent folder so this run is preserved
                # and the next pipeline starts with a clean Techengine<date>/.
                # Rename BEFORE archive so the JSON report captures the new paths.
                self._pipeline_archive_rename(p)
                # Auto-archive: hand the run to ArchiverAgent for a JSON
                # report + Gemma summary. No-op if mesh unavailable.
                self._submit_pipeline_for_archive()
                return
            else:
                self._pipeline_broadcast({"error": str(e)})
                return

        import shutil as _sh
        root = os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects"))
        for i, fr in enumerate(approved, start=1):
            final_path = lotus_config.next_frame_path(section, i)
            _sh.move(fr["temp_path"], final_path)
            fr["final_path"] = final_path
            rel = os.path.relpath(final_path, root).replace(os.sep, "/")
            fr["final_url"] = "/projects/" + rel
        p.stage = "done"
        self._pipeline_broadcast()
        # Auto-rename today's parent folder so this completed run is
        # preserved under `<brand><date>_<pipeline>_<HHMMSS>/` and the
        # next pipeline gets a fresh folder. Runs BEFORE archive so the
        # JSON report captures the new on-disk paths.
        self._pipeline_archive_rename(p)
        # Auto-archive — every successful save triggers a client-ready JSON
        # report. ArchiverAgent runs on its own thread so the save path
        # stays snappy.
        self._submit_pipeline_for_archive()

    def _tool_pipeline_approve(self, frame_ids) -> str:
        p = self._active_pipeline
        if not p: return "No active pipeline"
        ids = self._normalize_frame_ids(frame_ids)
        n = 0
        for fr in p.frames:
            if "all" in ids or fr["id"] in ids:
                if fr["status"] == "pending_review":
                    fr["status"] = "approved"; n += 1
        # Auto-advance to save stage if everything has a decision
        if all(f["status"] in ("approved", "denied", "failed") for f in p.frames):
            if any(f["status"] == "approved" for f in p.frames):
                p.stage = "save"
        self._pipeline_broadcast()
        return f"Approved {n} frame(s)"

    def _tool_pipeline_deny(self, frame_ids) -> str:
        p = self._active_pipeline
        if not p: return "No active pipeline"
        ids = self._normalize_frame_ids(frame_ids)
        n = 0
        for fr in p.frames:
            if "all" in ids or fr["id"] in ids:
                if fr["status"] == "pending_review":
                    fr["status"] = "denied"; n += 1
        self._pipeline_broadcast()
        return f"Denied {n} frame(s)"

    def _normalize_frame_ids(self, raw) -> set:
        """Accept 'all', 'f1', ['f1','f3'], [1,3], '1,3', etc."""
        if raw is None: return set()
        if isinstance(raw, str):
            r = raw.strip().lower()
            if r == "all": return {"all"}
            parts = [p.strip() for p in r.replace(",", " ").split()]
            return {p if p.startswith("f") else f"f{p}" for p in parts if p}
        if isinstance(raw, list):
            out = set()
            for v in raw:
                s = str(v).strip().lower()
                if s == "all":
                    return {"all"}
                out.add(s if s.startswith("f") else f"f{s}")
            return out
        return {str(raw).lower()}

    def _tool_set_pref(self, cmd: str) -> str:
        intent = _classify_config_intent(cmd)
        if not intent:
            return "Unrecognized preference change"
        res = self._handle_config(cmd, intent)
        self._broadcast(type="tool_result", tool="set_preference", result=res)
        return res

    def _handle_tool(self, tool_name: str, params: dict) -> str:
        """Execute an actionable tool via tools.ToolExecutor."""
        if not hasattr(self, "_tools"):
            try:
                from tools import ToolExecutor
                self._tools = ToolExecutor()
            except Exception as e:
                return f"Tool executor unavailable: {e}"

        self._broadcast(type="tool_call", tool=tool_name, input=params)

        # show_media: open a previously-captured file in Preview.app (macOS)
        # or the platform default image viewer. Also broadcasts a thumbnail.
        if tool_name == "show_media":
            import subprocess
            target = str(params.get("target", "")).lower().replace(" ", "")
            parent = lotus_config.parent_folder_for()
            cfg = lotus_config.load()["image"]
            try:
                files = os.listdir(parent)
            except FileNotFoundError:
                files = []
            chosen = None
            if target in ("screenshot", "screenshots", "screen"):
                cands = sorted(f for f in files if f.startswith("screen_") and f.endswith(".png"))
                if cands:
                    chosen = cands[-1]
            elif target in ("image", "images", "photo", "photos", "picture", "pictures"):
                cands = [f for f in files if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
                if cands:
                    chosen = max(cands, key=lambda f: os.path.getmtime(os.path.join(parent, f)))
            elif target.startswith("post"):
                for f in files:
                    low = f.lower()
                    if low.startswith(target) and low.endswith((".png", ".jpg", ".jpeg")):
                        chosen = f
                        break
            elif target == "reel":
                candidate = cfg["reel_name"] + cfg["reel_ext"]
                if candidate in files:
                    chosen = candidate
            if not chosen:
                self._broadcast(type="tool_result", tool="show_media",
                                result=f"No {target} found in today's folder.")
                return f"No {target} found in today's folder."
            full_path = os.path.join(parent, chosen)
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", full_path])
                elif sys.platform == "win32":
                    os.startfile(full_path)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", full_path])
            except Exception as e:
                self._broadcast(type="tool_result", tool="show_media",
                                result=f"Failed to open: {e}")
                return f"Failed to open {chosen}: {e}"
            rel = os.path.relpath(full_path, os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects")))
            image_url = "/projects/" + rel.replace(os.sep, "/")
            self._broadcast(type="tool_result", tool="show_media",
                            result=f"Opened {chosen}",
                            image_url=image_url,
                            image_path=full_path)
            return f"Opened {chosen} in Preview."

        # Screenshot is special: we save it into today's project folder so the
        # dashboard's /projects/ mount can serve it, and broadcast the URL so
        # the feed shows a live preview thumbnail.
        if tool_name == "screenshot":
            try:
                import pyautogui
                parent = lotus_config.parent_folder_for()
                ts = datetime.now().strftime("%H%M%S")
                out_name = f"screen_{ts}.png"
                out_path = os.path.join(parent, out_name)
                img = pyautogui.screenshot()
                img.save(out_path)
                rel = os.path.relpath(out_path, os.path.expanduser(lotus_config.load()["image"].get("projects_root", "~/LotusAgent/Projects")))
                image_url = "/projects/" + rel.replace(os.sep, "/")
                self._broadcast(
                    type="tool_result",
                    tool="screenshot",
                    result=f"Saved {out_name}",
                    image_url=image_url,
                    image_path=out_path,
                )
                return f"Screenshot saved as {out_name}."
            except Exception as e:
                self._broadcast(type="tool_result", tool="screenshot",
                                result=f"Screenshot failed: {e}")
                return f"Screenshot failed: {e}"

        # open_app: try resolved alias + globbed candidates until one works
        if tool_name == "open_app":
            import subprocess
            requested = params.get("app_name", "")
            candidates = _resolve_app_name(requested) if sys.platform == "darwin" else [requested]
            used = None; last_err = ""
            for cand in candidates:
                if sys.platform == "darwin":
                    r = subprocess.run(["open", "-a", cand], capture_output=True, text=True)
                    if r.returncode == 0:
                        used = cand; break
                    last_err = (r.stderr or r.stdout or "").strip()
                else:
                    try:
                        subprocess.Popen([cand], shell=True)
                        used = cand; break
                    except Exception as e:
                        last_err = str(e)
            result = f"Opened {used}" if used else f"Couldn't find app: {requested} ({last_err})"
            self._broadcast(type="tool_result", tool=tool_name, result=result)
            if used:
                return f"Opened {used}."
            return f"Sorry, I couldn't find {requested}."

        # All other tools: execute and broadcast the text result
        try:
            result = self._tools.execute(tool_name, params)
        except Exception as e:
            result = f"Error: {e}"
        self._broadcast(type="tool_result", tool=tool_name, result=str(result)[:500])

        # Voice-friendly short confirmations (open_app is handled above)
        if tool_name == "open_url":
            return f"Opened {params.get('url', 'the link')}."
        if tool_name == "hotkey":
            keys = params.get("keys", "")
            if keys == "command+q":
                return "Quit the app."
            if keys == "command+w":
                return "Closed."
            return f"Pressed {keys}."
        if tool_name == "scroll":
            return f"Scrolled {params.get('direction', '')}."
        if tool_name == "type_text":
            text_val = params.get("text", "")
            return f"Typed: {text_val[:40]}"
        if tool_name == "get_system_info":
            try:
                import json as _j
                info = _j.loads(result)
                cpu = info.get("cpu_percent", "?")
                ram = info.get("ram_used", "?")
                return f"CPU {cpu}, RAM used {ram}. Details on dashboard."
            except Exception:
                return "System info captured on dashboard."
        return str(result)[:300]

    def _handle_config(self, text: str, intent: str) -> str:
        if intent == "show":
            return lotus_config.describe_image_settings()
        if intent == "reset":
            lotus_config.reset_image()
            return "Image settings reset to defaults."
        # intent == "set"
        changes = []
        m = _CFG_ROOT_RE.search(text)
        if m:
            root = m.group(1).rstrip(",.;:)")
            lotus_config.set_projects_root(root)
            changes.append(f"projects root → {root}")
        m = _CFG_PREFIX_RE.search(text)
        if m:
            prefix = m.group(1).rstrip(",.;:)")
            lotus_config.set_child_prefix(prefix)
            changes.append(f"image prefix → {prefix}")
        m = _CFG_REEL_RE.search(text)
        if m:
            reel = m.group(1).rstrip(",.;:)")
            lotus_config.set_reel_name(reel)
            changes.append(f"reel name → {reel}")
        if not changes:
            return "No setting change parsed. Try: 'save projects in ~/X', 'call images shot', 'call reel final'."
        return "Settings updated. " + "; ".join(changes)

    def process(self, text: str) -> str:
        """Route every command through Gemma. Gemma decides: call a tool or
        reply with text. LOTUS is the front desk; Gemma is the brain.

        Serialized with a lock so dashboard commands and voice commands
        can't clobber each other's conversation state.
        """
        with self._process_lock:
            # Split run-together STT artifacts before Gemma sees them,
            # e.g. "opencloud" → "open cloud", "takescreenshot" → "take screenshot".
            text = _split_stt_compounds(text)
            self._broadcast(type="user_command", text=text)
            reply = self.think(text)
            self._broadcast(type="agent_response", text=reply)
            return reply

    def run_voice(self):
        """Main voice command loop: wake-word → voiceprint auth → command → reply.

        STT strategy: Google first (much better on proper nouns like "Lotus"),
        Whisper as offline fallback. Wake-word detection accepts close
        mis-transcriptions via WAKE_ALIASES.
        """
        print("\n" + "=" * 60)
        print("  LOTUS AGENT — Phase 1 (Voice + Gemma + Voiceprint Auth)")
        print(f"  Model: {MODEL} (local via Ollama)")
        print(f"  Wake word: '{WAKE_WORD}'  (aliases accepted for mistranscription)")
        if self.auth and self.auth.is_enrolled:
            print(f"  Auth: voiceprint required — authorized: {', '.join(self.auth.enrolled_users)}")
        print("  Say 'exit' to quit")
        print("=" * 60 + "\n")

        self.voice = VoiceEngine(tts_voice=TTS_VOICE)
        self.voice.speak("LOTUS agent online. Say Lotus to wake me.")

        import speech_recognition as sr

        # ── Smooth-listen tuning ────────────────────────────────────────
        # The mic stream is opened ONCE for the whole loop (reopening per
        # iteration triggers PyAudio segfaults on macOS, same bug we hit
        # during voiceprint enrollment). We also freeze the energy
        # threshold after calibration so it doesn't drift during use, and
        # use a tighter pause_threshold so phrases end faster = lower
        # perceived latency.
        self.voice.recognizer.dynamic_energy_threshold = False
        self.voice.recognizer.pause_threshold = 0.6      # sec of silence to end phrase
        self.voice.recognizer.non_speaking_duration = 0.3

        def _read():
            """Read one phrase; returns (audio, text) or (None, None).
            Mic is kept open outside the loop so the stream never closes
            between iterations."""
            try:
                print("🎙️  Listening...", end="\r", flush=True)
                audio = self.voice.recognizer.listen(
                    mic_source, timeout=None, phrase_time_limit=15
                )
            except sr.WaitTimeoutError:
                return None, None
            except Exception as e:
                print(f"\n⚠️  Mic read: {e}")
                return None, None
            text = None
            try:
                text = self.voice.recognizer.recognize_google(audio)
            except sr.UnknownValueError:
                pass
            except sr.RequestError:
                pass
            except Exception:
                pass
            if not text:
                try:
                    text = self.voice.recognizer.recognize_whisper(
                        audio, model="base", language="english"
                    )
                except Exception:
                    pass
            return audio, (text or "").strip() or None

        # Open the mic once and loop forever inside the `with` block
        with self.voice.microphone as mic_source:
            self.voice.recognizer.adjust_for_ambient_noise(mic_source, duration=0.5)
            print(f"✅  Stream open — energy threshold {self.voice.recognizer.energy_threshold:.0f}")
            self._voice_main_loop(_read, mic_source, sr)

    def _voice_main_loop(self, read_fn, mic_source, sr) -> None:
        """Process phrases until KeyboardInterrupt. Extracted so run_voice's
        `with mic as source:` can keep the PyAudio stream open across every
        iteration — the key to a smooth, stutter-free listening experience."""
        while True:
            try:
                audio, text = read_fn()
                if not text:
                    continue

                text_lower = text.lower().strip()
                print(f"👂 Heard: {text}      ")

                if text_lower in ("exit", "quit", "shutdown", "goodbye lotus"):
                    self.voice.speak("Shutting down.")
                    break

                wake_hit = self._detect_wake(text_lower)
                if not wake_hit:
                    continue

                command = text_lower.replace(wake_hit, "", 1).strip(" ,.!?-—")
                if not command:
                    self.voice.speak("Yes?")
                    # Dashboard hint: mic is hot for the follow-up command.
                    # Wrapped defensively so any socket/broadcast hiccup can
                    # NEVER break the voice loop.
                    try:
                        self._broadcast(type="voice_listening", active=True)
                    except Exception:
                        pass
                    # Use the already-open mic source; don't reopen
                    try:
                        audio2 = self.voice.recognizer.listen(
                            mic_source, timeout=8, phrase_time_limit=15
                        )
                    except Exception:
                        try: self._broadcast(type="voice_listening", active=False)
                        except Exception: pass
                        continue
                    try: self._broadcast(type="voice_listening", active=False)
                    except Exception: pass
                    try:
                        command = self.voice.recognizer.recognize_google(audio2) or ""
                    except Exception:
                        try:
                            command = self.voice.recognizer.recognize_whisper(
                                audio2, model="base", language="english"
                            ) or ""
                        except Exception:
                            command = ""
                    command = command.strip()
                    audio = audio2
                    if not command:
                        continue

                # Voiceprint verification with 5-min grace session
                if self.auth and self.auth.is_enrolled:
                    import time as _t
                    if self._auth_session_user and _t.time() < self._auth_session_until:
                        user_name = self._auth_session_user
                        remaining = int(self._auth_session_until - _t.time())
                        print(f"  🔓 Session active: {user_name} ({remaining}s left)")
                        self._broadcast(type="tool_call", tool="voice_auth", input={
                            "status": "session", "user": user_name,
                            "confidence": 1.0, "remaining_s": remaining,
                        })
                    else:
                        is_auth, user_name, conf = self.auth.verify(audio)
                        self._broadcast(type="tool_call", tool="voice_auth", input={
                            "status": "verified" if is_auth else "rejected",
                            "user": user_name,
                            "confidence": round(float(conf), 2),
                        })
                        if not is_auth:
                            if user_name == "locked_out":
                                self.voice.speak("System locked. Too many failed attempts.")
                                print("  🔒 LOCKED OUT")
                            else:
                                self.voice.speak("Voice not recognized. Access denied.")
                                print(f"  🚫 Denied — confidence {conf:.2f}")
                            continue
                        self._auth_session_user = user_name
                        self._auth_session_until = _t.time() + self._auth_session_seconds
                        print(f"  🔓 Verified: {user_name} ({conf:.2f}) — session {self._auth_session_seconds}s")
                        self.voice.speak(f"Yes, {user_name}.")
                else:
                    user_name = "user"

                print(f"👤 {user_name}: {command}")
                response = self.process(command)
                print(f"🤖 Lotus: {response}\n")
                if response:
                    self.voice.speak(response)
                    # Small cooldown so TTS tail doesn't re-trigger the mic
                    import time as _t
                    _t.sleep(0.4)

            except KeyboardInterrupt:
                print("\n🔴 Agent offline.")
                break
            except Exception as e:
                print(f"Error: {e}")
                continue

    def _detect_wake(self, text_lower: str) -> Optional[str]:
        """Return the matched wake alias (lowercased) if `text_lower` contains
        one, else None. Checks longest aliases first so 'lotus' matches before
        'lot' would (not in list, but defensive)."""
        for alias in sorted(WAKE_ALIASES, key=len, reverse=True):
            if alias in text_lower:
                return alias
        return None

    def run_text(self):
        """Text-only mode for testing without mic."""
        print("\n  LOTUS — Text mode (Phase 1)")
        print("  Type 'exit' to quit\n")

        while True:
            try:
                text = input("👤 You: ").strip()
                if not text or text.lower() in ["exit", "quit"]:
                    break

                response = self.process(text)
                print(f"🤖 Lotus: {response}\n")
            except KeyboardInterrupt:
                break


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else None

    if not mode:
        mode = input("Mode — [v]oice or [t]ext? (v/t): ").strip().lower()

    # Chrome preflight — opens (or reuses) a Chrome instance with CDP debug
    # port and waits for the user to be signed-in to Gemini before the agent
    # mesh loads. Sets LOTUS_CDP_URL so gemini_bot/grok_bot attach via CDP.
    # Skip with LOTUS_SKIP_PREFLIGHT=1 (e.g. when running in environments
    # where Chrome is unavailable and only Ollama/voice features are needed).
    if not os.environ.get("LOTUS_SKIP_PREFLIGHT"):
        from lotus_preflight import ensure_lotus_chrome
        os.environ["LOTUS_CDP_URL"] = ensure_lotus_chrome()

    agent = LotusPhase1()

    if mode in ["v", "voice"]:
        agent.run_voice()
    else:
        agent.run_text()


if __name__ == "__main__":
    main()
