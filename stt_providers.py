"""
Speech-to-text providers
------------------------
One adapter per transcription service, all with the same shape:

    provider.unavailable() -> str | None   # why it can't be used right now, or None
    provider.transcribe(wav_bytes) -> str  # "" if nothing was said; raises on failure

transcribe() tries them in the order of config.STT_ORDER (a dashboard
setting): a failure moves on to the next one,
an empty transcript is an answer ("they said nothing") and stops there.
Used by the mic listener and the dashboard's dictate button alike.
Each provider's health is logged to the dashboard once when it flips.
"""

import io
import os
import tempfile
import threading
import time

import config

TEST_WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "test-phrase.wav")


def _log(text: str):
    print(f"[stt] {text}")
    try:
        from web_dashboard import log_event
        log_event("SYSTEM", text)
    except Exception:
        pass


class FasterWhisperSTT:
    name, label = "faster-whisper", "faster-whisper server"

    def unavailable(self):
        return None if config.WHISPER_SERVER_URL else "no server URL"

    def transcribe(self, wav: bytes) -> str:
        import requests
        r = requests.post(f"{config.WHISPER_SERVER_URL}/transcribe",
                          files={"file": ("audio.wav", io.BytesIO(wav), "audio/wav")}, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.json().get("text", "").strip()


class OpenAISTT:
    name, label = "openai", "OpenAI"

    def unavailable(self):
        return None if config.OPENAI_API_KEY else "no OpenAI API key"

    def transcribe(self, wav: bytes) -> str:
        import requests
        r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                          headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                          files={"file": ("audio.wav", io.BytesIO(wav), "audio/wav")},
                          data={"model": config.OPENAI_STT_MODEL, "language": "en"}, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        return r.json().get("text", "").strip()


_whisper_model = None
_whisper_lock = threading.Lock()


def _local_model():
    """Load the on-Pi Whisper model once (WHISPER_MODEL needs a restart to change)."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            import whisper
            print(f"[stt] Loading local Whisper model '{config.WHISPER_MODEL}'...")
            _whisper_model = whisper.load_model(config.WHISPER_MODEL)
            print("[stt] Local Whisper model loaded")
    return _whisper_model


def prewarm_local():
    """Load local Whisper in the background so the offline fallback is instant."""
    def _go():
        try:
            _local_model()
        except ImportError:
            print("[stt] openai-whisper not installed — local transcription unavailable")
        except Exception as e:
            print(f"[stt] Failed to load local Whisper: {e}")
    threading.Thread(target=_go, daemon=True).start()


class LocalWhisperSTT:
    name, label = "local", "Whisper on the Pi"

    def unavailable(self):
        try:
            import whisper  # noqa: F401
        except ImportError:
            return "openai-whisper isn't installed"
        return None

    def transcribe(self, wav: bytes) -> str:
        model = _local_model()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav)
            path = tmp.name
        try:
            return model.transcribe(path, language="en", fp16=False).get("text", "").strip()
        finally:
            os.unlink(path)


PROVIDERS = {p.name: p for p in (FasterWhisperSTT(), OpenAISTT(), LocalWhisperSTT())}
_healthy = {name: True for name in PROVIDERS}


def order() -> list:
    return [n for n in config.STT_ORDER if n in PROVIDERS]


def transcribe(wav: bytes) -> str | None:
    """Run the cascade. Returns the text, or None if nothing was understood."""
    for name in order():
        p = PROVIDERS[name]
        why = p.unavailable()
        if why:
            continue
        t0 = time.time()
        try:
            text = p.transcribe(wav)
        except Exception as e:
            if _healthy[name]:
                _healthy[name] = False
                _log(f"Speech-to-text: {p.label} failed ({type(e).__name__}: {str(e)[:80]}) — trying the next one")
            else:
                print(f"[stt] {p.label} failed: {e}")
            continue
        if not _healthy[name]:
            _healthy[name] = True
            _log(f"Speech-to-text: {p.label} recovered")
        print(f"[stt] {p.label} ({time.time() - t0:.1f}s): \"{text}\"")
        return text or None
    print("[stt] No speech-to-text provider worked")
    return None


def test(name: str) -> str:
    """Transcribe the bundled test phrase (Settings page Test button)."""
    p = PROVIDERS[name]
    why = p.unavailable()
    if why:
        raise ValueError(why)
    with open(TEST_WAV, "rb") as f:
        wav = f.read()
    t0 = time.time()
    text = p.transcribe(wav)
    return f"{p.label} heard “{text}” in {time.time() - t0:.1f}s"
