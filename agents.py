"""
LOTUS Agent Mesh — child agents that the mother orchestrates.

Design goals (Somendra, 2026-04-22):
  - LOTUS (Gemma on the mother) stays the brain; agents are specialists.
  - Every agent implements the SAME small protocol so the mother can treat
    them interchangeably: submit(task) → work_id, status(work_id), health().
  - Agents run each submitted task on a daemon thread. The agent's own
    internal work can still be sync-blocking (e.g. a Playwright call); the
    thread wrapper isolates that from the mother's event loop.
  - Failure is a normal signal: agents raise, the wrapper tags the work as
    failed, health() exposes success/fail counts. The mother decides retry
    vs escalate (up to 3 retries per Somendra's policy).

Concrete agents in Phase 1:
  - ParserAgent   — MD file → validated prompt list (prompts_parser + Gemma)
  - RendererAgent — single prompt → saved PNG (gemini_bot.create_image_and_download)
  - ReviewAgent   — (image_path, prompt_text) → {match, reason}
                    OCR via pytesseract, judgement via Gemma.

ReviewAgent's judgement prompt is TEXT-FIDELITY focused:
  reject garbled text, missing brand mark (@techengine.lab), wrong slide
  number, or prompt-text echoed back as the image content.

Future agents (Phase 2+): DownloaderAgent (rescues stuck images),
WriterAgent (Claude.ai for long-form), ArchiverAgent (history DB + Drive).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Callable, Optional


def _config_model_default() -> str:
    """Resolve the Ollama model name from ~/.lotus_config.json →
    `ollama.model`, with OLLAMA_MODEL env var overriding for dev runs.
    Any lookup error falls back to gemma4:e4b so agents stay usable
    even when the config is missing/corrupt."""
    env = os.environ.get("OLLAMA_MODEL", "").strip()
    if env: return env
    try:
        import lotus_config as _lc
        return _lc.load().get("ollama", {}).get("model", "gemma4:e4b")
    except Exception:
        return "gemma4:e4b"


# ── Base protocol ──────────────────────────────────────────────────────

class BaseAgent:
    """Every child agent subclasses this. Only `run(task)` needs to be
    implemented; the rest (threading, status tracking, health counters)
    is handled here."""

    def __init__(self, name: str):
        self.name = name
        self._works: dict = {}                 # work_id → work-dict
        self._lock = threading.Lock()
        self._success_count: int = 0
        self._fail_count:    int = 0

    # ── public API the mother uses ───────────────────────────────────

    def submit(self, task: dict) -> str:
        """Queue a task and return a work_id. Starts a daemon thread that
        calls `self.run(task)`."""
        work_id = uuid.uuid4().hex[:10]
        with self._lock:
            self._works[work_id] = {
                "state":   "queued",
                "result":  None,
                "error":   None,
                "started": time.time(),
                "ended":   None,
                "task":    task,
            }
        threading.Thread(
            target=self._run_wrapper,
            args=(work_id, task),
            daemon=True,
            name=f"{self.name}-{work_id}",
        ).start()
        return work_id

    def status(self, work_id: str) -> dict:
        """Snapshot of one work's current state."""
        with self._lock:
            w = self._works.get(work_id)
            if not w:
                return {"state": "unknown", "work_id": work_id}
            ended = w["ended"] if w["ended"] is not None else time.time()
            return {
                "work_id": work_id,
                "state":   w["state"],
                "result":  w["result"],
                "error":   w["error"],
                "elapsed": round(ended - w["started"], 2),
            }

    def health(self) -> dict:
        """Rolling stats for the agent panel in the UI."""
        with self._lock:
            busy = sum(1 for w in self._works.values()
                       if w["state"] in ("queued", "running"))
            last_err = None
            # Walk most-recent-first to find the last error message.
            for w in sorted(self._works.values(),
                            key=lambda x: x.get("ended") or x["started"],
                            reverse=True):
                if w.get("error"):
                    last_err = w["error"]
                    break
            total = self._success_count + self._fail_count
            success_rate = (self._success_count / total) if total else None
            return {
                "name":          self.name,
                "alive":         True,
                "busy":          busy,
                "success_count": self._success_count,
                "fail_count":    self._fail_count,
                "success_rate":  success_rate,      # None if no work yet
                "last_error":    last_err,
            }

    # ── internals ────────────────────────────────────────────────────

    def _run_wrapper(self, work_id: str, task: dict) -> None:
        with self._lock:
            w = self._works.get(work_id)
            if w: w["state"] = "running"
        try:
            result = self.run(task)
            with self._lock:
                w = self._works.get(work_id)
                if w:
                    w["state"]  = "done"
                    w["result"] = result
                    w["ended"]  = time.time()
                    self._success_count += 1
        except Exception as e:
            # Keep the error string short enough to fit in a UI toast.
            err_msg = f"{type(e).__name__}: {e}"
            with self._lock:
                w = self._works.get(work_id)
                if w:
                    w["state"] = "failed"
                    w["error"] = err_msg[:400]
                    w["ended"] = time.time()
                    self._fail_count += 1

    def run(self, task: dict):
        """Subclass overrides. Synchronous work — can block. Raise on failure."""
        raise NotImplementedError(f"{self.name}.run() not implemented")


# ── Concrete agents ────────────────────────────────────────────────────

class ParserAgent(BaseAgent):
    """Wraps prompts_parser — MD file → validated prompt list."""

    def __init__(self):
        super().__init__("ParserAgent")

    def run(self, task: dict) -> dict:
        md_path = task["md_path"]
        if not os.path.exists(md_path):
            raise RuntimeError(f"MD file not found: {md_path}")
        # prompts_parser.extract_prompts takes md TEXT (not a path), and
        # it already runs the Gemma validation loop internally, returning
        # a list of cleaned prompt strings ready for rendering.
        from pathlib import Path as _P
        md_text = _P(md_path).read_text(encoding="utf-8", errors="replace")
        import prompts_parser
        prompts = prompts_parser.extract_prompts(md_text)
        return {
            "prompts":   prompts,
            "count":     len(prompts),
            "md_path":   md_path,
        }


class RendererAgent(BaseAgent):
    """Wraps gemini_bot.create_image_and_download — one prompt → one PNG."""

    def __init__(self):
        super().__init__("RendererAgent")

    def run(self, task: dict) -> dict:
        prompt  = task["prompt"]
        save_to = task["save_to"]
        if not prompt or not prompt.strip():
            raise RuntimeError("empty prompt")
        if not save_to:
            raise RuntimeError("save_to missing")
        import gemini_bot
        saved = gemini_bot.create_image_and_download(
            prompt, save_to, verbose=task.get("verbose", True))
        if not saved:
            raise RuntimeError("gemini_bot returned None — render or download failed")
        return {"path": saved, "prompt_len": len(prompt)}


class ReviewAgent(BaseAgent):
    """Checks a rendered image against its prompt on TWO axes:
      - text-fidelity (via pytesseract OCR + Gemma reasoning)
      - visual match  (via Gemma-vision directly on the image)

    gemma4:e4b reports capabilities=['vision',...], so a single multimodal
    call handles both — prompt + OCR-text + image all go in one round-trip.

    Image is downscaled to max 1024px before encoding so the base64 stays
    under ~1 MB; the vision model's accuracy on downscaled images is
    more than enough for this check (composition, colors, text presence).
    If vision is disabled or unavailable, falls back to text-only review.

    CONCURRENCY: Gemma-vision is GPU-bound and single-inference-at-a-time.
    Issuing N concurrent calls from multiple frame watchers just queues
    them behind each other until individual calls blow the timeout.
    We serialise via a class-wide semaphore (default 1, tunable via the
    LOTUS_REVIEW_CONCURRENCY env var) so each review gets full GPU time.

    Task shape: {
      "image_path":  "...",                        # required
      "prompt_text": "...",                        # required
      "vision":      True/False (default True),    # include image in Gemma call
    }

    Return shape: { match, reason, text_match?, vision_match?, ocr_chars }
    The top-level `match` is AND of the two sub-checks — either one failing
    rejects the frame.
    """

    # One semaphore shared across all ReviewAgent instances in the process.
    # Default 1 = fully serialised (safe). Bump carefully — GPU contention
    # on Apple Silicon makes throughput WORSE above ~2 for vision inference.
    _gemma_sem = threading.Semaphore(
        int(os.environ.get("LOTUS_REVIEW_CONCURRENCY", "1")))

    def __init__(self, ollama_url: str = "http://localhost:11434",
                 model: Optional[str] = None,
                 max_image_side: int = 1024):
        super().__init__("ReviewAgent")
        self.ollama_url = ollama_url
        self.model = model or _config_model_default()
        self.max_image_side = max_image_side

    def run(self, task: dict) -> dict:
        image_path  = task["image_path"]
        prompt_text = task.get("prompt_text", "")
        use_vision  = task.get("vision", True)

        if not os.path.exists(image_path):
            raise RuntimeError(f"image not found: {image_path}")

        # 1. OCR the image (cheap, deterministic, catches exact text errors).
        try:
            import pytesseract
            from PIL import Image
            pil_img = Image.open(image_path)
            ocr_text = pytesseract.image_to_string(pil_img)
        except Exception as e:
            raise RuntimeError(f"OCR failed: {e}")

        ocr_text = (ocr_text or "").strip()

        # 2. Encode image for the vision model — downscale first so we
        #    aren't shipping 9MB of base64 per frame.
        img_b64 = None
        if use_vision:
            try:
                img_b64 = self._encode_downscaled(image_path)
            except Exception as e:
                # Don't fail the review on encoding errors — degrade to text-only.
                print(f"[review] image encode failed, text-only mode: {e}")

        # 3. Ask Gemma (with vision if we have it).
        verdict = self._ask_gemma(prompt_text, ocr_text, img_b64)
        verdict["ocr_chars"] = len(ocr_text)
        verdict["had_vision"] = img_b64 is not None
        return verdict

    # ── private ──

    def _encode_downscaled(self, image_path: str) -> str:
        """Load, downscale so max dimension is <= max_image_side, return
        base64 PNG. This keeps the Ollama multimodal call fast."""
        import base64 as _b64, io as _io
        from PIL import Image as _PIL
        img = _PIL.open(image_path)
        # Convert to RGB to strip alpha — vision models don't need it and
        # transparent backgrounds sometimes confuse them.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > self.max_image_side:
            scale = self.max_image_side / max(w, h)
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, _PIL.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return _b64.b64encode(buf.getvalue()).decode("ascii")

    def _ask_gemma(self, prompt_text: str, ocr_text: str,
                   img_b64: Optional[str]) -> dict:
        import requests
        vision_mode = img_b64 is not None
        review_prompt = (
            "You are LOTUS Review Agent. Decide whether a generated image "
            "meets the prompt's requirements on TWO axes:\n"
            "  1. TEXT FIDELITY — compare the OCR-extracted text below to "
            "what the prompt asked for.\n"
            "  2. VISUAL MATCH — " +
            ("look at the attached image directly."
             if vision_mode else
             "(no image provided — skip this check).") +
            "\n\n"
            "REJECT when any of these are obviously wrong:\n"
            "- Garbled or placeholder text ('Lorem ipsum', random chars)\n"
            "- Missing required brand mark (e.g. @techengine.lab) that the prompt specified\n"
            "- Wrong slide-number in a numbered series\n"
            "- Prompt-text echoed back verbatim as the image content\n"
            "- Obviously wrong subject vs what the prompt requested "
            "(e.g. prompt says BMW i7 but image shows a motorcycle)\n"
            "- Obviously wrong aspect ratio or composition (e.g. prompt asks "
            "for portrait 4:5 and image is clearly square)\n"
            "- Color palette clearly off vs what the prompt specifies\n"
            "- Font/typography clearly off when the prompt was specific about it\n\n"
            "ACCEPT when differences are minor or not explicitly required.\n\n"
            f"PROMPT (first 1200 chars):\n{prompt_text[:1200]}\n\n"
            "OCR-EXTRACTED TEXT FROM THE IMAGE:\n"
            f"{ocr_text[:800] if ocr_text else '(no readable text)'}\n\n"
            "Respond with ONLY a single JSON object, no prose before or after:\n"
            '{"match": true|false, "text_match": true|false, '
            '"vision_match": true|false, "reason": "<15-word reason>"}'
        )
        payload: dict = {
            "model":   self.model,
            "prompt":  review_prompt,
            "stream":  False,
            "options": {"temperature": 0.1},
        }
        if vision_mode:
            payload["images"] = [img_b64]
        try:
            # Serialise all Gemma-review calls. Without this, many watchers
            # fire concurrently, all get queued on the single GPU, and each
            # request individually exceeds its 180s timeout.
            with ReviewAgent._gemma_sem:
                r = requests.post(
                    f"{self.ollama_url}/api/generate",
                    json=payload,
                    # Vision calls are heavier — bump the ceiling.
                    timeout=240 if vision_mode else 60,
                )
                r.raise_for_status()
                response = r.json().get("response", "").strip()
        except Exception as e:
            raise RuntimeError(f"Gemma review call failed: {e}")

        # Extract the first JSON object from the response — Gemma sometimes
        # wraps the reply in extra prose despite being told not to.
        m = re.search(r'\{.*?\}', response, re.DOTALL)
        if not m:
            return {
                "match":       True,      # default PASS on parse failure
                "reason":      "review parse failed — defaulting to pass",
                "text_match":  None, "vision_match": None,
                "raw":         response[:200],
            }
        try:
            data = json.loads(m.group(0))
            text_match   = data.get("text_match")
            vision_match = data.get("vision_match")
            match        = data.get("match")
            # If top-level match missing, derive from sub-checks.
            if match is None:
                if text_match is not None and vision_match is not None:
                    match = bool(text_match) and bool(vision_match)
                elif text_match is not None:
                    match = bool(text_match)
                else:
                    match = True
            return {
                "match":        bool(match),
                "text_match":   None if text_match is None   else bool(text_match),
                "vision_match": None if vision_match is None else bool(vision_match),
                "reason":       str(data.get("reason", ""))[:240],
            }
        except json.JSONDecodeError:
            return {
                "match":       True,
                "reason":      "review JSON invalid — defaulting to pass",
                "text_match":  None, "vision_match": None,
                "raw":         response[:200],
            }


class ArchiverAgent(BaseAgent):
    """Freezes a completed pipeline as a sellable JSON report.

    Task shape: {
        "pipeline":     <dict from Pipeline.as_event()>,   # required
        "reports_dir":  "<path>",                          # required
        "pipeline_id":  "<id>",                            # optional, for filename
        "ask_gemma":    True|False,                        # default True
    }

    Output: {
        "report_path": "<absolute path to the written JSON>",
        "summary":     "<one-paragraph narrative or empty>",
        "counts":      { by_lotus, manual, skipped, failed, generating, total },
    }
    """

    def __init__(self, ollama_url: str = "http://localhost:11434",
                 model: Optional[str] = None):
        super().__init__("ArchiverAgent")
        self.ollama_url = ollama_url
        self.model = model or _config_model_default()

    def run(self, task: dict) -> dict:
        pipeline    = task.get("pipeline") or {}
        reports_dir = task.get("reports_dir") or os.path.expanduser("~/LotusAgent/reports")
        ask_gemma   = task.get("ask_gemma", True)

        if not pipeline:
            raise RuntimeError("pipeline dict missing")

        # Compute counts from frames.
        frames = pipeline.get("frames", []) or []
        def _by_source(key: str) -> int:
            return sum(1 for f in frames if f.get("source") == key)
        def _by_status(key: str) -> int:
            return sum(1 for f in frames if f.get("status") == key)
        counts = {
            "by_lotus":   _by_source("lotus"),
            "manual":     _by_source("manual"),
            "skipped":    _by_source("skipped"),
            "failed":     _by_status("failed"),
            "generating": _by_status("generating"),
            "total":      len(frames),
        }

        summary = ""
        if ask_gemma:
            summary = self._ask_gemma_for_summary(pipeline, counts)

        # Write the JSON report.
        os.makedirs(reports_dir, exist_ok=True)
        safe_name = (pipeline.get("name") or pipeline.get("id") or "pipeline")
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", safe_name)
        ts = time.strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(
            reports_dir, f"{ts}_{safe_name}_{pipeline.get('id','')}.json")

        payload = {
            "archived_at":  time.time(),
            "archived_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pipeline":     pipeline,
            "counts":       counts,
            "summary":      summary,
        }
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        return {
            "report_path": report_path,
            "summary":     summary,
            "counts":      counts,
        }

    # ── private ──

    def _ask_gemma_for_summary(self, pipeline: dict, counts: dict) -> str:
        import requests
        name    = pipeline.get("name") or pipeline.get("id") or "pipeline"
        src     = os.path.basename(pipeline.get("source_md") or "")
        stage   = pipeline.get("stage", "")
        errs    = pipeline.get("errors") or []
        err_txt = ""
        if errs:
            sample = errs[-5:]
            err_txt = "\n".join(
                f"- [{e.get('stage','')}] {e.get('frame_id','?')}: {e.get('message','')}"
                for e in sample
            )
        started = pipeline.get("created_at")
        elapsed_txt = ""
        try:
            if isinstance(started, (int, float)):
                elapsed_s = time.time() - float(started)
                if elapsed_s < 60:
                    elapsed_txt = f"{int(elapsed_s)}s"
                elif elapsed_s < 3600:
                    elapsed_txt = f"{int(elapsed_s//60)}m{int(elapsed_s%60)}s"
                else:
                    elapsed_txt = f"{int(elapsed_s//3600)}h{int((elapsed_s%3600)//60)}m"
        except Exception: pass

        prompt = (
            "You are LOTUS Archiver. Summarise this pipeline run in ONE short, "
            "professional paragraph (max ~50 words) suitable for a client report.\n"
            "Be factual. No marketing language. Mention the totals, any notable "
            "failure pattern, and elapsed time.\n\n"
            f"Pipeline:   {name}\n"
            f"Source:     {src}\n"
            f"Stage:      {stage}\n"
            f"Elapsed:    {elapsed_txt or 'unknown'}\n"
            f"Counts:     {counts}\n"
            f"Recent errors ({len(errs)} total; showing last {len(errs[-5:])}):\n"
            f"{err_txt or '(none)'}\n\n"
            "Write the summary now, no preamble:"
        )
        try:
            r = requests.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": self.model, "prompt": prompt, "stream": False,
                    "options": {"temperature": 0.2},
                },
                timeout=45,
            )
            r.raise_for_status()
            text = (r.json().get("response") or "").strip()
            # Trim accidental "Summary:" prefix + collapse whitespace.
            text = re.sub(r"^\s*Summary[:\-\s]*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:1200]
        except Exception as e:
            return f"(summary unavailable — Gemma call failed: {e})"


class RemoteAgent(BaseAgent):
    """Proxies an agent that lives on another process (or machine) via
    WebSocket. Implements BaseAgent so it slots into AgentRegistry
    transparently — the mother submits work to "ReviewAgent" and the
    registry picks whichever provider (local or remote) is free.

    Wire protocol: see lotus-child.py docstring. Each `run(task)` call
    opens a fresh WS connection for simplicity; long-run sessions can
    add connection pooling later.

    Fault tolerance (D4):
      - Connect/handshake failures get ONE automatic retry with a fresh
        connection. This handles transient LAN hiccups without promoting
        them to a visible failure.
      - In-flight submit/poll failures (connection dropped after work
        was queued) can't be safely retried without risking double work,
        so those raise cleanly and let the caller retry at a higher
        level (e.g. the frame-retry logic in the mother).
    """

    def __init__(self, name: str, remote_agent_name: str,
                 ws_url: str, token: str,
                 poll_interval: float = 1.0,
                 deadline_seconds: float = 600.0):
        super().__init__(name)
        self.remote_agent_name = remote_agent_name
        self.ws_url = ws_url
        self.token = token
        self.poll_interval = poll_interval
        self.deadline_seconds = deadline_seconds
        # D4 circuit-breaker: each failure bumps `_recent_failures` by 1
        # (bounded at a small ceiling); each success resets to 0. The
        # registry's pick_best() uses this to down-weight flaky providers.
        self._recent_failures: int = 0
        # D5 liveness: populated by the mother's probe loop every ~30s.
        # `ping_fails` is the count of CONSECUTIVE probe failures; after
        # 3 the mother deregisters the agent. `last_ping_ok` is the unix
        # timestamp of the most recent successful probe, for UI display.
        self.ping_fails: int = 0
        self.last_ping_ok: Optional[float] = None

    # ── health override: expose remote-ness and host for the UI ──

    def health(self) -> dict:
        h = super().health()
        # Derive a friendly host:port label from the ws_url for the dashboard.
        try:
            from urllib.parse import urlparse as _urlparse
            parsed = _urlparse(self.ws_url)
            host = f"{parsed.hostname}:{parsed.port or ''}".rstrip(":")
        except Exception:
            host = self.ws_url
        h["remote"] = True
        h["host"]   = host
        h["recent_failures"] = self._recent_failures
        h["ping_fails"]      = self.ping_fails
        h["last_ping_ok"]    = self.last_ping_ok
        return h

    def ping(self, timeout: float = 5.0) -> bool:
        """Cheap liveness check: open a fresh WS, auth, send `health`,
        close. Used by the mother's D5 probe loop. Returns True on
        success and updates `last_ping_ok`; on failure bumps `ping_fails`.
        Does NOT bump `_recent_failures` — probes and real submits are
        scored separately so a long-idle child doesn't get
        circuit-breakered just from missed pings."""
        import asyncio as _aio
        import websockets as _ws

        async def _do():
            async with _ws.connect(self.ws_url, open_timeout=timeout,
                                   ping_interval=None) as ws:
                await ws.send(json.dumps({"type": "auth", "token": self.token}))
                first = json.loads(await _aio.wait_for(ws.recv(), timeout=timeout))
                if first.get("type") != "auth_ok":
                    return False
                await ws.send(json.dumps({"type": "health"}))
                snap = json.loads(await _aio.wait_for(ws.recv(), timeout=timeout))
                return snap.get("type") == "health_snapshot"

        try:
            ok = _aio.run(_do())
        except Exception:
            ok = False
        if ok:
            self.ping_fails = 0
            self.last_ping_ok = time.time()
            return True
        self.ping_fails += 1
        return False

    def run(self, task: dict) -> dict:
        # Each submit opens a fresh connection. We allow ONE retry if the
        # initial connection/handshake fails (transient LAN blip). Once
        # the server has accepted our submit we don't retry — the work
        # may already be in progress on the far side.
        import asyncio as _aio
        import websockets as _ws
        from websockets.exceptions import ConnectionClosed
        import time as _time

        async def _handshake(ws):
            await ws.send(json.dumps({"type": "auth", "token": self.token}))
            first = json.loads(await _aio.wait_for(ws.recv(), timeout=5))
            if first.get("type") != "auth_ok":
                raise RuntimeError(
                    f"remote {self.ws_url}: auth failed "
                    f"({first.get('reason', '?')})")

        async def _submit_and_poll(ws):
            await ws.send(json.dumps({
                "type":  "submit",
                "agent": self.remote_agent_name,
                "task":  task,
            }))
            reply = json.loads(await _aio.wait_for(ws.recv(), timeout=10))
            if reply.get("error"):
                raise RuntimeError(f"remote submit: {reply['error']}")
            wid = reply.get("work_id")
            if not wid:
                raise RuntimeError("remote submit returned no work_id")

            deadline = _time.time() + self.deadline_seconds
            while _time.time() < deadline:
                await _aio.sleep(self.poll_interval)
                await ws.send(json.dumps({
                    "type":    "status",
                    "agent":   self.remote_agent_name,
                    "work_id": wid,
                }))
                s = json.loads(await _aio.wait_for(ws.recv(), timeout=10))
                state = s.get("state")
                if state == "done":
                    return s.get("result")
                if state == "failed":
                    raise RuntimeError(
                        f"remote agent failed: {s.get('error') or 'no detail'}")
            raise RuntimeError(
                f"remote agent timeout after {self.deadline_seconds}s")

        async def _connect_and_run(allow_retry: bool):
            try:
                async with _ws.connect(self.ws_url, open_timeout=10,
                                       ping_interval=30, max_size=None) as ws:
                    await _handshake(ws)
                    return await _submit_and_poll(ws)
            except (ConnectionClosed, OSError, _aio.TimeoutError) as e:
                if allow_retry:
                    # Short delay then one retry with a fresh socket.
                    await _aio.sleep(1.0)
                    return await _connect_and_run(allow_retry=False)
                raise RuntimeError(
                    f"remote {self.ws_url}: transient network error: "
                    f"{type(e).__name__}: {e}") from e

        try:
            result = _aio.run(_connect_and_run(allow_retry=True))
            self._recent_failures = 0     # success clears the breaker
            return result
        except Exception:
            # Bounded counter so a long outage doesn't overflow int math;
            # pick_best only needs to know "any recent failure at all".
            self._recent_failures = min(self._recent_failures + 1, 10)
            raise


# ── Registry ───────────────────────────────────────────────────────────

class AgentRegistry:
    """Single source of truth for all agents. Supports MULTIPLE providers
    for the same logical agent name — e.g. a local `ReviewAgent` plus
    several `ReviewAgent` remotes on children. The mother's mesh picks
    the least-busy provider at submit time via `pick_best(name)`.

    Storage layout:
      - `_agents: dict[instance_key → BaseAgent]` — unique per provider.
        Instance keys are the agent name for local ("ReviewAgent") and
        "ReviewAgent@host:port" for remotes.
      - `_by_name: dict[base_name → list[instance_key]]` — lookup index
        so `pick_best("ReviewAgent")` finds all providers in O(1).
    """

    def __init__(self):
        self._agents: dict = {}
        self._by_name: dict = {}     # base agent name → [instance_keys]

    def register(self, agent: BaseAgent, instance_key: Optional[str] = None) -> str:
        """Register an agent under `instance_key` (defaults to `agent.name`).
        Returns the instance_key actually used. If the key is already taken
        the existing entry is replaced (latest wins)."""
        key = instance_key or agent.name
        base = agent.name
        self._agents[key] = agent
        bucket = self._by_name.setdefault(base, [])
        if key not in bucket:
            bucket.append(key)
        return key

    def get(self, key_or_name: str) -> Optional[BaseAgent]:
        """Lookup by exact instance key first, then fall through to
        picking the best provider matching the base name."""
        if key_or_name in self._agents:
            return self._agents[key_or_name]
        return self.pick_best(key_or_name)

    def pick_best(self, base_name: str) -> Optional[BaseAgent]:
        """Among all providers of `base_name`, return the best one. Scoring:
          1. Circuit-breaker penalty — providers with recent failures are
             demoted so a flaky remote child doesn't keep getting work
             while the local provider is healthy (D4).
          2. Fewer busy work items wins.
          3. More lifetime successes wins (ties break in favor of proven
             providers).
        Returns None if no providers are registered."""
        keys = self._by_name.get(base_name) or []
        providers = [self._agents[k] for k in keys if k in self._agents]
        if not providers:
            return None
        if len(providers) == 1:
            return providers[0]
        def _score(a: BaseAgent) -> tuple:
            h = a.health()
            penalty = min(h.get("recent_failures", 0), 5)
            return (penalty, h.get("busy", 0), -h.get("success_count", 0))
        return min(providers, key=_score)

    def providers_of(self, base_name: str) -> list:
        """All registered providers for a given base name."""
        keys = self._by_name.get(base_name) or []
        return [self._agents[k] for k in keys if k in self._agents]

    def remove(self, instance_key: str) -> bool:
        """Deregister one provider. Returns True if something was removed."""
        a = self._agents.pop(instance_key, None)
        if not a:
            return False
        bucket = self._by_name.get(a.name, [])
        if instance_key in bucket:
            bucket.remove(instance_key)
        return True

    def health_snapshot(self) -> list:
        return [a.health() for a in self._agents.values()]

    def __iter__(self):
        return iter(self._agents.values())


# Default registry the mother binds to. Imported by agent_phase1.py.
default_registry = AgentRegistry()
default_registry.register(ParserAgent())
default_registry.register(RendererAgent())
default_registry.register(ReviewAgent())
default_registry.register(ArchiverAgent())
