#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────
# LOTUS child installer — macOS
#
# Double-click from Finder, or run from Terminal:
#   ./install-child.command
#
# Bootstraps ONLY what's needed to run a LOTUS child on this Mac:
#   1. Verifies Python 3.10+  (points to installer if missing)
#   2. Verifies Tesseract       (for ReviewAgent's OCR; optional)
#   3. Runs setup-child.py       (the cross-platform wizard)
#
# This script does NOT auto-install Python/Tesseract — that requires
# admin rights and we don't silently grab sudo. It detects what's
# missing and prints the exact install command; you approve + run it.
# ────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

BOLD=$(tput bold 2>/dev/null || echo "")
DIM=$(tput dim   2>/dev/null || echo "")
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
ok()    { echo "${GREEN}✓${RESET} $1"; }
warn()  { echo "${YELLOW}⚠${RESET} $1"; }
die()   { echo "${RED}✗ $1${RESET}" >&2; exit 1; }

banner "LOTUS Agent Child — macOS installer"
echo "This machine: $(hostname)  ($(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null))"
echo "Repo folder:  $(pwd)"

# ── 1. Python 3.10+ ─────────────────────────────────────────────────
echo ""
if command -v python3 >/dev/null 2>&1; then
  PY_VER=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
  PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
  PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')
  if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
    ok "Python $PY_VER ($(which python3))"
  else
    die "Python is too old ($PY_VER). Need 3.10+.
    Install newer: ${BOLD}brew install python@3.12${RESET}
    or download from https://www.python.org/downloads/macos/"
  fi
else
  die "Python 3 not found.
    Install via Homebrew:  ${BOLD}brew install python@3.12${RESET}
    Or download:           https://www.python.org/downloads/macos/
    Then re-run this installer."
fi

# ── 2. Tesseract (optional — only for ReviewAgent's OCR) ───────────
echo ""
if command -v tesseract >/dev/null 2>&1; then
  ok "Tesseract $(tesseract --version 2>&1 | head -n1 | awk '{print $2}') — ReviewAgent OCR ready"
else
  warn "Tesseract not found — ReviewAgent's OCR pass will fail."
  echo "  Install: ${BOLD}brew install tesseract${RESET}"
  echo "  (Skip if this child won't host ReviewAgent.)"
  read -p "  Continue without Tesseract? [y/N]: " ANS
  case "$ANS" in y|Y|yes|YES) : ;; *) die "Aborted — install Tesseract then re-run.";; esac
fi

# ── 3. Chrome (optional — only for RendererAgent) ──────────────────
echo ""
if [ -d "/Applications/Google Chrome.app" ]; then
  ok "Google Chrome detected — RendererAgent can attach via CDP"
else
  warn "Google Chrome not found. Required ONLY if hosting RendererAgent."
  echo "  Download: https://www.google.com/chrome/"
fi

# ── 4. Run the wizard ───────────────────────────────────────────────
banner "Running setup-child.py"
exec python3 setup-child.py
