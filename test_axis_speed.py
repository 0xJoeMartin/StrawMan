"""Per-axis speed caps: run the real tracking loop (servo writes stubbed) and time
pan and tilt moves. Run: venv/bin/python test_axis_speed.py"""
import threading
import time
from collections import deque

import config
from servo_control import ServoController

ok = True


def check(name, good, detail=""):
    global ok
    ok &= bool(good)
    print(f"{'PASS' if good else 'FAIL'}  {name}  {detail}")


def make():
    s = ServoController.__new__(ServoController)
    s.kit = None
    s.pan_angle, s.tilt_angle = 90.0, 100.0
    s.target_pan, s.target_tilt = 90.0, 100.0
    s.speed_override = None
    s.calibrating = False
    s._pose_log = deque(maxlen=500)
    s._set_servo = lambda ch, a: None
    s._tracking_active = True
    threading.Thread(target=s._tracking_loop, daemon=True).start()
    return s


def time_move(s, attr, target, timeout=20):
    setattr(s, "target_" + attr, target)
    t0 = time.time()
    while abs(getattr(s, attr + "_angle") - target) > 0.6 and time.time() - t0 < timeout:
        time.sleep(0.005)
    return time.time() - t0


config.HEAD_TRACK_SPEED = 6.5
config.PAN_MAX_SPEED = 130.0
config.TILT_MAX_SPEED = 8.0

s = make()
t = time_move(s, "pan", 0)           # 90 degrees
rate = 90 / t
check("pan capped near 130 deg/s", 110 <= rate <= 140, f"({rate:.0f} deg/s, {t:.2f}s for 90 deg)")
t = time_move(s, "tilt", 120)        # 20 degrees (level -> drooped)
rate = 20 / t
check("tilt capped near 8 deg/s", 7 <= rate <= 8.8, f"({rate:.1f} deg/s, {t:.2f}s for 20 deg)")

# Slower requests still win (idle lift/droop speed_override, calibration)
s.speed_override = 0.05              # 0.05 deg/step ~= 2.5 deg/s
t = time_move(s, "tilt", 110)
rate = 10 / t
check("slower speed_override still applies to tilt", rate < 3.5, f"({rate:.1f} deg/s)")
s.speed_override = None

# 0 = no cap: back to HEAD_TRACK_SPEED
config.PAN_MAX_SPEED = 0
t = time_move(s, "pan", 90)
rate = 90 / t
check("PAN_MAX_SPEED 0 -> uncapped", rate > 200, f"({rate:.0f} deg/s)")

# Live: changing the setting takes effect without a restart
config.TILT_MAX_SPEED = 20.0
t = time_move(s, "tilt", 90)
rate = 20 / t
check("tilt cap change applies live", 17 <= rate <= 21.5, f"({rate:.1f} deg/s)")
s._tracking_active = False

print("ALL PASS" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
