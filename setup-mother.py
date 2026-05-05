#!/usr/bin/env python
"""
LOTUS Mother setup wizard.

Run once on the machine that will host the MOTHER (the brain + Ollama +
Gemma model + Chrome-for-Gemini + full dashboard). This is the
bigger-machine install — use it when you're moving LOTUS to a beefier
Mac or a Linux/Windows workstation with a better GPU.

What this wizard does:
  1. Detects OS, Python 3.10+, Ollama, Tesseract, Chrome.
  2. Creates a venv at ./venv and installs the FULL dep set
     (websockets, requests, Pillow, pytesseract, mistune, zeroconf,
      openai-whisper, edge-tts, pyaudio, resemblyzer, playwright, …).
  3. Pulls the Gemma variant you pick (default gemma4:e4b; suggests
     gemma4:12b and gemma4:31b when the machine has the RAM).
  4. Makes Ollama listen on 0.0.0.0 so LAN children can reach it.
  5. Writes ~/.lotus_config.json with your model choice + paths.
  6. Generates or reuses ~/.lotus_auth/token.txt (the shared LAN token).
  7. Writes a launcher:
        macOS  → run-mother.command
        Linux  → run-mother.sh
        Windows → run-mother.bat
  8. Optionally walks through Chrome CDP setup for RendererAgent.

This does NOT auto-install Ollama/Tesseract/Chrome — those are system
tools that want your approval. The wizard detects what's missing and
prints the exact install command; you run it, then re-invoke the
wizard.

Usage:
    cd LotusAgent
    python setup-mother.py

Re-run any time to change the model, upgrade deps, or reconfigure LAN.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
VENV = REPO / "venv"
TOKEN_PATH = Path(os.path.expanduser("~/.lotus_auth/token.txt"))
CONFIG_PATH = Path(os.path.expanduser("~/.lotus_config.json"))

# Curated Gemma variants, synced with lotus-model.py's GEMMA_CATALOG.
GEMMA_CATALOG = [
    ("gemma4:e4b",  "~5 GB",  "8 GB+ RAM",  "Default. Fast, fits on M1/M2 base hardware."),
    ("gemma4:e12b", "~14 GB", "16 GB+ RAM", "Efficient 12B — good balance of speed + quality."),
    ("gemma4:12b",  "~14 GB", "20 GB+ RAM", "Mid. Better reasoning than e4b."),
    ("gemma4:31b",  "~36 GB", "48 GB+ RAM", "Large. Best quality, needs beefy GPU or Mac Studio."),
]

# Full mother dep set. Keeps the child's narrow set (review/parse/archive)
# + adds the heavy voice + browser + vision bits the mother needs.
MOTHER_DEPS = [
    # Common with child
    "websockets", "requests", "pillow", "pytesseract", "mistune", "zeroconf",
    # Voice stack
    "SpeechRecognition", "edge-tts", "openai-whisper", "pyaudio",
    "pyperclip", "psutil",
    # Voice auth
    "resemblyzer", "numpy",
    # Browser automation
    "playwright", "playwright-stealth",
    # Screen / OCR (already in pytesseract above, just pyautogui)
    "pyautogui",
]


# ── Small helpers ──────────────────────────────────────────────────────

def banner(title: str) -> None:
    print("\n" + "=" * 64)
    print(" " + title)
    print("=" * 64)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\naborted"); sys.exit(130)
    return ans or default


def ask_yesno(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        ans = input(f"{prompt} ({d}): ").strip().lower()
        if not ans: return default
        if ans in ("y", "yes"): return True
        if ans in ("n", "no"):  return False


def run(cmd, check=True, capture=False):
    """Run a command, echoing it first so the user sees what's happening."""
    if isinstance(cmd, list):
        print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}")
    else:
        print(f"  $ {cmd}")
    return subprocess.run(
        cmd, check=check, shell=isinstance(cmd, str),
        capture_output=capture, text=True,
    )


def which(name: str) -> str:
    return shutil.which(name) or ""


def mem_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        return 0.0


# ── Steps ──────────────────────────────────────────────────────────────

def detect_env() -> dict:
    info = {
        "os":       platform.system(),
        "release":  platform.release(),
        "machine":  platform.machine(),
        "python":   ".".join(map(str, sys.version_info[:3])),
        "hostname": socket.gethostname(),
        "ram_gb":   round(mem_gb(), 1),
    }
    banner("Environment")
    for k, v in info.items():
        print(f"  {k:10} {v}")
    if sys.version_info < (3, 10):
        print("\n  ⚠ Python 3.10+ recommended.")
    return info


def check_prereqs(info: dict) -> bool:
    banner("Prerequisite check")
    os_name = info["os"]
    all_ok = True

    # Ollama
    if which("ollama"):
        # Version check: ollama --version
        try:
            v = subprocess.run(["ollama", "--version"], capture_output=True,
                               text=True, timeout=5).stdout.strip()
            print(f"  ✓ Ollama  ({v})")
        except Exception:
            print(f"  ✓ Ollama  ({which('ollama')})")
    else:
        print(f"  ✗ Ollama not installed.")
        if os_name == "Darwin":
            print(f"      Install: brew install ollama   (or from https://ollama.com)")
        elif os_name == "Windows":
            print(f"      Install: https://ollama.com/download/windows")
        else:
            print(f"      Install: curl -fsSL https://ollama.com/install.sh | sh")
        all_ok = False

    # Tesseract
    if which("tesseract"):
        try:
            v = subprocess.run(["tesseract", "--version"], capture_output=True,
                               text=True, timeout=5).stdout.splitlines()[0]
            print(f"  ✓ Tesseract  ({v})")
        except Exception:
            print(f"  ✓ Tesseract")
    else:
        print(f"  ✗ Tesseract not installed (needed for ReviewAgent OCR).")
        if os_name == "Darwin":
            print(f"      Install: brew install tesseract")
        elif os_name == "Windows":
            print(f"      Install: https://github.com/UB-Mannheim/tesseract/wiki")
        else:
            print(f"      Install: sudo apt install tesseract-ocr")
        all_ok = False

    # Chrome
    chrome_path = ""
    if os_name == "Darwin":
        if os.path.exists("/Applications/Google Chrome.app"): chrome_path = "/Applications/Google Chrome.app"
    elif os_name == "Windows":
        for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
            if os.path.exists(p): chrome_path = p; break
    else:
        chrome_path = which("google-chrome") or which("chromium") or ""
    if chrome_path:
        print(f"  ✓ Chrome  ({chrome_path})")
    else:
        print(f"  ⚠ Chrome not found — RendererAgent needs it (Gemini automation).")
        print(f"      Download: https://www.google.com/chrome/")
        # Not a hard failure — user may not want RendererAgent.

    # Portaudio (for pyaudio build)
    if os_name == "Darwin" and not shutil.which("brew"):
        print(f"  ⚠ Homebrew not found — needed for portaudio (pyaudio's C dep).")
        print(f"      Install: /bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
        all_ok = False
    elif os_name == "Darwin":
        try:
            subprocess.run(["brew", "list", "portaudio"], capture_output=True, check=True, timeout=5)
            print(f"  ✓ portaudio  (brew)")
        except Exception:
            print(f"  ⚠ portaudio not installed via brew — pyaudio pip install will fail.")
            print(f"      Install: brew install portaudio")
            all_ok = False

    return all_ok


def pick_model(info: dict) -> str:
    banner("Gemma model")
    ram = info.get("ram_gb", 0) or 0
    # Suggest based on available RAM (rough rule-of-thumb: need model size + 10GB headroom).
    print(f"  Your machine has {ram:.1f} GB RAM.\n")
    print(f"  {'NAME':<16} {'SIZE':<8} {'NEEDS':<14} NOTES")
    print("  " + "─" * 70)
    recommended = "gemma4:e4b"
    for name, size, needs, notes in GEMMA_CATALOG:
        needs_gb = int("".join(c for c in needs if c.isdigit()) or "0")
        marker = "  "
        if ram >= needs_gb:
            marker = "✓ "
            if needs_gb > 20 and "31" in name:
                recommended = name
            elif needs_gb > 14 and "12b" in name and recommended == "gemma4:e4b":
                recommended = name
        else:
            marker = "× "      # too heavy for this RAM
        print(f"  {marker}{name:<14} {size:<8} {needs:<14} {notes}")
    print()
    print(f"  ✓ = your machine has enough RAM.  × = too heavy.")
    return ask("\n  Which model?", recommended)


def ensure_venv() -> Path:
    banner("Python venv at ./venv")
    if VENV.exists():
        print(f"  venv already exists at {VENV}")
    else:
        run([sys.executable, "-m", "venv", str(VENV)])
    vpy = VENV / ("Scripts" if platform.system() == "Windows" else "bin") / \
          ("python.exe" if platform.system() == "Windows" else "python")
    run([str(vpy), "-m", "pip", "install", "--upgrade", "pip", "wheel"], check=False)
    print(f"  installing full mother deps ({len(MOTHER_DEPS)} pkgs)…")
    run([str(vpy), "-m", "pip", "install", *MOTHER_DEPS])
    # Playwright ships its own browser — download it.
    print(f"  installing Playwright Chromium…")
    run([str(vpy), "-m", "playwright", "install", "chromium"], check=False)
    return vpy


def configure_ollama_lan(info: dict) -> None:
    banner("Ollama LAN binding")
    print("  Children reach Ollama over the LAN; by default it only binds to")
    print("  127.0.0.1. Setting OLLAMA_HOST=0.0.0.0 makes it LAN-accessible.")
    if not ask_yesno("\n  Apply OLLAMA_HOST=0.0.0.0 and restart Ollama?", default=True):
        print("  Skipped — run children only on THIS machine or set it yourself.")
        return
    os_name = info["os"]
    if os_name == "Darwin":
        run(["launchctl", "setenv", "OLLAMA_HOST", "0.0.0.0"], check=False)
        # Restart via brew services if available, else via the app.
        if shutil.which("brew"):
            run(["brew", "services", "restart", "ollama"], check=False)
        print("  ✓ OLLAMA_HOST set + Ollama restarted.")
    elif os_name == "Windows":
        # Persistent env var via `setx`; user must restart Ollama manually.
        run(["setx", "OLLAMA_HOST", "0.0.0.0"], check=False)
        print("  ✓ OLLAMA_HOST set. Now QUIT + relaunch Ollama for it to take effect.")
    else:
        print("  Add OLLAMA_HOST=0.0.0.0 to /etc/systemd/system/ollama.service.d/override.conf,")
        print("  then: systemctl daemon-reload && systemctl restart ollama")


def pull_model(model: str) -> None:
    banner(f"Pulling Gemma: {model}")
    if not shutil.which("ollama"):
        print("  ✗ `ollama` not on PATH. Skipping pull.")
        return
    rc = subprocess.call(["ollama", "pull", model])
    if rc == 0:
        print(f"  ✓ {model} downloaded.")
    else:
        print(f"  ✗ pull failed (exit {rc}). Re-run setup-mother.py to retry.")


def ensure_token() -> str:
    """Create the shared auth token if missing. This is what children
    use when they register with the mother."""
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text().strip()
        if token:
            return token
    import secrets
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token)
    try: os.chmod(TOKEN_PATH, 0o600)
    except OSError: pass
    return token


def write_config(model: str) -> None:
    banner("Writing ~/.lotus_config.json")
    sys.path.insert(0, str(REPO))
    import lotus_config as _lc
    cfg = _lc.load()
    cfg.setdefault("ollama", {})["model"] = model
    _lc.save(cfg)
    print(f"  wrote ollama.model={model}")


def write_launcher(info: dict, vpy: Path) -> Path:
    banner("Launcher")
    os_name = info["os"]
    if os_name == "Windows":
        path = REPO / "run-mother.bat"
        body = (
            "@echo off\r\n"
            "rem LOTUS mother launcher (generated by setup-mother.py)\r\n"
            f'cd /d "{REPO}"\r\n'
            "rem CDP Chrome is optional — comment the next 3 lines if you don't\r\n"
            "rem want to use RendererAgent.\r\n"
            'start "" "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            '--remote-debugging-port=9222 '
            '--user-data-dir=%USERPROFILE%\\.lotus_auth\\chrome_cdp_profile '
            'https://gemini.google.com/app\r\n'
            "timeout /t 3 /nobreak >nul\r\n"
            f'set LOTUS_CDP_URL=http://localhost:9222\r\n'
            f'"{vpy}" agent_phase1.py v\r\n'
            "pause\r\n"
        )
    elif os_name == "Darwin":
        path = REPO / "run-mother.command"
        body = (
            "#!/usr/bin/env bash\n"
            "# LOTUS mother launcher (generated by setup-mother.py)\n"
            f'cd "{REPO}"\n'
            "# CDP Chrome for RendererAgent. Comment the next two lines if you\n"
            "# don't want Gemini automation.\n"
            '"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" '
            '--remote-debugging-port=9222 '
            '--user-data-dir="$HOME/.lotus_auth/chrome_cdp_profile" '
            'https://gemini.google.com/app &\n'
            'sleep 3\n'
            'export LOTUS_CDP_URL=http://localhost:9222\n'
            f'"{vpy}" agent_phase1.py v\n'
        )
    else:
        path = REPO / "run-mother.sh"
        body = (
            "#!/usr/bin/env bash\n"
            "# LOTUS mother launcher (generated by setup-mother.py)\n"
            f'cd "{REPO}"\n'
            f'"{vpy}" agent_phase1.py v\n'
        )
    path.write_text(body)
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError: pass
    print(f"  wrote {path}")
    return path


def final_instructions(info: dict, launcher: Path, token: str, model: str) -> None:
    banner("Next steps")
    os_name = info["os"]
    my_ip = "localhost"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); my_ip = s.getsockname()[0]; s.close()
    except Exception: pass

    print(f"\n  1. Start the mother:")
    if os_name == "Windows":
        print(f"        Double-click {launcher}")
    else:
        print(f"        {launcher}")
    print(f"\n  2. Model in use:  {model}")
    print(f"     Change later:  venv/bin/python lotus-model.py switch <name>")
    print(f"\n  3. To enlist children:")
    print(f"        - Copy this LotusAgent folder to the child machine.")
    print(f"        - Run install-child.bat (Windows) or install-child.command (Mac).")
    print(f"        - Give it THIS machine's IP:  {my_ip}")
    print(f"        - Give it THIS token:         {token}")
    print(f"     The mother auto-discovers children via mDNS once they're online.")
    print(f"\n  4. Dashboard URL (Chrome or the LOTUS Electron app):")
    print(f"        http://{my_ip}:8766/dashboard.html?token={token}")


# ── Main ──────────────────────────────────────────────────────────────

def main() -> None:
    banner("LOTUS Agent Mother — setup wizard")
    print("  Installs the FULL mother on this machine (Ollama + Gemma + full")
    print("  Python deps + Chrome CDP setup + launcher). Safe to re-run.")
    info = detect_env()
    if not check_prereqs(info):
        print("\n  Install missing prerequisites above, then re-run this wizard.")
        if not ask_yesno("  Continue anyway (you know what you're doing)?",
                          default=False):
            sys.exit(2)

    model = pick_model(info)
    if not ask_yesno(f"\n  Install venv + pull {model}?  This may take several minutes.",
                     default=True):
        sys.exit(0)

    vpy = ensure_venv()
    configure_ollama_lan(info)
    pull_model(model)
    token = ensure_token()
    write_config(model)
    launcher = write_launcher(info, vpy)
    final_instructions(info, launcher, token, model)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)
