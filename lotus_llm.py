"""
lotus_llm.py - the one place LOTUS talks to a language model.

Every former Gemma call site migrates onto `chat()` / `stream_chat()` here.
Claude API is the primary brain; local Ollama (Gemma) is the fallback when
there is no API key, no network, or today's spend cap is reached, so LOTUS
still runs offline.

Routing is by *task*, not by model name. Each task maps to a model + effort
in the `llm.tasks` block of ~/.lotus_config.json:

    classify   prompts_parser / file classifier / Upwork scoring  -> Haiku
    chat       voice loop, tool-calling assistant                  -> Sonnet
    extract    long-document prompt extraction                     -> Sonnet
    review     ReviewAgent / ArchiverAgent summaries               -> Sonnet
    draft      Upwork proposals and other short-form writing       -> Sonnet
    audit      Instagram/YouTube audits, comment drafts, content   -> Sonnet

API key lookup order: ANTHROPIC_API_KEY env var, then
~/.lotus_auth/anthropic_api_key.txt (same pattern as gemini_api_key.txt).

Cost controls built in:
  - prompt caching on every request (top-level cache_control), so the long,
    stable system prompts are billed at ~0.1x after the first call
  - per-task model + effort, cheapest model that does the job
  - a daily USD cap (`llm.daily_budget_usd`); over it, calls go to Ollama
  - usage + estimated cost appended per day to ~/.lotus_auth/llm_usage.json

Usage:
    import lotus_llm
    text = lotus_llm.chat([{"role": "user", "content": "hi"}], task="chat")
    data = lotus_llm.chat(msgs, system=SYS, task="classify", json_mode=True)
    for piece in lotus_llm.stream_chat(msgs, system=SYS, task="chat"):
        print(piece, end="")

    python lotus_llm.py status      # which backend is live, today's spend
    python lotus_llm.py ping        # one tiny real call per backend
"""

import json
import os
import re
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

import requests

AUTH_DIR = Path.home() / ".lotus_auth"
KEY_FILE = AUTH_DIR / "anthropic_api_key.txt"
USAGE_FILE = AUTH_DIR / "llm_usage.json"

# USD per 1M tokens: (input, output). Cache reads bill at 0.1x input,
# 5-minute cache writes at 1.25x input. Haiku's rate is the <=100K-prompt tier.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}

# Server-side refusal fallback ("default" form) is supported on these models
# only; Haiku has no server-side fallback.
_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

DEFAULT_LLM_CONFIG = {
    # "auto": Claude when a key is present, else Ollama.
    # "claude": Claude only (errors surface instead of falling back).
    # "ollama": local Gemma only - the pre-Claude behaviour.
    "provider": "auto",
    "ollama_fallback": True,
    "daily_budget_usd": 5.0,
    "tasks": {
        "classify": {"model": "claude-haiku-5-5", "effort": "low", "max_tokens": 1024},
        "chat": {"model": "claude-sonnet-5-5", "effort": "low", "max_tokens": 4096},
        "extract": {"model": "claude-sonnet-5-5", "effort": "medium", "max_tokens": 32000},
        "review": {"model": "claude-sonnet-5-5", "effort": "medium", "max_tokens": 4096},
        "draft": {"model": "claude-sonnet-5-5", "effort": "medium", "max_tokens": 4096},
        "audit": {"model": "claude-sonnet-5-5", "effort": "high", "max_tokens": 16000},
    },
}


class LLMError(RuntimeError):
    """Raised when no backend could answer."""


class LLMRefusal(LLMError):
    """Claude declined the request (stop_reason == "refusal")."""


_lock = threading.Lock()
_local = threading.local()
_client = None
_client_key = None


# ── config + credentials ──────────────────────────────────────────────────

def config() -> dict:
    """`llm.*` from ~/.lotus_config.json merged over DEFAULT_LLM_CONFIG."""
    try:
        import lotus_config as _lc
        saved = _lc.load().get("llm", {}) or {}
    except Exception:
        saved = {}
    cfg = {**DEFAULT_LLM_CONFIG, **{k: v for k, v in saved.items() if k != "tasks"}}
    tasks = {k: dict(v) for k, v in DEFAULT_LLM_CONFIG["tasks"].items()}
    for name, over in (saved.get("tasks") or {}).items():
        tasks[name] = {**tasks.get(name, {}), **over}
    cfg["tasks"] = tasks
    return cfg


def api_key() -> Optional[str]:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        key = KEY_FILE.read_text().strip()
    except OSError:
        return None
    return key or None


def _anthropic():
    """Lazily built client; rebuilt if the key file changes under us."""
    global _client, _client_key
    key = api_key()
    if not key:
        return None
    with _lock:
        if _client is None or key != _client_key:
            import anthropic
            _client = anthropic.Anthropic(api_key=key, max_retries=3)
            _client_key = key
    return _client


def _ollama_url() -> str:
    # Read per call, not at import: lotus-child.py points OLLAMA_URL at the
    # mother's Ollama, and the mother may have imported us before that.
    return os.environ.get("OLLAMA_URL", "http://localhost:11434")


def _ollama_model() -> str:
    env = os.environ.get("OLLAMA_MODEL", "").strip()
    if env:
        return env
    try:
        import lotus_config as _lc
        return _lc.load().get("ollama", {}).get("model", "gemma4:e4b")
    except Exception:
        return "gemma4:e4b"


# ── spend tracking ────────────────────────────────────────────────────────

def _load_usage() -> dict:
    try:
        return json.loads(USAGE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def spent_today() -> float:
    return float(_load_usage().get(date.today().isoformat(), {}).get("usd", 0.0))


def _record_usage(model: str, task: str, usage) -> None:
    rate_in, rate_out = PRICES.get(model, PRICES["claude-opus-5-5"])
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    c_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    c_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    usd = (inp * rate_in + c_read * rate_in * 0.1 + c_write * rate_in * 1.25
           + out * rate_out) / 1_000_000
    today = date.today().isoformat()
    with _lock:
        data = _load_usage()
        day = data.setdefault(today, {"usd": 0.0, "calls": 0, "by_task": {}})
        day["usd"] = round(day["usd"] + usd, 6)
        day["calls"] += 1
        t = day["by_task"].setdefault(task, {"calls": 0, "usd": 0.0, "input": 0,
                                             "cache_read": 0, "output": 0})
        t["calls"] += 1
        t["usd"] = round(t["usd"] + usd, 6)
        t["input"] += inp + c_write
        t["cache_read"] += c_read
        t["output"] += out
        try:
            AUTH_DIR.mkdir(exist_ok=True)
            tmp = USAGE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(USAGE_FILE)
        except OSError as e:
            print(f"[llm] could not write usage log: {e}", flush=True)


# ── backend choice ────────────────────────────────────────────────────────

def backend(cfg: Optional[dict] = None) -> str:
    """'claude' or 'ollama' - what the next call will use, and why not Claude."""
    cfg = cfg or config()
    if cfg["provider"] == "ollama":
        return "ollama"
    if not api_key():
        if cfg["provider"] == "claude":
            raise LLMError("llm.provider is 'claude' but no Anthropic API key "
                           f"(set ANTHROPIC_API_KEY or write {KEY_FILE})")
        return "ollama"
    if spent_today() >= float(cfg["daily_budget_usd"]):
        if cfg["provider"] == "claude" or not cfg["ollama_fallback"]:
            raise LLMError(f"daily Claude budget ${cfg['daily_budget_usd']} reached")
        print("[llm] daily budget reached - using Ollama", flush=True)
        return "ollama"
    return "claude"


def _task(cfg: dict, task: str) -> dict:
    if task not in cfg["tasks"]:
        raise ValueError(f"unknown llm task {task!r}; known: {sorted(cfg['tasks'])}")
    return cfg["tasks"][task]


_JSON_HINT = ("\n\nRespond with a single JSON object and nothing else - "
              "no prose, no markdown code fences.")


def _claude_kwargs(cfg, task, messages, system, json_mode, max_tokens) -> tuple:
    spec = _task(cfg, task)
    model = spec["model"]
    sys_text = (system or "") + (_JSON_HINT if json_mode else "")
    kwargs = {
        "model": model,
        "max_tokens": int(max_tokens or spec.get("max_tokens", 4096)),
        "messages": messages,
        # Automatic prompt caching: the stable system prompt + earlier turns
        # are cached; a prefix under the model's minimum just isn't cached.
        "cache_control": {"type": "ephemeral"},
        "output_config": {"effort": spec.get("effort", "medium")},
    }
    if sys_text:
        kwargs["system"] = sys_text
    use_beta = model in _FALLBACK_MODELS
    if use_beta:
        kwargs["betas"] = [_FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return model, use_beta, kwargs


def _text_of(resp) -> str:
    if resp.stop_reason == "refusal":
        cat = getattr(getattr(resp, "stop_details", None), "category", None)
        raise LLMRefusal(f"Claude declined the request (category={cat})")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _extract_json(text: str) -> str:
    """Strip code fences / stray prose so callers can json.loads() the result."""
    t = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if fence:
        t = fence.group(1).strip()
    if t[:1] in "{[":
        return t
    m = re.search(r"[\{\[].*[\}\]]", t, re.S)
    return m.group(0) if m else t


# ── Ollama (fallback) ─────────────────────────────────────────────────────

def _with_images(messages: list, images: Optional[list], claude: bool) -> list:
    """Attach base64 PNGs to the last user message - as image blocks for
    Claude, as Ollama's `images` field for Gemma. Copies, never mutates."""
    if not images:
        return list(messages)
    msgs = [dict(m) for m in messages]
    last = msgs[-1]
    if claude:
        text = last["content"]
        blocks = text if isinstance(text, list) else [{"type": "text", "text": text}]
        last["content"] = [{"type": "image", "source": {"type": "base64",
                            "media_type": "image/png", "data": b}} for b in images] + blocks
    else:
        last["images"] = list(images)
    return msgs


def _ollama_chat(messages, system, json_mode, max_tokens, timeout,
                 url=None, model=None) -> str:
    msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
    url = url or _ollama_url()
    body = {
        "model": model or _ollama_model(),
        "stream": False,
        "messages": msgs,
        # JSON calls are classifiers: keep them deterministic.
        "options": {"temperature": 0.0 if json_mode else 0.1,
                    "num_predict": int(max_tokens or 4096),
                    "num_ctx": 16384},
    }
    if json_mode:
        body["format"] = "json"
    try:
        r = requests.post(f"{url}/api/chat", json=body, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise LLMError(f"Ollama unreachable at {url}: {e}") from e
    return ((r.json().get("message") or {}).get("content") or "").strip()


def _ollama_stream(messages, system, max_tokens, timeout) -> Iterator[str]:
    msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
    body = {"model": _ollama_model(), "stream": True, "messages": msgs,
            "options": {"temperature": 0.3, "num_predict": int(max_tokens or 4096)}}
    try:
        with requests.post(f"{_ollama_url()}/api/chat", json=body, stream=True,
                           timeout=timeout) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = (chunk.get("message") or {}).get("content", "")
                if piece:
                    yield piece
                if chunk.get("done"):
                    break
    except requests.RequestException as e:
        raise LLMError(f"Ollama unreachable at {_ollama_url()}: {e}") from e


# ── public API ────────────────────────────────────────────────────────────

def last_backend() -> Optional[str]:
    """'claude' or 'ollama' - who answered this thread's last chat() call."""
    return getattr(_local, "backend", None)


def chat(messages: list, *, system: Optional[str] = None, task: str = "chat",
         json_mode: bool = False, max_tokens: Optional[int] = None,
         ollama_max_tokens: Optional[int] = None,
         images: Optional[list] = None,
         ollama_url: Optional[str] = None, ollama_model: Optional[str] = None,
         timeout: float = 600) -> str:
    """One request -> the reply text. With json_mode the text is a bare JSON
    object/array string ready for json.loads(). Raises LLMRefusal / LLMError.

    `ollama_max_tokens` caps only the Gemma fallback - for latency-bound
    callers whose old Gemma budget is far below what Claude needs to think.
    `images` are base64 PNGs attached to the last user message.
    `ollama_url` / `ollama_model` override the Gemma fallback per call
    (agent-mesh children point at the mother's Ollama this way)."""
    _local.backend = None
    import anthropic
    cfg = config()
    if backend(cfg) == "claude":
        model, use_beta, kwargs = _claude_kwargs(
            cfg, task, _with_images(messages, images, True), system,
            json_mode, max_tokens)
        client = _anthropic().with_options(timeout=timeout)
        api = client.beta.messages if use_beta else client.messages
        try:
            # Large outputs must stream to stay under the HTTP timeout.
            if kwargs["max_tokens"] > 16000:
                with api.stream(**kwargs) as s:
                    resp = s.get_final_message()
            else:
                resp = api.create(**kwargs)
            _record_usage(resp.model or model, task, resp.usage)
            text = _text_of(resp)
            _local.backend = "claude"
            return _extract_json(text) if json_mode else text
        except LLMRefusal:
            raise
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.APIConnectionError, anthropic.RateLimitError,
                anthropic.InternalServerError) as e:
            if cfg["provider"] == "claude" or not cfg["ollama_fallback"]:
                raise LLMError(f"Claude call failed: {e}") from e
            print(f"[llm] Claude unavailable ({type(e).__name__}) - "
                  f"falling back to Ollama", flush=True)
    text = _ollama_chat(_with_images(messages, images, False), system, json_mode,
                        ollama_max_tokens or max_tokens
                        or _task(cfg, task).get("max_tokens"), timeout,
                        url=ollama_url, model=ollama_model)
    _local.backend = "ollama"
    return _extract_json(text) if json_mode else text


def stream_chat(messages: list, *, system: Optional[str] = None,
                task: str = "chat", max_tokens: Optional[int] = None,
                timeout: float = 600) -> Iterator[str]:
    """Yield reply text as it arrives (voice loop / dashboard streaming)."""
    import anthropic
    cfg = config()
    if backend(cfg) == "claude":
        model, use_beta, kwargs = _claude_kwargs(cfg, task, messages, system,
                                                  False, max_tokens)
        client = _anthropic().with_options(timeout=timeout)
        api = client.beta.messages if use_beta else client.messages
        started = False
        try:
            with api.stream(**kwargs) as s:
                for piece in s.text_stream:
                    started = True
                    yield piece
                resp = s.get_final_message()
            _record_usage(resp.model or model, task, resp.usage)
            if resp.stop_reason == "refusal":
                raise LLMRefusal("Claude declined the request")
            return
        except LLMRefusal:
            raise
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.APIConnectionError, anthropic.RateLimitError,
                anthropic.InternalServerError) as e:
            # Once text has been spoken we can't restart on another model.
            if started or cfg["provider"] == "claude" or not cfg["ollama_fallback"]:
                raise LLMError(f"Claude stream failed: {e}") from e
            print(f"[llm] Claude unavailable ({type(e).__name__}) - "
                  f"falling back to Ollama", flush=True)
    yield from _ollama_stream(messages, system,
                              max_tokens or _task(cfg, task).get("max_tokens"), timeout)


def status() -> dict:
    cfg = config()
    try:
        live = backend(cfg)
    except LLMError as e:
        live = f"error: {e}"
    return {
        "backend": live,
        "provider": cfg["provider"],
        "api_key": "present" if api_key() else "missing",
        "spent_today_usd": round(spent_today(), 4),
        "daily_budget_usd": cfg["daily_budget_usd"],
        "ollama_model": _ollama_model(),
        "tasks": {k: f"{v['model']} / effort {v.get('effort')}"
                  for k, v in cfg["tasks"].items()},
    }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "ping":
        print(json.dumps(status(), indent=2))
        out = chat([{"role": "user", "content": 'Reply with {"ok": true}'}],
                   task="classify", json_mode=True, max_tokens=64)
        print("reply:", out)
        print("spent today: $%.6f" % spent_today())
    else:
        print(__doc__)
        sys.exit(2)
