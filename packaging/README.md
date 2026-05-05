# LOTUS Agent Child — Installer Builds

This folder produces **native installers** for the LOTUS child worker so end users can install it with one double-click — no Python / pip / git required on the target machine.

| Platform | Tool | Output artifact |
|---|---|---|
| Windows 64-bit | WiX Toolset 3.x + PyInstaller | `dist/lotus-child-1.0.0.msi` |
| macOS (11.0+) | pkgbuild + productbuild + PyInstaller | `dist/lotus-child-1.0.0.pkg` |

Both installers present a standard OS-native wizard: **Welcome → License → Install path → Progress → Finish**, with a Start Menu folder (Windows) / Applications folder entry (macOS) and a built-in uninstaller.

---

## ⚠️ Cross-compilation is NOT supported

You must run each platform's build on an actual machine of that platform:

- **Windows MSI** → build on a Windows 10/11 PC with WiX Toolset 3.x installed.
- **macOS .pkg** → build on macOS 11+ with Xcode command-line tools installed.

PyInstaller bundles the Python interpreter specific to the OS it's running on, so cross-building would produce broken binaries. Same for MSI / PKG — each uses OS-specific tooling.

---

## Building on Windows

**One-time setup on the build machine:**
1. Install **Python 3.10+** from <https://www.python.org/downloads/windows/>. Check *"Add python.exe to PATH"* at install time.
2. Install **WiX Toolset 3.11+** from <https://github.com/wixtoolset/wix3/releases> (grab `wix311.exe`). Add its `bin\` folder to PATH if the installer didn't.
3. Verify:
   ```
   python --version
   candle -?
   ```

**Each build:**
```cmd
cd LotusAgent
packaging\windows\build.bat
```

The script:
1. Validates the toolchain is on PATH.
2. Installs Python build deps (`pyinstaller`, `websockets`, `pillow`, `pytesseract`, `mistune`, `zeroconf`).
3. Runs PyInstaller via `packaging\lotus-child.spec` → produces `dist\lotus-child\` (standalone folder with `lotus-child.exe` + Python runtime).
4. Runs `heat.exe` to harvest the folder into a WiX components fragment.
5. Runs `candle` → `light` to compile + link the MSI.

**Output:** `dist\lotus-child-1.0.0.msi`

**Signing (optional, but recommended for distribution):**
```cmd
signtool sign /tr http://timestamp.digicert.com /td SHA256 /fd SHA256 ^
    /a dist\lotus-child-1.0.0.msi
```
You need a code-signing certificate. Without one, SmartScreen may warn users.

---

## Building on macOS

**One-time setup on the build Mac:**
1. Install Xcode command-line tools: `xcode-select --install` (gives you `pkgbuild` + `productbuild`).
2. Install Python 3.10+: `brew install python@3.12`.
3. Install PyInstaller: `pip install pyinstaller`.

**Each build:**
```bash
cd LotusAgent
bash packaging/mac/build.sh
```

The script:
1. Verifies toolchain (`python3`, `pkgbuild`, `productbuild`, `pyinstaller`).
2. Installs Python build deps via pip.
3. Runs PyInstaller → `dist/lotus-child/` (standalone folder with the `lotus-child` binary + Python runtime).
4. Stages payload into `build/pkg-root/Applications/LOTUS Child/` with two user-friendly `.command` launchers (*Start LOTUS Child* + *Configure LOTUS Child*).
5. `pkgbuild` → component pkg.
6. `productbuild` with `Distribution.xml` → wizard-wrapped installer.

**Output:** `dist/lotus-child-1.0.0.pkg`

**Signing (optional):**
```bash
productsign --sign "Developer ID Installer: Your Company (ABCDE12345)" \
    dist/lotus-child-1.0.0.pkg dist/lotus-child-1.0.0-signed.pkg
```
You need an Apple Developer ID Installer certificate. Without one, Gatekeeper may block installation (user can right-click → Open → Open to bypass).

---

## What gets installed

| | Windows | macOS |
|---|---|---|
| Install location | `C:\Program Files\LOTUS Agent\Child\` | `/Applications/LOTUS Child/` |
| Shortcuts | Start Menu → LOTUS Agent → {Start, Configure, Uninstall} LOTUS Child | Double-click `Start LOTUS Child.command` / `Configure LOTUS Child.command` |
| Uninstall | Settings → Apps → LOTUS Agent Child → Uninstall | Drag `/Applications/LOTUS Child/` to Trash (or run a custom uninstaller script) |

Neither installer touches the user's `~/.lotus_auth/` or `~/.lotus_child_config.json` — uninstalling leaves user config in place so a reinstall picks up where you left off.

---

## File manifest

```
packaging/
├── lotus-child.spec          # PyInstaller spec (cross-platform source of truth)
├── README.md                 # this file
├── windows/
│   ├── build.bat             # one-shot Windows build
│   ├── lotus-child.wxs       # WiX source for the MSI
│   └── LICENSE.rtf           # license shown in installer wizard
└── mac/
    ├── build.sh              # one-shot macOS build
    ├── Distribution.xml      # productbuild wizard definition
    ├── welcome.html          # first wizard page
    ├── license.txt           # license shown in installer wizard
    └── conclusion.html       # post-install page
```

## Branding the Windows installer (optional)

The WixUI_InstallDir flow accepts two custom bitmaps for visual branding:

| File | Size | Where it appears |
|---|---|---|
| `packaging/windows/banner.bmp` | 493 × 58 px | Top of each wizard page |
| `packaging/windows/dialog.bmp` | 493 × 312 px | Background of Welcome + Finish pages |

Drop both in place, then uncomment the two `<WixVariable>` lines inside the `<Product>` block in `lotus-child.wxs`.

## Versioning

Bump the version number in **three** places for a release:

1. `packaging/windows/lotus-child.wxs` — `<Product Version="1.0.0.0">`
2. `packaging/mac/Distribution.xml` — `<pkg-ref version="1.0.0">`
3. Build scripts — `lotus-child-1.0.0.msi` / `lotus-child-1.0.0.pkg` filenames (both have the version hard-coded in their last step; search for `1.0.0`).

Scripted version bump isn't in scope yet — three search-and-replaces before a release.
