#!/usr/bin/env bash
# ====================================================================
#   Build the LOTUS Agent Child .pkg installer on macOS.
#
#   Prerequisites (one-time):
#     1. Python 3.10+  → brew install python@3.12
#     2. PyInstaller   → pip install pyinstaller
#     3. Xcode cmdline tools  → xcode-select --install   (for pkgbuild)
#
#   Run from the REPO ROOT:
#     bash packaging/mac/build.sh
#
#   Output:
#     dist/lotus-child-1.0.0.pkg
# ====================================================================

set -e
cd "$(dirname "$0")/../.."

BOLD=$(tput bold 2>/dev/null || echo "")
GREEN=$(tput setaf 2 2>/dev/null || echo "")
RED=$(tput setaf 1 2>/dev/null || echo "")
RESET=$(tput sgr0 2>/dev/null || echo "")

say()  { echo "${BOLD}═══ $1 ═══${RESET}"; }
ok()   { echo "${GREEN}✓${RESET} $1"; }
die()  { echo "${RED}✗ $1${RESET}" >&2; exit 1; }

say "Verifying toolchain"
command -v python3  >/dev/null || die "python3 not on PATH"
command -v pkgbuild >/dev/null || die "pkgbuild not found — install Xcode cmdline tools"
command -v productbuild >/dev/null || die "productbuild not found — install Xcode cmdline tools"
command -v pyinstaller >/dev/null || {
  echo "  installing pyinstaller via pip…"
  python3 -m pip install --user pyinstaller
}
ok "python3, pkgbuild, productbuild, pyinstaller"

say "Installing Python build deps"
python3 -m pip install --quiet --upgrade \
  websockets requests pillow pytesseract mistune zeroconf
ok "deps"

say "PyInstaller bundle"
rm -rf build dist/lotus-child
pyinstaller packaging/lotus-child.spec --clean --noconfirm
[ -x dist/lotus-child/lotus-child ] || die "dist/lotus-child/lotus-child missing"
ok "bundle at dist/lotus-child/"

# ── Stage the install payload: /Applications/LOTUS Child/ ────────────
STAGE=build/pkg-root
rm -rf "$STAGE"
mkdir -p "$STAGE/Applications/LOTUS Child"
cp -R dist/lotus-child/* "$STAGE/Applications/LOTUS Child/"

# Drop two .command launchers so the user can double-click from Finder.
cat > "$STAGE/Applications/LOTUS Child/Start LOTUS Child.command" << 'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
./lotus-child
EOF
cat > "$STAGE/Applications/LOTUS Child/Configure LOTUS Child.command" << 'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
./lotus-child --reconfigure
EOF
chmod +x "$STAGE/Applications/LOTUS Child/Start LOTUS Child.command"
chmod +x "$STAGE/Applications/LOTUS Child/Configure LOTUS Child.command"

# ── Component pkg (just the payload) ────────────────────────────────
say "pkgbuild: component pkg"
mkdir -p build
pkgbuild \
  --root "$STAGE" \
  --identifier "com.lotusalgebra.lotus-child" \
  --version "1.0.0" \
  --install-location "/" \
  build/lotus-child-component.pkg
ok "component pkg"

# ── Product pkg (wraps component + wizard UI) ───────────────────────
say "productbuild: wizard installer"
mkdir -p dist
productbuild \
  --distribution packaging/mac/Distribution.xml \
  --resources packaging/mac \
  --package-path build \
  dist/lotus-child-1.0.0.pkg
ok "dist/lotus-child-1.0.0.pkg"

echo ""
say "SUCCESS"
echo "  .pkg: $(pwd)/dist/lotus-child-1.0.0.pkg"
echo "  Double-click it to install, or sign it with productsign for distribution:"
echo "    productsign --sign \"Developer ID Installer: Your Name\" \\"
echo "                dist/lotus-child-1.0.0.pkg dist/lotus-child-1.0.0-signed.pkg"
