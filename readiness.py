"""
Readiness — what's set up and working right now
-----------------------------------------------
One place that answers "can he do his job?", used by the setup wizard
(/welcome), the dashboard's Setup card, and startup.

Each check returns {"id", "label", "status", "detail", "fix"} where status is
  ok       working
  warn     works, but something's worth knowing (e.g. no offline voice)
  missing  needs setting up before he can do this
  off      turned off / not fitted, by choice or by hardware
CORE checks must all be ok for him to work: PIN, AI, voice, camera.

Checks are cheap: no AI calls; the only network calls are short "is it
there?" requests to LAN servers (Ollama, faster-whisper), cached briefly.
The cards' Test buttons do the real end-to-end tests.
"""

import time

import config

CORE = ("pin", "ai", "voice", "camera")
_cache = {}


def _reachable(url: str, timeout: float = 1.5) -> bool:
    """GET url, cached for 30 s."""
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    try:
        import requests
        ok = requests.get(url, timeout=timeout).status_code == 200
    except Exception:
        ok = False
    _cache[url] = (time.time(), ok)
    return ok


def _item(id_, label, status, detail="", fix=""):
    return {"id": id_, "label": label, "status": status, "detail": detail, "fix": fix}


def _parts():
    import web_dashboard as wd
    return getattr(wd, "_scarecrow_instance", None), wd


def check_pin():
    import dashboard_auth
    return _item("pin", "Dashboard PIN", "ok" if dashboard_auth.pin_is_set() else "missing",
                 "" if dashboard_auth.pin_is_set() else "Protects the dashboard on your network", "/setup")


def check_ai():
    import llm
    backend = config.AI_BACKEND
    sc, wd = _parts()
    if sc and getattr(sc, "brain", None):
        backend = sc.brain.backend
    p = llm.PROVIDERS[backend]()
    name = {"claude": "Claude", "ollama": "Ollama", "openai": "OpenAI-compatible"}[backend]
    try:
        p.ensure_ready()
    except ValueError as e:
        return _item("ai", "AI brain", "missing", f"{name}: {e}", "/welcome#ai")
    if backend == "ollama" and not _reachable(f"{p.url()}/api/tags"):
        return _item("ai", "AI brain", "missing", f"Ollama isn't answering at {p.url()}", "/welcome#ai")
    return _item("ai", "AI brain", "ok", f"{name} — {p.model()}")


def check_voice():
    import tts_providers
    usable = [n for n in tts_providers.order() if not tts_providers.PROVIDERS[n].unavailable()]
    if not usable:
        return _item("voice", "Voice", "missing", "No voice can speak — add a key or install espeak", "/welcome#voice")
    first = tts_providers.PROVIDERS[usable[0]].label
    offline = any(n in usable for n in ("piper", "espeak"))
    if not offline:
        return _item("voice", "Voice", "warn",
                     f"{first} — but no voice works without internet (sudo apt install espeak-ng)", "/settings")
    return _item("voice", "Voice", "ok", first)


def check_hearing():
    import stt_providers
    sc, wd = _parts()
    if not config.LISTEN_ENABLED:
        return _item("hearing", "Hearing", "off", "Listening is turned off — he talks but doesn't listen", "/welcome#hearing")
    if sc is not None and getattr(sc, "listener", None) is None:
        return _item("hearing", "Hearing", "warn", "Turned on, but needs a restart to start listening", "/settings")
    usable = []
    for n in stt_providers.order():
        p = stt_providers.PROVIDERS[n]
        if p.unavailable():
            continue
        if n == "faster-whisper" and not _reachable(f"{config.WHISPER_SERVER_URL}/health"):
            continue
        usable.append(p.label)
    if not usable:
        return _item("hearing", "Hearing", "missing", "No speech-to-text works yet", "/welcome#hearing")
    return _item("hearing", "Hearing", "ok", usable[0])


def check_camera():
    sc, wd = _parts()
    if sc is None:
        return _item("camera", "Camera", "missing", "The scarecrow isn't running", "")
    det = sc.detector
    frame = wd.get_latest_scene()[0]
    if not det.cap.isOpened() or frame is None:
        return _item("camera", "Camera", "missing", f"No picture from camera {config.CAMERA_INDEX}", "/welcome#camera")
    backend = getattr(det, "_backend", None)
    if backend == "hailo":
        return _item("camera", "Camera", "ok", "Seeing people with the Hailo accelerator")
    if backend == "cpu":
        return _item("camera", "Camera", "warn", "Seeing people on the CPU (works, tracking is choppier)")
    return _item("camera", "Camera", "missing", "Picture OK, but no person detector installed", "")


def check_servos():
    sc, wd = _parts()
    servos = getattr(sc, "servos", None) if sc else None
    if not servos or servos.kit is None:
        return _item("servos", "Head & jaw", "off", "No servo board found — his head and jaw won't move", "/welcome#servos")
    keys = ("PAN_MIN", "PAN_CENTER", "PAN_MAX", "TILT_MIN", "TILT_CENTER", "TILT_MAX", "TILT_DROOPED",
            "JAW_CLOSED", "JAW_OPEN")
    if any(config.SETTINGS.is_overridden(k) for k in keys) or config.SERVOS_CALIBRATED:
        return _item("servos", "Head & jaw", "ok", "Servo board found, limits set")
    return _item("servos", "Head & jaw", "warn", "Servo board found — limits are still the defaults", "/calibrate")


def check_radar():
    sc, wd = _parts()
    radar = getattr(sc, "radar", None) if sc else None
    if not config.RADAR_ENABLED:
        return _item("radar", "Radar", "off", "Turned off — he judges distance from the camera alone", "/settings")
    if not radar or not radar.enabled:
        return _item("radar", "Radar", "off", f"Not found on {config.RADAR_PORT} — optional", "/settings")
    if getattr(radar, "_frame_count", 0) == 0:
        return _item("radar", "Radar", "warn", "Port open but no readings yet", "/settings")
    return _item("radar", "Radar", "ok", "Measuring distance and approach")


CHECKS = [check_pin, check_ai, check_voice, check_hearing, check_camera, check_servos, check_radar]


def status() -> dict:
    items = []
    for fn in CHECKS:
        try:
            items.append(fn())
        except Exception as e:
            items.append(_item(fn.__name__[6:], fn.__name__[6:].title(), "warn", f"Couldn't check: {e}"))
    by_id = {i["id"]: i for i in items}
    core_ok = all(by_id[c]["status"] in ("ok", "warn") for c in CORE)
    return {"items": items, "core_ok": core_ok, "setup_done": bool(config.SETUP_DONE)}


def hardware() -> dict:
    """What was found at startup, for the wizard's first step."""
    sc, wd = _parts()
    out = {"running": sc is not None}
    if not sc:
        return out
    lst = getattr(sc, "listener", None)
    mic = None
    if lst is not None and getattr(lst, "_mic_index", None) is not None:
        try:
            mic = lst._pa.get_device_info_by_index(lst._mic_index)["name"]
        except Exception:
            mic = "found"
    out.update({
        "camera": bool(sc.detector.cap.isOpened()),
        "detector": getattr(sc.detector, "_backend", None),
        "servo_board": sc.servos.kit is not None,
        "mic": mic,
        "listening": lst is not None,
        "radar": bool(getattr(sc, "radar", None) and sc.radar.enabled),
    })
    return out


def auto_mark_done():
    """An install that already works (like one set up before the wizard existed)
    is marked set up, so it's never sent through the wizard or kept quiet."""
    if config.SETUP_DONE:
        return
    st = status()
    if st["core_ok"]:
        config.SETTINGS.set("SETUP_DONE", True)
        print("[setup] Everything essential works — setup marked done")
