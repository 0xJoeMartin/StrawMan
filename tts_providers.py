"""
Text-to-speech providers
------------------------
One small adapter per voice, all with the same shape:

    provider.unavailable() -> str | None   # why it can't be used right now, or None
    provider.synthesize(text) -> bytes     # audio in any format ffmpeg decodes; raises on failure

speech.SpeechEngine tries them in the order of config.TTS_ORDER (a dashboard
setting) and plays the first one that works, with loudness normalization and
jaw sync applied the same way to all of them. Settings are read from config on
every call, so dashboard edits apply on the next line.
"""

import os
import shutil
import subprocess
import tempfile
import time

import config

# Default cascade: cloud voices first, local last-resort voices after


class ElevenLabsTTS:
    name, label = "elevenlabs", "ElevenLabs"

    def unavailable(self):
        return None if config.ELEVENLABS_API_KEY else "no ElevenLabs API key"

    def synthesize(self, text: str) -> bytes:
        import requests
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVENLABS_VOICE_ID}",
            headers={"Accept": "audio/mpeg", "Content-Type": "application/json",
                     "xi-api-key": config.ELEVENLABS_API_KEY},
            json={"text": text, "model_id": config.ELEVENLABS_MODEL,
                  "voice_settings": {
                      "stability": config.ELEVENLABS_STABILITY,  # Lower = more dramatic variation
                      "similarity_boost": 0.7,
                      "style": config.ELEVENLABS_STYLE,
                      "use_speaker_boost": True,
                      "speed": config.ELEVENLABS_SPEED,
                  }},
            timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"ElevenLabs HTTP {r.status_code}: {r.text[:150]}")
        return r.content


class OpenAITTS:
    name, label = "openai", "OpenAI"

    def unavailable(self):
        return None if config.OPENAI_API_KEY else "no OpenAI API key"

    def synthesize(self, text: str) -> bytes:
        import requests
        body = {"model": config.OPENAI_TTS_MODEL, "voice": config.OPENAI_TTS_VOICE,
                "input": text, "response_format": "mp3"}
        if config.OPENAI_TTS_MODEL.startswith("gpt-") and config.OPENAI_TTS_INSTRUCTIONS:
            body["instructions"] = config.OPENAI_TTS_INSTRUCTIONS  # tts-1 models don't take these
        r = requests.post("https://api.openai.com/v1/audio/speech", json=body, timeout=15,
                          headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
        if r.status_code != 200:
            raise RuntimeError(f"OpenAI TTS HTTP {r.status_code}: {r.text[:150]}")
        return r.content


class PiperTTS:
    name, label = "piper", "Piper (on the Pi)"

    def unavailable(self):
        return None if shutil.which("piper") else "piper isn't installed"

    def synthesize(self, text: str) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = tmp.name
        try:
            proc = subprocess.run(
                ["piper", "--model", config.PIPER_MODEL, "--output_file", path,
                 "--length_scale", str(1.0 / config.PIPER_SPEED)],  # Piper uses length_scale (inverse of speed)
                input=text.encode(), capture_output=True, timeout=15)
            if proc.returncode != 0 or not os.path.getsize(path):
                raise RuntimeError(f"piper failed: {proc.stderr.decode(errors='replace')[:150]}")
            with open(path, "rb") as f:
                return f.read()
        finally:
            if os.path.exists(path):
                os.unlink(path)


class EspeakTTS:
    name, label = "espeak", "espeak (on the Pi)"

    @staticmethod
    def _binary():
        return shutil.which("espeak-ng") or shutil.which("espeak")

    def unavailable(self):
        return None if self._binary() else "espeak isn't installed (sudo apt install espeak-ng)"

    def synthesize(self, text: str) -> bytes:
        # Slow, deep whisper voice; --stdout gives WAV so it goes through the
        # same playback path (jaw sync, quiet-hours volume, loudness) as the rest
        proc = subprocess.run([self._binary(), "-v", "en+whisper", "-s", "120", "-p", "20", "--stdout", text],
                              capture_output=True, timeout=30)
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(f"espeak failed: {proc.stderr.decode(errors='replace')[:150]}")
        return proc.stdout


PROVIDERS = {p.name: p for p in (ElevenLabsTTS(), OpenAITTS(), PiperTTS(), EspeakTTS())}


def order() -> list:
    """config.TTS_ORDER, keeping only known providers."""
    return [n for n in config.TTS_ORDER if n in PROVIDERS]


def test(name: str) -> str:
    """Synthesize a short line without playing it (Settings page Test button)."""
    p = PROVIDERS[name]
    why = p.unavailable()
    if why:
        raise ValueError(why)
    t0 = time.time()
    audio = p.synthesize("Happy Halloween. I've been waiting for you.")
    return f"{p.label} made {len(audio) // 1024} KB of audio in {time.time() - t0:.1f}s"
