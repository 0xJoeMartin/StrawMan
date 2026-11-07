#!/usr/bin/env python3
"""
First-run setup checks — fake scarecrow, throwaway settings, no hardware.
(Needs the Pi's packages: it loads the real dashboard app.)

    python test_setup.py

Covers: readiness checks, the quiet-until-set-up gate, existing installs
being marked done automatically, the dashboard redirect, and the wizard's own
endpoints (status, snapshot, mic test, done).
"""

import os
import tempfile
import threading
import types

import numpy as np

os.environ.pop("INVOCATION_ID", None)
import config  # noqa: E402
import settings_store  # noqa: E402
config.SETTINGS = settings_store.SettingsStore(config, path=os.path.join(tempfile.mkdtemp(), "settings.json"))

import dashboard_auth  # noqa: E402
dashboard_auth.PIN_PATH = os.path.join(tempfile.mkdtemp(), "pin.json")
import web_dashboard as wd  # noqa: E402
import readiness  # noqa: E402


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'FAIL'}  {name}{'' if cond or not extra else '  ' + str(extra)}")
    if not cond:
        raise SystemExit(1)


def fake_scarecrow(listening=True, heard="Trick or treat"):
    det = types.SimpleNamespace(cap=types.SimpleNamespace(isOpened=lambda: True), _backend="hailo")
    lst = types.SimpleNamespace(_mic_index=None, listen_for_response=lambda: heard) if listening else None
    return types.SimpleNamespace(
        detector=det, servos=types.SimpleNamespace(kit=object()), listener=lst,
        radar=types.SimpleNamespace(enabled=False, _frame_count=0),
        speech=types.SimpleNamespace(is_speaking=False, engine="elevenlabs"),
        brain=types.SimpleNamespace(backend="claude"), _manual_mode=False,
        _interaction_lock=threading.Lock())


def main():
    sc = fake_scarecrow()
    wd._scarecrow_instance, wd._scarecrow_brain, wd._speech_engine = sc, sc.brain, sc.speech
    wd._latest_scene = (np.zeros((480, 640, 3), np.uint8), [{"box": 1}], [], None)
    config.AI_BACKEND = "claude"
    config.TTS_ORDER = ["elevenlabs", "openai", "piper", "espeak"]

    print("readiness")
    config.ANTHROPIC_API_KEY = config.ELEVENLABS_API_KEY = config.OPENAI_API_KEY = ""
    st = {i["id"]: i for i in readiness.status()["items"]}
    check("fresh: no PIN, no AI key, no voice -> missing", st["pin"]["status"] == st["ai"]["status"] == "missing"
          and (st["voice"]["status"] == "missing" or "espeak" in st["voice"]["detail"].lower() or st["voice"]["status"] == "ok"))
    check("camera with a frame + Hailo -> ok", st["camera"]["status"] == "ok")
    check("servo board, default limits -> warn (calibrate)", st["servos"]["status"] == "warn" and st["servos"]["fix"] == "/calibrate")
    check("radar off -> off", st["radar"]["status"] == "off")
    check("core not ok", not readiness.status()["core_ok"])

    dashboard_auth.set_pin("864213")
    config.ANTHROPIC_API_KEY, config.ELEVENLABS_API_KEY = "sk-ant-x", "el-x"
    st = readiness.status()
    check("PIN + AI key + voice key + camera -> core ok", st["core_ok"], st["items"])
    check("no offline voice -> voice is a warning, not a blocker",
          {i["id"]: i for i in st["items"]}["voice"]["status"] in ("warn", "ok"))

    print("quiet until set up")
    import scarecrow
    s = scarecrow.Scarecrow.__new__(scarecrow.Scarecrow)
    s._manual_mode, s._interaction_lock = False, threading.Lock()
    s.speech = types.SimpleNamespace(is_speaking=False)
    s._last_interaction_time = 0
    config.SETUP_DONE = False
    check("not set up -> won't start conversations", not s._can_interact())

    c = wd.app.test_client()
    with c.session_transaction(base_url="https://localhost") as sess:
        sess["auth"] = True
        sess["pin_at"] = dashboard_auth.pin_stamp()
    get = lambda p: c.get(p, base_url="https://localhost")
    post = lambda p, b: c.post(p, json=b, base_url="https://localhost")
    r = get("/")
    check("dashboard redirects to /welcome", r.status_code == 302 and r.headers["Location"].endswith("/welcome"))
    check("wizard page renders", get("/welcome").status_code == 200 and b"Let's get StrawMan talking" in get("/welcome").data)

    print("existing installs")
    readiness.auto_mark_done()
    check("core ok -> marked done automatically", config.SETUP_DONE and config.SETTINGS.get("SETUP_DONE"))
    check("…and he talks again", s._can_interact())
    check("dashboard shows normally", get("/").status_code == 200)
    config.SETTINGS.set("SETUP_DONE", False)
    config.ANTHROPIC_API_KEY = ""
    readiness.auto_mark_done()
    check("something essential missing -> not marked done", not config.SETUP_DONE)

    print("wizard endpoints")
    j = get("/api/setup/status").get_json()
    check("status: items, hardware, people in view", j["ok"] and j["hardware"]["servo_board"] and j["people_in_view"] == 1)
    r = get("/api/setup/snapshot.jpg")
    check("snapshot is a JPEG", r.status_code == 200 and r.data[:3] == b"\xff\xd8\xff")
    r = post("/api/setup/mic-test", {})
    check("mic test returns what he heard", r.get_json().get("text") == "Trick or treat")
    sc._interaction_lock.acquire()
    check("mic test refused while he's talking to someone", post("/api/setup/mic-test", {}).status_code == 409)
    sc._interaction_lock.release()
    sc.listener = None
    check("mic test refused when listening is off", post("/api/setup/mic-test", {}).status_code == 409)
    r = post("/api/setup/done", {"servos_calibrated": True})
    check("servos ticked as calibrated", r.status_code == 200 and config.SERVOS_CALIBRATED
          and {i["id"]: i for i in r.get_json()["items"]}["servos"]["status"] == "ok")
    r = post("/api/setup/done", {"done": True})
    check("finish -> set up", config.SETUP_DONE and any("Setup finished" in e["text"] for e in wd._interaction_log))
    j = get("/api/setup/cameras").get_json()
    check("camera list: real capture devices only, with names", j["ok"] and all(
        c["path"].startswith("/dev/video") and c["name"] for c in j["cameras"]), j)
    check("settings page links to the wizard", b'href="/welcome"' in get("/settings").data)
    check("dashboard has the Setup card", b'id="setup-items"' in get("/").data)

    print("\nAll setup checks passed.")


if __name__ == "__main__":
    main()
