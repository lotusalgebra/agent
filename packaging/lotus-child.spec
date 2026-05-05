# PyInstaller spec — bundles `lotus-child.py` into a standalone binary
# that includes Python + all required Python packages. End user doesn't
# need Python installed.
#
# Scope: hosts Parser / Review / Archive agents. RendererAgent is NOT
# bundled here because Playwright's Chromium (~280 MB) would bloat the
# installer and 99% of child deployments don't need it. If you want to
# add Renderer support later, extend this spec with:
#     hiddenimports += ['playwright', ...]
#     datas += collect_data_files('playwright')
# and run `playwright install chromium` as a post-install step.
#
# Build commands (run from the repo root):
#   Windows:  pyinstaller packaging\lotus-child.spec --clean --noconfirm
#   macOS:    pyinstaller packaging/lotus-child.spec --clean --noconfirm
#
# Output goes to dist/lotus-child/  (folder with executable + support files).

# ruff: noqa: F821  # PyInstaller provides `Analysis`, `PYZ`, `EXE`, `COLLECT` at runtime.

from PyInstaller.utils.hooks import collect_submodules
import os

block_cipher = None
repo_root = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# Python source files that lotus-child.py imports (directly or transitively)
# and that PyInstaller's static analysis might miss because agents.py pulls
# them via `import` inside class methods.
hiddenimports = [
    "agents",
    "lotus_mdns",
    "prompts_parser",
    "lotus_config",
    "lotus_history",
]
# Subpackage shakeouts for libs that do late-binding.
hiddenimports += collect_submodules("websockets")
hiddenimports += collect_submodules("zeroconf")

a = Analysis(
    [os.path.join(repo_root, "lotus-child.py")],
    pathex=[repo_root],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the installer small — these are heavy and optional.
        "playwright", "playwright_stealth",
        "google", "google.genai",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "matplotlib", "numpy", "scipy", "pandas",
        "torch", "tensorflow",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lotus-child",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX is flaky on modern macOS and over-aggressive on Windows
    console=True,        # child opens a console window to show its log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows metadata — shown in Task Manager + installer details.
    version_info=None,   # add a VS_VERSIONINFO dict later for Windows
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="lotus-child",
)
