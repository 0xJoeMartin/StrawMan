"""
Speech Module
-------------
Speaks lines through the voice providers in tts_providers.py, tried in the
dashboard's order (config.TTS_ORDER) until one works. Every clip goes through
the same playback path: loudness normalization, quiet-hours volume, and
audio-reactive amplitude extraction for jaw sync.
"""

import os
import time
import struct
import tempfile
import threading
import subprocess
import numpy as np
import config
import tts_providers
from config import (
    AUDIO_CHUNK_SIZE,
)

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    print("[speech] pyaudio not available — will use aplay fallback")


def _log_to_dashboard(source: str, text: str):
    """Forward a one-line health/recovery message to the dashboard log if
    available. Lazy import to avoid hard-depending on the dashboard module."""
    try:
        from web_dashboard import log_event
        log_event(source, text)
    except Exception:
        pass


class SpeechEngine:
    """Handles TTS and audio playback with amplitude extraction for jaw sync."""

    def __init__(self, on_amplitude=None):
        """
        Args:
            on_amplitude: Callback function(amplitude: float) called during playback
                          with the current audio amplitude (0.0–1.0). Used for jaw sync.
        """
        self.on_amplitude = on_amplitude
        self._is_speaking = False
        self._playback_thread = None
        self.volume = 1.0  # 0.0–1.0, used by quiet hours / whisper mode

        # Health per provider: a failure / recovery is logged once when it flips
        self._healthy = {name: True for name in tts_providers.PROVIDERS}
        usable = [n for n in tts_providers.order() if not tts_providers.PROVIDERS[n].unavailable()]
        for n in tts_providers.order():
            why = tts_providers.PROVIDERS[n].unavailable()
            if why:
                print(f"[speech] {tts_providers.PROVIDERS[n].label}: {why} — skipped")
        self.engine = usable[0] if usable else None  # last provider that spoke (or will, first)
        print(f"[speech] Voice order: {' → '.join(usable) or 'NONE USABLE'}")

    def _mark_failed(self, name: str, exc: Exception):
        label = tts_providers.PROVIDERS[name].label
        if self._healthy[name]:
            self._healthy[name] = False
            msg = f"Voice: {label} failed ({type(exc).__name__}) — using the next one"
            print(f"[speech] {msg}: {exc}")
            _log_to_dashboard("SYSTEM", msg)
        else:
            print(f"[speech] {label} failed: {exc}")

    def _mark_ok(self, name: str):
        if not self._healthy[name]:
            self._healthy[name] = True
            msg = f"Voice: {tts_providers.PROVIDERS[name].label} recovered"
            print(f"[speech] {msg}")
            _log_to_dashboard("SYSTEM", msg)

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    def speak(self, text: str, blocking: bool = True, provider: str = None):
        """
        Convert text to speech and play it.

        Args:
            text: The text to speak
            blocking: If True, wait for speech to finish. If False, return immediately.
            provider: Use only this voice (dashboard "Say it" button) instead of the cascade.
        """
        if self._is_speaking:
            print("[speech] Already speaking — skipping")
            return

        if blocking:
            self._do_speak(text, provider)
        else:
            self._playback_thread = threading.Thread(target=self._do_speak, args=(text, provider), daemon=True)
            self._playback_thread.start()

    def _do_speak(self, text: str, only: str = None):
        """Internal: try each voice in order, play the first clip we get."""
        self._is_speaking = True
        try:
            for name in ([only] if only else tts_providers.order()):
                p = tts_providers.PROVIDERS[name]
                if p.unavailable():
                    continue
                t0 = time.time()
                try:
                    audio_data = p.synthesize(text)
                except Exception as e:
                    self._mark_failed(name, e)
                    continue
                self._mark_ok(name)
                if name != self.engine and not only:
                    print(f"[speech] TTS engine → {name}")
                    self.engine = name
                print(f"[speech] {p.label}: {time.time() - t0:.1f}s, {len(audio_data)} bytes")
                self._play_audio(audio_data)
                return
            print("[speech] All TTS engines failed — nothing spoken")
        except Exception as e:
            print(f"[speech] Error: {e}")
        finally:
            self._is_speaking = False

    # --- Audio Playback with Amplitude Extraction ---

    def _play_audio(self, audio_data: bytes):
        """
        Play audio (MP3 or WAV) and extract amplitude in real-time for jaw sync.
        Uses ffmpeg to decode any format to raw PCM, then plays via pyaudio.
        Calls self.on_amplitude(float) with values 0.0–1.0 during playback.
        """
        if PYAUDIO_AVAILABLE:
            self._play_with_pyaudio(audio_data)
        else:
            self._play_with_ffplay(audio_data)

    def _find_supported_rate(self) -> int:
        """Probe the default audio output to find a sample rate it supports."""
        pa = pyaudio.PyAudio()
        try:
            dev_info = pa.get_default_output_device_info()
            dev_index = dev_info["index"]

            # Try common rates, most likely to least likely
            for rate in [48000, 44100, 22050, 16000, 96000, 8000]:
                try:
                    supported = pa.is_format_supported(
                        rate, output_device=dev_index, output_channels=1,
                        output_format=pyaudio.paInt16,
                    )
                    if supported:
                        return rate
                except ValueError:
                    continue

            # If nothing works, return the device's default rate
            return int(dev_info.get("defaultSampleRate", 48000))
        finally:
            pa.terminate()

    def _play_with_pyaudio(self, audio_data: bytes):
        """Decode any audio format to raw PCM via ffmpeg, play with pyaudio."""
        try:
            # Find a sample rate the audio device actually supports
            rate = self._find_supported_rate()
            print(f"[speech] Using sample rate: {rate}Hz")

            # Use ffmpeg to decode to raw 16-bit mono PCM at the supported rate
            proc = subprocess.Popen(
                ["ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ar", str(rate),
                 "-ac", "1", "pipe:1"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

            raw_pcm, _ = proc.communicate(input=audio_data, timeout=15)

            if not raw_pcm:
                print("[speech] ffmpeg produced no output")
                self._play_with_ffplay(audio_data)
                return

            # Play the raw PCM with pyaudio
            pa = pyaudio.PyAudio()
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=rate,
                output=True,
            )

            # Even out loudness between lines and between voices
            if config.TTS_NORMALIZE:
                raw_pcm = normalize_loudness(raw_pcm, config.TTS_LOUDNESS_DBFS)

            # Apply volume attenuation if in whisper/quiet mode
            if self.volume < 1.0:
                samples = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32)
                samples = (samples * self.volume).clip(-32768, 32767).astype(np.int16)
                raw_pcm = samples.tobytes()
                print(f"[speech] Volume attenuated to {self.volume:.0%}")

            chunk_size = AUDIO_CHUNK_SIZE * 2  # bytes (16-bit = 2 bytes per sample)
            offset = 0

            while offset < len(raw_pcm) and self._is_speaking:
                chunk = raw_pcm[offset:offset + chunk_size]
                stream.write(chunk)

                # Extract amplitude for jaw sync
                if self.on_amplitude and len(chunk) > 0:
                    amplitude = self._extract_amplitude(chunk, 2)
                    self.on_amplitude(amplitude)

                offset += chunk_size

            # Signal silence when done
            if self.on_amplitude:
                self.on_amplitude(0.0)

            stream.stop_stream()
            stream.close()
            pa.terminate()

        except Exception as e:
            print(f"[speech] pyaudio playback error: {e}")
            # Fall back to ffplay with the original audio data
            self._play_with_ffplay(audio_data)

    def _play_with_ffplay(self, audio_data: bytes):
        """Fallback: play any audio format using ffplay (no amplitude extraction)."""
        try:
            if self.on_amplitude:
                self.on_amplitude(0.5)  # Simple jaw open during speech

            # ffplay can read from a temp file — handles MP3, WAV, anything
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp.write(audio_data)
                tmp_path = tmp.name

            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", tmp_path],
                capture_output=True, timeout=30,
            )
            os.unlink(tmp_path)

        except Exception as e:
            print(f"[speech] ffplay error: {e}")
        finally:
            if self.on_amplitude:
                self.on_amplitude(0.0)

    def _extract_amplitude(self, audio_chunk: bytes, sample_width: int) -> float:
        """
        Extract the RMS amplitude from an audio chunk.
        Returns a normalized value between 0.0 and 1.0.
        """
        try:
            if sample_width == 2:
                fmt = f"<{len(audio_chunk) // 2}h"
                samples = struct.unpack(fmt, audio_chunk)
            elif sample_width == 1:
                samples = [b - 128 for b in audio_chunk]
            elif sample_width == 4:
                fmt = f"<{len(audio_chunk) // 4}i"
                samples = struct.unpack(fmt, audio_chunk)
            else:
                return 0.0

            if not samples:
                return 0.0

            # RMS amplitude
            rms = np.sqrt(np.mean(np.array(samples, dtype=np.float64) ** 2))

            # Normalize against a realistic speech level, not theoretical max.
            # Typical speech RMS is 1000–5000 for 16-bit audio. Using 3000 as
            # the reference means normal talking maps to ~1.0 and quiet parts
            # still produce visible jaw movement.
            speech_ref = 3000 if sample_width == 2 else (12 if sample_width == 1 else 200000000)
            normalized = min(rms / speech_ref, 1.0)

            # Apply a curve to exaggerate the movement — makes it look more natural
            return min(normalized ** 0.5, 1.0)

        except Exception:
            return 0.0


def normalize_loudness(pcm: bytes, target_dbfs: float) -> bytes:
    """
    Scale 16-bit mono PCM so its *voiced* loudness (RMS over the 20 ms frames
    louder than a tenth of the loudest one — i.e. ignoring pauses) hits
    target_dbfs, then soft-limit peaks so nothing clips. Gain is capped to
    +18 / -12 dB so near-silent or broken clips aren't blown up.
    """
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if x.size < 480:
        return pcm
    frame = 960 if x.size >= 960 else x.size
    frames = x[: x.size // frame * frame].reshape(-1, frame)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    if rms.max() <= 1e-5:
        return pcm
    voiced = frames[rms > rms.max() * 0.1]
    current = float(np.sqrt((voiced ** 2).mean()))
    gain = (10 ** (target_dbfs / 20)) / current
    gain = min(max(gain, 10 ** (-12 / 20)), 10 ** (18 / 20))
    y = x * gain
    # Soft knee above 0.8 full scale: tanh keeps peaks under 1.0 without hard clipping
    knee = 0.8
    over = np.abs(y) > knee
    if over.any():
        y[over] = np.sign(y[over]) * (knee + (1 - knee) * np.tanh((np.abs(y[over]) - knee) / (1 - knee)))
    return (y * 32767).astype(np.int16).tobytes()


# --- Standalone test ---
if __name__ == "__main__":
    print("Testing speech engine...")

    def amplitude_callback(amp):
        # Visual amplitude meter
        bars = int(amp * 40)
        print(f"  Amplitude: {'█' * bars}{'░' * (40 - bars)} {amp:.2f}", end='\r')

    engine = SpeechEngine(on_amplitude=amplitude_callback)

    test_lines = [
        "I see you standing there. Don't pretend you can't hear me.",
        "Happy Halloween. If you can call it that.",
    ]

    for line in test_lines:
        print(f"\nSaying: {line}")
        engine.speak(line, blocking=True)
        print()

    print("\nDone!")
