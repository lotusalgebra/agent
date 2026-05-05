"""
Voice Engine — STT (Whisper/Google) + TTS (edge-tts/pyttsx3)
"""

import io
import os
import sys
import wave
import tempfile
import threading

# Lazy imports — graceful fallback
speech_recognition = None
pyttsx3 = None
edge_tts = None


def _import_stt():
    global speech_recognition
    try:
        import speech_recognition as sr
        speech_recognition = sr
    except ImportError:
        print("⚠️  Install: pip install SpeechRecognition pyaudio")
        print("   On Windows: pip install pipwin && pipwin install pyaudio")
        sys.exit(1)


def _import_tts():
    global edge_tts, pyttsx3
    try:
        import edge_tts as et
        edge_tts = et
    except ImportError:
        try:
            import pyttsx3 as p3
            pyttsx3 = p3
        except ImportError:
            print("⚠️  Install TTS: pip install edge-tts  (or)  pip install pyttsx3")
            sys.exit(1)


class VoiceEngine:
    def __init__(self, tts_voice: str = "en-IN-PrabhatNeural"):
        """
        TTS voices (edge-tts):
          en-IN-PrabhatNeural  — Indian English male
          en-IN-NeerjaNeural   — Indian English female
          en-US-GuyNeural      — American male
          en-GB-RyanNeural     — British male
        """
        _import_stt()
        _import_tts()

        self.recognizer = speech_recognition.Recognizer()
        self.microphone = speech_recognition.Microphone()
        self.tts_voice = tts_voice
        self._tts_engine = None

        # Calibrate mic — guarded by a timeout because PyAudio's
        # Microphone.__enter__ hangs indefinitely on macOS when the
        # launching app lacks Microphone permission (TCC silently blocks
        # the open instead of raising). Without this guard the agent
        # appears to "freeze at calibration" with no actionable error.
        print("🎙️  Calibrating microphone...")
        _cal_err: list = [None]

        def _calibrate():
            try:
                with self.microphone as source:
                    self.recognizer.adjust_for_ambient_noise(
                        source, duration=2
                    )
            except Exception as e:
                _cal_err[0] = e

        t = threading.Thread(target=_calibrate, daemon=True)
        t.start()
        t.join(timeout=10.0)
        if t.is_alive():
            if sys.platform == "darwin":
                hint = (
                    "macOS Microphone permission is missing for the app "
                    "that launched LOTUS. Open System Settings → Privacy "
                    "& Security → Microphone and enable it for your "
                    "terminal (Terminal.app / iTerm). LOTUS spawned from "
                    "a non-terminal parent (e.g. an IDE or Claude Code) "
                    "inherits that parent's permission, not Python's."
                )
            elif sys.platform == "win32":
                hint = (
                    "Windows: Settings → Privacy → Microphone → allow "
                    "desktop apps to access the microphone."
                )
            else:
                hint = (
                    "Ensure the user running LOTUS has access to the "
                    "ALSA/PulseAudio mic device."
                )
            raise RuntimeError(
                "Microphone open hung for 10s — likely a permission "
                "denial. " + hint
            )
        if _cal_err[0] is not None:
            raise _cal_err[0]
        print("✅  Microphone ready\n")

        # Init offline TTS fallback
        if pyttsx3 and not edge_tts:
            self._tts_engine = pyttsx3.init()
            self._tts_engine.setProperty("rate", 175)

    def listen(self, timeout: float = None) -> str | None:
        """
        Listen for speech and return transcribed text.
        Returns None on timeout or recognition failure.
        """
        try:
            with self.microphone as source:
                audio = self.recognizer.listen(
                    source,
                    timeout=timeout,
                    phrase_time_limit=15
                )

            # Try Whisper first (offline, more accurate), fall back to Google
            try:
                text = self.recognizer.recognize_whisper(
                    audio, model="base", language="english"
                )
            except Exception:
                text = self.recognizer.recognize_google(audio)

            return text.strip() if text else None

        except speech_recognition.WaitTimeoutError:
            return None
        except speech_recognition.UnknownValueError:
            return None
        except Exception as e:
            print(f"  ⚠️  Listen error: {e}")
            return None

    def speak(self, text: str):
        """Convert text to speech and play it."""
        if not text:
            return

        # Clean text for speech
        clean = text.replace("*", "").replace("#", "").replace("`", "")
        clean = clean[:500]  # Limit length

        if edge_tts:
            self._speak_edge_tts(clean)
        elif self._tts_engine:
            self._speak_pyttsx3(clean)
        else:
            print(f"  🔊 [TTS unavailable]: {clean}")

    def _speak_edge_tts(self, text: str):
        """High-quality TTS via edge-tts (Microsoft)."""
        import asyncio
        import subprocess

        async def _generate():
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.close()
            communicate = edge_tts.Communicate(text, self.tts_voice)
            await communicate.save(tmp.name)
            return tmp.name

        try:
            # Run async in sync context
            loop = asyncio.new_event_loop()
            mp3_path = loop.run_until_complete(_generate())
            loop.close()

            # Play audio
            if sys.platform == "win32":
                # Windows — use ffplay or powershell
                try:
                    subprocess.run(
                        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", mp3_path],
                        check=True
                    )
                except FileNotFoundError:
                    subprocess.run(
                        ["powershell", "-c",
                         f"(New-Object Media.SoundPlayer '{mp3_path}').PlaySync()"],
                        check=True
                    )
            elif sys.platform == "darwin":
                subprocess.run(["afplay", mp3_path])
            else:
                subprocess.run(["mpv", "--no-video", mp3_path],
                               capture_output=True)

            os.unlink(mp3_path)

        except Exception as e:
            print(f"  ⚠️  TTS error: {e}")
            # Fallback
            if self._tts_engine:
                self._speak_pyttsx3(text)

    def _speak_pyttsx3(self, text: str):
        """Offline TTS fallback."""
        try:
            self._tts_engine.say(text)
            self._tts_engine.runAndWait()
        except Exception as e:
            print(f"  🔊 [Speech]: {text}")


class VoiceEngineAsync:
    """
    Async version for non-blocking voice in GUI apps.
    Wraps VoiceEngine with threading.
    """

    def __init__(self, **kwargs):
        self.engine = VoiceEngine(**kwargs)
        self._listening = False

    def listen_async(self, callback):
        """Listen in background thread, call callback(text) on result."""
        def _worker():
            self._listening = True
            while self._listening:
                text = self.engine.listen(timeout=5)
                if text:
                    callback(text)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def stop_listening(self):
        self._listening = False

    def speak_async(self, text: str):
        """Speak in background thread."""
        thread = threading.Thread(
            target=self.engine.speak, args=(text,), daemon=True
        )
        thread.start()
