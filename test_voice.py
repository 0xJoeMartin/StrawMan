#!/usr/bin/env python3
"""
Voice + hearing checks — no audio hardware, no network.

    python test_voice.py

Covers: the ElevenLabs request is unchanged, the TTS / STT cascades (order,
skipping, failover, health logging), loudness normalization, and settings
migration from the old single-engine keys.
"""

import json
import os
import tempfile
import types

import numpy as np

import config
import speech
import stt_providers
import tts_providers


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'FAIL'}  {name}{'' if cond or not extra else '  ' + str(extra)}")
    if not cond:
        raise SystemExit(1)


class FakeResp:
    def __init__(self, status=200, content=b"MP3", js=None):
        self.status_code, self.content, self.text, self._js = status, content, "err", js or {}

    def json(self):
        return self._js


def main():
    import requests
    calls = []
    requests.post = lambda url, **kw: calls.append((url, kw)) or FakeResp(js={"text": "hello"})

    print("ElevenLabs request")
    config.ELEVENLABS_API_KEY, config.ELEVENLABS_VOICE_ID = "k", "VOICE"
    config.ELEVENLABS_MODEL, config.ELEVENLABS_STABILITY, config.ELEVENLABS_STYLE = "eleven_flash_v2_5", 0.3, 0.5
    config.ELEVENLABS_SPEED = 1.1
    tts_providers.PROVIDERS["elevenlabs"].synthesize("Boo.")
    url, kw = calls[-1]
    check("url", url == "https://api.elevenlabs.io/v1/text-to-speech/VOICE")
    check("headers", kw["headers"] == {"Accept": "audio/mpeg", "Content-Type": "application/json", "xi-api-key": "k"})
    check("body", kw["json"] == {"text": "Boo.", "model_id": "eleven_flash_v2_5", "voice_settings": {
        "stability": 0.3, "similarity_boost": 0.7, "style": 0.5, "use_speaker_boost": True, "speed": 1.1}})
    check("timeout", kw["timeout"] == 10)

    print("OpenAI voice")
    config.OPENAI_API_KEY, config.OPENAI_TTS_VOICE = "ok", "onyx"
    config.OPENAI_TTS_MODEL, config.OPENAI_TTS_INSTRUCTIONS = "gpt-4o-mini-tts", "Creepy."
    tts_providers.PROVIDERS["openai"].synthesize("Boo.")
    check("instructions sent to gpt-4o-mini-tts", calls[-1][1]["json"].get("instructions") == "Creepy.")
    config.OPENAI_TTS_MODEL = "tts-1"
    tts_providers.PROVIDERS["openai"].synthesize("Boo.")
    check("not sent to tts-1", "instructions" not in calls[-1][1]["json"])
    requests.post = lambda url, **kw: FakeResp(status=401)
    try:
        tts_providers.PROVIDERS["openai"].synthesize("Boo.")
        check("non-200 raises", False)
    except RuntimeError:
        check("non-200 raises", True)

    print("Voice cascade")
    logs = []
    speech._log_to_dashboard = lambda src, text: logs.append(text)

    class Fake:
        def __init__(self, name, why=None, fail=False):
            self.name, self.label, self.why, self.fail, self.calls = name, name, why, fail, 0

        def unavailable(self):
            return self.why

        def synthesize(self, text):
            self.calls += 1
            if self.fail:
                raise RuntimeError("down")
            return f"audio:{self.name}".encode()

    a, b, c = Fake("a", fail=True), Fake("b", why="no key"), Fake("c")
    tts_providers.PROVIDERS = {"a": a, "b": b, "c": c}
    config.TTS_ORDER = ["a", "b", "c"]
    se = speech.SpeechEngine()
    played = []
    se._play_audio = played.append
    se.speak("hi")
    check("failed one skipped, unavailable one skipped, third plays", played == [b"audio:c"] and b.calls == 0)
    check("engine follows what spoke", se.engine == "c")
    se.speak("hi")
    check("failure logged once, not every line", sum("a failed" in l for l in logs) == 1, logs)
    a.fail = False
    se.speak("hi")
    check("recovery logged", any("a recovered" in l for l in logs) and played[-1] == b"audio:a")
    se.speak("hi", provider="c")
    check("'Say it' uses only the chosen voice", played[-1] == b"audio:c")
    a.fail = c.fail = True
    n = len(played)
    se.speak("hi")
    check("all fail -> nothing played, no crash", len(played) == n and not se.is_speaking)
    config.TTS_ORDER = ["c", "zzz"]
    check("unknown names in the order are ignored", tts_providers.order() == ["c"])

    print("Loudness normalization")
    sr, t = 48000, np.arange(48000) / 48000
    def clip(amp):
        tone = amp * np.sin(2 * np.pi * 220 * t) * (t % 0.5 < 0.35)    # speech-ish with pauses
        return (tone * 32767).astype(np.int16).tobytes()
    def voiced_db(pcm):
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768
        f = x.reshape(-1, 960); r = np.sqrt((f ** 2).mean(axis=1)); v = f[r > r.max() * 0.1]
        return 20 * np.log10(np.sqrt((v ** 2).mean()))
    for amp in (0.05, 0.2, 0.6):
        out = speech.normalize_loudness(clip(amp), -14.0)
        check(f"amp {amp}: lands within 1.5 dB of -14", abs(voiced_db(out) + 14) < 1.5, round(voiced_db(out), 1))
        peak = np.abs(np.frombuffer(out, dtype=np.int16)).max()
        check(f"amp {amp}: no clipping", peak < 32767)
    tiny = speech.normalize_loudness(clip(0.0005), -14.0)
    check("gain capped at +18 dB for near-silent clips", voiced_db(tiny) < -40)
    silence = bytes(9600)
    check("silence untouched", speech.normalize_loudness(silence, -14.0) == silence)

    print("Speech-to-text cascade")
    stt_providers._log = lambda text: logs.append(text)

    class FakeSTT:
        def __init__(self, name, text="hello", why=None, fail=False):
            self.name, self.label, self.text, self.why, self.fail = name, name, text, why, fail

        def unavailable(self):
            return self.why

        def transcribe(self, wav):
            if self.fail:
                raise RuntimeError("down")
            return self.text

    x, y, z = FakeSTT("x", fail=True), FakeSTT("y", why="no key"), FakeSTT("z", text="trick or treat")
    stt_providers.PROVIDERS = {"x": x, "y": y, "z": z}
    stt_providers._healthy = {"x": True, "y": True, "z": True}
    config.STT_ORDER = ["x", "y", "z"]
    check("fails over to the next usable one", stt_providers.transcribe(b"wav") == "trick or treat")
    check("failure logged", any("x failed" in l for l in logs))
    x.fail, x.text = False, ""
    check("empty transcript is an answer (no fallthrough)", stt_providers.transcribe(b"wav") is None)
    check("recovery logged", any("x recovered" in l for l in logs))

    print("Settings: old single-engine keys")
    import settings_store
    cfg = types.ModuleType("cfg")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")).read()
    exec(compile(src.split("# --- Dashboard overrides")[0], "config.py", "exec"), cfg.__dict__)
    path = os.path.join(tempfile.mkdtemp(), "settings.json")
    json.dump({"version": 1, "settings": {"STT_ENGINE": "openai", "TTS_ENGINE": "elevenlabs"}}, open(path, "w"))
    st = settings_store.SettingsStore(cfg, path=path)
    check("STT_ENGINE=openai -> openai first", cfg.STT_ORDER == ["openai", "faster-whisper", "local"], cfg.STT_ORDER)
    check("TTS_ENGINE=elevenlabs (the default) -> not marked modified", not st.is_overridden("TTS_ORDER"))
    check("order needs at least one", _raises(lambda: st.set("TTS_ORDER", [])))
    check("order rejects unknown names", _raises(lambda: st.set("TTS_ORDER", ["elevenlabs", "bogus"])))
    check("order drops duplicates", st.set("TTS_ORDER", ["espeak", "espeak", "openai"]) == ["espeak", "openai"])

    print("\nAll voice checks passed.")


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


if __name__ == "__main__":
    main()
