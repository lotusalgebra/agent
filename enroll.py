"""
Hands-free voice enrollment for LOTUS Agent.

Differs from auth.py::enroll():
- Countdown instead of input() prompts (safe to run non-interactively)
- Mic stream opened ONCE (works around a PyAudio/PortAudio segfault on macOS
  that was triggered by repeatedly re-opening the input stream in a loop)
- Voiceprint saved after every successful capture, so a mid-loop crash
  preserves the samples collected so far

Usage:  python enroll.py [name]   (defaults to "Somendra")
"""

import sys
import time
from datetime import datetime

import numpy as np
import speech_recognition as sr

from auth import (
    VoiceAuth,
    _init_engine,
    _get_embedding_from_audio_data,
    ENROLLMENT_PHRASES,
)


def enroll_hands_free(name: str = "Somendra", warmup_seconds: int = 3) -> float:
    _init_engine()
    auth = VoiceAuth()
    auth.voiceprints.pop(name, None)
    auth.config.pop(name, None)

    recognizer = sr.Recognizer()
    mic = sr.Microphone()

    print(f"\n=== Hands-free enrollment: {name} ===\n", flush=True)

    embeddings: list = []

    with mic as source:
        print("Calibrating mic (2 s)...", flush=True)
        recognizer.adjust_for_ambient_noise(source, duration=2)
        recognizer.dynamic_energy_threshold = False
        print("Ready.", flush=True)

        for i, phrase in enumerate(ENROLLMENT_PHRASES, 1):
            print(f"\n[{i}/{len(ENROLLMENT_PHRASES)}] Phrase:  \"{phrase}\"", flush=True)
            for n in range(warmup_seconds, 0, -1):
                print(f"   speak in {n}...", flush=True)
                time.sleep(1)
            print("   RECORDING NOW", flush=True)

            try:
                audio = recognizer.listen(source, timeout=8, phrase_time_limit=8)
                emb = _get_embedding_from_audio_data(audio)
                embeddings.append(emb)
                print(f"   captured sample {i}", flush=True)
            except sr.WaitTimeoutError:
                print(f"   TIMEOUT — no speech detected for sample {i}", flush=True)
                continue
            except Exception as e:
                print(f"   FAILED sample {i}: {type(e).__name__}: {e}", flush=True)
                continue

            # Persist after each successful capture
            auth.voiceprints[name] = np.array(embeddings)
            auth.config[name] = {
                "enrolled_at": datetime.now().isoformat(),
                "samples": len(embeddings),
                "role": "admin",
            }
            auth._save()
            print(f"   saved ({len(embeddings)} sample(s) on disk)", flush=True)

            time.sleep(0.5)

    if len(embeddings) < 3:
        print(f"\nOnly {len(embeddings)} usable samples — need at least 3.", flush=True)
        return 0.0

    avg = auth._self_similarity(name)
    print(f"\nFinal: {len(embeddings)} samples, consistency {avg:.3f}", flush=True)
    return avg


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "Somendra"
    score = enroll_hands_free(name)
    if score >= 0.70:
        print("Good consistency. Ready for verification.")
    elif score > 0:
        print("Below 0.70 — consider quieter room, closer mic, or re-run.")
    else:
        sys.exit(1)
