"""
Dashboard Server — WebSocket + HTTP for the LOTUS Agent browser UI.

Responsibilities:
  - Serve dashboard.html and any static assets at http://localhost:<http_port>/.
  - Serve generated media (images) under /projects/* — mapped to
    ~/LotusAgent/Projects so dashboard.html can render <img> tags without
    running into the file:// → http:// cross-origin block.
  - Broadcast agent events (user commands, tool calls, results, responses)
    to every connected WebSocket client.
  - Relay incoming commands typed in the dashboard back to the agent via a
    registered callback, so the dashboard SEND button is fully functional.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Callable, Optional

# Try websockets, graceful fallback
ws_module = None
try:
    import websockets
    ws_module = websockets
except ImportError:
    pass


PROJECTS_ROOT = os.path.expanduser("~/LotusAgent/Projects")
TOKEN_PATH = Path(os.path.expanduser("~/.lotus_auth/token.txt"))


def get_or_create_token() -> str:
    """Return the mother's shared auth token, generating it on first call.

    Stored at ~/.lotus_auth/token.txt (same directory as voiceprints, 0600
    perms). Clients must include this token in the first WebSocket message
    they send, or they get disconnected.
    """
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.exists():
        tok = TOKEN_PATH.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(tok)
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return tok


class DashboardServer:
    def __init__(self, ws_port: int = 8765, http_port: int = 8766,
                 bind: str = "0.0.0.0", require_token: bool = True):
        self.ws_port = ws_port
        self.http_port = http_port
        self.bind = bind                  # 0.0.0.0 = accept LAN connections
        self.require_token = require_token
        self.token = get_or_create_token() if require_token else None
        self.clients: set = set()         # authenticated only
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Snapshot of the current pipeline state, so a dashboard that
        # reconnects mid-pipeline picks up where we left off.
        self.last_pipeline_state: Optional[dict] = None
        # Snapshot of the most recent pipeline_list broadcast so dashboard
        # reconnects (Cmd+R) and fresh boots replay the multi-pipeline
        # registry. Without this, the All-Pipelines overview shows cards
        # via /api/pipelines but liveIds stays empty → cards aren't
        # clickable.
        self.last_pipeline_list: Optional[dict] = None
        # Callback the agent registers to provide a live queue snapshot
        # (cross-pipeline frames + pending approved prompts) for the
        # Queue tab. server.py is decoupled from LotusPhase1 so it can't
        # reach into _pipelines on its own — the agent injects this fn.
        self.queue_snapshot_fn = None
        # Unique-per-process server boot id. Dashboard compares it across
        # reconnects; if it changes, the mother has restarted and the
        # dashboard auto-reloads to pick up any HTML/UI changes.
        self.boot_id = secrets.token_hex(6)

        # Callback invoked when dashboard sends a user_command.
        # Signature: on_command(text: str) -> None
        self.on_command: Optional[Callable[[str], None]] = None
        # Callback for interactive-pipeline messages from dashboard.
        # Signature: on_pipeline(action: str, payload: dict) -> None
        self.on_pipeline: Optional[Callable[[str, dict], None]] = None
        # Callback for dashboard "direct_action" messages — deterministic
        # tool-invocation that bypasses Gemma's tool-routing so UI buttons
        # are never misrouted. Signature: on_direct_action(action, payload).
        self.on_direct_action: Optional[Callable[[str, dict], None]] = None
        # Callback for manual frame uploads — user drops a file for a single
        # failed frame via /api/frame-upload. Mother moves it into the
        # target Post{N}/Frame{M}.png slot and marks source=manual.
        # Signature: on_frame_upload(frame_id, src_path, new_name) -> (ok, msg)
        self.on_frame_upload: Optional[Callable[[str, str, Optional[str]],
                                                 tuple]] = None

    def start(self) -> None:
        """Start WebSocket + HTTP servers in background threads (non-blocking)."""
        if not ws_module:
            print("  ℹ️  Dashboard disabled (pip install websockets to enable)")
            return

        threading.Thread(target=self._run_ws, daemon=True).start()
        threading.Thread(target=self._run_http, daemon=True).start()

        self._running = True
        print(f"  📊 Dashboard: http://localhost:{self.http_port}/dashboard.html")
        print(f"  🔌 WebSocket: ws://localhost:{self.ws_port}")
        if self.require_token:
            print(f"  🔑 Auth token (give this to clients): {self.token}")
            print(f"     Stored at: {TOKEN_PATH}\n")
        else:
            print()

    # ── WebSocket ────────────────────────────────────────────────────────

    def _run_ws(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._ws_server())

    async def _ws_server(self) -> None:
        async def handler(websocket):
            # 1) Require auth as the VERY first message from the client.
            authed = not self.require_token
            if self.require_token:
                await websocket.send(json.dumps({
                    "type": "auth_required",
                    "message": "Send {type:'auth', token:'<token>'} as first message",
                }))
                try:
                    first = await asyncio.wait_for(websocket.recv(), timeout=10)
                    data = json.loads(first)
                    if data.get("type") == "auth" and data.get("token") == self.token:
                        authed = True
                        await websocket.send(json.dumps({
                            "type": "auth_ok",
                            "message": "Authenticated",
                        }))
                    else:
                        await websocket.send(json.dumps({
                            "type": "auth_failed",
                            "message": "Invalid token",
                        }))
                        await websocket.close()
                        return
                except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
                    try: await websocket.close()
                    except Exception: pass
                    return

            # 2) Authenticated — register and stream events
            self.clients.add(websocket)
            try:
                await websocket.send(json.dumps({
                    "type": "connected",
                    "message": "LOTUS Agent dashboard connected",
                    "boot_id": self.boot_id,
                }))
                # Replay the current pipeline state to the new client so
                # reconnects (Cmd+R) pick up mid-pipeline cleanly.
                if self.last_pipeline_state:
                    await websocket.send(json.dumps(self.last_pipeline_state))
                # Replay the multi-pipeline registry list too so the
                # All-Pipelines overview's liveIds populates immediately.
                if self.last_pipeline_list:
                    await websocket.send(json.dumps(self.last_pipeline_list))
                async for message in websocket:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    msg_type = data.get("type", "")
                    if msg_type == "user_command" and self.on_command:
                        text = data.get("text", "").strip()
                        if text:
                            threading.Thread(
                                target=self.on_command,
                                args=(text,),
                                daemon=True,
                            ).start()
                    elif msg_type.startswith("pipeline_") and self.on_pipeline:
                        # Strip 'pipeline_' prefix so callback gets 'approve_prompt' etc.
                        action = msg_type[len("pipeline_"):]
                        payload = {k: v for k, v in data.items() if k != "type"}
                        threading.Thread(
                            target=self.on_pipeline,
                            args=(action, payload),
                            daemon=True,
                        ).start()
                    elif msg_type == "direct_action" and self.on_direct_action:
                        action = (data.get("action") or "").strip()
                        payload = {k: v for k, v in data.items()
                                   if k not in ("type", "action")}
                        if action:
                            threading.Thread(
                                target=self.on_direct_action,
                                args=(action, payload),
                                daemon=True,
                            ).start()
            finally:
                self.clients.discard(websocket)

        async with ws_module.serve(handler, self.bind, self.ws_port):
            await asyncio.Future()  # run forever

    # ── HTTP (static files + /projects/ mount) ───────────────────────────

    def _run_http(self) -> None:
        dashboard_dir = os.path.dirname(os.path.abspath(__file__))
        # Capture the DashboardServer instance for nested Handler methods.
        # IMPORTANT: do NOT name this `server` — BaseHTTPRequestHandler
        # defines `self.server` on the request handler, so a bare `server`
        # reference inside a handler method resolves to the HTTPServer, not
        # the enclosing closure, and our callback lookup silently finds None.
        dashboard_self = self

        def _live_projects_root() -> str:
            """Re-read projects_root from lotus_config on every request so
            changing the root in the dashboard takes effect immediately,
            without a mother restart."""
            try:
                import lotus_config as _lc
                return os.path.expanduser(
                    _lc.load()["image"].get("projects_root", PROJECTS_ROOT)
                )
            except Exception:
                return PROJECTS_ROOT

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=dashboard_dir, **kwargs)

            def translate_path(self, path: str) -> str:
                if path.startswith("/projects/"):
                    rel = path[len("/projects/"):]
                    if "?" in rel:
                        rel = rel.split("?", 1)[0]
                    rel = os.path.normpath(rel).lstrip("/")
                    return os.path.join(_live_projects_root(), rel)
                # /recordings/<name>.wav → ~/.lotus_auth/recordings/<name>.wav
                if path.startswith("/recordings/"):
                    rel = path[len("/recordings/"):]
                    if "?" in rel: rel = rel.split("?", 1)[0]
                    rel = os.path.normpath(rel).lstrip("/")
                    return os.path.join(
                        os.path.expanduser("~/.lotus_auth/recordings"), rel)
                return super().translate_path(path)

            def _serve_with_ranges(self, fs_path: str, ctype: str) -> bool:
                """Serve a file with HTTP Range support so HTML5 <audio>
                elements can seek and enable the play button. Returns True
                if handled, False to fall through to the default handler."""
                if not os.path.isfile(fs_path):
                    return False
                size = os.path.getsize(fs_path)
                rng = self.headers.get("Range", "").strip()
                start, end = 0, size - 1
                if rng.startswith("bytes="):
                    try:
                        s, e = rng[6:].split("-", 1)
                        if s: start = int(s)
                        if e: end = int(e)
                        if start < 0 or end >= size or start > end:
                            self.send_response(416)  # Range Not Satisfiable
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.end_headers(); return True
                        self.send_response(206)
                        self.send_header("Content-Range",
                                         f"bytes {start}-{end}/{size}")
                    except Exception:
                        self.send_response(200)
                else:
                    self.send_response(200)
                length = end - start + 1
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if self.command == "HEAD":
                    return True
                with open(fs_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(64 * 1024, remaining))
                        if not chunk: break
                        try: self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError): break
                        remaining -= len(chunk)
                return True

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.end_headers()

            def do_POST(self):
                # Frame upload — user provides a manually-generated image for
                # a specific failed frame. Query: ?frame_id=fX&new_name=foo.png
                # Body: raw image bytes (PNG/JPEG). We write to a temp file
                # then hand the path to the mother's on_frame_upload callback
                # which moves it into the correct Post{N}/Frame{M}.png slot.
                if self.path.startswith("/api/frame-upload"):
                    try:
                        from urllib.parse import urlparse, parse_qs
                        qs = parse_qs(urlparse(self.path).query)
                        frame_id = (qs.get("frame_id", [""])[0]).strip()
                        new_name = (qs.get("new_name", [""])[0]).strip() or None
                        force    = (qs.get("force", ["0"])[0]) == "1"
                        if not frame_id:
                            self.send_response(400); self.end_headers()
                            self.wfile.write(b'{"error":"missing frame_id"}'); return
                        length = int(self.headers.get("Content-Length", "0"))
                        if length <= 0 or length > 50 * 1024 * 1024:
                            self.send_response(400); self.end_headers()
                            self.wfile.write(b'{"error":"bad length"}'); return
                        uploads_dir = os.path.expanduser("~/LotusAgent/uploads")
                        os.makedirs(uploads_dir, exist_ok=True)
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        tmp_path = os.path.join(
                            uploads_dir, f"frameupload_{ts}_{frame_id}.png")
                        remaining = length
                        with open(tmp_path, "wb") as f:
                            while remaining > 0:
                                chunk = self.rfile.read(min(65536, remaining))
                                if not chunk: break
                                f.write(chunk)
                                remaining -= len(chunk)
                        cb = getattr(dashboard_self, "on_frame_upload", None)
                        if not cb:
                            self.send_response(500); self.end_headers()
                            self.wfile.write(b'{"error":"no upload handler registered"}')
                            try: os.unlink(tmp_path)
                            except Exception: pass
                            return
                        ok, data = cb(frame_id, tmp_path, new_name, force)
                        # Decide status code:
                        #   - 200 on success
                        #   - 409 Conflict when target file exists and caller
                        #     didn't force/rename (UI shows the modal)
                        #   - 400 on any other validation error
                        if ok:
                            status = 200
                        elif isinstance(data, dict) and data.get("collision"):
                            status = 409
                        else:
                            status = 400
                        payload = {"ok": ok, **(data if isinstance(data, dict) else {"message": str(data)})}
                        body = json.dumps(payload).encode()
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        # Clean up temp file if the mother didn't move it.
                        try:
                            if os.path.exists(tmp_path):
                                os.unlink(tmp_path)
                        except Exception: pass
                        return
                    except Exception as e:
                        body = json.dumps({"error": str(e)}).encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                if self.path.startswith("/api/config/pipeline"):
                    # Live switch of the image-gen pipeline (playwright |
                    # human | api). Persists to ~/.lotus_config.json so the
                    # next agent restart picks it up; takes effect on the
                    # next _gen_image call (no restart needed). The user
                    # asked for this as a safety lever — flip away from a
                    # failing engine without killing the agent.
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        raw = self.rfile.read(length) if length > 0 else b"{}"
                        payload = json.loads(raw or b"{}")
                        choice = (payload.get("pipeline") or "").strip().lower()
                        valid = {"playwright", "human", "api"}
                        if choice not in valid:
                            body = json.dumps({
                                "error": f"pipeline must be one of {sorted(valid)}",
                            }).encode()
                            self.send_response(400)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            return
                        import lotus_config as _lc
                        cfg = _lc.load()
                        cfg.setdefault("image", {})["pipeline"] = choice
                        _lc.save(cfg)
                        try:
                            dashboard_self.broadcast({
                                "type": "config_updated",
                                "config": {"pipeline": choice},
                                "message": f"Image pipeline switched to {choice!r}",
                            })
                        except Exception:
                            pass
                        body = json.dumps({"ok": True, "pipeline": choice}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    except Exception as e:
                        body = json.dumps({"error": str(e)}).encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                if self.path.startswith("/api/upload"):
                    try:
                        from urllib.parse import urlparse, parse_qs
                        qs = parse_qs(urlparse(self.path).query)
                        name = (qs.get("name", ["upload.bin"])[0]).replace("/", "_").replace("..", "_")
                        length = int(self.headers.get("Content-Length", "0"))
                        if length <= 0 or length > 50 * 1024 * 1024:
                            self.send_response(400); self.end_headers()
                            self.wfile.write(b'{"error":"bad length"}'); return
                        uploads_dir = os.path.expanduser("~/LotusAgent/uploads")
                        os.makedirs(uploads_dir, exist_ok=True)
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        fname = f"{ts}_{name}"
                        dest = os.path.join(uploads_dir, fname)
                        remaining = length
                        with open(dest, "wb") as f:
                            while remaining > 0:
                                chunk = self.rfile.read(min(65536, remaining))
                                if not chunk: break
                                f.write(chunk)
                                remaining -= len(chunk)
                        # Auto-classify so the dashboard can warn when the
                        # user uploaded the wrong file. Heuristic is instant
                        # for the known schemas; Gemma is only called when
                        # the heuristic is silent. Either way, this is
                        # advisory — the user can override.
                        classification = {"verdict": "uncertain",
                                          "confidence": 0.0,
                                          "reason": "classifier disabled",
                                          "source": "fallback"}
                        try:
                            import lotus_file_classifier as _cls
                            classification = _cls.classify_file(dest)
                        except Exception as e:
                            classification["reason"] = f"classifier error ({type(e).__name__})"
                        body = json.dumps({
                            "path": dest, "name": fname, "size": length,
                            "classification": classification,
                        }).encode()
                        # Tell every connected dashboard the file landed.
                        # The agent will broadcast `parsing_started` when
                        # parsing kicks off, but the user also wants
                        # immediate confirmation that the upload itself
                        # succeeded. Best-effort — never block on this.
                        try:
                            dashboard_self.broadcast({
                                "type": "upload_received",
                                "name": fname, "path": dest, "size": length,
                                "classification": classification,
                                "message": f"Uploaded {fname} ({length} bytes)",
                            })
                        except Exception:
                            pass
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    except Exception as e:
                        body = json.dumps({"error": str(e)}).encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                self.send_response(404); self.end_headers()

            def do_GET(self):
                # Redirect / and /dashboard.html to the polished React build
                # (lotus-ui) so clients always land on the newest UI.
                # Compare the path portion only — query strings like ?token=
                # must not defeat the redirect.
                path_only = self.path.split("?", 1)[0]
                if path_only in ("/", "/dashboard", "/dashboard.html"):
                    query = self.path[len(path_only):]
                    self.send_response(302)
                    self.send_header("Location", "/lotus-ui/dist/index.html" + query)
                    self.end_headers()
                    return
                if self.path.startswith("/api/config"):
                    try:
                        import lotus_config as _lc
                        cfg = _lc.load()["image"]
                        parent = _lc.parent_folder_for()
                        body = json.dumps({
                            "brand": cfg.get("brand", "Techengine"),
                            "date_format": cfg.get("parent_format", "%m%d%Y"),
                            "parent_folder": os.path.basename(parent),
                            "parent_path": parent,
                            "projects_root": os.path.expanduser(cfg.get("projects_root", "~/LotusAgent/Projects")),
                            "multi_frame_sections": list(_lc.MULTI_FRAME_SECTIONS),
                            "single_item_slots": list(_lc.SINGLE_ITEM_SLOTS),
                            "frame_prefix": cfg.get("frame_prefix", "Frame"),
                            "frame_ext": cfg.get("frame_ext", ".png"),
                            "active_section": cfg.get("active_section", "Post1"),
                            "pipeline": cfg.get("pipeline", "playwright"),
                        }).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    except Exception as e:
                        body = json.dumps({"error": str(e)}).encode()
                        self.send_response(500); self.send_header("Content-Type","application/json")
                        self.send_header("Content-Length", str(len(body))); self.end_headers()
                        self.wfile.write(body); return
                # JSON endpoint: today's parent + all sub-sections (Post1, Reel, ...)
                # for gallery pre-population. Returns every .png/.jpg under the
                # active parent folder, recursively.
                # History DB — lists every pipeline run + its frames so the
                # Gallery tab can group the output by pipeline name and time.
                # Settings — key/value store persisted in lotus_history.db.
                if self.path.startswith("/api/settings"):
                    try:
                        import lotus_history as _h
                        body = json.dumps({"settings": _h.all_settings()}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(body); return
                    except Exception as e:
                        body = json.dumps({"error": str(e)}).encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body); return
                # Audio-recorder status + file listing for the REC panel.
                if self.path.startswith("/api/recordings"):
                    try:
                        import lotus_recorder as _r
                        body = json.dumps({
                            "status":     _r.status(),
                            "recordings": _r.list_recordings(limit=100),
                        }).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(body); return
                    except Exception as e:
                        body = json.dumps({"error": str(e)}).encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body); return
                if self.path.startswith("/api/history"):
                    try:
                        import lotus_history as _h
                        projects_dir_now = _live_projects_root()
                        path = self.path
                        # /api/history/runs/<id> → frames for that run
                        if "/runs/" in path:
                            try:
                                run_id = int(path.rsplit("/", 1)[-1].split("?")[0])
                                frames = _h.frames_for_run(run_id)
                            except Exception:
                                frames = []
                            enriched = []
                            for f in frames:
                                fpath = f.get("path", "")
                                url = ""
                                if fpath and fpath.startswith(projects_dir_now):
                                    url = "/projects/" + os.path.relpath(
                                        fpath, projects_dir_now).replace(os.sep, "/")
                                f["url"] = url
                                enriched.append(f)
                            body = json.dumps({"frames": enriched}).encode()
                        else:
                            runs = _h.list_runs(limit=100)
                            body = json.dumps({"runs": runs,
                                               "stats": _h.stats()}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    except Exception as e:
                        body = json.dumps({"error": str(e)}).encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body); return
                if self.path.startswith("/api/pipelines"):
                    try:
                        import lotus_pipelines_db as _pdb
                        import lotus_prompts_db as _prdb
                        rows = _pdb.list_all()
                        # Enrich with prompt review-gate counts so the A2
                        # overview can show approved/pending/rejected at a
                        # glance without a second round-trip per card.
                        for r in rows:
                            sh = r.get("source_hash")
                            r["prompt_counts"] = (
                                _prdb.count_by_status(sh) if sh
                                else {"pending": 0, "approved": 0, "rejected": 0}
                            )
                            r["source_basename"] = (
                                os.path.basename(r["source_md"])
                                if r.get("source_md") else ""
                            )
                        body = json.dumps({"pipelines": rows}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(body); return
                    except Exception as e:
                        body = json.dumps({"error": str(e)}).encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body); return
                if self.path.startswith("/api/queue"):
                    try:
                        fn = getattr(self.server.lotus, "queue_snapshot_fn", None)
                        snap = fn() if callable(fn) else {"pipelines": []}
                        body = json.dumps(snap, default=str).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(body); return
                    except Exception as e:
                        body = json.dumps({"error": str(e)}).encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body); return
                if self.path.startswith("/api/today"):
                    try:
                        import lotus_config as _lc
                        parent = _lc.parent_folder_for()
                    except Exception:
                        parent = os.path.join(_live_projects_root(), datetime.now().strftime("%m%d%y"))
                    projects_dir_now = _live_projects_root()
                    parent_rel = os.path.relpath(parent, projects_dir_now)
                    imgs = []
                    if os.path.isdir(parent):
                        for root, _, files in os.walk(parent):
                            for f in sorted(files):
                                low = f.lower()
                                if low.endswith((".png", ".jpg", ".jpeg", ".webp")):
                                    full = os.path.join(root, f)
                                    rel = os.path.relpath(full, projects_dir_now)
                                    imgs.append({
                                        "name": f,
                                        "url": "/projects/" + rel.replace(os.sep, "/"),
                                        "size": os.path.getsize(full),
                                        "section": os.path.relpath(
                                            os.path.dirname(full), parent
                                        ) or "(root)",
                                    })
                    body = json.dumps({"folder": parent_rel, "images": imgs}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                # WAV playback — serve with proper HTTP Range handling so
                # HTML5 <audio> can seek + enables the play button.
                if self.path.startswith("/recordings/") and \
                        self.path.lower().endswith((".wav", ".mp3", ".m4a", ".ogg")):
                    fs_path = self.translate_path(self.path)
                    ctype = "audio/wav" if fs_path.lower().endswith(".wav") \
                        else "audio/mpeg" if fs_path.lower().endswith(".mp3") \
                        else "audio/mp4" if fs_path.lower().endswith(".m4a") \
                        else "audio/ogg"
                    if self._serve_with_ranges(fs_path, ctype):
                        return
                # PNGs etc. need no-cache so a regenerated image (same path) reloads
                if any(self.path.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp")):
                    # Let parent serve, but inject no-cache header by wrapping
                    pass
                return super().do_GET()

            def end_headers(self):
                if self.path.startswith("/projects/"):
                    self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                super().end_headers()

            def log_message(self, format, *args):
                pass  # suppress access log

        server = HTTPServer((self.bind, self.http_port), Handler)
        # Make the DashboardServer reachable from inside Handler via
        # `self.server.lotus` — used by /api/queue to call the live
        # snapshot callback the agent registered on us.
        server.lotus = self  # type: ignore[attr-defined]
        server.serve_forever()

    # ── Broadcast ────────────────────────────────────────────────────────

    def broadcast(self, data: dict) -> None:
        """Send `data` to every connected dashboard client."""
        # Snapshot pipeline state for reconnect replay
        if data.get("type") == "pipeline_state":
            self.last_pipeline_state = data
        elif data.get("type") == "pipeline_cancel":
            self.last_pipeline_state = None
        elif data.get("type") == "pipeline_list":
            self.last_pipeline_list = data

        if not self._running or not self.clients or not self._loop:
            return

        message = json.dumps(data)

        async def _send_all():
            dead = set()
            for client in self.clients:
                try:
                    await client.send(message)
                except Exception:
                    dead.add(client)
            self.clients -= dead

        # Schedule on the WS event loop from whatever thread called us.
        try:
            asyncio.run_coroutine_threadsafe(_send_all(), self._loop)
        except Exception:
            pass
