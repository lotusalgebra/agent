"""
Gemini / NB2 image-generation via the OFFICIAL google-genai API.

Replacement for gemini.py (OCR + pyautogui + Chrome) — same public surface
so the pipeline can call it without changes:

    create_image_and_download(prompt: str, save_to: str) -> Optional[str]

Why this exists: Chrome-automation was failing every render (Tools anchor
OCR misses, stuck panels, 90s timeouts on 2 KB editorial prompts).
Switching to the API is reliable, faster, and handles prompts of any
length directly.

Setup:
  - API key(s) required. Get one at https://aistudio.google.com/apikey
  - Store at ~/.lotus_auth/gemini_api_key.txt — ONE KEY PER LINE.
    Multiple keys are rotated round-robin; on a rate-limit error the
    failed key is penalised and the next one is tried automatically.
    3 Pro accounts → drop 3 keys in the file, get 3× daily quota.
  - Or export GEMINI_API_KEY / GOOGLE_API_KEY in the environment.

Model: gemini-3.1-flash-image-preview (NB2). Configurable via
LOTUS_GEMINI_IMAGE_MODEL env var.
"""

from __future__ import annotations

import os
import time
import base64
from pathlib import Path
from typing import Optional

_KEY_PATH = Path(os.path.expanduser("~/.lotus_auth/gemini_api_key.txt"))

# Default to NB2 (Gemini 3.1 Flash Image). Falls back to NB1 on error.
MODEL = os.environ.get("LOTUS_GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image-preview")

# Module-level rotation state: the next index + per-key penalty cooldowns.
# _penalty[key] = unix timestamp until which the key is skipped.
_rotation_idx: int = 0
_penalty: dict = {}


# ── Availability / setup ─────────────────────────────────────────────

def _resolve_api_keys() -> list:
    """Return ALL configured API keys in priority order.

    - Env vars (GEMINI_API_KEY, GOOGLE_API_KEY, GOOGLE_GENAI_API_KEY) first
    - Then every non-blank, non-comment line from _KEY_PATH
    - De-duplicated while preserving order.
    """
    keys: list = []
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"):
        val = os.environ.get(var, "").strip()
        if val and val not in keys:
            keys.append(val)
    try:
        if _KEY_PATH.exists():
            for line in _KEY_PATH.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"): continue
                if line not in keys:
                    keys.append(line)
    except Exception:
        pass
    return keys


def _resolve_api_key() -> Optional[str]:
    """Back-compat single-key resolver — returns the first configured."""
    keys = _resolve_api_keys()
    return keys[0] if keys else None


def _pick_key(keys: list) -> Optional[str]:
    """Round-robin pick, skipping any key under penalty. Returns None if
    every key is penalised."""
    global _rotation_idx
    if not keys: return None
    now = time.time()
    n = len(keys)
    # Try each key at most once starting at _rotation_idx.
    for _ in range(n):
        idx = _rotation_idx % n
        _rotation_idx = (_rotation_idx + 1) % n
        key = keys[idx]
        if _penalty.get(key, 0) <= now:
            return key
    # Every key is penalised — return the one whose penalty expires soonest.
    soonest = min(keys, key=lambda k: _penalty.get(k, 0))
    return soonest


def _penalise(key: str, seconds: int = 60) -> None:
    """Mark a key as cooling-off for `seconds` (e.g. after a 429)."""
    _penalty[key] = time.time() + seconds


def is_available(verbose: bool = False) -> bool:
    """Quick probe — is the SDK installed AND at least one API key found?"""
    try:
        import google.genai  # noqa: F401
    except Exception as e:
        if verbose: print(f"[gemini-api] google-genai SDK missing: {e}")
        return False
    if not _resolve_api_keys():
        if verbose:
            print(f"[gemini-api] No API keys. Set GEMINI_API_KEY or drop keys at {_KEY_PATH}")
        return False
    return True


def status() -> dict:
    """Diagnostic summary for the dashboard."""
    sdk_ok = False
    try:
        import google.genai  # noqa
        sdk_ok = True
    except Exception:
        pass
    keys = _resolve_api_keys()
    now = time.time()
    return {
        "sdk_installed":  sdk_ok,
        "api_keys_found": len(keys),
        "keys_active":    sum(1 for k in keys if _penalty.get(k, 0) <= now),
        "key_source":     _key_source() if keys else None,
        "model":          MODEL,
        "key_previews":   [(k[:6] + "…" + k[-4:]) if len(k) > 14 else "(short)" for k in keys],
    }


def _key_source() -> str:
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"):
        if os.environ.get(var):
            return f"env:{var}"
    if _KEY_PATH.exists():
        return f"file:{_KEY_PATH}"
    return "unknown"


def save_api_key(key: str, append: bool = False) -> None:
    """Persist key(s) under ~/.lotus_auth/. `append=True` adds without
    overwriting existing keys — useful when enrolling multiple Pro accounts."""
    key = (key or "").strip()
    if not key:
        raise ValueError("API key is empty")
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if append and _KEY_PATH.exists():
        existing = _KEY_PATH.read_text()
        if key in existing:
            return
        if not existing.endswith("\n"): existing += "\n"
        _KEY_PATH.write_text(existing + key + "\n")
    else:
        _KEY_PATH.write_text(key + "\n")
    try:
        os.chmod(_KEY_PATH, 0o600)
    except OSError:
        pass


def remove_api_key(preview_or_key: str) -> int:
    """Remove keys matching the given full key OR its `AIzaSy…last4` preview.
    Returns the number of keys removed."""
    target = (preview_or_key or "").strip()
    if not target:
        return 0
    if not _KEY_PATH.exists():
        return 0
    removed = 0
    kept_lines: list = []
    for line in _KEY_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept_lines.append(line); continue
        match = (stripped == target) or (
            len(stripped) > 14 and
            target.startswith(stripped[:6]) and target.endswith(stripped[-4:])
        )
        if match:
            removed += 1
            # Also clear any penalty cooldown we had on it.
            _penalty.pop(stripped, None)
            continue
        kept_lines.append(line)
    if removed:
        _KEY_PATH.write_text("\n".join(kept_lines) + "\n")
        try: os.chmod(_KEY_PATH, 0o600)
        except OSError: pass
    return removed


def test_key(key: str, timeout: float = 20.0) -> dict:
    """Fire a tiny text-only call to confirm the key is live + has API
    access. Cheap: ~1c or free-tier, no image generated."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "empty key"}
    try:
        from google import genai
    except Exception as e:
        return {"ok": False, "error": f"SDK missing: {e}"}
    try:
        client = genai.Client(api_key=key)
        r = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=["ping"],
        )
        text = (getattr(r, "text", "") or "").strip()
        return {"ok": True, "model": "gemini-2.5-flash",
                "reply_preview": text[:60] or "(empty)"}
    except Exception as e:
        msg = str(e)
        kind = "rate_limit" if any(w in msg.lower() for w in ("429","quota","exceed")) else \
               "auth"       if any(w in msg.lower() for w in ("401","403","key","permission","invalid")) else \
               "network"    if any(w in msg.lower() for w in ("connect","network","timeout","dns")) else "other"
        return {"ok": False, "error": msg[:200], "kind": kind}


# ── Image generation ─────────────────────────────────────────────────

_aspect_map = {
    # Instagram carousels / stories
    "4:5":   "4:5",
    "1:1":   "1:1",
    "9:16":  "9:16",
    "16:9":  "16:9",
    "3:4":   "3:4",
    "4:3":   "4:3",
}

def _detect_aspect(prompt: str) -> str:
    """Sniff an aspect-ratio hint from the prompt. Defaults to 4:5
    (Instagram carousel, which is what this user generates)."""
    low = (prompt or "").lower()
    for key in _aspect_map:
        if key in low:
            return key
    if "1080 by 1350" in low or "portrait" in low: return "4:5"
    if "1080 by 1920" in low or "story" in low:    return "9:16"
    if "square" in low and "instagram" in low:     return "1:1"
    return "4:5"


def create_image_and_download(prompt: str, save_to: str,
                              wait_timeout: Optional[float] = None,
                              verbose: bool = True) -> Optional[str]:
    """Generate one image and save it to `save_to`. Returns the saved path
    on success, None on failure.

    Compatible signature with gemini.create_image_and_download so the
    pipeline loop doesn't need changes.
    """
    if not prompt or not prompt.strip():
        if verbose: print("[gemini-api] Empty prompt.")
        return None
    keys = _resolve_api_keys()
    if not keys:
        if verbose:
            print(f"[gemini-api] No API keys — set GEMINI_API_KEY or "
                  f"save to {_KEY_PATH} (one per line)")
        return None
    try:
        from google import genai
        from google.genai import types as gt
    except Exception as e:
        if verbose: print(f"[gemini-api] SDK import failed: {e}")
        return None

    wait_timeout = wait_timeout or 180.0
    aspect = _detect_aspect(prompt)

    # Try each key until one succeeds (or all are exhausted for this prompt).
    # On rate-limit (429 / RESOURCE_EXHAUSTED) penalise the key for 60s and
    # rotate to the next. On model-access errors, fall back to NB1 once.
    resp = None
    tried = 0
    max_tries = len(keys) * 2   # every key, both models at worst
    fallback_used = False
    t0 = time.time()
    while resp is None and tried < max_tries:
        key = _pick_key(keys)
        if not key: break
        tried += 1
        client = genai.Client(api_key=key)
        model_id = "gemini-2.5-flash-image" if fallback_used else MODEL
        if verbose:
            print(f"[gemini-api] try#{tried} model={model_id} key={key[:6]}…{key[-4:]} "
                  f"aspect={aspect} prompt={len(prompt)} chars")
        try:
            resp = client.models.generate_content(
                model=model_id,
                contents=[prompt],
                config=gt.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=gt.ImageConfig(aspect_ratio=aspect),
                ),
            )
            break
        except Exception as e:
            msg = str(e).lower()
            is_429        = "429" in msg or "resource_exhausted" in msg
            is_per_minute = any(w in msg for w in ("requests per minute", "rate limit", "rpm", "try again"))
            is_plan_quota = any(w in msg for w in ("plan and billing", "current quota", "billing details", "upgrade"))
            is_model_access = any(w in msg for w in (
                "not found", "permission", "not enabled",
                "does not have access", "404",
            ))
            if verbose:
                print(f"[gemini-api]   FAIL ({type(e).__name__}): {str(e)[:220]}")
            # Plan-level quota exceeded (e.g. free-tier account hitting a
            # paid-only model) → the key itself is fine, just switch model.
            # Do NOT penalise the key since it'll work for the fallback.
            if (is_429 and is_plan_quota and not fallback_used) or \
               (is_model_access and not fallback_used):
                if verbose:
                    print(f"[gemini-api]   model {model_id} unavailable on this plan — "
                          f"falling back to gemini-2.5-flash-image")
                fallback_used = True
                resp = None
                continue
            # Per-minute rate limit → penalise this key, rotate to next.
            if is_429 and is_per_minute:
                _penalise(key, seconds=60)
                resp = None
                continue
            # Ambiguous 429 (not clearly per-minute nor clearly plan-level)
            # → try the other keys, then the fallback model, before giving up.
            if is_429:
                _penalise(key, seconds=30)
                # If every key has been tried once on this model and we
                # haven't tried the fallback yet, switch model now.
                if tried >= len(keys) and not fallback_used:
                    fallback_used = True
                    # Clear penalties so fallback model gets fresh shots.
                    for k in keys:
                        _penalty.pop(k, None)
                resp = None
                continue
            # Unknown error — rotate keys anyway.
            _penalise(key, seconds=15)
            resp = None
            continue

    if resp is None:
        if verbose:
            print(f"[gemini-api] all {len(keys)} key(s) exhausted for this prompt")
        return None

    elapsed = time.time() - t0
    if verbose:
        print(f"[gemini-api] response in {elapsed:.1f}s")

    # Pull the first image part out of the response.
    image_bytes: Optional[bytes] = None
    try:
        for cand in (getattr(resp, "candidates", []) or []):
            content = getattr(cand, "content", None)
            if not content: continue
            for part in (getattr(content, "parts", []) or []):
                inline = getattr(part, "inline_data", None)
                if inline and getattr(inline, "data", None):
                    raw = inline.data
                    # SDK sometimes gives bytes, sometimes base64 str.
                    if isinstance(raw, str):
                        try:
                            raw = base64.b64decode(raw)
                        except Exception:
                            pass
                    if isinstance(raw, (bytes, bytearray)) and len(raw) > 1000:
                        image_bytes = bytes(raw)
                        break
            if image_bytes: break
    except Exception as e:
        if verbose: print(f"[gemini-api] response-parse failed: {e}")
        return None

    if not image_bytes:
        if verbose:
            # If Gemini refused / safety-blocked, the response has text.
            txt = ""
            try:
                txt = getattr(resp, "text", "") or ""
            except Exception:
                pass
            print(f"[gemini-api] no image returned. Response text: {txt[:240]!r}")
        return None

    # Write atomically.
    dst = Path(save_to)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    tmp.write_bytes(image_bytes)
    tmp.replace(dst)
    if verbose:
        print(f"[gemini-api] saved {dst} ({len(image_bytes)//1024} KB)")
    return str(dst)


if __name__ == "__main__":
    import sys
    print("=== status ===")
    import json as _json
    print(_json.dumps(status(), indent=2))
    if len(sys.argv) >= 3:
        out = create_image_and_download(sys.argv[1], sys.argv[2])
        print(f"\nresult: {out}")
