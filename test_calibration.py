#!/usr/bin/env python3
"""
Calibration page checks — the servo board is forced OFF, so nothing moves.

    python test_calibration.py

Runs the /calibrate API against the real ServoController in simulation mode
with a throwaway settings file: control on/off, jog limits, saving limits
(with validation), the idle auto-release, and manual mode stopping the
scarecrow from following people.
"""

import os
import sys
import tempfile
import time
import types

import servo_control
servo_control.SERVOS_AVAILABLE = False      # never touch real hardware from a test

import config
import settings_store
config.SETTINGS = settings_store.SettingsStore(config, path=os.path.join(tempfile.mkdtemp(), "settings.json"))

events = []
wd = types.ModuleType("web_dashboard")
wd.log_event = lambda src, text, **kw: events.append(text)
sys.modules["web_dashboard"] = wd

from flask import Flask  # noqa: E402
import calibration_page  # noqa: E402
import ui_kit  # noqa: E402


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'FAIL'}  {name}{'' if cond or not extra else '  ' + str(extra)}")
    if not cond:
        raise SystemExit(1)


def wait_until(fn, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.05)
    return False


def main():
    servos = servo_control.ServoController()
    servos.start_tracking_loop()
    sc = types.SimpleNamespace(_manual_mode=False, speech=types.SimpleNamespace(is_speaking=False))
    wd._servo_controller, wd._scarecrow_instance = servos, sc
    app = Flask(__name__)
    ui_kit.install(app)
    app.register_blueprint(calibration_page.bp)
    c = app.test_client()
    post = lambda url, body: c.post(url, json=body)

    print("page")
    check("renders", c.get("/calibrate").status_code == 200)

    print("control")
    check("moves refused until you take control", post("/api/calibrate/move", {"axis": "pan", "delta": 5}).status_code == 409)
    j = post("/api/calibrate/control", {"on": True}).get_json()
    check("take control -> manual mode + slow moves", j["control"] and sc._manual_mode and servos.calibrating)

    print("moving")
    post("/api/calibrate/move", {"axis": "pan", "angle": 60})
    check("pan glides to 60", wait_until(lambda: abs(servos.pan_angle - 60) < 1))
    t0 = time.time(); post("/api/calibrate/move", {"axis": "pan", "angle": 90}); wait_until(lambda: abs(servos.pan_angle - 90) < 1)
    check("at calibration speed (30° takes > 0.4 s)", time.time() - t0 > 0.4)
    post("/api/calibrate/move", {"axis": "pan", "angle": -50})
    check("never below 0°", servos.target_pan == 0)
    post("/api/calibrate/move", {"axis": "tilt", "angle": 400})
    check("never above 180°", servos.target_tilt == 180)
    post("/api/calibrate/move", {"axis": "tilt", "angle": config.TILT_CENTER})
    post("/api/calibrate/move", {"axis": "jaw", "delta": 5})
    check("jaw jogs directly", servos.jaw_angle == config.JAW_CLOSED + 5)
    sc.speech.is_speaking = servos._jaw_active = True
    check("jaw refused while he's talking", post("/api/calibrate/move", {"axis": "jaw", "delta": 1}).status_code == 409)
    sc.speech.is_speaking = servos._jaw_active = False

    print("saving limits")
    post("/api/calibrate/move", {"axis": "pan", "angle": 95}); wait_until(lambda: abs(servos.pan_angle - 95) < 1)
    r = post("/api/calibrate/set", {"axis": "pan", "key": "PAN_CENTER"})
    check("save center = 95, live at once", r.status_code == 200 and config.PAN_CENTER == 95 and config.SETTINGS.get("PAN_CENTER") == 95)
    post("/api/calibrate/move", {"axis": "pan", "angle": 10})
    r = post("/api/calibrate/set", {"axis": "pan", "key": "PAN_MAX"})
    check("max below center refused", r.status_code == 400 and config.PAN_MAX != 10, r.get_json())
    check("wrong limit for the axis refused", post("/api/calibrate/set", {"axis": "pan", "key": "JAW_OPEN"}).status_code == 400)
    check("logged", any("pan center = 95°" in e for e in events), events)

    print("poses")
    sc._manual_mode = False
    check("pose refused without control", post("/api/calibrate/pose", {"pose": "assembly"}).status_code == 409)
    post("/api/calibrate/control", {"on": True})
    r = post("/api/calibrate/pose", {"pose": "assembly"})
    check("assembly: every servo to 90°", r.status_code == 200 and servos.target_pan == servos.target_tilt == 90
          and servos.jaw_angle == 90)
    check("…past the saved limits is allowed (it's for assembling)", servos.jaw_angle > config.JAW_OPEN)
    r = post("/api/calibrate/pose", {"pose": "center"})
    check("saved center: pan/tilt center, jaw closed", servos.target_pan == config.PAN_CENTER
          and servos.target_tilt == config.TILT_CENTER and servos.jaw_angle == config.JAW_CLOSED)
    check("unknown pose refused", post("/api/calibrate/pose", {"pose": "dance"}).status_code == 400)

    print("test sweep")
    check("sweep starts", post("/api/calibrate/sweep", {"axis": "jaw"}).status_code == 200)
    check("other moves wait for it", post("/api/calibrate/move", {"axis": "pan", "delta": 1}).status_code == 409)
    check("finishes", wait_until(lambda: not calibration_page._sweep_lock.locked(), 6))

    print("handing back")
    j = post("/api/calibrate/control", {"on": False}).get_json()
    check("done -> manual off, normal speed, recenter", not j["control"] and not servos.calibrating
          and servos.target_pan == config.PAN_CENTER)
    calibration_page.IDLE_RELEASE_SECONDS = 1
    post("/api/calibrate/control", {"on": True})
    check("forgotten tab: control handed back after idling", wait_until(lambda: not sc._manual_mode, 12))
    check("idle release logged", any("idle" in e for e in events))

    print("manual mode stops him following people (scarecrow.py)")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scarecrow.py")).read()
    check("tracking gated on manual mode", "if credible and not self._manual_mode:" in src)
    check("drift-to-center gated on manual mode", "not self._manual_mode and not self._returned_to_center" in src)

    servos.stop_tracking_loop()
    print("\nAll calibration checks passed.")


if __name__ == "__main__":
    main()
