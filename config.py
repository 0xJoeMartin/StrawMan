"""
Scarecrow Configuration
-----------------------
Code defaults for every tunable. Most of these can be changed from the
dashboard's Settings page; those edits live in settings.json and are applied
on top of these values at the bottom of this file (see settings_store.py).
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- API Keys ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # "Rachel" default
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# --- AI Backend ---
AI_BACKEND = "claude"           # "claude", "ollama" or "openai" (any OpenAI-compatible API)
AI_MAX_TOKENS = 60              # ~45 words of headroom — fits 1-2 short sentences without truncation

# Claude settings
AI_MODEL = "claude-haiku-5-5"  # Fast + cheap. Use "claude-sonnet-4-6" for wittier lines.

# Ollama settings (local LLM on Mac Studio or similar)
OLLAMA_BASE_URL = "http://localhost:11434"  # Ollama API base URL (set yours on the dashboard Settings page)
OLLAMA_MODEL = "llava"          # Vision-capable model: "llava", "llava-llama3", "bakllava", etc.

# OpenAI-compatible API settings (OpenAI, OpenRouter, Groq, Gemini, LM Studio…)
OPENAI_COMPAT_BASE_URL = "https://api.openai.com/v1"
OPENAI_COMPAT_MODEL = "gpt-4.1-mini"   # Must accept images. Avoid reasoning models (they burn the token cap thinking)
OPENAI_COMPAT_API_KEY = os.getenv("OPENAI_COMPAT_API_KEY", "")  # Empty + OpenAI URL = use OPENAI_API_KEY

# --- Camera ---
CAMERA_INDEX = 0          # USB camera device index (usually 0)
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_FPS = 30

# --- Person Detection (YOLOv8) ---
DETECTION_CONFIDENCE = 0.57   # Min confidence to count as a person (0.0–1.0). Tuned on Hailo: sidewalk walkers ~0.56-0.66, junk <=0.57; 0.50 is the noise floor
DETECTION_LOG_RAW = False    # Diagnostic: log every candidate (score, size, verdict) to detections.log
DETECTION_MIN_AREA = 0.003   # Min bounding box area as fraction of frame. A person on the sidewalk measured >= 0.0036
DETECTION_MODEL = "yolov8n-pose.pt"  # Nano pose model — fast on Pi 5. Use "yolov8s-pose.pt" for better accuracy.
TRACK_MIN_FRAMES = 10        # Track must be seen this many frames before the head follows it

# --- Timing ---
COOLDOWN_SECONDS = 10       # Min seconds between interactions (starts AFTER the interaction ends)
DETECTION_HOLD_FRAMES = 10  # Person must be detected this many consecutive frames before triggering
SCAN_INTERVAL = 0.01        # Seconds between detection scans (lower = faster with Hailo)

# --- Person Tracking ---
TRACK_IOU_THRESHOLD = 0.25    # Min bounding box overlap to consider it the same person (0–1)
TRACK_EXPIRE_SECONDS = 15.0   # Forget a person after they leave the frame for this long
TRACK_MAX_HISTORY = 100        # Max spoken-to IDs to remember (prevents memory growth over a long night)

# --- Servo Channels (PCA9685) ---
PAN_CHANNEL = 0     # Head left/right
TILT_CHANNEL = 1    # Head up/down
JAW_CHANNEL = 2     # Mouth open/close

# --- Servo Ranges (degrees) ---
# Adjust these after calibrating your specific servos and mounting
PAN_MIN = 0        # Max right (90° from center) — widened from 10 so he can look back toward the door
PAN_MAX = 170      # Max left (80° from center)
PAN_CENTER = 90    # Straight ahead

TILT_MIN = 70      # Looking up (servo wired so smaller = upward)
TILT_MAX = 130     # Looking down
TILT_CENTER = 100  # Level
TILT_DROOPED = 120 # Idle "asleep" pose — head slumped forward toward chest

JAW_CLOSED = 63    # Mouth closed position
JAW_OPEN = 83      # Mouth fully open

# --- Servo Behavior ---
HEAD_TRACK_SPEED = 6.5     # Degrees per update cycle at 50Hz (max ~300 deg/sec)
PAN_MAX_SPEED = 130.0      # Degrees/second cap on every pan move (0 = no cap)
TILT_MAX_SPEED = 8.0       # Degrees/second cap on every tilt move — the tilt servo lifts the whole head (0 = no cap)
JAW_THRESHOLD = 0.02       # Min audio amplitude to open jaw (ignore silence)
JAW_UPDATE_RATE = 0.03     # Seconds between jaw updates during speech

# --- Radar (HLK-LD2450 mmWave sensor) ---
RADAR_ENABLED = True             # Set False to skip radar init (graceful degrade)
RADAR_PORT = "/dev/ttyAMA0"      # Pi 5 primary UART on GPIO header
RADAR_BAUD = 256000              # LD2450 default baud
RADAR_X_FILTER_LEN = 5           # Median filter window for X smoothing (~0.45s at 11fps)
RADAR_TARGET_TIMEOUT = 0.5       # Seconds before a target is considered stale
RADAR_SETTLE_SECONDS = 3.0       # Ignore radar this long after the head turns (radar is on his face)
RADAR_SETTLE_DEGREES = 8.0       # ...where "turns" = pan or tilt moved more than this within that time
RADAR_MATCH_DEGREES = 15.0       # A radar target only describes a person if their directions agree this closely
CAMERA_HFOV_DEG = 60.0           # Camera's horizontal field of view (not measured yet)

# --- Idle Behavior ---
IDLE_SCAN_ENABLED = False        # Periodic idle look-around at all (False = never sweeps; still tracks/talks to people)
IDLE_SCAN_MIN_INTERVAL = 90.0    # Min seconds between idle wake-up + scan cycles
IDLE_SCAN_MAX_INTERVAL = 180.0   # Max seconds between idle wake-up + scan cycles
IDLE_SCAN_RANGE = 45             # Degrees from center for the side-to-side sweep
IDLE_LIFT_SPEED = 0.25           # Degrees per cycle when lifting from drooped (slow + dramatic)
IDLE_SCAN_SPEED = 0.5            # Degrees per cycle during the side-to-side sweep
IDLE_DROOP_SPEED = 0.25          # Degrees per cycle when slumping back to drooped pose

# --- Voice look (glance toward a blind spot when he hears talking) ---
VOICE_LOOK_ENABLED = False       # Idle + nobody in view + voices heard → glance at VOICE_LOOK_PAN/TILT
VOICE_LOOK_PAN = 20              # Where to glance (set it from the dashboard joystick)
VOICE_LOOK_TILT = 100            # Level
VOICE_LOOK_HOLD_SECONDS = 6.0    # How long he stares there before giving up
VOICE_LOOK_COOLDOWN_SECONDS = 20.0  # Min seconds between glances
VOICE_LOOK_MIN_SPEECH_MS = 400   # Talking (within ~2 s) needed to count as voices

# --- Audio ---
AUDIO_SAMPLE_RATE = 22050
AUDIO_CHUNK_SIZE = 1024

# --- Listener (microphone input) ---
LISTEN_ENABLED = True           # Set False to disable listen-and-reply entirely
LISTEN_DURATION = 6.0           # Max seconds to listen for a response
LISTEN_SILENCE_TIMEOUT = 0.5    # Seconds of silence after speech = "they're done talking"
LISTEN_ENERGY_THRESHOLD = 300   # RMS energy threshold to count as speech (tune for your mic/environment)
LISTEN_POST_SPEAK_DELAY = 0.3   # Seconds to wait after the scarecrow speaks before listening (speaker decay)
LISTEN_MIC_FLUSH_SECONDS = 0.0  # Seconds of mic buffer to discard at start of listening (0 = none)
MAX_CONVERSATION_TURNS = 3      # Max back-and-forth exchanges (scarecrow speaks + listens = 1 turn)

# Lines the scarecrow says when the person doesn't respond
SILENCE_TAUNTS = [
    "Hm? Nothing to say?",
    "Cat got your tongue?",
    "That's what I thought.",
    "Speechless. I get that a lot.",
    "Smart. Silence is probably safer.",
]
WHISPER_MODEL = "tiny"          # Whisper model size: "tiny", "base", "small" (bigger = slower but more accurate)
VAD_CONFIDENCE = 0.3            # Silero VAD threshold — audio below this is treated as noise, not speech (0.0–1.0)

# --- Speech-to-Text Backend ---
STT_ENGINE = "faster-whisper"   # First choice: "faster-whisper" (LAN server), "openai" (cloud API) or "local" (on-device Whisper)
# Tried in this order until one works (stt_providers.py); edit on the dashboard Settings page
STT_ORDER = [STT_ENGINE] + [p for p in ("faster-whisper", "openai", "local") if p != STT_ENGINE]
WHISPER_SERVER_URL = "http://localhost:8178"  # faster-whisper server (set yours on the dashboard Settings page)
OPENAI_STT_MODEL = "whisper-1"  # or "gpt-4o-mini-transcribe" / "gpt-4o-transcribe"

# --- TTS ---
TTS_ENGINE = "elevenlabs"   # First choice: "elevenlabs", "openai", "piper" or "espeak"
# Tried in this order until one works (tts_providers.py); edit on the dashboard Settings page
TTS_ORDER = [TTS_ENGINE] + [p for p in ("elevenlabs", "openai", "piper", "espeak") if p != TTS_ENGINE]
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"   # "tts-1" / "tts-1-hd" ignore the instructions below
OPENAI_TTS_VOICE = "onyx"
OPENAI_TTS_INSTRUCTIONS = "Speak as an old, creepy scarecrow: low, slow and raspy, quietly menacing."
TTS_NORMALIZE = True        # Even out loudness between lines and between voices
TTS_LOUDNESS_DBFS = -14.0   # Target speech loudness (ElevenLabs lines measure about -14)
PIPER_MODEL = "en_US-lessac-medium"  # Piper voice model (fallback)
PIPER_SPEED = 0.85          # Slightly slower = creepier
ELEVENLABS_MODEL = "eleven_flash_v2_5"  # Low-latency model
ELEVENLABS_STABILITY = 0.3  # Lower = more dramatic variation
ELEVENLABS_STYLE = 0.5
ELEVENLABS_SPEED = 1.0     # 0.7–1.2; his voice talks slowly (~95 words a minute at 1.0)

# --- Personality ---
# "friendly" (welcoming, no threat), "snarky" (sarcastic, teasing observational humor),
# "creepy" (clever and unsettling), "menacing" (calm, quiet, deeply threatening)
PERSONA = "creepy"

# --- Pre-cached Lines (played while waiting for AI response) ---
FILLER_LINES = [
    "Well, well, well... what do we have here?",
    "I've been waiting for you...",
    "Oh... another one.",
    "I can see you...",
    "Don't. Move.",
]

# --- Quiet Hours (whisper mode) ---
QUIET_HOURS_ENABLED = True
QUIET_HOURS_START = 23    # Hour (0–23) when quiet mode begins (11pm)
QUIET_HOURS_END = 7       # Hour (0–23) when quiet mode ends (7am)
QUIET_HOURS_VOLUME = 0.25 # Volume during quiet hours (0.0–1.0)
QUIET_HOURS_IDLE_SCAN = False  # Idle head sweeps during quiet hours (False = stays asleep; still wakes for people)

# --- Debug ---
DEBUG = False               # Set True to show camera feed and detection boxes
LOG_CONVERSATIONS = True    # Log AI interactions to conversations.log
# --- First-run setup (dashboard /welcome) ---
SETUP_DONE = False          # Until set up, he runs but doesn't start conversations
SERVOS_CALIBRATED = False   # Ticked in the setup wizard after calibrating
SAVE_AI_PHOTOS = False      # Save the photos sent to the AI in ai_photos/ (last 30 conversations) — for debugging

# --- Dashboard overrides (settings.json) — must stay at the very end ---
import sys as _sys  # noqa: E402
from settings_store import SettingsStore as _SettingsStore  # noqa: E402
SETTINGS = _SettingsStore(_sys.modules[__name__])
