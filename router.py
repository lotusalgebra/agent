"""
Hybrid Router — Routes commands between Claude.ai (search/research) and API (tasks/tools).

Claude.ai handles: web search, research, current news, deep research
API handles: screen control, file ops, app launching, system tasks, quick Q&A
"""

import re
import time
import sys

pyautogui = None
pyperclip = None

def _init():
    global pyautogui, pyperclip
    try:
        import pyautogui as pag
        pyautogui = pag
    except ImportError:
        pass
    try:
        import pyperclip as pc
        pyperclip = pc
    except ImportError:
        pass

_init()

# Search/research triggers — route to Claude.ai
SEARCH_TRIGGERS = [
    "search", "google", "look up", "find out", "what's happening",
    "latest", "current", "news", "trending", "today",
    "who is", "what is the price", "stock price",
    "weather", "score", "results", "update",
    "research", "deep research", "analyze this url",
    "compare", "review", "summarize this article",
    "what happened", "did .* win", "is .* still",
    "how much does", "where can i",
]

# Task triggers — route to API
TASK_TRIGGERS = [
    "open", "click", "type", "screenshot", "scroll",
    "close", "minimize", "maximize", "switch",
    "create file", "write file", "read file", "list files",
    "run command", "terminal", "execute",
    "copy", "paste", "clipboard",
    "note", "remind", "timer",
    "system", "cpu", "ram", "disk",
    "move mouse", "press", "hotkey",
]


class HybridRouter:
    def __init__(self):
        self.claude_url = "https://claude.ai/new"
        self.page_load_wait = 3

    def classify(self, command: str) -> str:
        """Classify command as 'search' (Claude.ai) or 'task' (API)."""
        cmd_lower = command.lower().strip()

        # Explicit search routing
        if any(phrase in cmd_lower for phrase in [
            "search for", "look up", "google",
            "what's the latest", "any news",
            "search claude", "ask claude",
            "deep research", "research about"
        ]):
            return "search"

        # Check search triggers
        for trigger in SEARCH_TRIGGERS:
            if re.search(r'\b' + trigger + r'\b', cmd_lower):
                return "search"

        # Check task triggers
        for trigger in TASK_TRIGGERS:
            if trigger in cmd_lower:
                return "task"

        # Questions about current affairs → search
        if cmd_lower.startswith(("what", "who", "when", "where", "how much", "is there", "did")):
            return "search"

        return "task"

    def send_to_claude_chat(self, query: str) -> str:
        """
        Focus Claude.ai → type query → send.
        """
        if not pyautogui:
            return "pyautogui not installed"

        try:
            # Step 1: Find Claude.ai tab or open new one
            found = self._focus_claude_tab()
            if not found:
                self._open_new_chat()

            time.sleep(1)

            # Step 2: Click input area (bottom center of screen)
            screen_w, screen_h = pyautogui.size()
            input_x = screen_w // 2
            input_y = screen_h - 130

            # Try OCR for precise input location
            precise = self._find_input_area()
            if precise:
                input_x, input_y = precise

            pyautogui.click(input_x, input_y)
            time.sleep(0.5)

            # Step 3: Clear + paste query (clipboard for Unicode safety)
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.1)

            if pyperclip:
                pyperclip.copy(query)
                pyautogui.hotkey('ctrl', 'v')
            else:
                pyautogui.typewrite(query, interval=0.01)

            time.sleep(0.3)

            # Step 4: Send
            pyautogui.press('enter')

            return f"Sent to Claude.ai: '{query[:80]}' — check browser for response."

        except Exception as e:
            return f"Claude.ai automation error: {e}"

    def _focus_claude_tab(self) -> bool:
        """Find and focus existing Claude.ai browser window."""
        try:
            if sys.platform == "win32":
                import ctypes

                EnumWindows = ctypes.windll.user32.EnumWindows
                GetWindowTextW = ctypes.windll.user32.GetWindowTextW
                SetForegroundWindow = ctypes.windll.user32.SetForegroundWindow
                ShowWindow = ctypes.windll.user32.ShowWindow
                IsWindowVisible = ctypes.windll.user32.IsWindowVisible

                WNDENUMPROC = ctypes.WINFUNCTYPE(
                    ctypes.c_bool, ctypes.c_int, ctypes.c_int
                )

                target = [None]

                def callback(hwnd, lparam):
                    if IsWindowVisible(hwnd):
                        buf = ctypes.create_unicode_buffer(512)
                        GetWindowTextW(hwnd, buf, 512)
                        title = buf.value.lower()
                        if "claude" in title and ("chrome" in title or "edge" in title or "firefox" in title or "brave" in title):
                            target[0] = hwnd
                            return False
                    return True

                EnumWindows(WNDENUMPROC(callback), 0)

                if target[0]:
                    ShowWindow(target[0], 9)  # SW_RESTORE
                    SetForegroundWindow(target[0])
                    time.sleep(0.5)
                    return True

            elif sys.platform == "darwin":
                import subprocess
                script = '''
                tell application "Google Chrome"
                    repeat with w in windows
                        set tabIdx to 0
                        repeat with t in tabs of w
                            set tabIdx to tabIdx + 1
                            if URL of t contains "claude.ai" then
                                set active tab index of w to tabIdx
                                set index of w to 1
                                activate
                                return true
                            end if
                        end repeat
                    end repeat
                end tell
                return false
                '''
                r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
                return "true" in r.stdout.lower()

            else:
                import subprocess
                r = subprocess.run(["xdotool", "search", "--name", "Claude"], capture_output=True, text=True)
                if r.stdout.strip():
                    wid = r.stdout.strip().split('\n')[0]
                    subprocess.run(["xdotool", "windowactivate", wid])
                    time.sleep(0.5)
                    return True

        except Exception:
            pass
        return False

    def _open_new_chat(self):
        """Open fresh Claude.ai chat."""
        import webbrowser
        webbrowser.open(self.claude_url)
        time.sleep(self.page_load_wait)

    def _find_input_area(self):
        """Try OCR to locate Claude.ai text input precisely."""
        try:
            import pytesseract
            screenshot = pyautogui.screenshot()
            data = pytesseract.image_to_data(screenshot, output_type=pytesseract.Output.DICT)
            for i, text in enumerate(data["text"]):
                t = str(text).lower()
                if any(kw in t for kw in ["reply", "message", "ask"]):
                    return (
                        data["left"][i] + data["width"][i] // 2,
                        data["top"][i] + data["height"][i] // 2
                    )
        except Exception:
            pass
        return None
