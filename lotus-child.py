#!/usr/bin/env python
"""
LOTUS child process — hosts a subset of the agent mesh as a standalone
worker. The mother connects (mDNS discovery in Phase D3; manually configured
via `LOTUS_CHILDREN` env var for now) and delegates work via WebSocket.

Protocol (all frames are JSON):
  client → server  {type:"auth", token:"<shared>"}
  server → client  {type:"auth_ok"}  or  {type:"auth_failed", reason}
  client → server  {type:"submit", agent:"<name>", task:{...}}
  server → client  {type:"submitted", work_id:"<id>"}  or  {type:"submitted", error:"..."}
  client → server  {type:"status",  agent:"<name>", work_id:"<id>"}
  server → client  {type:"status_result", work_id, state, result, error, elapsed}
  client → server  {type:"health"}
  server → client  {type:"health_snapshot", agents:[...]}
  client → server  {type:"hello"}  (keepalive, server echoes)
  server → client  {type:"hello_ack", child_id:"...", hostname:"..."}

Usage:
  python lotus-child.py --port 8770 --agents ReviewAgent
  python lotus-child.py --port 8771 --agents ParserAgent,ReviewAgent --bind 0.0.0.0

The child inherits the mother's shared auth token from ~/.lotus_auth/token.txt,
so only callers that know the token can submit work. For a LAN child running on
a different machine, copy that token file over (or pass --token).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import sys
import uuid
from pathlib import Path

# Ensure we can import agents.py from the repo root regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))


DEFAULT_TOKEN_PATH = Path(os.path.expanduser("~/.lotus_auth/token.txt"))
DEFAULT_CONFIG_PATH = Path(os.path.expanduser("~/.lotus_child_config.json"))


def _build_agent_factories(ollama_url: str) -> dict:
    """Build factories bound to a specific Ollama URL. We create them
    AFTER setting the OLLAMA_URL env var so prompts_parser picks it up
    during the import chain triggered by ParserAgent.run()."""
    import agents as _agents_mod  # lazy import — env var must be set first
    return {
        "ParserAgent":   lambda: _agents_mod.ParserAgent(),
        "RendererAgent": lambda: _agents_mod.RendererAgent(),
        "ReviewAgent":   lambda: _agents_mod.ReviewAgent(ollama_url=ollama_url),
        "ArchiverAgent": lambda: _agents_mod.ArchiverAgent(ollama_url=ollama_url),
    }


async def _handler(ws, registry, token, child_id: str):
    """One WebSocket connection lifetime — keeps state minimal (auth flag
    + forwards every subsequent message to the registry)."""
    authed = False
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")

            # Auth gate — all other messages require it.
            if t == "auth":
                if msg.get("token") == token:
                    authed = True
                    await ws.send(json.dumps({"type": "auth_ok"}))
                    print(f"[child] ✓ client authed from {ws.remote_address}", flush=True)
                else:
                    await ws.send(json.dumps({"type": "auth_failed",
                                               "reason": "bad token"}))
                    return
                continue
            if not authed:
                await ws.send(json.dumps({"type": "auth_failed",
                                           "reason": "send auth first"}))
                continue

            if t == "hello":
                await ws.send(json.dumps({
                    "type":     "hello_ack",
                    "child_id": child_id,
                    "hostname": socket.gethostname(),
                    "agents":   [a["name"] for a in registry.health_snapshot()],
                }))
            elif t == "submit":
                name = msg.get("agent") or ""
                task = msg.get("task") or {}
                agent = registry.get(name)
                if not agent:
                    await ws.send(json.dumps({
                        "type": "submitted",
                        "error": f"no such agent on this child: {name}",
                    }))
                    continue
                try:
                    wid = agent.submit(task)
                    await ws.send(json.dumps({
                        "type":    "submitted",
                        "agent":   name,
                        "work_id": wid,
                    }))
                    print(f"[child] submit {name} → {wid}", flush=True)
                except Exception as e:
                    await ws.send(json.dumps({
                        "type":  "submitted",
                        "error": f"{type(e).__name__}: {e}",
                    }))
            elif t == "status":
                name = msg.get("agent") or ""
                wid = msg.get("work_id") or ""
                agent = registry.get(name)
                if not agent or not wid:
                    await ws.send(json.dumps({
                        "type": "status_result", "work_id": wid,
                        "state": "unknown",
                    }))
                    continue
                s = agent.status(wid)
                await ws.send(json.dumps({
                    "type": "status_result",
                    "work_id": wid,
                    **{k: v for k, v in s.items() if k != "work_id"},
                }))
            elif t == "health":
                snap = registry.health_snapshot()
                await ws.send(json.dumps({
                    "type":   "health_snapshot",
                    "agents": snap,
                    "child_id": child_id,
                }))
    except Exception as e:
        # websockets.ConnectionClosed and friends — normal lifecycle, quiet.
        from websockets.exceptions import ConnectionClosed
        if not isinstance(e, ConnectionClosed):
            print(f"[child] connection error: {type(e).__name__}: {e}", flush=True)


def _load_token(cli_token: str) -> str:
    if cli_token:
        return cli_token.strip()
    if DEFAULT_TOKEN_PATH.exists():
        return DEFAULT_TOKEN_PATH.read_text().strip()
    print(f"[child] no token: pass --token or place one at {DEFAULT_TOKEN_PATH}")
    sys.exit(2)


def _reconfigure() -> int:
    """Interactive re-prompt for the child's connection settings, saved
    to ~/.lotus_child_config.json. Launched from the installer's
    "Configure LOTUS Child" shortcut — keeps users out of the CLI flag
    minefield. Does NOT touch venv / deps / launchers (those are the
    installer's job; this is one-click-per-reconfig for the user)."""
    print("=" * 64)
    print(" LOTUS Child — reconfigure")
    print("=" * 64)
    existing = {}
    try:
        if DEFAULT_CONFIG_PATH.exists():
            existing = json.loads(DEFAULT_CONFIG_PATH.read_text())
            print(f" Loaded existing config from {DEFAULT_CONFIG_PATH}")
    except Exception as e:
        print(f" (couldn't read existing config: {e})")

    def _ask(prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        try:
            ans = input(f"  {prompt}{suffix}: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\naborted"); sys.exit(130)
        return ans or default

    print()
    mother_host = _ask("Mother's hostname or LAN IP (Ollama host)",
                       existing.get("mother_host", "192.168.1.1"))
    ollama_default = existing.get("ollama_url") or f"http://{mother_host}:11434"
    ollama_url = _ask("Ollama URL on mother", ollama_default)

    # Token: echo an obfuscated default but accept paste cleanly.
    if existing.get("token"):
        tok_hint = existing["token"][:4] + "…" + existing["token"][-3:]
        token = _ask(f"Auth token (keep existing: {tok_hint})", existing["token"])
    else:
        token = _ask("Auth token from mother's ~/.lotus_auth/token.txt", "")
    if not token:
        print("✗ token required; aborting"); return 2

    port = _ask("Listen port", str(existing.get("port", 8770)))
    agents = _ask("Agents to host (comma-separated)",
                  existing.get("agents", "ReviewAgent,ParserAgent,ArchiverAgent"))
    cdp_url = _ask("CDP URL (leave blank unless hosting RendererAgent)",
                   existing.get("cdp_url", ""))

    try:
        port_int = int(port)
    except ValueError:
        print(f"✗ port must be an integer, got {port!r}"); return 2

    config = {
        "mother_host": mother_host,
        "ollama_url":  ollama_url,
        "token":       token,
        "port":        port_int,
        "bind":        "0.0.0.0",
        "agents":      agents,
        "cdp_url":     cdp_url,
    }

    # Stash the shared token at the canonical location so other tools
    # (the mother when it connects back, future reruns of the wizard)
    # don't have to re-prompt.
    token_dir = DEFAULT_TOKEN_PATH.parent
    try:
        token_dir.mkdir(parents=True, exist_ok=True)
        DEFAULT_TOKEN_PATH.write_text(token)
        try: os.chmod(DEFAULT_TOKEN_PATH, 0o600)
        except OSError: pass
    except Exception as e:
        print(f"  ⚠ couldn't write token file ({e}) — continuing anyway")

    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_CONFIG_PATH.write_text(json.dumps(config, indent=2))
    try: os.chmod(DEFAULT_CONFIG_PATH, 0o600)
    except OSError: pass
    print()
    print(f"  ✓ wrote {DEFAULT_CONFIG_PATH}")
    print(f"  ✓ wrote {DEFAULT_TOKEN_PATH}")

    # Preflight — warn, don't fail, so users can still save settings when
    # the mother isn't booted yet.
    print()
    try:
        import urllib.request as _ur
        with _ur.urlopen(f"{ollama_url.rstrip('/')}/api/tags", timeout=5) as r:
            body = r.read().decode()
        print(f"  ✓ Ollama reachable at {ollama_url}")
        try:
            import json as _json
            models = [m["name"] for m in _json.loads(body).get("models", [])]
            print(f"      models: {models[:6]}")
        except Exception: pass
    except Exception as e:
        print(f"  ⚠ Ollama not reachable ({e})")
        print(f"      If the mother isn't running yet, that's fine — this will")
        print(f"      pass when you start it. Otherwise check OLLAMA_HOST=0.0.0.0")
        print(f"      on the mother.")

    print()
    print("=" * 64)
    print(" Reconfigure complete.")
    print(" Start the child now — either from the Start-menu / Applications")
    print(" shortcut, or by re-running this binary without --reconfigure.")
    print("=" * 64)

    # Give the user a beat to read the output when launched from a
    # double-click shortcut (the window would otherwise close instantly).
    if sys.platform.startswith("win"):
        try: input("\nPress Enter to close… ")
        except (KeyboardInterrupt, EOFError): pass
    return 0


def _load_config(path: Path) -> dict:
    """Load persisted config from disk. Keys are the same as CLI flags —
    CLI values always win when both are provided."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _merge(cli_val, cfg_val, default):
    """Resolution order: CLI flag → config file → default."""
    if cli_val not in (None, ""):
        return cli_val
    if cfg_val not in (None, ""):
        return cfg_val
    return default


async def _run(args) -> None:
    # Config file first (supplies defaults); CLI flags override.
    config = _load_config(Path(args.config)) if args.config else {}

    token       = _load_token(_merge(args.token, config.get("token"), ""))
    bind        = _merge(args.bind, config.get("bind"), "0.0.0.0")
    port        = int(_merge(args.port, config.get("port"), 8770))
    ollama_url  = _merge(args.ollama_url, config.get("ollama_url"),
                         "http://localhost:11434")
    cdp_url     = _merge(args.cdp_url, config.get("cdp_url"), "")
    agents_csv  = _merge(args.agents, config.get("agents"), "ReviewAgent")
    mother_host = _merge(args.mother_host, config.get("mother_host"), "")

    # Retarget the shared Ollama BEFORE importing agents so prompts_parser
    # picks up the URL at import time (its module-level OLLAMA_URL reads env).
    os.environ["OLLAMA_URL"] = ollama_url
    # CDP URL for the RendererAgent — only relevant if Renderer is hosted.
    if cdp_url:
        os.environ["LOTUS_CDP_URL"] = cdp_url

    # Now import agents (triggers prompts_parser import → env is set).
    import agents as _agents_mod
    factories = _build_agent_factories(ollama_url)

    registry = _agents_mod.AgentRegistry()
    names = [a.strip() for a in (agents_csv or "").split(",") if a.strip()]
    if not names:
        print("[child] --agents / config must list at least one agent name"); sys.exit(2)
    for name in names:
        factory = factories.get(name)
        if not factory:
            print(f"[child] unknown agent: {name} "
                  f"(known: {list(factories.keys())})"); sys.exit(2)
        registry.register(factory())

    child_id = uuid.uuid4().hex[:10]
    host = socket.gethostname()

    # ── Startup banner ──────────────────────────────────────────────
    print("=" * 64, flush=True)
    print(" LOTUS Agent Child", flush=True)
    print("=" * 64, flush=True)
    print(f"  id:           {child_id}", flush=True)
    print(f"  host:         {host}  ({platform.system()} {platform.release()})", flush=True)
    print(f"  listen:       ws://{bind}:{port}", flush=True)
    print(f"  token:        {'set (' + str(len(token)) + ' chars)' if token else 'MISSING'}", flush=True)
    print(f"  mother:       {mother_host or '(not set — mother connects to us, not vice versa)'}", flush=True)
    print(f"  ollama_url:   {ollama_url}", flush=True)
    if cdp_url:
        print(f"  LOTUS_CDP_URL: {cdp_url}", flush=True)
    print(f"  agents:       {[a['name'] for a in registry.health_snapshot()]}", flush=True)
    print("=" * 64, flush=True)

    # Light Ollama preflight — if the user mis-typed the URL, fail fast
    # rather than on the first review call.
    try:
        import requests as _req
        r = _req.get(f"{ollama_url}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name","") for m in r.json().get("models",[])]
        print(f"[child] ✓ Ollama reachable at {ollama_url}  models: {models[:5]}",
              flush=True)
    except Exception as e:
        print(f"[child] ⚠ Ollama preflight FAILED: {e}", flush=True)
        print(f"[child]   review/parse/archive calls will fail until this is fixed.",
              flush=True)

    # mDNS announcement so mothers on the same LAN auto-discover this child.
    # Best-effort — if zeroconf isn't installed we just skip and expect
    # the mother to use the explicit LOTUS_CHILDREN env var fallback.
    announcer = None
    try:
        import lotus_mdns
        announcer = lotus_mdns.ChildAnnouncer(
            port=port,
            agents=[a["name"] for a in registry.health_snapshot()],
            token=token,
            child_id=child_id,
        )
        print(f"[mdns] announced as {announcer.instance}", flush=True)
        print(f"[mdns]   advertised IP: {announcer.ip}", flush=True)
    except Exception as e:
        print(f"[mdns] announce failed ({type(e).__name__}: {e}) — child still reachable via "
              f"LOTUS_CHILDREN env var on the mother", flush=True)

    import websockets  # deferred so import errors show up cleanly
    try:
        async with websockets.serve(
            lambda ws: _handler(ws, registry, token, child_id),
            bind, port, max_size=None,
        ):
            print(f"[child] ready — waiting for connections", flush=True)
            await asyncio.Future()   # run forever
    finally:
        if announcer:
            announcer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="LOTUS agent child process")
    parser.add_argument("--reconfigure", action="store_true",
                        help="Launch the interactive configuration wizard "
                             "(re-prompt for host, token, agents) and exit.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help=f"JSON config file (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--port", type=int, default=None,
                        help="WebSocket listen port (default 8770)")
    parser.add_argument("--bind", default=None,
                        help="Bind address (default 0.0.0.0 to accept LAN)")
    parser.add_argument("--token", default=None,
                        help="Shared auth token. Defaults to ~/.lotus_auth/token.txt.")
    parser.add_argument("--agents", default=None,
                        help="Comma-separated agent names. Known: "
                             "ParserAgent, RendererAgent, ReviewAgent, ArchiverAgent")
    parser.add_argument("--ollama-url", default=None,
                        help="Ollama endpoint for ReviewAgent/ParserAgent/ArchiverAgent. "
                             "Default: http://localhost:11434. For a child pointing at "
                             "the mother's Ollama: http://<mother-ip>:11434")
    parser.add_argument("--cdp-url", default=None,
                        help="CDP URL for RendererAgent's Chrome. "
                             "Set only if hosting RendererAgent on this child.")
    parser.add_argument("--mother-host", default=None,
                        help="Informational: the mother's hostname/IP (shown in banner)")
    args = parser.parse_args()
    # --reconfigure short-circuits before touching the network — lets the
    # user fix a bad config file without the child failing to boot first.
    if args.reconfigure:
        sys.exit(_reconfigure())
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[child] shutdown")


if __name__ == "__main__":
    main()
