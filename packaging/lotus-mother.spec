# PyInstaller spec — bundles `agent_phase1.py` (the LOTUS mother) + the
# full agent stack (voice, voiceprint auth, dashboard server, agent mesh,
# browser bots, content pipelines) into a standalone executable.
#
# End user does NOT need Python or pip, but DOES need:
#   - Ollama installed locally with `gemma4:e4b` pulled  (http://localhost:11434)
#   - Google Chrome installed  (CDP-driven gemini_bot / grok_video / upwork)
#
# Build commands (run from the repo root with venv active):
#   pyinstaller packaging/lotus-mother.spec --clean --noconfirm
#
# Output: dist/lotus-mother/   (folder; executable + dylibs + datas)
#
# Phase 1 keeps `console=True` so the smoke-test prints to stdout. Phase 2
# (Electron-wrapping) flips it to False so the bundled mother runs headless
# under the Electron main process.

# ruff: noqa: F821  # PyInstaller injects Analysis/PYZ/EXE/COLLECT at runtime.

from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    copy_metadata,
)
import os

block_cipher = None
repo_root = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# ── Hidden imports ─────────────────────────────────────────────────────
# Modules referenced via late-binding string imports, `from server import …`
# inside class methods, or third-party plugin discovery the static analyzer
# misses.

hiddenimports = [
    # First-party modules pulled by agent_phase1.py + transitive imports.
    # Listed explicitly so PyInstaller picks them up even when the import
    # happens inside a method body or `__init__`.
    "agents", "auth", "blender", "browser", "enroll",
    "gemini", "gemini_api", "gemini_bot",
    "grok_bot", "grok_video",
    "lotus_config", "lotus_file_classifier", "lotus_history",
    "lotus_mdns", "lotus_pipelines_db", "lotus_preflight",
    "lotus_prompts_db", "lotus_recorder",
    "photoshop", "prompts_parser",
    "router", "server", "tools",
    "upwork_agent", "upwork_db",
    "voice",
]

# Subpackage shakeouts for libraries with plugin / late-binding patterns.
hiddenimports += collect_submodules("websockets")
hiddenimports += collect_submodules("zeroconf")
hiddenimports += collect_submodules("speech_recognition")
hiddenimports += collect_submodules("edge_tts")
hiddenimports += collect_submodules("resemblyzer")
hiddenimports += collect_submodules("librosa")
hiddenimports += collect_submodules("playwright")
hiddenimports += collect_submodules("playwright_stealth")
hiddenimports += collect_submodules("mistune")

# ── Data files ─────────────────────────────────────────────────────────
# Non-Python assets the mother loads at runtime.

datas = []
datas += collect_data_files("resemblyzer")        # pretrained.pt voiceprint model
datas += collect_data_files("librosa")            # example_data, assets
datas += collect_data_files("playwright")         # driver scripts
datas += collect_data_files("edge_tts")           # voices.json
datas += collect_data_files("speech_recognition") # bundled flac binary
datas += collect_data_files("google.genai")       # schema definitions

# Package metadata (dist-info/METADATA) — required when the code calls
# `importlib.metadata.version("<pkg>")`. lotus_preflight.py does this for
# playwright to enforce ≥ 1.59.
datas += copy_metadata("playwright")

# Repo assets the mother references by relative path.
datas += [
    (os.path.join(repo_root, "dashboard.html"), "."),
    (os.path.join(repo_root, "blender_jobs.py.tmpl"), "."),
    (os.path.join(repo_root, "photoshop_jobs.jsx"), "."),
    (os.path.join(repo_root, "lotus-ui", "dist"), "lotus-ui/dist"),
]

# ── Analysis / build pipeline ──────────────────────────────────────────

a = Analysis(
    [os.path.join(repo_root, "agent_phase1.py")],
    pathex=[repo_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Light defaults: offline STT dropped — Google STT is the fallback
        # path inside `recognize_whisper` try/except in voice.py.
        "whisper", "tiktoken",
        # Heavy GUI toolkits — never used.
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        # Heavy ML frameworks beyond torch — never used.
        "tensorflow", "matplotlib", "pandas",
        # Build/test tooling that shouldn't ship.
        "pytest", "PyInstaller",
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
    name="lotus-mother",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,   # Phase 1: stdout visible. Flip to False in Phase 2.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="lotus-mother",
)
