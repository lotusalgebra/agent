"""
Tool Executor — Screen control, file ops, app launcher, system tasks.
Handles all tool calls from Claude.
"""

import os
import sys
import json
import time
import shutil
import platform
import subprocess
import base64
from datetime import datetime
from pathlib import Path

# Lazy imports
pyautogui = None
pyperclip = None
psutil_mod = None


def _init_screen():
    global pyautogui
    if pyautogui is None:
        try:
            import pyautogui as pag
            pag.FAILSAFE = True  # Move mouse to corner to abort
            pag.PAUSE = 0.3
            pyautogui = pag
        except ImportError:
            print("⚠️  pip install pyautogui Pillow")


def _init_clipboard():
    global pyperclip
    if pyperclip is None:
        try:
            import pyperclip as pc
            pyperclip = pc
        except ImportError:
            pass


def _init_system():
    global psutil_mod
    if psutil_mod is None:
        try:
            import psutil
            psutil_mod = psutil
        except ImportError:
            pass


NOTES_FILE = os.path.expanduser("~/.lotus_notes.json")


class ToolExecutor:
    def __init__(self):
        _init_screen()
        _init_clipboard()
        _init_system()
        self.screenshot_dir = os.path.expanduser("~/.lotus_screenshots")
        os.makedirs(self.screenshot_dir, exist_ok=True)

    def execute(self, tool_name: str, params: dict) -> str:
        """Route tool calls to handler methods."""
        handler = getattr(self, f"tool_{tool_name}", None)
        if handler:
            try:
                return handler(**params)
            except Exception as e:
                return f"Error executing {tool_name}: {str(e)}"
        return f"Unknown tool: {tool_name}"

    # ── Screen Control ──────────────────────────────────────────

    def tool_screenshot(self) -> str:
        """Take screenshot, save it, return description."""
        if not pyautogui:
            return "pyautogui not installed"

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.screenshot_dir, f"screen_{timestamp}.png")

        img = pyautogui.screenshot()
        img.save(path)

        # Get screen size and active window info
        w, h = pyautogui.size()
        info = f"Screenshot saved: {path} | Screen: {w}x{h}"

        # Try to get active window title
        try:
            if sys.platform == "win32":
                import ctypes
                buf = ctypes.create_unicode_buffer(256)
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
                info += f" | Active window: {buf.value}"
            elif sys.platform == "darwin":
                result = subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to get name of first process whose frontmost is true'],
                    capture_output=True, text=True
                )
                info += f" | Active app: {result.stdout.strip()}"
            else:
                result = subprocess.run(
                    ["xdotool", "getactivewindow", "getwindowname"],
                    capture_output=True, text=True
                )
                info += f" | Active window: {result.stdout.strip()}"
        except Exception:
            pass

        # OCR the screenshot for text content
        try:
            import pytesseract
            text = pytesseract.image_to_string(img)
            if text.strip():
                info += f"\n\nVisible text on screen:\n{text[:2000]}"
        except Exception:
            info += "\n(OCR not available — install pytesseract for screen reading)"

        return info

    def tool_click(self, x: int, y: int, button: str = "left") -> str:
        if not pyautogui:
            return "pyautogui not installed"
        pyautogui.click(x, y, button=button)
        return f"Clicked {button} at ({x}, {y})"

    def tool_type_text(self, text: str) -> str:
        if not pyautogui:
            return "pyautogui not installed"
        pyautogui.typewrite(text, interval=0.02) if text.isascii() else pyautogui.write(text)
        return f"Typed: {text[:50]}..."

    def tool_hotkey(self, keys: str) -> str:
        if not pyautogui:
            return "pyautogui not installed"
        key_list = [k.strip() for k in keys.split("+")]
        pyautogui.hotkey(*key_list)
        return f"Pressed: {keys}"

    def tool_mouse_move(self, x: int, y: int) -> str:
        if not pyautogui:
            return "pyautogui not installed"
        pyautogui.moveTo(x, y, duration=0.3)
        return f"Mouse moved to ({x}, {y})"

    def tool_scroll(self, direction: str, amount: int = 3) -> str:
        if not pyautogui:
            return "pyautogui not installed"
        clicks = amount if direction == "up" else -amount
        pyautogui.scroll(clicks)
        return f"Scrolled {direction} by {amount}"

    def tool_find_on_screen(self, query: str) -> str:
        """Find text on screen using OCR."""
        if not pyautogui:
            return "pyautogui not installed"

        img = pyautogui.screenshot()
        try:
            import pytesseract
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

            matches = []
            for i, text in enumerate(data["text"]):
                if query.lower() in str(text).lower():
                    x = data["left"][i] + data["width"][i] // 2
                    y = data["top"][i] + data["height"][i] // 2
                    matches.append({"text": text, "x": x, "y": y})

            if matches:
                return f"Found '{query}' at {len(matches)} location(s): {json.dumps(matches[:5])}"
            return f"'{query}' not found on screen"

        except ImportError:
            return "pytesseract not installed — pip install pytesseract"

    # ── App & System ────────────────────────────────────────────

    def tool_open_app(self, app_name: str) -> str:
        """Open application by name."""
        app = app_name.lower()

        if sys.platform == "win32":
            app_map = {
                "chrome": "chrome", "browser": "chrome",
                "notepad": "notepad", "calculator": "calc",
                "explorer": "explorer", "files": "explorer",
                "terminal": "wt", "cmd": "cmd",
                "code": "code", "vscode": "code",
                "spotify": "spotify",
                "slack": "slack",
                "teams": "teams",
                "word": "winword", "excel": "excel",
                "powerpoint": "powerpnt",
                "photoshop": "photoshop",
                "blender": "blender",
                "premiere": "Adobe Premiere Pro",
                "after effects": "AfterFX",
                "capcut": "CapCut",
            }
            cmd = app_map.get(app, app_name)
            try:
                subprocess.Popen(cmd, shell=True)
                return f"Opened {app_name}"
            except Exception:
                # Try Start Menu search
                subprocess.Popen(f'start "" "{cmd}"', shell=True)
                return f"Attempted to open {app_name}"

        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", app_name])
            return f"Opened {app_name}"

        else:
            subprocess.Popen([app_name.lower()])
            return f"Opened {app_name}"

    def tool_open_url(self, url: str) -> str:
        """Open URL in default browser."""
        import webbrowser
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        webbrowser.open(url)
        return f"Opened {url}"

    def tool_run_command(self, command: str) -> str:
        """Run shell command and return output."""
        # Safety check
        dangerous = ["rm -rf /", "format c:", "del /f /s /q",
                      ":(){:|:&};:", "mkfs", "dd if="]
        if any(d in command.lower() for d in dangerous):
            return "⚠️ Blocked: dangerous command detected"

        try:
            result = subprocess.run(
                command, shell=True,
                capture_output=True, text=True,
                timeout=30
            )
            output = result.stdout + result.stderr
            return output[:3000] if output else "Command executed (no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out (30s limit)"
        except Exception as e:
            return f"Error: {e}"

    def tool_get_system_info(self) -> str:
        """Get system resource info."""
        info = {
            "platform": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        }

        if psutil_mod:
            cpu = psutil_mod.cpu_percent(interval=1)
            mem = psutil_mod.virtual_memory()
            disk = psutil_mod.disk_usage("/")
            info.update({
                "cpu_percent": f"{cpu}%",
                "ram_total": f"{mem.total / (1024**3):.1f} GB",
                "ram_used": f"{mem.used / (1024**3):.1f} GB ({mem.percent}%)",
                "disk_total": f"{disk.total / (1024**3):.1f} GB",
                "disk_free": f"{disk.free / (1024**3):.1f} GB",
            })

            # Top processes
            procs = []
            for p in psutil_mod.process_iter(["name", "cpu_percent"]):
                try:
                    if p.info["cpu_percent"] > 1:
                        procs.append(f"{p.info['name']}: {p.info['cpu_percent']}%")
                except Exception:
                    pass
            if procs:
                info["top_processes"] = procs[:5]

        return json.dumps(info, indent=2)

    # ── File Operations ─────────────────────────────────────────

    def tool_read_file(self, path: str) -> str:
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return f"File not found: {path}"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return content[:5000]
        except Exception as e:
            return f"Error reading file: {e}"

    def tool_write_file(self, path: str, content: str) -> str:
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Written {len(content)} chars to {path}"

    def tool_list_files(self, path: str = ".") -> str:
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return f"Path not found: {path}"

        items = []
        for item in sorted(os.listdir(path)):
            full = os.path.join(path, item)
            if os.path.isdir(full):
                items.append(f"📁 {item}/")
            else:
                size = os.path.getsize(full)
                items.append(f"📄 {item} ({size:,} bytes)")

        return "\n".join(items[:50])

    # ── Clipboard ───────────────────────────────────────────────

    def tool_get_clipboard(self) -> str:
        if pyperclip:
            return pyperclip.paste() or "(clipboard empty)"
        return "pyperclip not installed"

    def tool_set_clipboard(self, text: str) -> str:
        if pyperclip:
            pyperclip.copy(text)
            return f"Copied to clipboard: {text[:50]}..."
        return "pyperclip not installed"

    # ── Notes ───────────────────────────────────────────────────

    def tool_take_note(self, title: str, content: str) -> str:
        notes = self._load_notes()
        notes.append({
            "title": title,
            "content": content,
            "created": datetime.now().isoformat()
        })
        self._save_notes(notes)
        return f"Note saved: {title}"

    def tool_get_notes(self) -> str:
        notes = self._load_notes()
        if not notes:
            return "No notes saved."
        lines = []
        for i, n in enumerate(notes, 1):
            lines.append(f"{i}. [{n['created'][:10]}] {n['title']}: {n['content'][:100]}")
        return "\n".join(lines)

    def _load_notes(self) -> list:
        if os.path.exists(NOTES_FILE):
            with open(NOTES_FILE, "r") as f:
                return json.load(f)
        return []

    def _save_notes(self, notes: list):
        with open(NOTES_FILE, "w") as f:
            json.dump(notes, f, indent=2)
