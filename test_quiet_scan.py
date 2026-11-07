"""Idle sweeps vs quiet hours: runs the real _idle_behavior loop with fake
servos and a fake clock. Run: venv/bin/python test_quiet_scan.py"""
import threading
import config
import scarecrow


class FakeServos:
    def __init__(self, tilt=None):
        self.targets = []
        self.tilt_angle = config.TILT_CENTER if tilt is None else tilt
    def set_target(self, pan, tilt):
        self.targets.append((round(pan), round(tilt)))
        self.tilt_angle = tilt
    def _clamp_pan(self, a):
        return a


def run(quiet: bool, scan_setting: bool, cycles: int = 3, enabled: bool = True):
    config.IDLE_SCAN_ENABLED = enabled
    config.QUIET_HOURS_IDLE_SCAN = scan_setting
    sc = scarecrow.Scarecrow.__new__(scarecrow.Scarecrow)
    sc.servos = FakeServos()
    sc.running = True
    sc._is_quiet_hours = lambda: quiet
    sc._should_scan = lambda: True
    sc._wait_for_pose = lambda *a, **k: None
    # Fake clock: sleep() advances it; stop after enough time for `cycles` wake-ups.
    clock = {"t": 1_000_000.0}
    stop_at = clock["t"] + cycles * (config.IDLE_SCAN_MAX_INTERVAL + 60)
    real_sleep, real_time = scarecrow.time.sleep, scarecrow.time.time
    def fake_sleep(s):
        clock["t"] += s
        if clock["t"] > stop_at:
            sc.running = False
    scarecrow.time.sleep = fake_sleep
    scarecrow.time.time = lambda: clock["t"]
    try:
        sc._idle_behavior()
    finally:
        scarecrow.time.sleep, scarecrow.time.time = real_sleep, real_time
    sweeps = sum(1 for p, t in sc.servos.targets if p != config.PAN_CENTER)
    return sweeps, sc.servos.targets[:3]


cases = [
    # name, quiet, quiet-hours sweeps, master switch, expect sweeps
    ("quiet hours, sweeps OFF", True, False, True, False),
    ("quiet hours, sweeps ON ", True, True, True, True),
    ("daytime,     sweeps OFF", False, False, True, True),
    ("daytime,     sweeps ON ", False, True, True, True),
    ("MASTER OFF,  daytime   ", False, True, False, False),
    ("MASTER OFF,  quiet+ON  ", True, True, False, False),
]
ok = True
for name, quiet, setting, enabled, expect_sweeps in cases:
    sweeps, first = run(quiet, setting, enabled=enabled)
    good = (sweeps > 0) == expect_sweeps
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  {name}: side-sweep targets={sweeps}  first targets={first}")

# Resting pose with sweeps off: level at startup, never slumped.
_, first = run(False, False, enabled=False)
good = first[:1] == [(config.PAN_CENTER, config.TILT_CENTER)] and all(t != config.TILT_DROOPED for _, t in first)
ok &= good
print(f"{'PASS' if good else 'FAIL'}  sweeps OFF: rests level, not slumped   first targets={first}")

# Switched off while slumped: lifts back to level.
config.IDLE_SCAN_ENABLED = False
sc = scarecrow.Scarecrow.__new__(scarecrow.Scarecrow)
sc.servos = FakeServos(tilt=config.TILT_DROOPED)
sc._should_scan = lambda: True
sc._wait_for_pose = lambda *a, **k: None
sc._lift_to_level_if_drooped()
good = sc.servos.targets == [(config.PAN_CENTER, config.TILT_CENTER)]
ok &= good
print(f"{'PASS' if good else 'FAIL'}  switched off while slumped -> lifts to level   targets={sc.servos.targets}")

print("ALL PASS" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
