#!/usr/bin/env python
"""
Cross-platform LOTUS child setup wizard.

Run this ONE script on each machine you want to enlist as a LOTUS child
(Windows / macOS / Linux). It:

  1. Detects the OS + Python version.
  2. Prompts for:
       - mother host (Mac's hostname or IP, e.g. 192.168.50.111)
       - auth token (from mother's ~/.lotus_auth/token.txt)
       - which agents to host (default: all four)
       - whether to host RendererAgent (needs its own Chrome + Gemini login)
  3. Installs narrow Python deps into a local venv in the repo folder.
  4. Writes ~/.lotus_child_config.json so `lotus-child.py` picks up the
     config on start.
  5. Generates a launcher script:
       - Windows → run-child.bat
       - macOS   → run-child.command  (double-clickable from Finder)
       - Linux   → run-child.sh
  6. Pings the mother's Ollama and WebSocket to verify network path.
  7. Prints the exact command to start the child.

Usage:
    cd LotusAgent
    python setup-child.py

This does NOT install Ollama — children use the mother's Ollama over the LAN.
If you chose RendererAgent, you'll need to install Chrome + log into Gemini
separately; the script prints step-by-step instructions for that.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.path.expanduser("~/.lotus_child_config.json"))
TOKEN_PATH  = Path(os.path.expanduser("~/.lotus_auth/token.txt"))

KNOWN_AGENTS = ["ParserAgent", "RendererAgent", "ReviewAgent", "ArchiverAgent"]
AGENTS_NEEDING_CHROME = {"RendererAgent"}


# ── Small helpers ──────────────────────────────────────────────────────

def banner(title: str) -> None:
    print("\n" + "=" * 64)
    print(" " + title)
    print("=" * 64)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    ans = input(f"{prompt}{suffix}: ").strip()
    return ans or default


def ask_yesno(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        ans = input(f"{prompt} ({d}): ").strip().lower()
        if not ans: return default
        if ans in ("y", "yes"): return True
        if ans in ("n", "no"):  return False


def ask_multi(prompt: str, options: list, defaults: list) -> list:
    """Ask user to pick from `options`; comma-separated, defaults to `defaults`.
    Returns the final list."""
    print(prompt)
    for i, opt in enumerate(options, 1):
        marker = "✓" if opt in defaults else "·"
        print(f"   {marker} {i}. {opt}")
    default_str = ",".join(str(options.index(d)+1) for d in defaults)
    raw = input(f"  Pick (comma-separated numbers) [{default_str}]: ").strip()
    if not raw:
        raw = default_str
    picked = []
    for token in raw.split(","):
        token = token.strip()
        if not token: continue
        try:
            picked.append(options[int(token) - 1])
        except (ValueError, IndexError):
            print(f"   ignoring invalid choice: {token!r}")
    return picked


def run(cmd, check=True, capture=False) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return subprocess.run(
        cmd, check=check, shell=isinstance(cmd, str),
        capture_output=capture, text=True,
    )


# ── Steps ──────────────────────────────────────────────────────────────

def detect_env() -> dict:
    sys_info = {
        "os":      platform.system(),            # Windows / Darwin / Linux
        "release": platform.release(),
        "python":  ".".join(map(str, sys.version_info[:3])),
        "hostname": socket.gethostname(),
    }
    banner("Environment")
    for k, v in sys_info.items():
        print(f"  {k:10} {v}")
    # Guard rails
    if sys.version_info < (3, 10):
        print("\n  ⚠ Python 3.10+ recommended. Child may work on older but no guarantees.")
    return sys_info


def prompt_config(sys_info: dict, existing: dict) -> dict:
    banner("Child configuration")
    # Mother connection details
    mother_host = ask(
        "Mother's hostname or LAN IP (where Ollama runs)",
        existing.get("mother_host") or "192.168.1.100",
    )
    ollama_url = ask(
        "Ollama URL on mother",
        existing.get("ollama_url") or f"http://{mother_host}:11434",
    )
    # Auth token
    token_default = existing.get("token") or ""
    if not token_default and TOKEN_PATH.exists():
        token_default = TOKEN_PATH.read_text().strip()
    print(f"\n  Token lives at {TOKEN_PATH} on the MOTHER.")
    print(f"  Copy its contents here (it's a long random string):")
    token = ask("Token", token_default)
    if not token:
        print("  ✗ token is required — get it from the mother and re-run.")
        sys.exit(2)
    # Listen port
    port = ask("Port this child should listen on",
               str(existing.get("port", 8770)))
    # Agents to host
    defaults_agents = existing.get("agents") or ["ParserAgent", "ReviewAgent", "ArchiverAgent"]
    if isinstance(defaults_agents, str):
        defaults_agents = [a.strip() for a in defaults_agents.split(",") if a.strip()]
    # Filter known + preserve order
    defaults_agents = [a for a in defaults_agents if a in KNOWN_AGENTS]
    if not defaults_agents:
        defaults_agents = ["ParserAgent", "ReviewAgent", "ArchiverAgent"]
    print("\n  Which agents should this child host?")
    print("    ReviewAgent / ParserAgent / ArchiverAgent → need Ollama only (light)")
    print("    RendererAgent                              → needs Chrome + Gemini login (extra setup)")
    chosen = ask_multi("  Select agents:", KNOWN_AGENTS, defaults_agents)
    # Chrome / CDP for Renderer
    cdp_url = ""
    if "RendererAgent" in chosen:
        print("\n  RendererAgent needs its OWN Chrome with Gemini logged in.")
        cdp_url = ask(
            "CDP URL for this child's Chrome",
            existing.get("cdp_url") or "http://localhost:9222",
        )
    return {
        "mother_host": mother_host,
        "ollama_url":  ollama_url,
        "token":       token,
        "port":        int(port),
        "bind":        "0.0.0.0",
        "agents":      ",".join(chosen),
        "cdp_url":     cdp_url,
    }


def preflight(config: dict) -> None:
    banner("Preflight checks")
    # Ollama reachability
    import urllib.request, urllib.error
    try:
        url = config["ollama_url"].rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=5) as r:
            body = r.read().decode()
        print(f"  ✓ Ollama reachable at {config['ollama_url']}")
        try:
            import json as _json
            models = [m["name"] for m in _json.loads(body).get("models",[])]
            print(f"    models: {models[:6]}")
        except Exception: pass
    except Exception as e:
        print(f"  ⚠ Ollama NOT reachable ({e})")
        print(f"    Fix: on the mother, set OLLAMA_HOST=0.0.0.0 and restart Ollama.")
        print(f"    Verify with: curl {config['ollama_url']}/api/tags")
    # Local port collision check
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    try:
        s.bind((config["bind"], config["port"]))
        print(f"  ✓ port {config['port']} is available for binding")
    except Exception as e:
        print(f"  ⚠ port {config['port']} NOT bindable: {e}")
        print(f"    Another process is using it — pick a different port or free it.")
    finally:
        s.close()


def ensure_venv(sys_info: dict) -> Path:
    """Create a venv inside the repo ('.childvenv/') and install narrow deps.
    Uses the current Python interpreter. Returns the venv's Python path."""
    banner("Python venv")
    venv_dir = REPO / ".childvenv"
    if venv_dir.exists():
        print(f"  venv already exists at {venv_dir}")
    else:
        print(f"  creating venv at {venv_dir}")
        run([sys.executable, "-m", "venv", str(venv_dir)])
    # Path to venv python + pip (Windows vs POSIX layout differs)
    if sys_info["os"] == "Windows":
        vpy = venv_dir / "Scripts" / "python.exe"
    else:
        vpy = venv_dir / "bin" / "python"

    # Narrow dep set — just what child-hosted agents need.
    # - websockets: the child protocol
    # - requests:   Ollama HTTP calls
    # - pytesseract + Pillow: ReviewAgent OCR (Windows needs tesseract binary separately)
    # - mistune:    ParserAgent
    # If RendererAgent later, add playwright + install its browsers.
    deps = ["websockets", "requests", "pillow", "pytesseract", "mistune",
            "zeroconf"]   # mDNS auto-discovery so mother finds us without env var
    print(f"  installing: {' '.join(deps)}")
    run([str(vpy), "-m", "pip", "install", "--upgrade", "pip", "wheel"], check=False)
    run([str(vpy), "-m", "pip", "install", *deps])
    return vpy


def write_config(config: dict) -> None:
    banner("Writing config")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Also stash the token at the canonical spot if it wasn't there already,
    # so other tools (mother pointing at this child, verification scripts)
    # can find it consistently.
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TOKEN_PATH.exists() or TOKEN_PATH.read_text().strip() != config["token"]:
        TOKEN_PATH.write_text(config["token"])
        try:
            os.chmod(TOKEN_PATH, 0o600)   # POSIX only; Windows ignores
        except OSError: pass
        print(f"  wrote token → {TOKEN_PATH}")
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError: pass
    print(f"  wrote config → {CONFIG_PATH}")


def write_launcher(sys_info: dict, vpy: Path) -> Path:
    """Generate a double-clickable launcher in the repo root."""
    banner("Launcher")
    if sys_info["os"] == "Windows":
        path = REPO / "run-child.bat"
        body = (
            "@echo off\r\n"
            "rem LOTUS child launcher (generated by setup-child.py)\r\n"
            f'cd /d "{REPO}"\r\n'
            f'"{vpy}" lotus-child.py\r\n'
            "pause\r\n"
        )
    elif sys_info["os"] == "Darwin":
        path = REPO / "run-child.command"
        body = (
            "#!/usr/bin/env bash\n"
            "# LOTUS child launcher (generated by setup-child.py)\n"
            f'cd "{REPO}"\n'
            f'"{vpy}" lotus-child.py\n'
        )
    else:
        path = REPO / "run-child.sh"
        body = (
            "#!/usr/bin/env bash\n"
            "# LOTUS child launcher (generated by setup-child.py)\n"
            f'cd "{REPO}"\n'
            f'"{vpy}" lotus-child.py\n'
        )
    path.write_text(body)
    try:
        # Make it executable on POSIX (no-op on Windows).
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError: pass
    print(f"  wrote launcher → {path}")
    return path


def firewall_notes(sys_info: dict, config: dict) -> None:
    banner("Firewall")
    if sys_info["os"] == "Windows":
        print(f"  Windows Defender will prompt the first time the child binds to "
              f"port {config['port']}. Allow ACCESS for Private networks (not public).")
        print(f"  If you never see the prompt, run (as admin) in PowerShell:")
        print(f"    netsh advfirewall firewall add rule name=\"LOTUS child\" "
              f"dir=in action=allow protocol=TCP localport={config['port']}")
    elif sys_info["os"] == "Darwin":
        print("  macOS: on first run, System Settings may ask to allow incoming")
        print("         connections for Python. Allow.")
    else:
        print(f"  Linux: make sure ufw / iptables allows inbound TCP "
              f"{config['port']} on the LAN interface.")


def final_instructions(sys_info: dict, launcher: Path, config: dict) -> None:
    banner("Done — next steps")
    print(f"\n  Start the child (from any user on this machine):")
    if sys_info["os"] == "Windows":
        print(f"    Double-click: {launcher}")
        print(f"    Or from cmd:  {launcher}")
    else:
        print(f"    Double-click: {launcher}")
        print(f"    Or from shell: {launcher}")
    print("\n  The child will log to its own terminal. Keep it open while running.")
    print("\n  ── Back on the MOTHER (Mac Studio), restart with this env var ──")
    ip_hint = socket.gethostname()
    print(f"    export LOTUS_CHILDREN=\"ws://{ip_hint}:{config['port']}?agents={config['agents']}\"")
    print(f"    (If hostname doesn't resolve on the Mac, use this machine's LAN IP.)")
    if "RendererAgent" in config["agents"]:
        print("\n  ── RendererAgent extra setup (this machine needs its own Chrome) ──")
        if sys_info["os"] == "Windows":
            print('    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                  f'--remote-debugging-port={config["cdp_url"].rsplit(":",1)[-1]} '
                  '--user-data-dir=%USERPROFILE%\\.lotus_auth\\chrome_cdp_profile '
                  'https://gemini.google.com/app')
        else:
            print('    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome '
                  f'--remote-debugging-port={config["cdp_url"].rsplit(":",1)[-1]} '
                  '--user-data-dir="$HOME/.lotus_auth/chrome_cdp_profile" '
                  'https://gemini.google.com/app &')
        print("    Log into Gemini in that Chrome window ONCE; the profile persists.")
    print("\n  To re-run the wizard: python setup-child.py")


# ── Main ──────────────────────────────────────────────────────────────

def main() -> None:
    banner("LOTUS Agent Child — setup wizard")
    print("  This sets up THIS machine as a LOTUS child.")
    print("  The mother (Mac Studio) already running the main agent_phase1.py\n"
          "  keeps control; children run workloads and report back.")

    sys_info = detect_env()
    existing = {}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text())
            print(f"\n  Found existing config at {CONFIG_PATH} — using as defaults.")
        except Exception: pass

    config = prompt_config(sys_info, existing)
    preflight(config)
    if not ask_yesno("\nProceed to install venv + write config?", default=True):
        print("  Aborted. No changes made.")
        sys.exit(0)

    vpy = ensure_venv(sys_info)
    write_config(config)
    launcher = write_launcher(sys_info, vpy)
    firewall_notes(sys_info, config)
    final_instructions(sys_info, launcher, config)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)
