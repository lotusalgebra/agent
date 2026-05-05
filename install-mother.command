#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────
# LOTUS MOTHER installer — macOS
#
# Double-click from Finder, or run from Terminal:
#   ./install-mother.command
#
# Use this when you're setting up the FULL mother on a Mac (usually the
# bigger machine with good GPU + RAM). Children use install-child.command.
#
# This verifies prereqs then runs setup-mother.py which does the heavy
# lifting (venv, pip deps, Playwright, Ollama, Gemma pull, launcher).
# ────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

BOLD=$(tput bold 2>/dev/null || echo "")
RED=$(tput setaf 1 2>/dev/null || echo "")
GREEN=$(tput setaf 2 2>/dev/null || echo "")
YELLOW=$(tput setaf 3 2>/dev/null || echo "")
CYAN=$(tput setaf 6 2>/dev/null || echo "")
RESET=$(tput sgr0 2>/dev/null || echo "")

banner() {
  echo ""
  echo "${BOLD}${CYAN}═══════════════════════════════════════════════════${RESET}"
  echo "${BOLD}${CYAN}  $1${RESET}"
  echo "${BOLD}${CYAN}═══════════════════════════════════════════════════${RESET}"
}
ok()   { echo "${GREEN}✓${RESET} $1"; }
warn() { echo "${YELLOW}⚠${RESET} $1"; }
die()  { echo "${RED}✗ $1${RESET}" >&2; exit 1; }

banner "LOTUS Agent Mother — macOS installer"
echo "This machine: $(hostname)  ($(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null))"
echo "Repo folder:  $(pwd)"

# ── Python 3.10+ ───────────────────────────────────────────────────
echo ""
if command -v python3 >/dev/null 2>&1; then
  PY_VER=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
  PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
  PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')
  if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
    ok "Python $PY_VER"
  else
    die "Python $PY_VER is too old. Install newer: ${BOLD}brew install python@3.12${RESET}"
  fi
else
  die "Python 3 not found. Install: ${BOLD}brew install python@3.12${RESET}"
fi

# ── Homebrew (needed for portaudio → pyaudio) ──────────────────────
echo ""
if command -v brew >/dev/null 2>&1; then
  ok "Homebrew present"
else
  warn "Homebrew not found — needed to install portaudio + tesseract + ollama."
  echo "  Install: ${BOLD}/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"${RESET}"
  read -p "  Continue anyway? [y/N]: " ANS
  case "$ANS" in y|Y|yes|YES) : ;; *) die "Install Homebrew then re-run.";; esac
fi

# ── Ollama ─────────────────────────────────────────────────────────
echo ""
if command -v ollama >/dev/null 2>&1; then
  ok "Ollama detected"
else
  warn "Ollama not installed."
  echo "  Install: ${BOLD}brew install ollama && brew services start ollama${RESET}"
  echo "  (The wizard will pull the Gemma model once Ollama is running.)"
  read -p "  Continue and set it up manually later? [y/N]: " ANS
  case "$ANS" in y|Y|yes|YES) : ;; *) die "Install Ollama then re-run.";; esac
fi

# ── Tesseract ──────────────────────────────────────────────────────
echo ""
if command -v tesseract >/dev/null 2>&1; then
  ok "Tesseract $(tesseract --version 2>&1 | head -n1 | awk '{print $2}')"
else
  warn "Tesseract not installed — ReviewAgent OCR will fail without it."
  echo "  Install: ${BOLD}brew install tesseract${RESET}"
fi

# ── portaudio (for pyaudio) ────────────────────────────────────────
echo ""
if brew list portaudio >/dev/null 2>&1; then
  ok "portaudio (brew)"
else
  warn "portaudio missing — pyaudio pip install will fail."
  echo "  Install: ${BOLD}brew install portaudio${RESET}"
fi

# ── Chrome ─────────────────────────────────────────────────────────
echo ""
if [ -d "/Applications/Google Chrome.app" ]; then
  ok "Google Chrome detected"
else
  warn "Google Chrome not found — RendererAgent needs it."
  echo "  Download: https://www.google.com/chrome/"
fi

banner "Running setup-mother.py"
exec python3 setup-mother.py
