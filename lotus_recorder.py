"""Simple background audio recorder for LOTUS.

Records from the default input device to WAV files in
`~/.lotus_auth/recordings/`. Starts/stops via `start()` / `stop()`;
`status()` returns whether recording is active and current file path.

Design:
- One active WAV file at a time, rotated every `ROTATE_SECONDS` so a
  long session doesn't turn into one huge 2-hour file that's painful
  to scrub through.
- Uses pyaudio (already a Phase-1 dependency) — blocking reads on a
  dedicated thread, each chunk appended to the active WAV.
- Intentionally verbose events printed to stdout so the mother log
  shows when recording starts/stops/rotates.
- Recording is NEVER hidden from the user — the UI shows a pulsing
  REC badge the whole time. This module is the ENGINE, not a policy
  enforcer; always pair with a visible indicator in the frontend.
"""
from __future__ import annotations

import os
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

_RECORDINGS_DIR = Path(os.path.expanduser("~/.lotus_auth/recordings"))
_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

# 16 kHz mono 16-bit is plenty for voice + keeps files small.
_SAMPLE_RATE = 16_000
_CHANNELS    = 1
_SAMPLE_W    = 2   # int16
_CHUNK       = 1024
_ROTATE_SEC  = 600  # 10-minute files

_state_lock = threading.Lock()
_active     = False
_worker:    Optional[threading.Thread] = None
_stop_ev    = threading.Event()
_cur_file:  Optional[str] = None
_started_at: Optional[float] = None
_total_bytes = 0


def _new_wav_path() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(_RECORDINGS_DIR / f"rec_{ts}.wav")


def _record_loop():
    global _cur_file, _total_bytes
    try:
        import pyaudio
    except ImportError as e:
        print(f"[recorder] pyaudio unavailable ({e}) — recording disabled")
        with _state_lock:
            globals()["_active"] = False
        return

    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(
            format=pa.get_format_from_width(_SAMPLE_W),
            channels=_CHANNELS, rate=_SAMPLE_RATE,
            input=True, frames_per_buffer=_CHUNK,
        )
    except Exception as e:
        print(f"[recorder] could not open mic: {e}")
        pa.terminate()
        with _state_lock:
            globals()["_active"] = False
        return

    print(f"[recorder] recording STARTED @ {_SAMPLE_RATE}Hz mono")
    segment_start = time.time()
    path = _new_wav_path()
    _cur_file = path
    wf = wave.open(path, "wb")
    wf.setnchannels(_CHANNELS)
    wf.setsampwidth(_SAMPLE_W)
    wf.setframerate(_SAMPLE_RATE)
    print(f"[recorder] → {path}")

    try:
        while not _stop_ev.is_set():
            try:
                data = stream.read(_CHUNK, exception_on_overflow=False)
            except Exception as e:
                print(f"[recorder] read err: {e}"); break
            wf.writeframes(data)
            _total_bytes += len(data)

            if time.time() - segment_start >= _ROTATE_SEC:
                # Rotate file so long sessions produce manageable chunks.
                wf.close()
                path = _new_wav_path()
                _cur_file = path
                wf = wave.open(path, "wb")
                wf.setnchannels(_CHANNELS)
                wf.setsampwidth(_SAMPLE_W)
                wf.setframerate(_SAMPLE_RATE)
                segment_start = time.time()
                print(f"[recorder] rotated → {path}")
    finally:
        try: wf.close()
        except Exception: pass
        try: stream.stop_stream(); stream.close()
        except Exception: pass
        pa.terminate()
        with _state_lock:
            globals()["_active"] = False
        print(f"[recorder] recording STOPPED — total {_total_bytes//1024} KB")


def start() -> dict:
    """Begin recording. Idempotent — returns current status either way."""
    global _worker, _started_at, _total_bytes
    with _state_lock:
        if _active:
            return status()
        globals()["_active"] = True
    _stop_ev.clear()
    _started_at = time.time()
    _total_bytes = 0
    _worker = threading.Thread(target=_record_loop, daemon=True)
    _worker.start()
    return status()


def stop() -> dict:
    """End recording. Safe to call when already stopped."""
    with _state_lock:
        if not _active:
            return status()
    _stop_ev.set()
    if _worker is not None:
        _worker.join(timeout=3.0)
    return status()


def status() -> dict:
    elapsed = time.time() - _started_at if (_active and _started_at) else 0.0
    return {
        "active":    _active,
        "file":      _cur_file,
        "elapsed_s": int(elapsed),
        "bytes":     _total_bytes,
        "dir":       str(_RECORDINGS_DIR),
    }


def list_recordings(limit: int = 50) -> list[dict]:
    """Enumerate saved WAV files newest-first."""
    out = []
    for p in sorted(_RECORDINGS_DIR.glob("rec_*.wav"),
                    key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        st = p.stat()
        out.append({
            "name":     p.name,
            "path":     str(p),
            "size":     st.st_size,
            "mtime":    datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    return out
