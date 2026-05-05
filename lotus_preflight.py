"""
LOTUS Chrome preflight — ensures a Chrome instance with CDP debug port is
running and signed into Gemini before the agent mesh starts.

Productization rule: zero manual steps when sign-in cookies are cached.
On first run / expired session, opens Chrome and waits for the user to
sign in, broadcasting state to the dashboard so the React UI can render
a "Sign in to Gemini" overlay.

Cross-platform: Mac / Windows / Linux Chrome path discovery.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

DEFAULT_CDP_PORT = 9222
DEFAULT_PROFILE_DIR = Path.home() / ".lotus_auth" / "chrome_cdp_profile"
GEMINI_START_URL = "https://gemini.google.com/app"
SIGNIN_TIMEOUT_S = 600  # 10 min for the user to sign in before we give up


# ── Chrome binary discovery ────────────────────────────────────────────

def _find_chrome() -> str:
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    elif sys.platform == "win32":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    else:  # linux / bsd
        for name in ("google-chrome", "google-chrome-stable",
                     "chromium", "chromium-browser"):
            p = shutil.which(name)
            if p:
                return p
        candidates = ["/usr/bin/google-chrome", "/usr/bin/chromium"]

    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise RuntimeError(
        "Google Chrome not found. Install from https://www.google.com/chrome/ "
        f"(checked: {candidates!r})"
    )


# ── CDP probes ─────────────────────────────────────────────────────────

def cdp_alive(port: int = DEFAULT_CDP_PORT,
              timeout: float = 1.5) -> Optional[dict]:
    """Return /json/version dict if CDP is responding, else None."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _list_tabs(port: int, timeout: float = 2.0) -> Optional[list]:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _open_gemini_tab(port: int, timeout: float = 5.0) -> bool:
    """Tell Chrome to open a Gemini tab via CDP HTTP API.

    Tries PUT /json/new (modern Chrome) first, then GET (older variants).
    """
    target = f"http://127.0.0.1:{port}/json/new?{GEMINI_START_URL}"
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(target, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False


def _gemini_tab_state(port: int) -> Optional[bool]:
    """True if an existing tab is on signed-in Gemini; False if it's stuck
    on accounts.google.com; None if no Gemini-related tab is open."""
    tabs = _list_tabs(port)
    if tabs is None:
        return None
    saw_signin = False
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        url = (tab.get("url") or "").lower()
        if "gemini.google.com/app" in url and "accounts.google.com" not in url:
            return True
        if "accounts.google.com" in url and "service=cl" in url:
            saw_signin = True
        if "accounts.google.com" in url and "gemini" in url:
            saw_signin = True
    return False if saw_signin else None


def gemini_signed_in(port: int = DEFAULT_CDP_PORT,
                     timeout: float = 2.0) -> Optional[bool]:
    """
    Determine Gemini sign-in state:
      True  — a tab is on gemini.google.com/app, not redirected to sign-in.
      False — a tab is on the accounts.google.com sign-in flow (signed out).
      None  — CDP unreachable.

    If no Gemini tab exists at all, opens one via CDP and waits briefly for
    the URL to settle so we can give a definitive answer.
    """
    state = _gemini_tab_state(port)
    if state is True or state is False:
        return state
    if _list_tabs(port, timeout) is None:
        return None  # CDP not reachable

    # No Gemini tab present — open one and wait for it to load.
    if not _open_gemini_tab(port):
        return None  # couldn't tell Chrome to open the tab
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        time.sleep(0.5)
        state = _gemini_tab_state(port)
        if state is True or state is False:
            return state
    # Tab opened but URL didn't settle either way — assume signed-out so the
    # outer wait-loop prompts the user.
    return False


# ── Chrome launch ──────────────────────────────────────────────────────

def _clear_singleton_locks(profile_dir: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile_dir / name).unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass  # if Chrome itself is using it, launch will surface the error


def launch_chrome(profile_dir: Path = DEFAULT_PROFILE_DIR,
                  port: int = DEFAULT_CDP_PORT,
                  start_url: str = GEMINI_START_URL) -> subprocess.Popen:
    """
    Launch Chrome with CDP debug port, detached so it survives LOTUS exit.
    If user later restarts LOTUS without closing this Chrome, preflight
    detects the live CDP and reuses it.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    _clear_singleton_locks(profile_dir)
    chrome = _find_chrome()
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


# ── Playwright environment guards ──────────────────────────────────────

MIN_PLAYWRIGHT_VERSION = "1.59"

# Stable substrings used to locate (and reapply) the LOTUS patch in
# Playwright's crBrowser.js. The patch wraps a browser-level CDP call
# that real Chrome rejects (Chromium-for-Testing accepts it). Without
# the wrap, connect_over_cdp raises and the burst dies.
_CRBROWSER_REL = ("driver", "package", "lib", "server", "chromium",
                  "crBrowser.js")
_CRBROWSER_MARKER = "// LOTUS patch:"
_CRBROWSER_UNPATCHED = (
    '    if (this._browser.options.name !== "clank" && this._options.acceptDownloads !== "internal-browser-default") {\n'
    '      promises.push(\n'
    '        this._browser._session.send("Browser.setDownloadBehavior", {\n'
    '          behavior: this._options.acceptDownloads === "accept" ? "allowAndName" : "deny",\n'
    '          browserContextId: this._browserContextId,\n'
    '          downloadPath: this._browser.options.downloadsPath,\n'
    '          eventsEnabled: true\n'
    '        })\n'
    '      );\n'
    '    }'
)
_CRBROWSER_PATCHED = (
    '    if (this._browser.options.name !== "clank" && this._options.acceptDownloads !== "internal-browser-default") {\n'
    '      // LOTUS patch: real Chrome (not Chromium-for-Testing) responds with\n'
    '      // "Browser context management is not supported." to the browser-level\n'
    '      // Browser.setDownloadBehavior CDP method, which fails connect_over_cdp.\n'
    '      // Catch and ignore — gemini_bot uses page.expect_download (page-level),\n'
    '      // which works regardless of browser-level config.\n'
    '      promises.push(\n'
    '        this._browser._session.send("Browser.setDownloadBehavior", {\n'
    '          behavior: this._options.acceptDownloads === "accept" ? "allowAndName" : "deny",\n'
    '          browserContextId: this._browserContextId,\n'
    '          downloadPath: this._browser.options.downloadsPath,\n'
    '          eventsEnabled: true\n'
    '        }).catch(() => { /* LOTUS patch — see comment above */ })\n'
    '      );\n'
    '    }'
)


def _version_tuple(v: str) -> tuple:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def ensure_playwright_version(min_version: str = MIN_PLAYWRIGHT_VERSION,
                              log: Callable[[str], None] = print) -> None:
    """Refuse to proceed if Playwright is older than min_version.

    Older versions had a setDownloadBehavior bug that broke connect_over_cdp
    even with the crBrowser.js patch applied.
    """
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
    try:
        actual = _pkg_version("playwright")
    except PackageNotFoundError as e:
        raise RuntimeError(
            f"Playwright not installed — pip install 'playwright>={min_version}'"
        ) from e
    if _version_tuple(actual) < _version_tuple(min_version):
        raise RuntimeError(
            f"Playwright {actual} is too old. LOTUS requires >= {min_version}. "
            f"Run: pip install -U 'playwright>={min_version}'"
        )
    log(f"[preflight] playwright {actual} OK (>= {min_version})")


def ensure_crBrowser_patch(log: Callable[[str], None] = print) -> None:
    """Re-apply the LOTUS Chrome-CDP patch to playwright's crBrowser.js if it
    has been wiped (e.g. by `pip install -U playwright`).

    Idempotent — checks for the marker comment first. If Playwright internals
    have moved (the unpatched template no longer matches), logs a warning
    and skips rather than corrupting the file.
    """
    try:
        import playwright
    except ImportError:
        log("[preflight] playwright not installed — skipping crBrowser patch")
        return
    target = Path(playwright.__file__).parent.joinpath(*_CRBROWSER_REL)
    if not target.exists():
        log(f"[preflight] crBrowser.js not found at {target} — skipping patch")
        return
    src = target.read_text(encoding="utf-8")
    if _CRBROWSER_MARKER in src:
        log("[preflight] crBrowser.js: LOTUS patch already applied")
        return
    if _CRBROWSER_UNPATCHED not in src:
        log("[preflight] crBrowser.js: unpatched template not matched — "
            "Playwright internals may have changed. Patch NOT applied; "
            "connect_over_cdp may fail.")
        return
    target.write_text(src.replace(_CRBROWSER_UNPATCHED, _CRBROWSER_PATCHED, 1),
                      encoding="utf-8")
    log("[preflight] crBrowser.js: LOTUS patch re-applied")


# ── Orchestrator ───────────────────────────────────────────────────────

def ensure_lotus_chrome(port: int = DEFAULT_CDP_PORT,
                        profile_dir: Path = DEFAULT_PROFILE_DIR,
                        log: Callable[[str], None] = print,
                        broadcast: Optional[Callable[[dict], None]] = None,
                        signin_timeout_s: int = SIGNIN_TIMEOUT_S) -> str:
    """
    Ensure a CDP-debuggable Chrome is running, signed into Gemini, and
    return the CDP URL (e.g. http://127.0.0.1:9222) for use by gemini_bot.

    `broadcast(payload)` is optional — called on each state transition so a
    dashboard can render a sign-in prompt overlay. States emitted:
      chrome_found / launching_chrome / chrome_ready /
      awaiting_signin / ready / failed
    """
    # Validate Playwright env before touching Chrome — fast-fail with a
    # clear message instead of cryptic CDP errors deeper in the stack.
    ensure_playwright_version(log=log)
    ensure_crBrowser_patch(log=log)

    cdp_url = f"http://127.0.0.1:{port}"

    def _emit(state: str, message: str) -> None:
        log(f"[preflight] {state}: {message}")
        if broadcast:
            try:
                broadcast({"event": "preflight",
                           "state": state, "message": message})
            except Exception:
                pass

    info = cdp_alive(port)
    if info:
        _emit("chrome_found",
              f"reusing Chrome on :{port} ({info.get('Browser', '?')})")
    else:
        _emit("launching_chrome",
              f"starting Chrome on :{port} with profile {profile_dir}")
        launch_chrome(profile_dir=profile_dir, port=port)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if cdp_alive(port):
                break
        else:
            _emit("failed",
                  f"Chrome did not expose CDP on :{port} within 30s")
            raise RuntimeError(
                f"Chrome failed to expose CDP on :{port} within 30s. "
                f"Check that the binary at {_find_chrome()!r} can run.")
        _emit("chrome_ready", f"CDP responding on :{port}")

    if gemini_signed_in(port):
        _emit("ready", "Gemini signed-in, LOTUS proceeding")
        return cdp_url

    _emit("awaiting_signin",
          "Sign in to Gemini in the Chrome window that just opened. "
          "LOTUS will continue automatically.")
    deadline = time.monotonic() + signin_timeout_s
    while time.monotonic() < deadline:
        time.sleep(2.0)
        if gemini_signed_in(port):
            _emit("ready", "Gemini sign-in detected, LOTUS proceeding")
            return cdp_url

    _emit("failed",
          f"Gemini sign-in not detected within {signin_timeout_s}s")
    raise RuntimeError(
        f"User did not complete Gemini sign-in within {signin_timeout_s}s. "
        f"Re-run LOTUS after signing in.")
