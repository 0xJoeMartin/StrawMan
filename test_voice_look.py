"""Voice look: speech counting, mic hand-off, and the glance decision/motion.
Fake mic, fake voice detector, fake servos. Run: venv/bin/python test_voice_look.py"""
import contextlib
import io
import threading
import time

import numpy as np

import config
import listener as listener_mod
import scarecrow
import voice_watch as vw

ok = True


def check(name, good, detail=""):
    global ok
    ok &= bool(good)
    print(f"{'PASS' if good else 'FAIL'}  {name}  {detail}")


# --- resampling + speech counting -----------------------------------------
check("48 kHz chunk -> 16 kHz", len(vw.to_16k(np.zeros(1024, np.float32), 48000)) == 341)
check("44.1 kHz chunk -> 16 kHz", abs(len(vw.to_16k(np.zeros(1024, np.float32), 44100)) - 372) <= 1)

config.VOICE_LOOK_MIN_SPEECH_MS = 400
config.VAD_CONFIDENCE = 0.3


def feed(probs, t0=0.0, step=vw.WINDOW_MS / 1000):
    c = vw.SpeechCounter()
    return [i for i, p in enumerate(probs) if c.add(t0 + i * step, p)]


need = int(np.ceil(400 / vw.WINDOW_MS))
check("13 windows of talking triggers", feed([0.9] * need) == [need - 1], f"(need {need})")
check("noise below threshold never triggers", feed([0.2] * 200) == [])
check("refractory: one trigger per 2 s", len(feed([0.9] * 60)) == 1)
# Isolated blips 1 s apart never add up inside the 2 s window
c = vw.SpeechCounter()
fired = [c.add(i * 1.0, 0.9) for i in range(30)]
check("scattered blips don't trigger", not any(fired))


# --- mic hand-off ------------------------------------------------------------
class FakeStream:
    def __init__(self, pa):
        self.pa = pa
        pa.open_count += 1
        pa.open_now += 1
        pa.current = self
    def get_read_available(self):
        return 0 if self is self.pa.stalled else 1 << 20
    def read(self, n, exception_on_overflow=False):
        time.sleep(0.005)
        return b"\x00\x00" * n
    def stop_stream(self):
        pass
    def close(self):
        self.pa.open_now -= 1


class FakePA:
    def __init__(self):
        self.open_count = 0
        self.open_now = 0
        self.current = self.stalled = None
    def open(self, **kw):
        if self.open_now:
            raise OSError("Device busy")  # a USB mic can only be opened once
        return FakeStream(self)


class FakeListener:
    FORMAT, CHANNELS, CHUNK = 8, 1, 1024
    def __init__(self):
        self._pa = FakePA()
        self._mic_index = 1
        self._sample_rate = 48000


class FakeVad:
    prob = 0.0
    def reset_states(self):
        pass
    def __call__(self, x, sr):
        assert len(x) == 512 and sr == 16000
        return np.float32(self.prob)


fake_vad = FakeVad()
listener_mod._get_vad_model = lambda: fake_vad
config.VOICE_LOOK_ENABLED = True
heard = []
muted = {"v": False}
lst = FakeListener()
w = vw.VoiceWatcher(lst, lambda: heard.append(time.time()), is_muted=lambda: muted["v"])
w.start()
time.sleep(0.3)
check("watcher opens the mic when enabled", lst._pa.open_now == 1)
w.pause()
check("pause() returns with the mic closed", lst._pa.open_now == 0)
time.sleep(0.2)
check("stays closed while paused", lst._pa.open_now == 0)
w.resume()
time.sleep(0.3)
check("resume() reopens the mic", lst._pa.open_now == 1 and lst._pa.open_count == 2)

# Listener.listen_for_response hands the mic over
real = listener_mod.Listener
fake_self = type("L", (), {})()
fake_self.watcher = w
fake_self._listen_for_response = lambda: ("mic free" if lst._pa.open_now == 0 else "mic busy")
check("listen_for_response pauses the watcher", real.listen_for_response(fake_self) == "mic free")
time.sleep(0.3)
check("...and gives it back afterwards", lst._pa.open_now == 1)

fake_vad.prob = 0.9
muted["v"] = True
time.sleep(0.6)
check("his own voice is ignored", heard == [])
muted["v"] = False
time.sleep(0.5)
check("still ignored during the echo tail", heard == [])
time.sleep(1.5)
check("voices heard after the tail", len(heard) >= 1, f"(callbacks={len(heard)})")
fake_vad.prob = 0.0
config.VOICE_LOOK_ENABLED = False
time.sleep(0.3)
check("switch off -> mic released", lst._pa.open_now == 0)

# Oct 27: the USB mic went silent with the watcher's stream open; read() blocked
# forever, the mic was never handed over and he couldn't hear anyone.
logged = []
listener_mod._log_to_dashboard = lambda src, text: logged.append(text)
listener_mod.MIC_STALL_SECONDS = 0.2
config.VOICE_LOOK_ENABLED = True
deadline = time.time() + 3
while lst._pa.open_now == 0 and time.time() < deadline:  # watcher notices within ~1 s
    time.sleep(0.05)
opens = lst._pa.open_count
lst._pa.stalled = lst._pa.current
time.sleep(0.5)
check("silent mic -> watcher closes it and opens a fresh stream",
      lst._pa.open_count == opens + 1 and lst._pa.open_now == 1, f"(opens={lst._pa.open_count - opens})")
check("stall and recovery each logged once", logged == ["Mic stopped sending audio — reopening it",
                                                         "Mic is sending audio again"], str(logged))
lst._pa.stalled = lst._pa.current
time.sleep(0.05)  # let the watcher get stuck waiting on the silent stream
t0 = time.time()
w.pause()
took = time.time() - t0
check("pause() still gets the mic from a silent stream", lst._pa.open_now == 0 and 0.05 < took < 0.4,
      f"({took:.2f}s)")
w.resume()
w.stop()


# --- glance decision + motion -----------------------------------------------
class FakeServos:
    def __init__(self):
        self.targets = []
        self.speed_override = None
    def set_target(self, pan, tilt):
        self.targets.append((round(pan), round(tilt)))
    def _clamp_pan(self, a):
        return max(config.PAN_MIN, min(config.PAN_MAX, a))
    def _clamp_tilt(self, a):
        return max(config.TILT_MIN, min(config.TILT_MAX, a))


def make(now=None):
    sc = scarecrow.Scarecrow.__new__(scarecrow.Scarecrow)
    sc.servos = FakeServos()
    sc.speech = type("S", (), {"is_speaking": False})()
    sc._interaction_lock = threading.Lock()
    sc._manual_mode = False
    sc.running = True
    sc._glance_until = 0.0
    sc._last_glance_time = 0.0
    sc._voice_skip_logged = (None, 0.0)
    sc._last_interaction_time = 0.0
    sc._last_detection_time = time.time() - 60
    sc._returned_to_center = True
    sc._is_quiet_hours = lambda: False
    sc._wait_for_pose = lambda *a, **k: None
    return sc


scarecrow.log_event = lambda *a, **k: None
config.VOICE_LOOK_ENABLED = True
config.VOICE_LOOK_PAN, config.VOICE_LOOK_TILT = 20, 100
config.VOICE_LOOK_HOLD_SECONDS = 0.3
config.VOICE_LOOK_COOLDOWN_SECONDS = 20
config.IDLE_SCAN_ENABLED = False

sc = make()
check("idle + nobody around -> may glance", sc._voice_look_blocker(time.time()) is None)
blockers = []
for name, tweak, want in [
    ("switch off", lambda s: setattr(config, "VOICE_LOOK_ENABLED", False), "off"),
    ("quiet hours", lambda s: setattr(s, "_is_quiet_hours", lambda: True), "quiet hours"),
    ("manual mode", lambda s: setattr(s, "_manual_mode", True), "busy"),
    ("he's talking", lambda s: setattr(s.speech, "is_speaking", True), "busy"),
    ("just talked to someone", lambda s: setattr(s, "_last_interaction_time", time.time() - 5), "busy"),
    ("someone in view", lambda s: setattr(s, "_last_detection_time", time.time() - 1), "someone in view"),
    ("glanced 10 s ago", lambda s: setattr(s, "_last_glance_time", time.time() - 10), "cooldown"),
    ("mid-glance", lambda s: setattr(s, "_glance_until", time.time() + 5), "busy"),
]:
    s = make()
    tweak(s)
    got = s._voice_look_blocker(time.time())
    config.VOICE_LOOK_ENABLED = True
    check(f"blocked: {name}", got == want, f"({got})")

# Nobody shows up: glance over, hold, go back level (sweeps off)
sc = make()
sc._on_voice_heard()
check("glance claims the head (sweeps + drift-to-center paused)", sc._glance_active() and not sc._should_scan())
time.sleep(0.6)
check("glance -> hold -> back to level", sc.servos.targets == [(20, 100), (config.PAN_CENTER, config.TILT_CENTER)],
      str(sc.servos.targets))
check("head released afterwards", not sc._glance_active() and sc.servos.speed_override is None)
check("second voice within cooldown ignored", sc._voice_look_blocker(time.time()) == "cooldown")

# Sweeps on: rest pose is slumped
config.IDLE_SCAN_ENABLED = True
sc = make()
sc._do_glance(time.time())
check("sweeps on -> back to drooped", sc.servos.targets[-1] == (config.PAN_CENTER, config.TILT_DROOPED))
config.IDLE_SCAN_ENABLED = False

# Someone appears during the hold: no return move, tracking keeps the head
sc = make()
start = time.time()
threading.Timer(0.1, lambda: setattr(sc, "_last_detection_time", time.time())).start()
sc._do_glance(start)
check("person found -> stays with them (no return)", sc.servos.targets == [(20, 100)], str(sc.servos.targets))

# Glance pose is clamped to the calibrated range
config.VOICE_LOOK_PAN = -30
sc = make()
sc._do_glance(time.time())
check("glance clamped to PAN_MIN", sc.servos.targets[0][0] == config.PAN_MIN)

# Skipped glances are logged with the reason; the same reason again only every VOICE_SKIP_LOG_SECONDS
sc = make()
sc._last_detection_time = time.time() - 1
out = io.StringIO()
with contextlib.redirect_stdout(out):
    sc._on_voice_heard()
    sc._on_voice_heard()                                   # same reason, too soon: quiet
    sc._last_detection_time = time.time() - 60
    sc._last_glance_time = time.time() - 10
    sc._on_voice_heard()                                   # new reason: logged
    sc._voice_skip_logged = ("cooldown", time.time() - scarecrow.VOICE_SKIP_LOG_SECONDS - 1)
    sc._on_voice_heard()                                   # same reason, long enough ago: logged
lines = out.getvalue().splitlines()
check("skip reasons logged, repeats rate-limited",
      lines == ["[voice] Heard voices — not glancing (someone in view)"] +
               ["[voice] Heard voices — not glancing (cooldown)"] * 2, str(lines))

print("ALL PASS" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
