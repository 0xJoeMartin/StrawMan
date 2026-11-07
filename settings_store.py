"""
Settings Store
--------------
Runtime-editable settings, layered over the defaults in config.py.

  - Defaults are NEVER written to disk. They are whatever config.py (plus
    .env, for the few values read from it) says at import time, so a code
    update ships new defaults and a reset can always get back to them.
  - Edits made from the dashboard are stored in settings.json (next to this
    file). "Reset" just deletes the override. Saving a value equal to the
    default removes the override, so the "modified" badge stays honest.
  - Writes are atomic (tmp file + os.replace); the previous file is kept as
    settings.json.bak.
  - A corrupt settings file is moved aside to *.corrupt and ignored, and a
    single bad value is dropped on load, so a bad edit can never stop the
    scarecrow from starting.
  - SETTINGS_SCHEMA is the single source of truth: it drives validation here
    and the /settings page (settings_page.py).

Applying values:
  - config.py builds the store as its very last step, which writes every
    override onto the config module. `from config import X` therefore sees
    the dashboard value at startup.
  - "live" settings are also written onto the config module the moment they
    are saved. Code that wants a live value must read it as `config.X` at use
    time, not via `from config import X`.
  - "restart" settings are only written to settings.json; the running
    process keeps its startup value until restarted. pending_restart() lists
    the ones that differ from what the process started with.

File format:
  {"version": 1, "settings": {"<KEY>": <value>, ...}}
"""

import json
import os
import re
import shutil
import threading

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

PERSONA_CHOICES = ["friendly", "snarky", "creepy", "menacing"]
RESOLUTIONS = ["640x480", "800x600", "1280x720", "1280x960", "1920x1080"]

GROUPS = ["AI", "Voice", "Hearing", "Camera & detection", "Behavior",
          "Quiet hours", "Logging", "Servos", "Radar", "Setup"]


def _s(key, type_, group, label, help_="", tier="config", apply="live", **kw):
    return {"key": key, "type": type_, "group": group, "label": label, "help": help_,
            "tier": tier, "apply": apply, **kw}


# tier: "main" (also on the dashboard home page) / "config" / "advanced"
# apply: "live" (next use picks it up) / "restart" (needs a service restart)
SETTINGS_SCHEMA = [
    # --- AI -----------------------------------------------------------------
    _s("AI_BACKEND", "choice", "AI", "AI backend",
       "Where his lines come from. Set on the dashboard home page.",
       tier="main", choices=["claude", "ollama", "openai"]),
    _s("PERSONA", "choice", "AI", "Persona",
       "His personality. The prompt text for each persona is on the Prompts page.",
       tier="main", choices=PERSONA_CHOICES),
    _s("AI_MODEL", "str", "AI", "Model",
       "Haiku is fast and cheap; Sonnet is wittier.", test="claude", card="claude"),
    _s("OLLAMA_BASE_URL", "url", "AI", "Server URL",
       "e.g. http://192.168.1.50:11434", card="ollama"),
    _s("OLLAMA_MODEL", "str", "AI", "Model",
       "Must accept pictures and be pulled on the server (llava, llava-llama3, bakllava…).",
       test="ollama", card="ollama"),
    _s("OPENAI_COMPAT_BASE_URL", "url", "AI", "API URL",
       "Pick a preset or paste the base URL of any OpenAI-style chat API.", presets=True, card="openai"),
    _s("OPENAI_COMPAT_MODEL", "str", "AI", "Model",
       "Must accept pictures. Avoid thinking models (gpt-5, o-series, qwen3…): "
       "they spend the short token budget thinking and say nothing.", test="openai", card="openai"),
    _s("AI_MAX_TOKENS", "int", "AI", "Max tokens per line",
       "Hard cap on line length. 60 fits 1–2 short sentences; higher and he rambles.",
       tier="advanced", min=20, max=300),

    # --- Voice --------------------------------------------------------------
    _s("TTS_ORDER", "order", "Voice", "Voice order",
       "Tried top to bottom — the first one that works speaks. Untick one to skip it.",
       choices=["elevenlabs", "openai", "piper", "espeak"],
       labels={"elevenlabs": "ElevenLabs", "openai": "OpenAI", "piper": "Piper (on the Pi)",
               "espeak": "espeak (on the Pi)"}),
    _s("ELEVENLABS_VOICE_ID", "str", "Voice", "Voice ID",
       "Voice ID from your ElevenLabs voice library.", card="tts-elevenlabs"),
    _s("ELEVENLABS_MODEL", "str", "Voice", "Model",
       "eleven_flash_v2_5 is the low-latency one.", card="tts-elevenlabs"),
    _s("ELEVENLABS_STABILITY", "float", "Voice", "Stability",
       "Lower = more dramatic variation between lines.", min=0.0, max=1.0, step=0.05, card="tts-elevenlabs"),
    _s("ELEVENLABS_STYLE", "float", "Voice", "Style",
       "How strongly the voice's style is exaggerated.", min=0.0, max=1.0, step=0.05, card="tts-elevenlabs"),
    _s("ELEVENLABS_SPEED", "float", "Voice", "Speed",
       "1.0 = the voice's own pace. ElevenLabs allows 0.7 to 1.2.", min=0.7, max=1.2, step=0.05, card="tts-elevenlabs"),
    _s("OPENAI_TTS_VOICE", "choice", "Voice", "Voice",
       "onyx and ash are deep; ballad and sage are softer. Use “Say it” to hear one.", card="tts-openai",
       choices=["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"]),
    _s("OPENAI_TTS_MODEL", "choice", "Voice", "Model",
       "gpt-4o-mini-tts follows the speaking instructions below; tts-1 models ignore them.",
       card="tts-openai", choices=["gpt-4o-mini-tts", "tts-1", "tts-1-hd"]),
    _s("OPENAI_TTS_INSTRUCTIONS", "str", "Voice", "How to speak",
       "Tone and delivery for gpt-4o-mini-tts.", card="tts-openai"),
    _s("PIPER_MODEL", "str", "Voice", "Voice model",
       "Piper voice model name, e.g. en_US-lessac-medium.", card="tts-piper"),
    _s("PIPER_SPEED", "float", "Voice", "Speed",
       "1.0 = normal. Slower is creepier.", min=0.5, max=2.0, step=0.05, card="tts-piper"),
    _s("TTS_NORMALIZE", "bool", "Voice", "Even out loudness",
       "Brings every line, from every voice, to the same loudness before it's played.", tier="advanced"),
    _s("TTS_LOUDNESS_DBFS", "float", "Voice", "Target loudness",
       "dBFS. His ElevenLabs lines measure about -14; higher is louder. Quiet hours still apply on top.",
       tier="advanced", min=-30.0, max=-6.0, step=1.0),

    # --- Hearing ------------------------------------------------------------
    _s("LISTEN_ENABLED", "bool", "Hearing", "Listen for replies",
       "Off = he talks but never listens for an answer.", apply="restart"),
    _s("STT_ORDER", "order", "Hearing", "Speech-to-text order",
       "Tried top to bottom until one works. Untick one to skip it.",
       choices=["faster-whisper", "openai", "local"],
       labels={"faster-whisper": "faster-whisper server", "openai": "OpenAI", "local": "Whisper on the Pi"}),
    _s("WHISPER_SERVER_URL", "url", "Hearing", "Server URL",
       "e.g. http://192.168.1.50:8178", card="stt-faster-whisper"),
    _s("OPENAI_STT_MODEL", "choice", "Hearing", "Model",
       "whisper-1 is the classic; the gpt-4o ones are newer.", card="stt-openai",
       choices=["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"]),
    _s("WHISPER_MODEL", "choice", "Hearing", "Model size",
       "Always loaded as the offline fallback. Bigger = slower.",
       apply="restart", choices=["tiny", "base", "small"], card="stt-local"),
    _s("MAX_CONVERSATION_TURNS", "int", "Hearing", "Max conversation turns",
       "How many times he'll answer back before moving on.", min=1, max=6),
    _s("LISTEN_ENERGY_THRESHOLD", "int", "Hearing", "Mic energy threshold",
       "Loudness that counts as someone talking. Raise it if wind or traffic sets it off; "
       "lower it if he doesn't hear people.", tier="advanced", min=50, max=5000, step=10),
    _s("VAD_CONFIDENCE", "float", "Hearing", "Voice detection confidence",
       "How sure the voice detector must be that audio is speech, not noise.",
       tier="advanced", min=0.0, max=1.0, step=0.05),
    _s("LISTEN_DURATION", "float", "Hearing", "Max listen time", "Seconds.",
       tier="advanced", min=2.0, max=15.0, step=0.5),
    _s("LISTEN_SILENCE_TIMEOUT", "float", "Hearing", "End-of-speech silence",
       "Seconds of quiet after speech that means they're done talking.",
       tier="advanced", min=0.2, max=3.0, step=0.1),
    _s("LISTEN_POST_SPEAK_DELAY", "float", "Hearing", "Pause before listening",
       "Seconds to wait after he speaks so he doesn't hear his own echo.",
       tier="advanced", min=0.0, max=2.0, step=0.1),

    # --- Camera & detection -------------------------------------------------
    _s("CAMERA_INDEX", "int", "Camera & detection", "Camera device",
       "Video device number (0 = /dev/video0).", apply="restart", min=0, max=9),
    _s("CAMERA_RESOLUTION", "resolution", "Camera & detection", "Camera resolution",
       "640×480 is recommended. Higher sends bigger photos to the AI. Widescreen sizes "
       "(1280×720, 1920×1080) crop the view on many webcams.", apply="restart",
       choices=RESOLUTIONS, targets=["CAMERA_WIDTH", "CAMERA_HEIGHT"]),
    _s("COOLDOWN_SECONDS", "int", "Camera & detection", "Cooldown between visitors",
       "Seconds after a conversation ends before he'll start another.", min=0, max=120),
    _s("DETECTION_CONFIDENCE", "float", "Camera & detection", "Person confidence",
       "How sure the detector must be that it's a person. Lower catches more, with more false alarms.",
       tier="advanced", min=0.52, max=0.95, step=0.01),   # 0.50 is the Hailo model's noise floor
    _s("DETECTION_MIN_AREA", "float", "Camera & detection", "Min person size",
       "Ignore people smaller than this fraction of the frame (far away / false positives).",
       tier="advanced", min=0.001, max=0.1, step=0.001),
    _s("DETECTION_LOG_RAW", "bool", "Camera & detection", "Log all detections (diagnostic)",
       "Writes every person candidate (confidence, size, passed or why not) to detections.log "
       "on the Pi, including ones below the confidence and size limits. Use it to tune those two; "
       "turn it off afterwards. Doesn't change who he reacts to.", tier="advanced"),
    _s("DETECTION_HOLD_FRAMES", "int", "Camera & detection", "Frames before he talks",
       "Consecutive frames a person must be seen before he starts a conversation.",
       tier="advanced", min=1, max=30),
    _s("TRACK_MIN_FRAMES", "int", "Camera & detection", "Frames before he looks",
       "Frames a person must be seen before his head follows them.",
       tier="advanced", min=1, max=30),

    # --- Behavior -----------------------------------------------------------
    _s("IDLE_SCAN_ENABLED", "bool", "Behavior", "Idle head sweeps",
       "The periodic wake-up-and-look-around. Off = no idle movement at all: he rests level, "
       "looking straight ahead (no slumping), and still turns to people and talks."),
    _s("IDLE_SCAN_MIN_INTERVAL", "float", "Behavior", "Idle wake-up: min wait",
       "Seconds asleep (head drooped) before he wakes and scans the yard.", min=10, max=600, step=5),
    _s("IDLE_SCAN_MAX_INTERVAL", "float", "Behavior", "Idle wake-up: max wait",
       "The actual wait is random between min and max.", min=10, max=600, step=5),
    _s("FILLER_LINES", "lines", "Behavior", "Filler lines",
       "Sometimes said while the AI is thinking. One per line."),
    _s("SILENCE_TAUNTS", "lines", "Behavior", "Silence taunts",
       "Said when someone doesn't answer him. One per line."),
    _s("IDLE_SCAN_RANGE", "int", "Behavior", "Idle scan width",
       "Degrees either side of center during the idle sweep.", tier="advanced", min=0, max=90),
    _s("IDLE_LIFT_SPEED", "float", "Behavior", "Idle lift speed",
       "Degrees per step when raising his head from the drooped pose.",
       tier="advanced", min=0.05, max=5.0, step=0.05),
    _s("IDLE_SCAN_SPEED", "float", "Behavior", "Idle sweep speed",
       "Degrees per step during the side-to-side sweep.", tier="advanced", min=0.05, max=5.0, step=0.05),
    _s("IDLE_DROOP_SPEED", "float", "Behavior", "Idle droop speed",
       "Degrees per step when slumping back to the drooped pose.",
       tier="advanced", min=0.05, max=5.0, step=0.05),
    _s("VOICE_LOOK_ENABLED", "bool", "Behavior", "Voice look",
       "When nobody is in view and he hears people talking, he glances toward the spot set with "
       "\"Set voice look here\" under Manual control (e.g. a blind spot behind a pillar). "
       "Silent: nothing is recorded or sent anywhere. Off during quiet hours."),
    _s("VOICE_LOOK_PAN", "int", "Behavior", "Voice look: pan",
       "Where he glances. Easiest to set with the joystick and \"Set voice look here\".",
       tier="advanced", min=0, max=180),
    _s("VOICE_LOOK_TILT", "int", "Behavior", "Voice look: tilt", tier="advanced", min=0, max=180),
    _s("VOICE_LOOK_HOLD_SECONDS", "float", "Behavior", "Voice look: hold",
       "Seconds he stares that way. If someone shows up he talks to them as usual; "
       "if not he quietly goes back to resting.", min=1, max=30, step=0.5),
    _s("VOICE_LOOK_COOLDOWN_SECONDS", "float", "Behavior", "Voice look: cooldown",
       "Minimum seconds between glances, so a chatty sidewalk doesn't keep his head busy.",
       min=5, max=300, step=5),
    _s("VOICE_LOOK_MIN_SPEECH_MS", "int", "Behavior", "Voice look: talking needed",
       "Milliseconds of talking (within about 2 s) before he reacts. Higher = fewer false alarms.",
       tier="advanced", min=100, max=2000, step=50),

    # --- Quiet hours --------------------------------------------------------
    _s("QUIET_HOURS_ENABLED", "bool", "Quiet hours", "Quiet hours", "Whisper mode at night.",
       tier="main"),
    _s("QUIET_HOURS_START", "hour", "Quiet hours", "Starts at", tier="main", min=0, max=23),
    _s("QUIET_HOURS_END", "hour", "Quiet hours", "Ends at", tier="main", min=0, max=23),
    _s("QUIET_HOURS_VOLUME", "float", "Quiet hours", "Quiet volume", "0 = silent, 1 = full volume.",
       tier="main", min=0.0, max=1.0, step=0.05),
    _s("QUIET_HOURS_IDLE_SCAN", "bool", "Quiet hours", "Head sweeps",
       "Keep doing the idle look-around during quiet hours. Off = he stays slumped asleep "
       "(quieter, less servo wear) but still wakes up when someone walks up.", tier="main"),

    # --- Logging ------------------------------------------------------------
    _s("LOG_CONVERSATIONS", "bool", "Logging", "Log conversations",
       "Append every AI line and its scene description to conversations.log on the Pi."),
    _s("SAVE_AI_PHOTOS", "bool", "Logging", "Save the photos sent to the AI",
       "For checking what he actually sees. Keeps the last 30 conversations in ai_photos/ on the Pi. "
       "These are pictures of your visitors — leave it off unless you're checking something.",
       tier="advanced"),

    # --- Servos (all advanced; calibration page comes in Phase 4) -----------
    _s("PAN_CHANNEL", "int", "Servos", "Pan channel", "PCA9685 channel (0–15).",
       tier="advanced", apply="restart", min=0, max=15),
    _s("TILT_CHANNEL", "int", "Servos", "Tilt channel", "PCA9685 channel (0–15).",
       tier="advanced", apply="restart", min=0, max=15),
    _s("JAW_CHANNEL", "int", "Servos", "Jaw channel", "PCA9685 channel (0–15).",
       tier="advanced", apply="restart", min=0, max=15),
    _s("PAN_MIN", "int", "Servos", "Pan min", "Degrees. Furthest right he can look.",
       tier="advanced", min=0, max=180),
    _s("PAN_CENTER", "int", "Servos", "Pan center", "Degrees. Straight ahead.",
       tier="advanced", min=0, max=180),
    _s("PAN_MAX", "int", "Servos", "Pan max", "Degrees. Furthest left he can look.",
       tier="advanced", min=0, max=180),
    _s("TILT_MIN", "int", "Servos", "Tilt min", "Degrees. Looking up.",
       tier="advanced", min=0, max=180),
    _s("TILT_CENTER", "int", "Servos", "Tilt center", "Degrees. Level.",
       tier="advanced", min=0, max=180),
    _s("TILT_MAX", "int", "Servos", "Tilt max", "Degrees. Looking down.",
       tier="advanced", min=0, max=180),
    _s("TILT_DROOPED", "int", "Servos", "Tilt drooped (asleep)",
       "Degrees. Head slumped forward while idle. Must be between tilt min and max.",
       tier="advanced", min=0, max=180),
    _s("JAW_CLOSED", "int", "Servos", "Jaw closed", "Degrees.",
       tier="advanced", min=0, max=180),
    _s("JAW_OPEN", "int", "Servos", "Jaw open", "Degrees.",
       tier="advanced", min=0, max=180),
    _s("HEAD_TRACK_SPEED", "float", "Servos", "Head tracking speed",
       "Max degrees per 50 Hz step while following a person.",
       tier="advanced", apply="restart", min=1.0, max=15.0, step=0.5),
    _s("PAN_MAX_SPEED", "float", "Servos", "Max pan speed",
       "Degrees per second, for every left/right head move. 0 = no limit (as fast as the servo goes).",
       tier="advanced", min=0, max=400, step=5),
    _s("TILT_MAX_SPEED", "float", "Servos", "Max tilt speed",
       "Degrees per second, for every up/down head move. The tilt servo lifts the whole head, "
       "so keep this slow to spare it. 0 = no limit.",
       tier="advanced", min=0, max=400, step=1),
    _s("JAW_THRESHOLD", "float", "Servos", "Jaw threshold",
       "Audio level below which the jaw stays shut.", tier="advanced", min=0.0, max=0.2, step=0.005),

    # --- Setup (driven by the /welcome wizard, not shown on the Settings page) ---
    _s("SETUP_DONE", "bool", "Setup", "Setup finished", tier="main"),
    _s("SERVOS_CALIBRATED", "bool", "Setup", "Servos calibrated", tier="main"),

    # --- Radar --------------------------------------------------------------
    _s("RADAR_ENABLED", "bool", "Radar", "Radar enabled",
       "HLK-LD2450 mmWave radar for distance + approach. Off if you don't have one.",
       apply="restart"),
    _s("RADAR_PORT", "str", "Radar", "Radar serial port", tier="advanced", apply="restart"),
    _s("RADAR_BAUD", "choice", "Radar", "Radar baud rate", tier="advanced", apply="restart",
       choices=[9600, 19200, 38400, 57600, 115200, 230400, 256000, 460800]),
    _s("RADAR_SETTLE_SECONDS", "float", "Radar", "Settle time after a head turn",
       "The radar is on his face: right after his head turns it reports stale targets or slides "
       "one target from object to object. Radar is ignored for this long after a turn.",
       tier="advanced", min=0, max=10, step=0.5),
    _s("RADAR_SETTLE_DEGREES", "float", "Radar", "What counts as a head turn",
       "Pan or tilt moving more than this many degrees within the settle time.",
       tier="advanced", min=1, max=45, step=1),
    _s("RADAR_MATCH_DEGREES", "float", "Radar", "Direction match",
       "A radar target only describes a person when their directions agree within this many degrees.",
       tier="advanced", min=5, max=60, step=1),
    _s("CAMERA_HFOV_DEG", "float", "Radar", "Camera field of view",
       "Horizontal field of view of the camera, in degrees — used to work out each person's direction.",
       tier="advanced", min=30, max=180, step=1),
]

SCHEMA_BY_KEY = {s["key"]: s for s in SETTINGS_SCHEMA}

# Old single-choice keys that may still be in settings.json
LEGACY_ORDER_KEYS = {"TTS_ENGINE": "TTS_ORDER", "STT_ENGINE": "STT_ORDER"}

MAX_STR = 300
MAX_LINES = 50


def _rules(v):
    """Cross-field checks over the full effective settings. Yields
    (keys involved, message) for each rule that fails."""
    if not v["PAN_MIN"] < v["PAN_CENTER"] < v["PAN_MAX"]:
        yield ["PAN_MIN", "PAN_CENTER", "PAN_MAX"], "Pan needs min < center < max"
    if not v["TILT_MIN"] < v["TILT_CENTER"] < v["TILT_MAX"]:
        yield ["TILT_MIN", "TILT_CENTER", "TILT_MAX"], "Tilt needs min < center < max"
    if not v["TILT_MIN"] <= v["TILT_DROOPED"] <= v["TILT_MAX"]:
        yield ["TILT_DROOPED", "TILT_MIN", "TILT_MAX"], "Tilt drooped must be between tilt min and max"
    if v["JAW_CLOSED"] == v["JAW_OPEN"]:
        yield ["JAW_CLOSED", "JAW_OPEN"], "Jaw open and closed can't be the same angle"
    chans = [v["PAN_CHANNEL"], v["TILT_CHANNEL"], v["JAW_CHANNEL"]]
    if len(set(chans)) != 3:
        yield ["PAN_CHANNEL", "TILT_CHANNEL", "JAW_CHANNEL"], "Pan, tilt and jaw need different channels"
    if v["IDLE_SCAN_MIN_INTERVAL"] > v["IDLE_SCAN_MAX_INTERVAL"]:
        yield (["IDLE_SCAN_MIN_INTERVAL", "IDLE_SCAN_MAX_INTERVAL"],
               "Idle wake-up min wait can't be longer than the max")


def coerce(meta: dict, value):
    """Validate + normalize one value against its schema entry. Raises ValueError."""
    t, label = meta["type"], meta["label"]

    def bounded(x):
        if "min" in meta and x < meta["min"]:
            raise ValueError(f"{label}: must be at least {meta['min']}")
        if "max" in meta and x > meta["max"]:
            raise ValueError(f"{label}: must be at most {meta['max']}")
        return x

    if t == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{label}: must be on or off")
    if t in ("int", "hour"):
        if isinstance(value, bool):
            raise ValueError(f"{label}: must be a whole number")
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: must be a whole number")
        if f != int(f):
            raise ValueError(f"{label}: must be a whole number")
        return bounded(int(f))
    if t == "float":
        if isinstance(value, bool):
            raise ValueError(f"{label}: must be a number")
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: must be a number")
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError(f"{label}: must be a number")
        return bounded(f)
    if t in ("choice", "resolution"):
        for c in meta["choices"]:
            if value == c or str(value) == str(c):
                return c
        raise ValueError(f"{label}: must be one of {', '.join(map(str, meta['choices']))}")
    if t in ("str", "url"):
        if not isinstance(value, str):
            raise ValueError(f"{label}: must be text")
        value = value.strip()
        if not value:
            raise ValueError(f"{label}: can't be empty — use Reset to go back to the default")
        if len(value) > MAX_STR or "\n" in value:
            raise ValueError(f"{label}: must be a single line under {MAX_STR} characters")
        if t == "url":
            if not re.match(r"^https?://[^\s/]+", value):
                raise ValueError(f"{label}: must start with http:// or https://")
            value = value.rstrip("/")
        return value
    if t == "order":
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, list):
            raise ValueError(f"{label}: must be a list")
        out = []
        for v in value:
            if v not in meta["choices"]:
                raise ValueError(f"{label}: unknown option {v!r}")
            if v not in out:
                out.append(v)
        if not out:
            raise ValueError(f"{label}: keep at least one ticked")
        return out
    if t == "lines":
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, list):
            raise ValueError(f"{label}: must be a list of lines")
        lines = [str(l).strip() for l in value if str(l).strip()]
        if not lines:
            raise ValueError(f"{label}: needs at least one line")
        if len(lines) > MAX_LINES:
            raise ValueError(f"{label}: at most {MAX_LINES} lines")
        if any(len(l) > MAX_STR for l in lines):
            raise ValueError(f"{label}: lines must be under {MAX_STR} characters")
        return lines
    raise ValueError(f"{label}: unknown type {t}")


class SettingsStore:
    def __init__(self, config_module, path: str = SETTINGS_PATH):
        self._config = config_module
        self._path = path
        self._lock = threading.RLock()
        self._hooks = {}
        self._defaults = {s["key"]: self._read_config(s) for s in SETTINGS_SCHEMA}
        self._overrides = {}
        self._load()
        for key in self._overrides:
            self._write_config(key, self._overrides[key])
        # What this process started with — restart settings differing from
        # this are "pending restart".
        self._startup = self.values()

    # --- config module bridge ----------------------------------------------

    def _read_config(self, meta):
        if meta["type"] == "resolution":
            w, h = (getattr(self._config, t) for t in meta["targets"])
            return f"{w}x{h}"
        val = getattr(self._config, meta["key"])
        return list(val) if meta["type"] in ("lines", "order") else val

    def _write_config(self, key, value):
        meta = SCHEMA_BY_KEY[key]
        if meta["type"] == "resolution":
            w, h = (int(x) for x in value.split("x"))
            setattr(self._config, meta["targets"][0], w)
            setattr(self._config, meta["targets"][1], h)
        else:
            setattr(self._config, key, list(value) if isinstance(value, list) else value)

    # --- persistence -------------------------------------------------------

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            raw = data.get("settings", {})
            if not isinstance(raw, dict):
                raise ValueError("'settings' is not an object")
        except Exception as e:
            bad = self._path + ".corrupt"
            try:
                shutil.move(self._path, bad)
            except OSError:
                pass
            print(f"[settings] settings.json unreadable ({e}) — moved to {bad}, using defaults")
            return

        # Settings that became ordered lists: "X first" -> [X, then the default order]
        for old, new in LEGACY_ORDER_KEYS.items():
            if old in raw:
                first = raw.pop(old)
                if new not in raw:
                    rest = [c for c in SCHEMA_BY_KEY[new]["choices"] if c != first]
                    raw[new] = [first] + rest
                    print(f"[settings] {old}={first!r} carried over as {new}")
        clean = {}
        for key, value in raw.items():
            meta = SCHEMA_BY_KEY.get(key)
            if not meta:
                print(f"[settings] Ignoring unknown setting {key}")
                continue
            try:
                value = coerce(meta, value)
            except ValueError as e:
                print(f"[settings] Ignoring bad value for {key}: {e}")
                continue
            if value != self._defaults[key]:   # same as default = not an override
                clean[key] = value
        # A combination that breaks a cross-field rule drops the overrides
        # involved, falling back to the (known-good) defaults for them.
        merged = {**self._defaults, **clean}
        for keys, msg in list(_rules(merged)):
            dropped = [k for k in keys if k in clean]
            for k in dropped:
                clean.pop(k)
            print(f"[settings] {msg} — ignoring {', '.join(dropped) or 'nothing'}")
        self._overrides = clean
        if clean:
            print(f"[settings] Loaded {len(clean)} override(s) from settings.json")

    def _save(self):
        data = {"version": 1, "settings": self._overrides}
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        if os.path.exists(self._path):
            shutil.copy2(self._path, self._path + ".bak")
        os.replace(tmp, self._path)

    # --- reads -------------------------------------------------------------

    def get(self, key):
        with self._lock:
            return self._overrides.get(key, self._defaults[key])

    def default(self, key):
        return self._defaults[key]

    def is_overridden(self, key) -> bool:
        with self._lock:
            return key in self._overrides

    def values(self) -> dict:
        with self._lock:
            return {**self._defaults, **self._overrides}

    def pending_restart(self) -> list:
        """Restart-only settings whose saved value differs from what this
        process started with."""
        cur = self.values()
        return [s["key"] for s in SETTINGS_SCHEMA
                if s["apply"] == "restart" and cur[s["key"]] != self._startup[s["key"]]]

    # --- writes ------------------------------------------------------------

    def add_hook(self, key, fn):
        """fn(value) runs before a live setting is committed. Raising
        ValueError rejects the change (nothing is saved)."""
        self._hooks[key] = fn

    def set_many(self, changes: dict) -> dict:
        """Validate and save several settings at once (all or nothing).
        A value of None resets that key to its default. Returns the new
        effective values of the changed keys."""
        with self._lock:
            new = dict(self._overrides)
            for key, value in changes.items():
                meta = SCHEMA_BY_KEY.get(key)
                if not meta:
                    raise ValueError(f"Unknown setting {key}")
                if value is None:
                    new.pop(key, None)
                    continue
                value = coerce(meta, value)
                if value == self._defaults[key]:
                    new.pop(key, None)
                else:
                    new[key] = value
            merged = {**self._defaults, **new}
            for _keys, msg in _rules(merged):
                raise ValueError(msg)

            current = self.values()
            changed = [k for k in changes if merged[k] != current[k]]
            for key in changed:
                if key in self._hooks and SCHEMA_BY_KEY[key]["apply"] == "live":
                    self._hooks[key](merged[key])

            if new != self._overrides:
                self._overrides = new
                self._save()
            for key in changed:
                if SCHEMA_BY_KEY[key]["apply"] == "live":
                    self._write_config(key, merged[key])
            return {k: merged[k] for k in changes}

    def set(self, key, value):
        return self.set_many({key: value})[key]

    def reset(self, keys):
        return self.set_many({k: None for k in keys})
