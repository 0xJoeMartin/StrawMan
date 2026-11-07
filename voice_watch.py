"""
Voice Watch
-----------
While he's idle, keep an ear open. When the mic hears a person talking (Silero
voice detector, nothing is recorded, transcribed or sent anywhere), call
`on_voice()` so the scarecrow can glance toward his blind spot.

The watcher shares the Listener's microphone. A USB mic can only be opened by
one stream at a time, so Listener.listen_for_response() pauses the watcher
(pause() blocks until the watcher's stream is closed) and resumes it after.
"""

import threading
import time
from collections import deque

import numpy as np

import config
import listener as listener_mod

VAD_WINDOW = 512          # Silero wants 512-sample windows at 16 kHz (32 ms)
VAD_RATE = 16000
WINDOW_MS = VAD_WINDOW * 1000 / VAD_RATE
HISTORY_SECONDS = 2.0     # speech must add up to MIN_SPEECH_MS within this span
ECHO_TAIL_SECONDS = 1.0   # ignore the mic this long after he stops talking
REFRACTORY_SECONDS = 2.0  # after a trigger, don't fire again for this long


def to_16k(chunk: np.ndarray, rate: int) -> np.ndarray:
    """Resample one float32 chunk to 16 kHz. Integer ratios (48 kHz → 16 kHz)
    average neighbouring samples, which doubles as a cheap low-pass."""
    if rate == VAD_RATE:
        return chunk
    if rate % VAD_RATE == 0:
        f = rate // VAD_RATE
        n = len(chunk) // f * f
        return chunk[:n].reshape(-1, f).mean(axis=1)
    target = int(round(len(chunk) * VAD_RATE / rate))
    if target <= 1:
        return chunk[:0]
    x = np.linspace(0, len(chunk) - 1, target)
    return np.interp(x, np.arange(len(chunk)), chunk).astype(np.float32)


class SpeechCounter:
    """Turns a stream of per-window VAD probabilities into 'someone is talking'
    decisions. Pure logic — no audio, no clock of its own — so it is testable."""

    def __init__(self):
        self.hist = deque()   # (t, is_speech)
        self.blocked_until = 0.0

    def clear(self):
        self.hist.clear()

    def add(self, t: float, prob: float) -> bool:
        """Feed one window. Returns True when enough speech has been heard."""
        self.hist.append((t, prob >= config.VAD_CONFIDENCE))
        while self.hist and t - self.hist[0][0] > HISTORY_SECONDS:
            self.hist.popleft()
        if t < self.blocked_until:
            return False
        speech_ms = sum(1 for _, s in self.hist if s) * WINDOW_MS
        if speech_ms >= config.VOICE_LOOK_MIN_SPEECH_MS:
            self.hist.clear()
            self.blocked_until = t + REFRACTORY_SECONDS
            return True
        return False


class VoiceWatcher:
    """Background thread that listens for voices while VOICE_LOOK_ENABLED is on.

    Args:
        lst: the Listener (its PyAudio instance, mic index and rate are reused)
        on_voice: called (from the watcher thread) when someone is heard talking
        is_muted: callable, True while he is speaking (his own voice is ignored)
    """

    def __init__(self, lst, on_voice, is_muted=lambda: False):
        self._lst = lst
        self._on_voice = on_voice
        self._is_muted = is_muted
        self._cond = threading.Condition()
        self._pauses = 0
        self._stream_open = False
        self._running = False
        self._thread = None
        self.counter = SpeechCounter()
        self.last_heard = 0.0
        self._mic_stalled = False   # health flag: logged once on each flip
        lst.watcher = self

    # --- mic hand-off -------------------------------------------------------
    def pause(self, timeout: float = 3.0):
        """Stop listening and wait (up to `timeout`) until the mic is free."""
        with self._cond:
            self._pauses += 1
            self._cond.notify_all()
            deadline = time.time() + timeout
            while self._stream_open and time.time() < deadline:
                self._cond.wait(0.05)

    def resume(self):
        with self._cond:
            self._pauses = max(0, self._pauses - 1)
            self._cond.notify_all()

    def _may_listen(self) -> bool:
        return self._running and self._pauses == 0 and bool(config.VOICE_LOOK_ENABLED)

    # --- thread -------------------------------------------------------------
    def start(self):
        if self._thread or not getattr(self._lst, "_pa", None):
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="voice-watch")
        self._thread.start()

    def stop(self):
        self._running = False
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        while self._running:
            with self._cond:
                while self._running and not self._may_listen():
                    self._cond.wait(1.0)
            if not self._running:
                return
            try:
                self._listen_session()
            except Exception as e:
                print(f"[voice] Watcher error: {e}")
                time.sleep(5)

    def _open_stream(self):
        lst = self._lst
        return lst._pa.open(format=lst.FORMAT, channels=lst.CHANNELS, rate=lst._sample_rate,
                            input=True, frames_per_buffer=lst.CHUNK,
                            input_device_index=lst._mic_index)

    def _listen_session(self):
        vad = listener_mod._get_vad_model()
        if vad is None:
            print("[voice] No voice detector (torch/Silero missing) — voice look disabled")
            self._running = False
            return
        import torch

        with self._cond:
            if not self._may_listen():
                return
            try:
                stream = self._open_stream()
            except Exception as e:
                print(f"[voice] Could not open mic: {e}")
                stream = None
            self._stream_open = stream is not None
        if stream is None:
            time.sleep(5)
            return

        rate = self._lst._sample_rate
        buf = np.zeros(0, dtype=np.float32)
        muted_until = 0.0
        self.counter.clear()
        with listener_mod._vad_lock:
            vad.reset_states()
        try:
            while self._may_listen():
                data = listener_mod.read_chunk(stream, self._lst.CHUNK)
                if data is None:
                    # The mic went silent with the stream open — close it (finally)
                    # and let _run() open a fresh one, which brings it back.
                    if not self._mic_stalled:
                        self._mic_stalled = True
                        print("[voice] Mic stopped sending audio — reopening it")
                        listener_mod._log_to_dashboard("HEARING", "Mic stopped sending audio — reopening it")
                    return
                if self._mic_stalled:
                    self._mic_stalled = False
                    print("[voice] Mic is sending audio again")
                    listener_mod._log_to_dashboard("HEARING", "Mic is sending audio again")
                now = time.time()
                if self._is_muted():
                    muted_until = now + ECHO_TAIL_SECONDS
                if now < muted_until:
                    buf = buf[:0]
                    self.counter.clear()
                    continue
                pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                buf = np.concatenate([buf, to_16k(pcm, rate)])
                heard = False
                while len(buf) >= VAD_WINDOW:
                    win, buf = buf[:VAD_WINDOW], buf[VAD_WINDOW:]
                    with listener_mod._vad_lock:
                        prob = vad(torch.from_numpy(np.ascontiguousarray(win)), VAD_RATE).item()
                    if self.counter.add(now, prob):
                        heard = True
                if heard:
                    self.last_heard = now
                    try:
                        self._on_voice()
                    except Exception as e:
                        print(f"[voice] on_voice error: {e}")
        finally:
            try:
                try:
                    stream.stop_stream()
                finally:
                    stream.close()
            finally:
                with self._cond:
                    self._stream_open = False
                    self._cond.notify_all()
