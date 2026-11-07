"""Overlapping slow moves must never leave the head stuck slow.
Reproduces the Oct 16 bug (two center(slow=True) calls overlapping saved and
'restored' the slow 0.5 as the normal speed). No hardware: the servo object is
built without __init__ and angles are moved by a fake tracking loop.
Run: venv/bin/python test_speed_override.py"""
import threading, time
import config
import servo_control
from servo_control import ServoController

config.HEAD_TRACK_SPEED = 6.5
sc = ServoController.__new__(ServoController)
sc.pan_angle, sc.tilt_angle = 60.0, 100.0
sc.target_pan, sc.target_tilt = 60.0, 100.0
sc.speed_override = None
sc.calibrating = False
sc.set_target = lambda p, t: (setattr(sc, "target_pan", p), setattr(sc, "target_tilt", t))
seen = []
stop = False
def fake_loop():
    while not stop:
        base = sc.speed_override if sc.speed_override is not None else config.HEAD_TRACK_SPEED
        seen.append(base)
        for ax, tgt in (("pan_angle", "target_pan"), ("tilt_angle", "target_tilt")):
            d = getattr(sc, tgt) - getattr(sc, ax)
            if abs(d) > 0.5:
                setattr(sc, ax, getattr(sc, ax) + max(-base, min(base, d)))
        time.sleep(0.005)
lt = threading.Thread(target=fake_loop, daemon=True); lt.start()

# Two overlapping slow returns to center (main loop + post-conversation thread)
a = threading.Thread(target=sc.center, kwargs={"slow": True})
b = threading.Thread(target=sc.center, kwargs={"slow": True})
a.start(); time.sleep(0.05); b.start(); a.join(15); b.join(15)
stop = True; lt.join(1)

slow_used = 0.5 in seen
after = sc.speed_override if sc.speed_override is not None else config.HEAD_TRACK_SPEED
ok = slow_used and sc.speed_override is None and after == 6.5 and config.HEAD_TRACK_SPEED == 6.5
print(f"{'PASS' if ok else 'FAIL'}  overlapping slow centers: slow move used={slow_used}, "
      f"override after={sc.speed_override}, effective speed after={after}, setting={config.HEAD_TRACK_SPEED}")
raise SystemExit(0 if ok else 1)
