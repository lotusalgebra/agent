"""
LOTUS AGENT — JARVIS-style Voice AI Assistant
Built by Somendra | Lotus Algebra
"""

import json
import asyncio
import threading
from datetime import datetime
from anthropic import Anthropic
from voice import VoiceEngine
from tools import ToolExecutor
from server import DashboardServer
from router import HybridRouter
from auth import VoiceAuth, setup_auth_interactive

SYSTEM_PROMPT = """You are LOTUS — an AI agent assistant created by Somendra at Lotus Algebra.
You can SEE and CONTROL the user's screen, execute tasks, open apps, search the web, and manage files.

PERSONALITY:
- Confident, precise, military-briefing style responses
- Keep voice responses SHORT (1-3 sentences max for speech)
- For complex results, say a summary and mention details are on dashboard

AVAILABLE TOOLS:
You have access to these tools via function calling. Use them to accomplish tasks:

1. screenshot — Take a screenshot of the current screen
2. click — Click at screen coordinates (x, y)
3. type_text — Type text at current cursor position
4. hotkey — Press keyboard shortcuts (e.g., ctrl+c, alt+tab)
5. open_app — Open an application by name
6. open_url — Open a URL in default browser
7. run_command — Run a shell/terminal command
8. read_file — Read contents of a file
9. write_file — Write content to a file
10. list_files — List files in a directory
11. mouse_move — Move mouse to coordinates
12. scroll — Scroll up or down
13. find_on_screen — Find text or UI element on screen (OCR)
14. get_clipboard — Get clipboard contents
15. set_clipboard — Set clipboard contents
16. get_system_info — Get system information (CPU, RAM, etc.)
17. take_note — Save a note/reminder
18. get_notes — Retrieve saved notes

When asked to do something on screen, take a screenshot first to understand the current state,
then perform actions step by step. Confirm completion to the user.

Keep spoken responses under 30 words. Be direct. No fluff."""

TOOLS = [
    {"name": "screenshot", "description": "Capture current screen. Returns image description.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "click", "description": "Click at screen position", "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"}}, "required": ["x", "y"]}},
    {"name": "type_text", "description": "Type text at cursor", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "hotkey", "description": "Press keyboard shortcut", "input_schema": {"type": "object", "properties": {"keys": {"type": "string", "description": "Keys separated by + (e.g. ctrl+c, alt+tab, win+d)"}}, "required": ["keys"]}},
    {"name": "open_app", "description": "Open application by name", "input_schema": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]}},
    {"name": "open_url", "description": "Open URL in browser", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "run_command", "description": "Run shell command", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write to file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "list_files", "description": "List directory contents", "input_schema": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}, "required": []}},
    {"name": "mouse_move", "description": "Move mouse to position", "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x", "y"]}},
    {"name": "scroll", "description": "Scroll up or down", "input_schema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down"]}, "amount": {"type": "integer", "default": 3}}, "required": ["direction"]}},
    {"name": "find_on_screen", "description": "Find text/element on screen using OCR", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_clipboard", "description": "Get clipboard contents", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "set_clipboard", "description": "Set clipboard text", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "get_system_info", "description": "Get CPU, RAM, disk info", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "take_note", "description": "Save a note", "input_schema": {"type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"}}, "required": ["title", "content"]}},
    {"name": "get_notes", "description": "List saved notes", "input_schema": {"type": "object", "properties": {}, "required": []}},
]


class LotusAgent:
    def __init__(self, api_key: str, voice_enabled: bool = True):
        self.client = Anthropic(api_key=api_key)
        self.voice = VoiceEngine() if voice_enabled else None
        self.tools = ToolExecutor()
        self.router = HybridRouter()
        self.auth = VoiceAuth()
        self.dashboard = DashboardServer()
        self.conversation_history = []
        self.is_listening = False
        self.wake_word = "lotus"
        self.verified_user = None  # Currently verified speaker

        print("\n" + "=" * 50)
        print("  LOTUS AGENT — Online (Hybrid + Voice Auth)")
        print("  🔍 Search/Research → Claude.ai (Pro)")
        print("  🔧 Tasks/Actions   → API (Direct)")
        print("  🔐 Voice Auth      → Speaker Verified")
        print("  Say 'Lotus' to activate, 'exit' to quit")
        print("=" * 50 + "\n")

    def process_command(self, user_input: str) -> str:
        """Route command: Claude.ai for search, API for tasks."""

        # Step 1: Classify command
        route = self.router.classify(user_input)

        self.dashboard.broadcast({
            "type": "user_command",
            "text": f"[{route.upper()}] {user_input}",
            "timestamp": datetime.now().isoformat()
        })

        # Step 2: Route accordingly
        if route == "search":
            return self._handle_search(user_input)
        else:
            return self._handle_task(user_input)

    def _handle_search(self, query: str) -> str:
        """Send search/research queries to Claude.ai browser window."""
        print(f"  🔍 Routing to Claude.ai (web search)...")

        self.dashboard.broadcast({
            "type": "tool_call",
            "tool": "claude.ai",
            "input": {"query": query},
            "timestamp": datetime.now().isoformat()
        })

        result = self.router.send_to_claude_chat(query)

        self.dashboard.broadcast({
            "type": "agent_response",
            "text": result,
            "timestamp": datetime.now().isoformat()
        })

        return f"Sent to Claude.ai for search. {result}"

    def _handle_task(self, user_input: str) -> str:
        """Handle action commands via API with tools."""
        print(f"  🔧 Routing to API (tools)...")

        self.conversation_history.append({
            "role": "user",
            "content": user_input
        })

        self.dashboard.broadcast({
            "type": "user_command",
            "text": user_input,
            "timestamp": datetime.now().isoformat()
        })

        # Claude API call with tools
        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=self.conversation_history
        )

        # Process response — handle tool calls in a loop
        while response.stop_reason == "tool_use":
            assistant_content = response.content
            self.conversation_history.append({
                "role": "assistant",
                "content": assistant_content
            })

            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    tool_id = block.id

                    self.dashboard.broadcast({
                        "type": "tool_call",
                        "tool": tool_name,
                        "input": tool_input,
                        "timestamp": datetime.now().isoformat()
                    })

                    print(f"  🔧 Executing: {tool_name}({json.dumps(tool_input, indent=None)[:80]})")

                    # Execute the tool
                    result = self.tools.execute(tool_name, tool_input)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": str(result)[:5000]  # Truncate long results
                    })

                    self.dashboard.broadcast({
                        "type": "tool_result",
                        "tool": tool_name,
                        "result": str(result)[:500],
                        "timestamp": datetime.now().isoformat()
                    })

            self.conversation_history.append({
                "role": "user",
                "content": tool_results
            })

            # Continue the conversation
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=self.conversation_history
            )

        # Extract final text response
        final_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                final_text += block.text

        self.conversation_history.append({
            "role": "assistant",
            "content": response.content
        })

        self.dashboard.broadcast({
            "type": "agent_response",
            "text": final_text,
            "timestamp": datetime.now().isoformat()
        })

        # Keep conversation manageable
        if len(self.conversation_history) > 40:
            self.conversation_history = self.conversation_history[-30:]

        return final_text

    def run_voice_loop(self):
        """Main voice command loop with speaker verification."""
        if not self.voice:
            print("Voice disabled. Use text mode.")
            self.run_text_loop()
            return

        # Check enrollment
        if not self.auth.is_enrolled:
            print("  🔐 No voiceprint enrolled. Starting setup...")
            self.auth.enroll(
                input("  Enter your name: ").strip(),
                self.voice
            )

        print(f"  🔐 Authorized users: {', '.join(self.auth.enrolled_users)}")

        self.dashboard.start()
        print("🎙️  Listening for wake word 'Lotus'...\n")

        while True:
            try:
                # Listen for wake word — raw audio capture
                import speech_recognition as sr
                with self.voice.microphone as source:
                    audio = self.voice.recognizer.listen(source, timeout=None, phrase_time_limit=15)

                # Transcribe
                try:
                    text = self.voice.recognizer.recognize_whisper(audio, model="base", language="english")
                except Exception:
                    try:
                        text = self.voice.recognizer.recognize_google(audio)
                    except Exception:
                        continue

                if text is None:
                    continue

                text_lower = text.lower().strip()

                # Exit command (no auth needed)
                if text_lower in ["exit", "quit", "shutdown", "stop"]:
                    self.voice.speak("Shutting down.")
                    print("\n🔴 Agent offline.")
                    break

                # Check for wake word
                if self.wake_word in text_lower:
                    # VERIFY SPEAKER from the same audio that had the wake word
                    is_auth, user_name, confidence = self.auth.verify(audio)

                    if not is_auth:
                        if user_name == "locked_out":
                            self.voice.speak("System locked. Too many failed attempts.")
                            print("  🔒 LOCKED OUT — unauthorized voice attempts")
                        else:
                            self.voice.speak("Voice not recognized. Access denied.")
                            print(f"  🚫 Auth failed — confidence: {confidence:.2f}")

                        self.dashboard.broadcast({
                            "type": "tool_call",
                            "tool": "auth_denied",
                            "input": {"confidence": round(confidence, 2)},
                            "timestamp": datetime.now().isoformat()
                        })
                        continue

                    # Authorized!
                    self.verified_user = user_name
                    print(f"  🔓 Verified: {user_name} ({confidence:.2f})")

                    # Extract command from wake word phrase
                    command = text_lower.replace(self.wake_word, "").strip()
                    if not command:
                        self.voice.speak(f"Yes, {user_name}?")
                        command = self.voice.listen(timeout=8)
                        if not command:
                            continue

                    print(f"\n👤 {user_name}: {command}")

                    self.dashboard.broadcast({
                        "type": "tool_call",
                        "tool": "auth_verified",
                        "input": {"user": user_name, "confidence": round(confidence, 2)},
                        "timestamp": datetime.now().isoformat()
                    })

                    # Process through Claude
                    response = self.process_command(command)
                    print(f"🤖 Lotus: {response}\n")

                    # Speak response
                    self.voice.speak(response)

            except KeyboardInterrupt:
                print("\n🔴 Agent offline.")
                break
            except Exception as e:
                print(f"Error: {e}")
                continue

    def run_text_loop(self):
        """Fallback text-only mode."""
        self.dashboard.start()

        while True:
            try:
                user_input = input("\n👤 You: ").strip()
                if not user_input:
                    continue
                if user_input.lower() in ["exit", "quit"]:
                    print("🔴 Agent offline.")
                    break

                response = self.process_command(user_input)
                print(f"🤖 Lotus: {response}")

                if self.voice:
                    self.voice.speak(response)

            except KeyboardInterrupt:
                print("\n🔴 Agent offline.")
                break


def main():
    import os
    import sys

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        api_key = input("Enter your Anthropic API key: ").strip()
        os.environ["ANTHROPIC_API_KEY"] = api_key

    # Auth management commands
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()

        if cmd == "enroll":
            name = sys.argv[2] if len(sys.argv) > 2 else input("Name: ").strip()
            auth = setup_auth_interactive()
            return

        elif cmd == "users":
            auth = VoiceAuth()
            print(f"\n  Enrolled users: {', '.join(auth.enrolled_users) or 'None'}")
            print(f"  Engine: {auth.get_status()['engine']}")
            print(f"  Threshold: {auth.get_status()['threshold']}")
            return

        elif cmd == "remove":
            name = sys.argv[2] if len(sys.argv) > 2 else input("Name to remove: ").strip()
            auth = VoiceAuth()
            if auth.remove_user(name):
                print(f"  ✅ Removed {name}")
            else:
                print(f"  ❌ User {name} not found")
            return

        elif cmd == "reset":
            import shutil
            auth_dir = os.path.expanduser("~/.lotus_auth")
            if os.path.exists(auth_dir):
                shutil.rmtree(auth_dir)
                print("  ✅ All voiceprints deleted")
            return

    print("\n  ╔══════════════════════════════════════╗")
    print("  ║        LOTUS AGENT v1.0              ║")
    print("  ║  Voice AI Assistant + Speaker Auth    ║")
    print("  ╠══════════════════════════════════════╣")
    print("  ║  Commands:                           ║")
    print("  ║    python agent.py          — Run     ║")
    print("  ║    python agent.py enroll   — Add user║")
    print("  ║    python agent.py users    — List    ║")
    print("  ║    python agent.py remove X — Delete  ║")
    print("  ║    python agent.py reset    — Wipe all║")
    print("  ╚══════════════════════════════════════╝\n")

    mode = input("  Mode — [v]oice or [t]ext? (v/t): ").strip().lower()
    voice_enabled = mode != "t"

    agent = LotusAgent(api_key=api_key, voice_enabled=voice_enabled)

    if voice_enabled:
        agent.run_voice_loop()
    else:
        agent.run_text_loop()


if __name__ == "__main__":
    main()
