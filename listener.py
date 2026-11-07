"""
Listener Module
---------------
Listens for a spoken response after the scarecrow speaks.
Uses simple energy-based voice activity detection + Whisper for transcription.
Designed for a single listen-and-transcribe cycle, not continuous conversation.
"""

import io
import wave
from collections import deque
import threading
import time
import numpy as np
import config
import stt_providers
from config import (
    LISTEN_MIC_FLUSH_SECONDS, AUDIO_SAMPLE_RATE,
)

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    print("[listener] pyaudio not available — listening disabled")


def _log_to_dashboard(source: str, text: str):
    """Forward a one-line health/recovery message to the dashboard log if
    available. Lazy import to avoid hard-depending on the dashboard module."""
    try:
        from web_dashboard import log_event
        log_event(source, text)
    except Exception:
        pass

# Silero VAD model (lazy-loaded). It keeps internal state, so the conversation
# listener and the idle voice watcher (voice_watch.py) take turns via this lock.
_vad_model = None
_vad_lock = threading.RLock()


def _get_vad_model():
    """Lazy-load the Silero VAD model."""
    global _vad_model
    if _vad_model is None:
        try:
            import torch
        except ImportError:
            print("[listener] torch not installed — VAD disabled, will rely on energy threshold only")
            return None
        try:
            print("[listener] Loading Silero VAD model...")
            _vad_model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                trust_repo=True,
            )
            print("[listener] Silero VAD model loaded")
        except Exception as e:
            print(f"[listener] Failed to load Silero VAD: {e}")
    return _vad_model


# The USB mic can stop sending audio while its stream stays open (USB capture
# errors; Oct 27 it went silent mid-read). PortAudio's read() then blocks
# forever, so the voice watcher never gave the mic back and he couldn't hear
# anyone until a restart. Reads wait for data with a deadline instead; a fresh
# open of the stream brings the mic back.
MIC_STALL_SECONDS = 2.0


def read_chunk(stream, n: int, timeout: float | None = None):
    """Read n frames, or return None if the mic sends nothing for `timeout`
    seconds (default MIC_STALL_SECONDS) — the caller should close the stream."""
    deadline = time.time() + (MIC_STALL_SECONDS if timeout is None else timeout)
    while stream.get_read_available() < n:
        if time.time() > deadline:
            return None
        time.sleep(0.005)
    return stream.read(n, exception_on_overflow=False)


# ~0.25 s of audio kept from just before speech starts, so the first syllable isn't clipped
PREROLL_CHUNKS = 12
# Fewer loud chunks than this (~80 ms at 48 kHz / 1024) is a click or thump, not a word
MIN_SPEECH_CHUNKS = 4


class Listener:
    """
    Listens for a single spoken response from a person.

    Usage:
        listener = Listener()
        text = listener.listen_for_response()
        if text:
            print(f"They said: {text}")
    """

    # Audio recording settings
    FORMAT = pyaudio.paInt16 if PYAUDIO_AVAILABLE else None
    CHANNELS = 1
    CHUNK = 1024

    def __init__(self):
        self._pa = None
        self._sample_rate = AUDIO_SAMPLE_RATE
        if PYAUDIO_AVAILABLE:
            self._pa = pyaudio.PyAudio()

            # Find the USB microphone (prefer "microphone" in name, then any USB)
            self._mic_index = self._find_usb_mic()
            if self._mic_index is not None:
                info = self._pa.get_device_info_by_index(self._mic_index)
                print(f"[listener] Using mic: {info['name']} (index {self._mic_index})")
                # Use the mic's native sample rate
                self._sample_rate = self._find_working_rate(self._mic_index)
                print(f"[listener] Recording at {self._sample_rate}Hz")
            else:
                print("[listener] Using default microphone")

        # Pre-load Silero VAD in background
        import threading
        threading.Thread(target=_get_vad_model, daemon=True).start()

        # Speech-to-text runs through stt_providers in the dashboard's order
        print(f"[listener] Speech-to-text order: {' → '.join(stt_providers.order())}")

        # Always pre-load local Whisper in the background so it's ready as an
        # offline fallback if the other providers fail mid-night.
        stt_providers.prewarm_local()

    def _find_usb_mic(self) -> int | None:
        """Try to find a USB microphone device.
        Prefers devices with 'microphone' in the name, falls back to any USB input."""
        if not self._pa:
            return None

        usb_devices = []
        mic_devices = []

        for i in range(self._pa.get_device_count()):
            try:
                info = self._pa.get_device_info_by_index(i)
                name = info.get("name", "").lower()
                max_inputs = info.get("maxInputChannels", 0)
                if max_inputs > 0:
                    if "microphone" in name:
                        mic_devices.append(i)
                    elif "usb" in name:
                        usb_devices.append(i)
            except Exception:
                continue

        # Prefer dedicated microphone devices over generic USB audio
        if mic_devices:
            return mic_devices[0]
        if usb_devices:
            return usb_devices[0]
        return None

    def _find_working_rate(self, device_index: int) -> int:
        """Probe the mic for a supported sample rate."""
        for rate in [48000, 44100, 22050, 16000]:
            try:
                supported = self._pa.is_format_supported(
                    rate,
                    input_device=device_index,
                    input_channels=1,
                    input_format=pyaudio.paInt16,
                )
                if supported:
                    return rate
            except (ValueError, Exception):
                continue
        # Fall back to device's default rate
        info = self._pa.get_device_info_by_index(device_index)
        return int(info["defaultSampleRate"])

    def listen_for_response(self) -> str | None:
        """Listen once (see _listen_for_response). The idle voice watcher, if
        running, gives up the mic first and takes it back afterwards."""
        w = getattr(self, "watcher", None)
        if w:
            w.pause()
        try:
            return self._listen_for_response()
        finally:
            if w:
                w.resume()

    def _listen_for_response(self) -> str | None:
        """
        Listen for a spoken response for up to LISTEN_DURATION seconds.

        Returns the transcribed text if someone spoke, or None if silence.

        The logic:
        1. Start recording
        2. Wait for speech (energy above threshold)
        3. Once speech is detected, keep recording until silence returns
        4. If no speech detected within LISTEN_DURATION, give up
        5. Transcribe whatever was captured with Whisper
        """
        if not PYAUDIO_AVAILABLE or not self._pa:
            print("[listener] Cannot listen — pyaudio not available")
            return None

        print("[listener] Listening for response...")

        try:
            stream = self._pa.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self._sample_rate,
                input=True,
                frames_per_buffer=self.CHUNK,
                input_device_index=self._mic_index,
            )
        except Exception as e:
            print(f"[listener] Could not open mic: {e}")
            return None

        # Optionally discard the start of the mic buffer to clear residual
        # speaker bleed. Default is 0s — the post-speak delay in scarecrow.py
        # already gives the speaker time to decay before the stream opens,
        # and flushing extra audio just delays catching the start of the
        # person's response.
        if LISTEN_MIC_FLUSH_SECONDS > 0:
            flush_chunks = int(self._sample_rate * LISTEN_MIC_FLUSH_SECONDS / self.CHUNK)
            for _ in range(flush_chunks):
                try:
                    stream.read(self.CHUNK, exception_on_overflow=False)
                except Exception:
                    pass
            print(f"[listener] Mic buffer flushed ({LISTEN_MIC_FLUSH_SECONDS:.1f}s)")

        frames = []                        # the utterance being recorded
        preroll = deque(maxlen=PREROLL_CHUNKS)  # audio just before it started
        energies = []
        speech_detected = False
        loud_chunks = 0
        silence_start = None
        accepted = None
        listen_start = time.time()

        try:
            while True:
                elapsed = time.time() - listen_start

                # Hard timeout — give up (keeping an utterance cut off mid-way if it's real speech)
                if elapsed > config.LISTEN_DURATION:
                    if speech_detected and self._is_speech(frames, loud_chunks):
                        accepted = frames
                    break

                # Read audio chunk
                try:
                    data = read_chunk(stream, self.CHUNK)
                except Exception:
                    continue
                if data is None:
                    print(f"[listener] Mic stopped sending audio for {MIC_STALL_SECONDS:.0f}s "
                          f"— giving up this listen (it's reopened next time)")
                    _log_to_dashboard("HEARING", "Mic stopped sending audio mid-listen; reopening it next time")
                    break

                # Calculate energy of this chunk
                energy = self._get_energy(data)
                energies.append(energy)

                if config.DEBUG:
                    bars = int(min(energy / config.LISTEN_ENERGY_THRESHOLD, 3.0) * 10)
                    print(f"  Energy: {'█' * bars}{'░' * (30 - bars)} {energy:.0f}", end='\r')

                if not speech_detected:
                    preroll.append(data)
                    if energy > config.LISTEN_ENERGY_THRESHOLD:
                        speech_detected = True
                        loud_chunks = 1
                        frames = list(preroll)
                        silence_start = None
                        print("[listener] Speech detected!")
                    continue

                frames.append(data)
                if energy > config.LISTEN_ENERGY_THRESHOLD:
                    loud_chunks += 1
                    silence_start = None  # Reset silence timer
                elif silence_start is None:
                    silence_start = time.time()
                elif time.time() - silence_start > config.LISTEN_SILENCE_TIMEOUT:
                    # Enough silence after the sound — was it a person?
                    print("[listener] End of speech detected")
                    if self._is_speech(frames, loud_chunks):
                        accepted = frames
                        break
                    # A click, a thump, his own echo… ignore it and keep listening
                    print(f"[listener] Ignored a noise — still listening "
                          f"({config.LISTEN_DURATION - (time.time() - listen_start):.1f}s left)")
                    speech_detected = False
                    frames, loud_chunks, silence_start = [], 0, None
                    preroll.clear()

        finally:
            try:
                stream.stop_stream()
            finally:
                stream.close()

        # Always log a one-line audio summary so we can diagnose detection
        # failures from the dashboard log without enabling DEBUG.
        if energies:
            peak = max(energies)
            mean = sum(energies) / len(energies)
            over = 100 * sum(1 for e in energies if e > config.LISTEN_ENERGY_THRESHOLD) / len(energies)
            print(f"[listener] Audio stats: peak={peak:.0f} mean={mean:.0f} "
                  f"above_threshold={over:.0f}% (threshold={config.LISTEN_ENERGY_THRESHOLD})")

        if accepted is None:
            print("[listener] No speech detected — silence")
            return None

        # Transcribe (the voice detector already accepted it)
        return self._transcribe(accepted, vad_checked=True)

    def _is_speech(self, frames: list[bytes], loud_chunks: int) -> bool:
        """Was this burst of sound a person talking? Very short bursts (clicks,
        thumps) are dismissed without running the voice detector."""
        if loud_chunks < MIN_SPEECH_CHUNKS:
            print(f"[listener] Too short to be speech ({loud_chunks} loud chunk(s))")
            return False
        if not self._check_vad(frames):
            print("[listener] VAD rejected audio — not human speech")
            return False
        return True

    def _check_vad(self, frames: list[bytes]) -> bool:
        """
        Run Silero VAD on recorded audio to verify it contains human speech.
        Returns True if speech is detected, False if it's just noise.
        Falls back to True (permissive) if VAD is unavailable.
        """
        vad = _get_vad_model()
        if vad is None:
            return True  # No VAD available — don't block

        try:
            import torch

            # Combine raw audio frames into a single tensor
            audio = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0

            # Silero VAD expects 16kHz mono — resample if needed
            if self._sample_rate != 16000:
                # Simple linear resample
                duration = len(audio) / self._sample_rate
                target_len = int(duration * 16000)
                indices = np.linspace(0, len(audio) - 1, target_len).astype(int)
                audio = audio[indices]

            tensor = torch.FloatTensor(audio)

            # Process in 512-sample windows (32ms at 16kHz) and take the max confidence
            window_size = 512
            max_confidence = 0.0
            with _vad_lock:
                # Run VAD on the full clip — get overall speech probability
                vad.reset_states()
                for i in range(0, len(tensor) - window_size, window_size):
                    chunk = tensor[i:i + window_size]
                    confidence = vad(chunk, 16000).item()
                    max_confidence = max(max_confidence, confidence)
                vad.reset_states()

            print(f"[listener] VAD confidence: {max_confidence:.2f} (threshold: {config.VAD_CONFIDENCE})")
            return max_confidence >= config.VAD_CONFIDENCE

        except Exception as e:
            print(f"[listener] VAD error: {e}")
            return True  # Fail open — don't block transcription on VAD errors

    def _transcribe(self, frames: list[bytes], vad_checked: bool = False) -> str | None:
        """VAD-check the recorded frames (unless already done), then run the speech-to-text cascade."""
        if not frames:
            return None

        # Verify with Silero VAD that this is actually speech, not noise
        if not vad_checked and not self._check_vad(frames):
            print("[listener] VAD rejected audio — not human speech")
            return None

        # Build WAV in memory
        wav_buffer = io.BytesIO()
        wf = wave.open(wav_buffer, "wb")
        wf.setnchannels(self.CHANNELS)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(self._sample_rate)
        wf.writeframes(b"".join(frames))
        wf.close()
        text = stt_providers.transcribe(wav_buffer.getvalue())
        if text:
            text = self._filter_hallucinations(text)

        return text

    @staticmethod
    def _filter_hallucinations(text: str) -> str | None:
        """Filter out common Whisper hallucination patterns."""
        hallucination_patterns = [
            "thank you", "thanks for watching", "subscribe",
            "like and subscribe", "see you next time",
            "you", "bye", "goodbye", "welcome",
            "the end", "oh", "hmm", "uh",
            "...", "♪", "music",
            "i've been watching you", "i've been watching you.",
        ]
        text_lower = text.lower().strip(".")
        if text_lower in hallucination_patterns or len(text_lower) < 3:
            print(f"[listener] Filtered likely hallucination: \"{text}\"")
            return None
        return text

    def _get_energy(self, audio_chunk: bytes) -> float:
        """Calculate RMS energy of an audio chunk."""
        try:
            samples = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float64)
            return np.sqrt(np.mean(samples ** 2))
        except Exception:
            return 0.0

    def release(self):
        """Clean up audio resources."""
        if self._pa:
            self._pa.terminate()
        print("[listener] Released")


# --- Standalone test ---
if __name__ == "__main__":
    import sys

    if not PYAUDIO_AVAILABLE:
        print("ERROR: pyaudio is not installed. Run: pip install pyaudio")
        sys.exit(1)

    pa = pyaudio.PyAudio()

    # Step 1: List all audio devices
    print("=" * 50)
    print("AUDIO DEVICES")
    print("=" * 50)
    mic_count = 0
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        inputs = info.get("maxInputChannels", 0)
        if inputs > 0:
            mic_count += 1
            name = info["name"]
            rate = int(info["defaultSampleRate"])
            marker = " <-- USB" if "usb" in name.lower() or "microphone" in name.lower() else ""
            print(f"  [{i}] {name}  (channels={inputs}, rate={rate}){marker}")

    if mic_count == 0:
        print("\n  NO INPUT DEVICES FOUND!")
        print("  - Is the USB mic plugged in?")
        print("  - Try: arecord -l")
        pa.terminate()
        sys.exit(1)

    # Step 2: Find best mic (prefer "microphone" in name, then USB)
    mic_devices = []
    usb_devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        name = info.get("name", "").lower()
        if info.get("maxInputChannels", 0) > 0:
            if "microphone" in name:
                mic_devices.append(i)
            elif "usb" in name:
                usb_devices.append(i)

    mic_idx = mic_devices[0] if mic_devices else (usb_devices[0] if usb_devices else None)
    if mic_idx is not None:
        info = pa.get_device_info_by_index(mic_idx)
        native_rate = int(info["defaultSampleRate"])
        print(f"\nUsing mic: {info['name']} (index {mic_idx}, native rate {native_rate}Hz)")
    else:
        native_rate = AUDIO_SAMPLE_RATE
        print("\nNo USB mic found — using default input device")

    # Step 3: Live audio level monitor
    print("\n" + "=" * 50)
    print("LIVE AUDIO LEVELS  (speak into the mic)")
    print(f"Energy threshold: {config.LISTEN_ENERGY_THRESHOLD}")
    print("Press Ctrl+C to move to transcription test")
    print("=" * 50 + "\n")

    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=native_rate,
            input=True,
            frames_per_buffer=1024,
            input_device_index=mic_idx,
        )

        peak_energy = 0
        while True:
            data = stream.read(1024, exception_on_overflow=False)
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float64)
            energy = np.sqrt(np.mean(samples ** 2))
            peak_energy = max(peak_energy, energy)

            bar_len = int(min(energy / max(config.LISTEN_ENERGY_THRESHOLD * 2, 1), 1.0) * 40)
            bar = "█" * bar_len + "░" * (40 - bar_len)

            status = "SPEECH" if energy > config.LISTEN_ENERGY_THRESHOLD else "quiet"
            print(f"  {bar} {energy:6.0f} / {config.LISTEN_ENERGY_THRESHOLD} [{status}]  peak={peak_energy:.0f}   ", end="\r")

    except KeyboardInterrupt:
        stream.stop_stream()
        stream.close()
        print(f"\n\nPeak energy seen: {peak_energy:.0f}")
        if peak_energy < config.LISTEN_ENERGY_THRESHOLD:
            print(f"WARNING: Peak energy ({peak_energy:.0f}) never reached threshold ({config.LISTEN_ENERGY_THRESHOLD})")
            print(f"  Try lowering the mic energy threshold (dashboard Settings → Hearing) to ~{max(int(peak_energy * 0.6), 50)}")
        else:
            print("Mic levels look good!")
    except Exception as e:
        print(f"\nERROR opening mic stream: {e}")
        pa.terminate()
        sys.exit(1)

    # Step 4: Transcription test
    print("\n" + "=" * 50)
    print("TRANSCRIPTION TEST")
    print(f"Speak now — listening for up to {config.LISTEN_DURATION}s...")
    print("=" * 50 + "\n")

    listener = Listener()
    time.sleep(2)  # Let Whisper finish loading

    text = listener.listen_for_response()
    if text:
        print(f'\nYou said: "{text}"')
    else:
        print("\nNo speech detected.")

    listener.release()
    pa.terminate()
    print("Done!")
