"""
lotus_mcp.py - LOTUS's client for external MCP servers (Instagram, YouTube).

LOTUS launches each server as a stdio subprocess and calls its tools from
ordinary synchronous code:

    import lotus_mcp
    with lotus_mcp.session("instagram") as ig:
        media = ig.call("ig_get_media_list", {"limit": 5})
        for m in media["data"]:
            ig.call("ig_get_comments", {"media_id": m["id"]})

    lotus_mcp.call("youtube", "youtube_analytics_overview")   # one-shot

Servers (pinned; change with `mcp.servers.<name>` in ~/.lotus_config.json):

    instagram  npx @mikusnuz/meta-mcp            official Graph API, MIT
    youtube    uvx pauling-ai/youtube-mcp-server Data + Analytics APIs, MIT

Credentials never live in the config file or the repo:

    ~/.lotus_auth/mcp/instagram.env   INSTAGRAM_ACCESS_TOKEN=...
                                      INSTAGRAM_USER_ID=...
    ~/.lotus_auth/mcp/youtube/client_secret.json   Google "Desktop app"
                                      OAuth client; token.json is written
                                      next to it after the first browser
                                      consent (sign in as the channel owner).

Nothing here posts on its own. Write tools (replies, publishing) are only
called by lotus_social after the user approves a draft in the dashboard.

    python lotus_mcp.py status                 # what is configured
    python lotus_mcp.py tools instagram        # list a server's tools
    python lotus_mcp.py call youtube youtube_get_channel '{"mine": true}'
"""

import asyncio
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Optional

AUTH_DIR = Path.home() / ".lotus_auth"
MCP_DIR = AUTH_DIR / "mcp"

# Pinned versions: a server update can rename tools or change their output,
# and both packages run code with the user's social-account credentials.
DEFAULT_SERVERS = {
    "instagram": {
        "command": "npx",
        "args": ["-y", "@mikusnuz/meta-mcp@2.2.0"],
        "env_file": "instagram.env",
        "required_env": ["INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"],
        "required_files": [],
    },
    "youtube": {
        "command": "uvx",
        "args": ["--from",
                 "git+https://github.com/pauling-ai/youtube-mcp-server"
                 "@ec13ee51d4be48ad84cced79fee7d78f9b2f68f8",
                 "youtube-studio-mcp"],
        "env": {"YOUTUBE_MCP_CONFIG_DIR": str(MCP_DIR / "youtube")},
        "env_file": "youtube.env",
        "required_env": [],
        "required_files": [str(MCP_DIR / "youtube" / "client_secret.json")],
    },
}


class MCPError(RuntimeError):
    """A server could not start, or a tool call failed."""


class MCPNotConfigured(MCPError):
    """Credentials for this server are missing - see status()."""


# ── config ────────────────────────────────────────────────────────────────

def servers() -> dict:
    """DEFAULT_SERVERS merged with `mcp.servers` from ~/.lotus_config.json."""
    try:
        import lotus_config as _lc
        saved = (_lc.load().get("mcp", {}) or {}).get("servers", {}) or {}
    except Exception:
        saved = {}
    out = {k: dict(v) for k, v in DEFAULT_SERVERS.items()}
    for name, over in saved.items():
        out[name] = {**out.get(name, {}), **over}
    return out


def _read_env_file(path: Path) -> dict:
    env = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def _spec(name: str) -> dict:
    spec = servers().get(name)
    if not spec:
        raise MCPError(f"unknown MCP server {name!r} (known: {', '.join(servers())})")
    if spec.get("enabled") is False:
        raise MCPNotConfigured(f"MCP server {name!r} is disabled in config")
    return spec


def _env_for(spec: dict) -> dict:
    env = dict(spec.get("env") or {})
    if spec.get("env_file"):
        env.update(_read_env_file(MCP_DIR / spec["env_file"]))
    return env


def missing(name: str) -> list:
    """What still has to be set up before `name` can run (empty = ready)."""
    spec = servers().get(name) or {}
    env = {**os.environ, **_env_for(spec)}
    out = [f"{k} in {MCP_DIR / spec.get('env_file', '')}"
           for k in spec.get("required_env", []) if not env.get(k)]
    out += [f"file {p}" for p in spec.get("required_files", [])
            if not Path(p).expanduser().exists()]
    if spec.get("command") and not shutil.which(spec["command"]):
        out.append(f"command {spec['command']!r} on PATH")
    return out


def configured(name: str) -> bool:
    return name in servers() and servers()[name].get("enabled") is not False \
        and not missing(name)


def status() -> dict:
    return {name: {"enabled": spec.get("enabled") is not False,
                   "ready": configured(name),
                   "missing": missing(name),
                   "command": " ".join([spec.get("command", "")] + spec.get("args", []))}
            for name, spec in servers().items()}


# ── results ───────────────────────────────────────────────────────────────

def _decode(result) -> Any:
    """CallToolResult -> plain Python (structured content, else parsed text)."""
    texts = [getattr(b, "text", "") for b in (result.content or [])
             if getattr(b, "type", "") == "text"]
    if result.is_error:
        raise MCPError(" ".join(texts).strip() or "tool returned an error")
    if result.structured_content is not None:
        sc = result.structured_content
        # Python servers wrap non-dict returns as {"result": ...}
        return sc["result"] if isinstance(sc, dict) and list(sc) == ["result"] else sc
    joined = "\n".join(texts).strip()
    try:
        return json.loads(joined)
    except (ValueError, TypeError):
        return joined


# ── session: one live server process, driven from sync code ───────────────

class Session:
    """A running MCP server. The anyio-based client has to be entered and
    exited by one task, so a private event loop thread owns it and sync
    callers hand it requests through a queue."""

    def __init__(self, name: str, *, timeout: float = 120,
                 server: Any = None):
        self.name = name
        self.timeout = timeout
        self._server = server          # tests: an in-process MCPServer
        self._loop = asyncio.new_event_loop()
        self._queue: Optional[asyncio.Queue] = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._tools: Optional[list] = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"mcp-{name}")

    def _params(self):
        from mcp.client.stdio import StdioServerParameters
        spec = _spec(self.name)
        gaps = missing(self.name)
        if gaps:
            raise MCPNotConfigured(f"{self.name} MCP not set up - missing: "
                                   + "; ".join(gaps))
        for p in spec.get("required_files", []):
            Path(p).expanduser().parent.mkdir(parents=True, exist_ok=True)
        return StdioServerParameters(command=spec["command"],
                                     args=list(spec.get("args", [])),
                                     env=_env_for(spec))

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        from mcp import Client
        self._queue = asyncio.Queue()
        try:
            target = self._server if self._server is not None else self._params()
            log = open(AUTH_DIR / f"mcp_{self.name}.log", "a") \
                if AUTH_DIR.exists() else None
            try:
                if log is not None and self._server is None:
                    # Keep server stderr (npm/uv chatter, auth prompts) out of
                    # the voice console.
                    from mcp.client.stdio import stdio_client
                    target = stdio_client(target, errlog=log)
                client = Client(target, read_timeout_seconds=self.timeout)
                async with client:
                    self._client = client
                    self._ready.set()
                    while True:
                        item = await self._queue.get()
                        if item is None:
                            break
                        coro_fn, fut = item
                        try:
                            res = await coro_fn(client)
                            self._loop.call_soon_threadsafe(_resolve, fut, res, None)
                        except BaseException as e:   # noqa: BLE001 - relayed
                            self._loop.call_soon_threadsafe(_resolve, fut, None, e)
            finally:
                if log is not None:
                    log.close()
        except BaseException as e:   # noqa: BLE001 - surfaced to start()
            self._error = e
            self._ready.set()

    def start(self) -> "Session":
        self._thread.start()
        if not self._ready.wait(self.timeout):
            raise MCPError(f"{self.name} MCP server did not start within "
                           f"{self.timeout:.0f}s (see ~/.lotus_auth/mcp_{self.name}.log)")
        if self._error is not None:
            if isinstance(self._error, MCPError):
                raise self._error
            raise MCPError(f"{self.name} MCP server failed to start: "
                           f"{self._error}") from self._error
        return self

    def _submit(self, coro_fn, timeout: Optional[float] = None):
        import concurrent.futures
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (coro_fn, fut))
        try:
            return fut.result(timeout or self.timeout)
        except concurrent.futures.TimeoutError:
            raise MCPError(f"{self.name}: call timed out") from None

    def tools(self) -> list:
        """[{name, description, input_schema}] - cached per session."""
        if self._tools is None:
            async def _list(c):
                out, cursor = [], None
                while True:
                    page = await c.list_tools(cursor=cursor)
                    out += page.tools
                    cursor = page.next_cursor
                    if not cursor:
                        return out
            self._tools = [{"name": t.name, "description": t.description or "",
                            "input_schema": t.input_schema}
                           for t in self._submit(_list)]
        return self._tools

    def call(self, tool: str, arguments: Optional[dict] = None,
             timeout: Optional[float] = None) -> Any:
        """Call one tool; returns decoded JSON (or text). Raises MCPError."""
        async def _call(c):
            return await c.call_tool(tool, arguments or {})
        try:
            result = self._submit(_call, timeout)
        except MCPError:
            raise
        except Exception as e:
            raise MCPError(f"{self.name}.{tool} failed: {e}") from e
        try:
            return _decode(result)
        except MCPError as e:
            raise MCPError(f"{self.name}.{tool}: {e}") from None

    def close(self):
        if self._thread.is_alive() and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            self._thread.join(15)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


def _resolve(fut, result, error):
    if fut.done():
        return
    if error is not None:
        fut.set_exception(error)
    else:
        fut.set_result(result)


def session(name: str, *, timeout: float = 120) -> Session:
    """`with lotus_mcp.session("instagram") as ig: ig.call(...)`"""
    return Session(name, timeout=timeout)


def call(name: str, tool: str, arguments: Optional[dict] = None,
         timeout: float = 120) -> Any:
    """One-shot: start the server, call one tool, stop it."""
    with session(name, timeout=timeout) as s:
        return s.call(tool, arguments)


# ── CLI ───────────────────────────────────────────────────────────────────

def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        for name, st in status().items():
            mark = "ready" if st["ready"] else ("disabled" if not st["enabled"] else "NOT SET UP")
            print(f"{name:10s} {mark}")
            print(f"           {st['command']}")
            for m in st["missing"]:
                print(f"           missing: {m}")
        return 0
    if cmd == "tools" and len(argv) > 2:
        with session(argv[2]) as s:
            for t in s.tools():
                print(f"{t['name']:40s} {t['description'].splitlines()[0][:90] if t['description'] else ''}")
        return 0
    if cmd == "call" and len(argv) > 3:
        args = json.loads(argv[4]) if len(argv) > 4 else {}
        print(json.dumps(call(argv[2], argv[3], args), indent=2, default=str))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
