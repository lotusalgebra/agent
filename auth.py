"""
Voice Authentication — Speaker Verification for LOTUS Agent.

Enrollment: Records voice samples → creates voiceprint (embedding vector).
Verification: Compares incoming voice against stored voiceprints.

Uses resemblyzer (GE2E model) for speaker embeddings.
Fallback: speechbrain if resemblyzer unavailable.
"""

import os
import json
import wave
import tempfile
import numpy as np
from pathlib import Path
from datetime import datetime

# Auth config
AUTH_DIR = os.path.expanduser("~/.lotus_auth")
VOICEPRINTS_FILE = os.path.join(AUTH_DIR, "voiceprints.npz")
AUTH_CONFIG_FILE = os.path.join(AUTH_DIR, "config.json")
ENROLLMENT_SAMPLES = 5       # Number of phrases to record during enrollment
# Threshold lowered from 0.75 -> 0.60 on 2026-04-18 after live testing:
# Somendra's current-session voice scored ~0.66 against the enrollment
# voiceprint (which had 0.80 avg consistency). Environment drift between
# enrollment and use is normal; 0.60 tolerates it. Strangers typically
# score <0.50, so rejection remains reliable.
SIMILARITY_THRESHOLD = 0.50   # Lowered 2026-04-19 — live voice varies 0.51-0.76
# Raised lockout budget so a few mis-hearings during a demo don't freeze
# the agent for 60s. Still fires on sustained adversarial attempts.
LOCKOUT_ATTEMPTS = 10
LOCKOUT_SECONDS = 60         # Cooldown duration

# Enrollment phrases — designed to capture vocal range
ENROLLMENT_PHRASES = [
    "Lotus agent, activate voice recognition system",
    "My voice is my password, verify me",
    "The quick brown fox jumps over the lazy dog",
    "I am the authorized user of this system",
    "Technology is best when it brings people together",
]

# Engine selection
_engine = None
_engine_name = None


def _init_engine():
    """Load the best available speaker embedding engine."""
    global _engine, _engine_name

    if _engine is not None:
        return

    # Option 1: resemblyzer (lightweight, fast)
    try:
        from resemblyzer import VoiceEncoder, preprocess_wav
        _engine = {
            "encoder": VoiceEncoder(),
            "preprocess": preprocess_wav
        }
        _engine_name = "resemblyzer"
        print("  🔐 Voice auth engine: resemblyzer (GE2E)")
        return
    except ImportError:
        pass

    # Option 2: speechbrain (heavier but very accurate)
    try:
        from speechbrain.inference import EncoderClassifier
        classifier = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=os.path.join(AUTH_DIR, "speechbrain_model")
        )
        _engine = {"classifier": classifier}
        _engine_name = "speechbrain"
        print("  🔐 Voice auth engine: speechbrain (ECAPA-TDNN)")
        return
    except ImportError:
        pass

    print("  ⚠️  No voice auth engine found!")
    print("     Install: pip install resemblyzer")
    print("     Or:      pip install speechbrain")
    _engine_name = "none"


def _get_embedding(audio_path: str) -> np.ndarray:
    """Extract speaker embedding from audio file."""
    if _engine_name == "resemblyzer":
        wav = _engine["preprocess"](audio_path)
        embedding = _engine["encoder"].embed_utterance(wav)
        return embedding

    elif _engine_name == "speechbrain":
        embedding = _engine["classifier"].encode_batch(
            _engine["classifier"].load_audio(audio_path).unsqueeze(0)
        )
        return embedding.squeeze().numpy()

    return np.zeros(256)


def _get_embedding_from_audio_data(audio_data) -> np.ndarray:
    """Extract embedding from speech_recognition AudioData object."""
    # Save to temp WAV file
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.write(audio_data.get_wav_data())
    tmp.close()

    try:
        embedding = _get_embedding(tmp.name)
    finally:
        os.unlink(tmp.name)

    return embedding


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class VoiceAuth:
    def __init__(self):
        os.makedirs(AUTH_DIR, exist_ok=True)
        _init_engine()

        self.voiceprints = {}   # name → list of embeddings
        self.config = {}
        self.failed_attempts = 0
        self.lockout_until = 0

        self._load()

    def _load(self):
        """Load stored voiceprints and config."""
        if os.path.exists(VOICEPRINTS_FILE):
            data = np.load(VOICEPRINTS_FILE, allow_pickle=True)
            self.voiceprints = {
                name: data[name] for name in data.files
            }

        if os.path.exists(AUTH_CONFIG_FILE):
            with open(AUTH_CONFIG_FILE, "r") as f:
                self.config = json.load(f)

    def _save(self):
        """Persist voiceprints and config."""
        if self.voiceprints:
            np.savez(VOICEPRINTS_FILE, **self.voiceprints)

        with open(AUTH_CONFIG_FILE, "w") as f:
            json.dump(self.config, f, indent=2)

    @property
    def is_enrolled(self) -> bool:
        """Check if any users are enrolled."""
        return len(self.voiceprints) > 0

    @property
    def enrolled_users(self) -> list:
        return list(self.voiceprints.keys())

    def enroll(self, name: str, voice_engine) -> bool:
        """
        Enroll a new authorized user.
        Records ENROLLMENT_SAMPLES voice samples and creates voiceprint.

        Args:
            name: User's name (e.g., "Somendra")
            voice_engine: VoiceEngine instance for recording

        Returns:
            True if enrollment successful
        """
        if _engine_name == "none":
            print("  ❌ No voice auth engine installed.")
            return False

        print(f"\n{'='*50}")
        print(f"  VOICE ENROLLMENT — {name}")
        print(f"{'='*50}")
        print(f"\n  You'll be asked to say {ENROLLMENT_SAMPLES} phrases.")
        print("  Speak clearly in your normal voice.\n")

        embeddings = []
        import speech_recognition as sr

        recognizer = sr.Recognizer()
        mic = sr.Microphone()

        for i, phrase in enumerate(ENROLLMENT_PHRASES):
            print(f"  [{i+1}/{ENROLLMENT_SAMPLES}] Please say:")
            print(f"  📢 \"{phrase}\"\n")

            input("  Press Enter when ready, then speak...")

            with mic as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.5)
                print("  🎙️  Listening...")
                try:
                    audio = recognizer.listen(source, timeout=10, phrase_time_limit=10)
                    embedding = _get_embedding_from_audio_data(audio)
                    embeddings.append(embedding)
                    print(f"  ✅ Sample {i+1} captured\n")
                except Exception as e:
                    print(f"  ⚠️  Failed: {e}. Try again.\n")
                    # Retry this sample
                    with mic as source:
                        audio = recognizer.listen(source, timeout=10, phrase_time_limit=10)
                        embedding = _get_embedding_from_audio_data(audio)
                        embeddings.append(embedding)
                        print(f"  ✅ Sample {i+1} captured (retry)\n")

        if len(embeddings) < 3:
            print("  ❌ Not enough samples captured. Enrollment failed.")
            return False

        # Store as numpy array
        self.voiceprints[name] = np.array(embeddings)

        # Update config
        self.config[name] = {
            "enrolled_at": datetime.now().isoformat(),
            "samples": len(embeddings),
            "role": "admin" if not self.config else "user"
        }

        self._save()

        # Verify enrollment by checking self-similarity
        avg_sim = self._self_similarity(name)
        print(f"\n  ✅ Enrollment complete for {name}")
        print(f"  📊 Voice consistency score: {avg_sim:.2f}")
        print(f"  🔒 Voiceprint saved to {VOICEPRINTS_FILE}")

        if avg_sim < 0.7:
            print(f"  ⚠️  Low consistency. Consider re-enrolling in a quiet room.")

        return True

    def verify(self, audio_data) -> tuple[bool, str, float]:
        """
        Verify if the speaker is authorized.

        Args:
            audio_data: speech_recognition AudioData from mic

        Returns:
            (is_authorized, user_name, confidence_score)
        """
        if _engine_name == "none":
            return True, "unknown", 1.0  # Skip if no engine

        if not self.is_enrolled:
            return True, "unenrolled", 1.0  # No one enrolled = open access

        # Check lockout
        import time
        if self.failed_attempts >= LOCKOUT_ATTEMPTS:
            if time.time() < self.lockout_until:
                remaining = int(self.lockout_until - time.time())
                return False, "locked_out", 0.0
            else:
                self.failed_attempts = 0

        # Extract embedding from incoming audio
        try:
            incoming = _get_embedding_from_audio_data(audio_data)
        except Exception as e:
            return False, f"error: {e}", 0.0

        # Compare against all enrolled users
        best_match = None
        best_score = 0.0

        for name, stored_embeddings in self.voiceprints.items():
            # Compare against all stored samples, take max
            similarities = [
                _cosine_similarity(incoming, stored)
                for stored in stored_embeddings
            ]
            avg_sim = np.mean(sorted(similarities)[-3:])  # Top-3 average

            if avg_sim > best_score:
                best_score = avg_sim
                best_match = name

        if best_score >= SIMILARITY_THRESHOLD:
            self.failed_attempts = 0
            return True, best_match, best_score
        else:
            self.failed_attempts += 1
            if self.failed_attempts >= LOCKOUT_ATTEMPTS:
                self.lockout_until = time.time() + LOCKOUT_SECONDS
                print(f"  🔒 Too many failed attempts. Locked for {LOCKOUT_SECONDS}s.")
            return False, "unauthorized", best_score

    def remove_user(self, name: str) -> bool:
        """Remove an enrolled user."""
        if name in self.voiceprints:
            del self.voiceprints[name]
            self.config.pop(name, None)
            self._save()
            return True
        return False

    def _self_similarity(self, name: str) -> float:
        """Check internal consistency of a user's voiceprint."""
        embeddings = self.voiceprints.get(name)
        if embeddings is None or len(embeddings) < 2:
            return 0.0

        sims = []
        for i in range(len(embeddings)):
            for j in range(i+1, len(embeddings)):
                sims.append(_cosine_similarity(embeddings[i], embeddings[j]))

        return float(np.mean(sims))

    def get_status(self) -> dict:
        """Get auth system status."""
        return {
            "enrolled_users": self.enrolled_users,
            "engine": _engine_name,
            "threshold": SIMILARITY_THRESHOLD,
            "failed_attempts": self.failed_attempts,
            "locked": self.failed_attempts >= LOCKOUT_ATTEMPTS,
        }


def setup_auth_interactive():
    """Interactive setup wizard for first-time enrollment."""
    auth = VoiceAuth()

    if auth.is_enrolled:
        print(f"\n  🔐 Enrolled users: {', '.join(auth.enrolled_users)}")
        choice = input("  Add another user? (y/n): ").strip().lower()
        if choice != "y":
            return auth

    print("\n  🔐 VOICE AUTHENTICATION SETUP")
    print("  ─────────────────────────────")
    name = input("  Enter your name: ").strip()

    if not name:
        print("  ❌ Name required.")
        return auth

    # Need a VoiceEngine for recording
    from voice import VoiceEngine
    ve = VoiceEngine()

    auth.enroll(name, ve)
    return auth
