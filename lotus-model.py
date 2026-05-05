#!/usr/bin/env python
"""
LOTUS model management CLI.

Operations on the Ollama model backing LOTUS's local-brain agents
(ReviewAgent, ParserAgent, ArchiverAgent, controller heartbeat):

    lotus-model list          Show every Ollama model installed locally
                              and mark which one LOTUS currently uses.
    lotus-model current       Print the active LOTUS model name.
    lotus-model suggest       Print the Gemma variants you can pull.
    lotus-model pull <name>   Download a model (uses `ollama pull`).
    lotus-model switch <name> Tell LOTUS to use <name> (edits
                              ~/.lotus_config.json → ollama.model).
                              Prints the command to restart the mother.
    lotus-model remove <name> Delete a model from Ollama (frees disk).
                              Refuses to remove LOTUS's active model.
    lotus-model upgrade       Same as `pull` but re-pulls the ACTIVE
                              model — useful when Ollama publishes a new
                              build under the same tag.

All commands talk to Ollama at `http://localhost:11434` unless the env
var OLLAMA_URL overrides it. Writes to config are local-only (no
network), so `switch` is safe even when the mother is down.

Usage:
    cd ~/LotusAgent
    venv/bin/python lotus-model.py <command> [args]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Curated list of Gemma variants worth showing in `suggest` — these
# change with each Gemma release. Edit the list as new builds ship.
GEMMA_CATALOG = [
    ("gemma4:e4b",  "~5 GB",  "Default. Fast, 8B params, fits on M1/M2 base hardware."),
    ("gemma4:12b",  "~14 GB", "Mid. Better reasoning, needs ~16 GB free RAM."),
    ("gemma4:31b",  "~36 GB", "Large. Best quality, needs ~40 GB free RAM + decent GPU."),
    ("gemma4:e12b", "~14 GB", "Efficient 12B. Similar quality to gemma4:12b, ~20% faster."),
]


# ── config helpers ────────────────────────────────────────────────────

def _load_lotus_config() -> dict:
    """Safe-import lotus_config from the repo root."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lotus_config as _lc
    return _lc.load()


def _save_lotus_config(cfg: dict) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lotus_config as _lc
    _lc.save(cfg)


def _current_model() -> str:
    try:
        return _load_lotus_config().get("ollama", {}).get("model", "gemma4:e4b")
    except Exception:
        return "gemma4:e4b"


# ── Ollama helpers ────────────────────────────────────────────────────

def _ollama_tags() -> list:
    """Return list of installed models via Ollama HTTP API."""
    url = f"{OLLAMA_URL.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"✗ Ollama not reachable at {OLLAMA_URL} ({e})", file=sys.stderr)
        print(f"  Is it running? Try: brew services start ollama  (macOS)", file=sys.stderr)
        sys.exit(2)
    return data.get("models", []) or []


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"


# ── commands ──────────────────────────────────────────────────────────

def cmd_list(_args) -> int:
    models = _ollama_tags()
    current = _current_model()
    if not models:
        print("No Ollama models installed. Try: lotus-model pull gemma4:e4b")
        return 0
    hdr = f"{'CUR':<4} {'NAME':<28} {'SIZE':>10}  {'MODIFIED':<24} DIGEST"
    print(hdr)
    print("─" * len(hdr))
    for m in sorted(models, key=lambda x: x.get("name", "")):
        name = m.get("name", "?")
        size = _fmt_size(m.get("size", 0))
        mod  = (m.get("modified_at", "") or "")[:24]
        dig  = (m.get("digest", "") or "")[:12]
        mark = " *  " if name == current else "    "
        print(f"{mark} {name:<28} {size:>10}  {mod:<24} {dig}")
    print()
    print(f"(* = active LOTUS model from ~/.lotus_config.json)")
    return 0


def cmd_current(_args) -> int:
    print(_current_model())
    return 0


def cmd_suggest(_args) -> int:
    installed = {m["name"] for m in _ollama_tags()}
    current = _current_model()
    print("Gemma variants you can pull:\n")
    print(f"  {'NAME':<16} {'SIZE':<8} {'STATUS':<18} NOTES")
    print("  " + "─" * 70)
    for name, size, notes in GEMMA_CATALOG:
        if name == current:
            status = "installed ★ active"
        elif name in installed:
            status = "installed"
        else:
            status = "available"
        print(f"  {name:<16} {size:<8} {status:<18} {notes}")
    print()
    print("  Pull with:  lotus-model pull <name>")
    print("  Switch to:  lotus-model switch <name>  (after pulling)")
    return 0


def cmd_pull(args) -> int:
    if not shutil.which("ollama"):
        print("✗ `ollama` command not on PATH. Install from https://ollama.com", file=sys.stderr)
        return 2
    name = args.name
    print(f"Pulling {name} via Ollama…")
    # Stream output so the user sees the download progress live.
    rc = subprocess.call(["ollama", "pull", name])
    if rc != 0:
        print(f"✗ pull failed (exit {rc})", file=sys.stderr)
        return rc
    print(f"✓ {name} available locally.")
    print(f"  To make LOTUS use it now:  lotus-model switch {name}")
    return 0


def cmd_switch(args) -> int:
    name = args.name
    installed = {m["name"] for m in _ollama_tags()}
    if name not in installed:
        print(f"✗ {name} is not installed. Pull it first:")
        print(f"    lotus-model pull {name}")
        return 2
    cfg = _load_lotus_config()
    cfg.setdefault("ollama", {})["model"] = name
    _save_lotus_config(cfg)
    print(f"✓ Wrote ollama.model={name} to ~/.lotus_config.json")
    print()
    print("Restart the mother to pick up the new model:")
    print("  pkill -f agent_phase1.py")
    print("  cd ~/LotusAgent")
    print("  LOTUS_CDP_URL=http://localhost:9222 venv/bin/python -u agent_phase1.py v")
    print()
    print("Children are UNAFFECTED — they already call the mother's Ollama,")
    print("so they'll automatically use the new model too.")
    return 0


def cmd_remove(args) -> int:
    if not shutil.which("ollama"):
        print("✗ `ollama` not on PATH", file=sys.stderr); return 2
    name = args.name
    current = _current_model()
    if name == current and not args.force:
        print(f"✗ {name} is LOTUS's currently active model — refusing to remove.")
        print(f"  Switch to another model first:  lotus-model switch <other>")
        print(f"  Or force with:                   lotus-model remove {name} --force")
        return 2
    print(f"Removing {name} from Ollama…")
    rc = subprocess.call(["ollama", "rm", name])
    if rc != 0:
        print(f"✗ remove failed (exit {rc})", file=sys.stderr); return rc
    print(f"✓ {name} removed.")
    return 0


def cmd_upgrade(_args) -> int:
    current = _current_model()
    print(f"Re-pulling the active model {current} to grab the latest build…")
    if not shutil.which("ollama"):
        print("✗ `ollama` not on PATH", file=sys.stderr); return 2
    rc = subprocess.call(["ollama", "pull", current])
    if rc != 0:
        print(f"✗ pull failed (exit {rc})", file=sys.stderr); return rc
    print(f"✓ {current} is up to date.")
    print("  Restart the mother to reload weights:")
    print("    pkill -f agent_phase1.py  && <your mother launch cmd>")
    return 0


# ── main ──────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        prog="lotus-model",
        description="Manage the Ollama model LOTUS uses for its local brain.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list",    help="List all installed Ollama models").set_defaults(fn=cmd_list)
    sub.add_parser("current", help="Print LOTUS's active model").set_defaults(fn=cmd_current)
    sub.add_parser("suggest", help="Show curated Gemma variants + status").set_defaults(fn=cmd_suggest)

    pull_p = sub.add_parser("pull", help="Download a model via `ollama pull`")
    pull_p.add_argument("name", help="Model name, e.g. gemma4:12b")
    pull_p.set_defaults(fn=cmd_pull)

    switch_p = sub.add_parser("switch", help="Point LOTUS at an already-installed model")
    switch_p.add_argument("name", help="Model name, e.g. gemma4:31b")
    switch_p.set_defaults(fn=cmd_switch)

    rm_p = sub.add_parser("remove", help="Delete an installed model")
    rm_p.add_argument("name", help="Model name")
    rm_p.add_argument("--force", action="store_true",
                      help="Remove even if it's the active LOTUS model")
    rm_p.set_defaults(fn=cmd_remove)

    sub.add_parser("upgrade", help="Re-pull the active model for the latest build")\
        .set_defaults(fn=cmd_upgrade)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
