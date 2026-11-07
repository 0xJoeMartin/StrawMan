"""
Web Dashboard
-------------
Scarecrow control dashboard + live camera feed.

Serves a stacked layout: video feed on top, controls below.
Features:
  - Live MJPEG camera feed with detection overlays
  - Scare level control (runtime, no restart)
  - Free-text manual speak trigger (puppeteer mode)
  - Scrolling interaction log
  - Health metrics (CPU temp, uptime, last interaction)

Runs as a background thread alongside the main scarecrow loop.
View it at: http://<pi-ip-address>:5000
"""

import cv2
import io
import os
import subprocess
import time
import logging
import threading
from collections import deque
from flask import Flask, Response, render_template_string, request, jsonify

import config
from person_detector import SKELETON_EDGES, KEYPOINT_CONFIDENCE

# --- Skeleton overlay colors (BGR) ---
SKELETON_LINE_COLOR = (255, 0, 255)     # magenta limb lines
SKELETON_POINT_COLOR = (255, 200, 255)  # light pink keypoint dots

# Silence Flask's per-request logging (the dashboard polls every few seconds)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# --- Shared state ---

# Frame buffer for video stream
_current_frame = None
_frame_lock = threading.Lock()
# Latest raw (unannotated) frame + detections, for the Prompts page Test button
_latest_scene = (None, None, None, None)  # (frame, detections, tracks, tracker)


def get_latest_scene():
    """(raw_frame, detections, tracks, tracker) from the most recent main-loop frame."""
    with _frame_lock:
        return _latest_scene

# Interaction log (capped ring buffer)
_interaction_log = deque(maxlen=200)
_PROCESS_STARTED = time.time()  # lets the page notice a restart and reopen the video stream
_log_lock = threading.Lock()

# References to scarecrow subsystems (set by register_scarecrow())
_speech_engine = None
_servo_controller = None
_scarecrow_brain = None
_scarecrow_instance = None
_listener = None

# Server start time
_start_time = time.time()
_last_interaction_time = 0


app = Flask(__name__)
import ui_kit  # noqa: E402  (fonts + icons shared by every page)
ui_kit.install(app)

# PIN protection for every page, API and the video feed (see dashboard_auth.py)
import dashboard_auth  # noqa: E402
dashboard_auth.init_app(app)

# Prompts page: view / edit / reset prompt sections and fallback lines
from prompts_page import bp as _prompts_bp  # noqa: E402
app.register_blueprint(_prompts_bp)
from settings_page import bp as _settings_bp  # noqa: E402
app.register_blueprint(_settings_bp)
from calibration_page import bp as _calibrate_bp  # noqa: E402
app.register_blueprint(_calibrate_bp)
from setup_wizard import bp as _setup_bp  # noqa: E402
app.register_blueprint(_setup_bp)


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>StrawMan Dashboard</title>
    {{ ui_head() }}
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            background: #111;
            color: #e0e0e0;
            font-size: 14px;
            padding: 12px;
        }

        h1 {
            color: #ff8800;
            font-size: 1.4em;
            text-align: center;
            padding: 8px 0 12px;
        }
        h1 span { font-size: 0.6em; color: #666; display: block; margin-top: 2px; }
        h1 .logo { display: block; height: 96px; width: auto; margin: 0 auto; }
        .topnav { display: flex; justify-content: center; gap: 20px; margin: -4px 0 14px; }
        .topnav a { display: flex; align-items: center; gap: 6px; color: #aaa; text-decoration: none; }
        .topnav a:hover { color: #ff8800; }

        /* --- Video feed --- */
        .video-container {
            text-align: center;
            margin-bottom: 16px;
        }
        .video-container img {
            width: 100%;
            max-width: 720px;
            border: 2px solid #ff8800;
            border-radius: 12px;
        }

        /* --- Panel grid --- */
        .panels {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            max-width: 720px;
            margin: 0 auto;
        }
        @media (max-width: 600px) {
            .panels { grid-template-columns: 1fr; }
        }

        .panel {
            background: #1c1c1c;
            border-radius: 12px;
            padding: 14px 16px;
            box-shadow: 0 2px 2px rgba(0,0,0,.35), 0 1px 5px rgba(0,0,0,.3);
        }
        .panel h2 {
            display: flex;
            align-items: center;
            gap: 10px;
            color: #e8e8e8;
            font-size: 18px;
            font-weight: 400;
            margin-bottom: 12px;
        }
        .panel h2 .i { color: #ff8800; }

        /* --- Persona + backend tiles --- */
        .persona-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
        .backend-select { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
        .persona-btn, .backend-btn {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
            min-width: 0;
            padding: 10px 4px;
            border: 1px solid #333;
            border-radius: 12px;
            background: #232323;
            color: #ccc;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }
        .persona-btn .i, .backend-btn .i { color: #9a9a9a; }
        .persona-btn:hover, .backend-btn:hover { border-color: #ff8800; color: #fff; }
        .persona-btn.active, .backend-btn.active {
            border-color: #ff8800;
            background: rgba(255, 136, 0, 0.16);
            color: #ffb35c;
        }
        .persona-btn.active .i, .backend-btn.active .i { color: #ff8800; }

        /* --- Speak input --- */
        .speak-form {
            display: flex;
            gap: 8px;
        }
        .speak-form input {
            flex: 1;
            padding: 9px 12px;
            border: 1px solid #3a3a3a;
            border-radius: 8px;
            background: #141414;
            color: #e0e0e0;
            font-size: 14px;
        }
        .speak-form input:focus {
            outline: none;
            border-color: #ff8800;
        }
        .speak-form button {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 0 16px;
            border: none;
            border-radius: 20px;
            background: #ff8800;
            color: #111;
            font-weight: 500;
            cursor: pointer;
            font-size: 14px;
        }
        .speak-form button:hover { background: #ffaa33; }
        .speak-form button:disabled {
            background: #555;
            color: #888;
            cursor: not-allowed;
        }
        .speak-form .mic-btn {
            width: 40px;
            padding: 0;
            justify-content: center;
            border: 1px solid #3a3a3a;
            border-radius: 50%;
            background: #232323;
            color: #ff8800;
            flex-shrink: 0;
            transition: background 0.2s;
        }
        .speak-form .mic-btn:hover { background: #2e2e2e; }
        .speak-form .mic-btn.recording {
            color: #fff;
            border-color: #cc2200;
            background: #cc2200;
            animation: micpulse 1s ease-in-out infinite;
        }
        @keyframes micpulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.45; }
        }
        .speak-status {
            margin-top: 8px;
            font-size: 12px;
            color: #888;
        }
        .speak-status:empty { display: none; }

        /* --- Interaction log --- */
        .log-container {
            grid-column: 1 / -1;
        }
        .log-entries {
            max-height: 300px;
            overflow-y: auto;
            scrollbar-color: #3a3a3a transparent;
            scrollbar-width: thin;
        }
        .log-entry {
            display: flex;
            gap: 12px;
            padding: 8px 0;
            border-top: 1px solid #262626;
        }
        .log-entry:first-child { border-top: none; }
        .log-icon {
            width: 30px; height: 30px;
            border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            flex: none;
            background: #262626;
            color: #999;
        }
        .log-icon.spoke { background: rgba(255,136,0,.15); color: #ff8800; }
        .log-icon.heard { background: rgba(94,224,143,.12); color: #5ee08f; }
        .log-text { color: #e6e6e6; line-height: 1.35; }
        .log-meta { color: #7d7d7d; font-size: 12px; margin-top: 2px; }
        .log-scene { color: #888; font-style: italic; font-size: 12px; margin-top: 2px; }
        .log-empty { color: #555; text-align: center; padding: 20px; }

        /* --- Setup checklist --- */
        .setup-link { margin-left: auto; font-size: 13px; color: #888; text-decoration: none; display: flex; align-items: center; gap: 4px; }
        .setup-link:hover { color: #ff8800; }
        .setup-items { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 6px 14px; }
        .setup-item { display: flex; align-items: center; gap: 8px; min-width: 0; text-decoration: none; }
        .setup-item .i { flex: none; }
        .setup-item.ok .i { color: #5ee08f; } .setup-item.warn .i { color: #ffaa00; }
        .setup-item.missing .i { color: #ff6b6b; } .setup-item.off .i { color: #777; }
        .setup-t { color: #ddd; } .setup-d { color: #888; font-size: 12px; display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

        /* --- Health stats --- */
        .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
        @media (max-width: 600px) { .stats { grid-template-columns: 1fr 1fr; row-gap: 12px; } }
        .stat { display: flex; align-items: center; gap: 10px; min-width: 0; }
        .stat-icon {
            width: 32px; height: 32px;
            border-radius: 50%;
            background: rgba(255,136,0,.14);
            color: #ff8800;
            display: flex; align-items: center; justify-content: center;
            flex: none;
        }
        .stat-label { color: #888; font-size: 12px; }
        .health-value { color: #e8e8e8; font-size: 16px; white-space: nowrap; }
        .health-value.warn { color: #ffaa00; }
        .health-value.hot { color: #ff4444; }

        .backend-info {
            margin-top: 8px;
            font-size: 12px;
            color: #888;
            min-height: 1.2em;
        }

        /* --- Quiet hours --- */
        .quiet-row {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 10px;
        }
        .quiet-row label { font-size: 0.85em; color: #ccc; }
        .quiet-toggle {
            position: relative;
            width: 44px;
            height: 24px;
            cursor: pointer;
        }
        .quiet-toggle input { display: none; }
        .quiet-toggle .slider {
            position: absolute;
            inset: 0;
            background: #444;
            border-radius: 12px;
            transition: 0.2s;
        }
        .quiet-toggle .slider::before {
            content: '';
            position: absolute;
            width: 18px; height: 18px;
            left: 3px; top: 3px;
            background: #ccc;
            border-radius: 50%;
            transition: 0.2s;
        }
        .quiet-toggle input:checked + .slider { background: #ff8800; }
        .quiet-toggle input:checked + .slider::before { transform: translateX(20px); }
        .quiet-line { display: flex; align-items: center; gap: 8px 18px; flex-wrap: wrap; }
        .quiet-time-inputs {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .quiet-time-inputs select {
            padding: 6px 8px;
            border: 1px solid #3a3a3a;
            border-radius: 8px;
            background: #141414;
            color: #e0e0e0;
            font-size: 0.85em;
        }
        .quiet-time-inputs span { color: #888; font-size: 0.85em; }
        .quiet-volume {
            display: flex;
            align-items: center;
            gap: 8px;
            flex: 1;
            min-width: 200px;
        }
        .quiet-volume input[type=range] {
            flex: 1;
            accent-color: #ff8800;
        }
        .quiet-volume span { font-size: 0.8em; color: #888; min-width: 32px; }
        .quiet-scan { display: flex; align-items: center; gap: 6px; font-size: 0.85em; color: #ccc; cursor: pointer; }
        .quiet-scan input { accent-color: #ff8800; }
        .quiet-status {
            margin-left: auto;
            font-size: 12px;
            font-weight: 500;
            padding: 3px 10px;
            border-radius: 10px;
            white-space: nowrap;
        }
        .quiet-status.active { background: #332200; color: #ff8800; }
        .quiet-status.inactive { background: #232323; color: #777; }

        /* --- Manual servo control (joystick) --- */
        .joystick-area {
            display: flex;
            gap: 20px;
            align-items: center;
            margin-top: 12px;
            flex-wrap: wrap;
        }
        .joystick-pad {
            position: relative;
            width: min(160px, 70vw);
            aspect-ratio: 1;
            background: #1a1a1a;
            border: 1px solid #3a3a3a;
            border-radius: 12px;
            flex-shrink: 0;
            touch-action: none;
            cursor: grab;
            user-select: none;
        }
        .joystick-pad.disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }
        .joystick-pad.active {
            border-color: #ff8800;
            cursor: grabbing;
        }
        .joystick-pad .crosshair-h,
        .joystick-pad .crosshair-v {
            position: absolute;
            background: #2c2c2c;
            pointer-events: none;
        }
        .joystick-pad .crosshair-h {
            left: 0; right: 0; top: 50%;
            height: 1px;
        }
        .joystick-pad .crosshair-v {
            top: 0; bottom: 0; left: 50%;
            width: 1px;
        }
        .joystick-knob {
            position: absolute;
            width: 36px;
            height: 36px;
            margin-left: -18px;
            margin-top: -18px;
            background: #ff8800;
            border: 2px solid #fff;
            border-radius: 50%;
            pointer-events: none;
            box-shadow: 0 0 8px rgba(255, 136, 0, 0.6);
            left: 50%;
            top: 50%;
        }
        .joystick-pad.disabled .joystick-knob {
            background: #555;
            border-color: #888;
            box-shadow: none;
        }
        .joystick-readouts {
            display: flex;
            flex-direction: column;
            gap: 6px;
            font-family: monospace;
            font-size: 0.85em;
            color: #888;
        }
        .joystick-readouts .axis-label {
            color: #ccc;
            font-weight: bold;
            display: inline-block;
            width: 36px;
        }
        .joystick-readouts .target { color: #ff8800; }
        .joystick-readouts .actual { color: #ccc; }
        .manual-presets {
            display: flex;
            gap: 6px;
            margin-top: 10px;
        }
        .manual-presets button {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            padding: 8px 0;
            border: 1px solid #333;
            border-radius: 12px;
            background: #232323;
            color: #ccc;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
        }
        .manual-presets button:hover { border-color: #ff8800; color: #fff; }
        .manual-presets button:disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }
        .manual-hint {
            margin-top: 8px;
            font-size: 0.75em;
            color: #666;
            font-style: italic;
        }
    </style>
</head>
<body>
    <h1>
        <img class="logo" src="/static/strawman-logo.png" alt="StrawMan">
        <span id="connection-status">connecting...</span>
    </h1>
    <div class="topnav"><a href="/settings">{{ icon("cog", 18) }}Settings</a><a href="/prompts">{{ icon("script-text-outline", 18) }}Prompts</a><a href="/logout">{{ icon("logout", 18) }}Log out</a></div>

    <div class="video-container">
        <img id="video" src="/video_feed" alt="Camera Feed" onerror="videoBroken()" />
    </div>

    <div class="panels">
        <!-- Manual Servo Control -->
        <div class="panel" style="grid-column: 1 / -1;">
            <h2>{{ icon("gamepad-variant-outline") }}Manual control</h2>
            <div class="quiet-row">
                <label class="quiet-toggle">
                    <input type="checkbox" id="manual-mode" onchange="toggleManualMode()">
                    <span class="slider"></span>
                </label>
                <label>Manual mode (pauses idle movement and auto-engagement)</label>
            </div>
            <div class="joystick-area">
                <div id="joystick-pad" class="joystick-pad disabled">
                    <div class="crosshair-h"></div>
                    <div class="crosshair-v"></div>
                    <div id="joystick-knob" class="joystick-knob"></div>
                </div>
                <div class="joystick-readouts">
                    <div>
                        <span class="axis-label">Pan</span>
                        target <span class="target" id="pan-target">--</span>°
                        / live <span class="actual" id="pan-actual">--</span>°
                    </div>
                    <div>
                        <span class="axis-label">Tilt</span>
                        target <span class="target" id="tilt-target">--</span>°
                        / live <span class="actual" id="tilt-actual">--</span>°
                    </div>
                </div>
            </div>
            <div class="manual-presets">
                <button id="preset-center" disabled onclick="setPreset('center')">{{ icon("crosshairs-gps", 18) }}Center</button>
                <button id="preset-drooped" disabled onclick="setPreset('drooped')">{{ icon("sleep", 18) }}Drooped</button>
                <button id="preset-voice" disabled onclick="setPreset('voice')" title="Look where he glances when he hears voices">{{ icon("ear-hearing", 18) }}Voice look</button>
                <button id="set-voice-look" disabled onclick="setVoiceLook()" title="Save where his head is now as the voice-look spot">{{ icon("content-save-outline", 18) }}Set voice look here</button>
            </div>
            <div class="manual-hint"><a href="/calibrate" style="color:#ff8800">Calibrate servos</a> — set how far he can turn, look up and down, and open his jaw.</div>
            <div class="manual-hint" id="manual-hint">
                Turn on manual mode to steer his head yourself.
            </div>
        </div>

        <!-- Manual Speak -->
        <div class="panel" style="grid-column: 1 / -1;">
            <h2>{{ icon("account-voice") }}Puppeteer</h2>
            <div class="speak-form">
                <input type="text" id="speak-input" placeholder="Type something spooky…"
                       onkeydown="if(event.key==='Enter') speak()" />
                <button id="mic-btn" class="mic-btn" onclick="toggleDictate()" title="Voice input">{{ icon("microphone") }}</button>
                <button id="speak-btn" onclick="speak()">{{ icon("send", 18) }}Speak</button>
            </div>
            <div class="speak-status" id="speak-status"></div>
        </div>

        <!-- Persona -->
        <div class="panel">
            <h2>{{ icon("drama-masks") }}Persona</h2>
            <div class="persona-buttons">
                <button class="persona-btn" data-persona="friendly" onclick="setPersona('friendly')">{{ icon("emoticon-happy-outline", 22) }}Friendly</button>
                <button class="persona-btn" data-persona="snarky" onclick="setPersona('snarky')">{{ icon("emoticon-wink-outline", 22) }}Snarky</button>
                <button class="persona-btn" data-persona="creepy" onclick="setPersona('creepy')">{{ icon("ghost-outline", 22) }}Creepy</button>
                <button class="persona-btn" data-persona="menacing" onclick="setPersona('menacing')">{{ icon("skull-outline", 22) }}Menacing</button>
            </div>
        </div>

        <!-- AI Backend -->
        <div class="panel">
            <h2>{{ icon("brain") }}AI backend</h2>
            <div class="backend-select">
                <button class="backend-btn" data-backend="claude" onclick="setBackend('claude')">{{ icon("cloud-outline", 22) }}Claude</button>
                <button class="backend-btn" data-backend="ollama" onclick="setBackend('ollama')">{{ icon("server-network", 22) }}Ollama</button>
                <button class="backend-btn" data-backend="openai" onclick="setBackend('openai')" title="Any OpenAI-compatible API — set it up on the Settings page">{{ icon("api", 22) }}OpenAI API</button>
            </div>
            <div class="backend-info" id="backend-info"></div>
        </div>

        <!-- Health Metrics -->
        <div class="panel" style="grid-column: 1 / -1;">
            <h2>{{ icon("heart-pulse") }}Health</h2>
            <div class="stats">
                <div class="stat"><span class="stat-icon">{{ icon("thermometer", 18) }}</span><div><div class="health-value" id="h-cpu-temp">--</div><div class="stat-label">CPU temp</div></div></div>
                <div class="stat"><span class="stat-icon">{{ icon("timer-outline", 18) }}</span><div><div class="health-value" id="h-uptime">--</div><div class="stat-label">Uptime</div></div></div>
                <div class="stat"><span class="stat-icon">{{ icon("counter", 18) }}</span><div><div class="health-value" id="h-interactions">--</div><div class="stat-label">Interactions</div></div></div>
                <div class="stat"><span class="stat-icon">{{ icon("clock-outline", 18) }}</span><div><div class="health-value" id="h-last-interaction">--</div><div class="stat-label">Last interaction</div></div></div>
            </div>
        </div>

        <!-- Quiet Hours -->
        <div class="panel" style="grid-column: 1 / -1;">
            <h2>{{ icon("weather-night") }}Quiet hours
                <span class="quiet-status inactive" id="quiet-status">Inactive</span>
                <label class="quiet-toggle" title="Quiet hours on/off">
                    <input type="checkbox" id="quiet-enabled" onchange="updateQuietHours()">
                    <span class="slider"></span>
                </label>
            </h2>
            <div class="quiet-line">
                <div class="quiet-time-inputs">
                    <select id="quiet-start" onchange="updateQuietHours()">
                    </select>
                    <span>to</span>
                    <select id="quiet-end" onchange="updateQuietHours()">
                    </select>
                </div>
                <div class="quiet-volume">
                    <label style="color:#ccc; display:flex;" title="Volume during quiet hours">{{ icon("volume-low") }}</label>
                    <input type="range" id="quiet-vol" min="0" max="100" value="25"
                           onchange="updateQuietHours()" />
                    <span id="quiet-vol-label">25%</span>
                </div>
                <label class="quiet-scan" title="Keep doing the idle look-around during quiet hours. Off = stays asleep but still wakes for people.">
                    <input type="checkbox" id="quiet-scan" onchange="updateQuietHours()"> Head sweeps
                </label>
            </div>
        </div>

        <!-- Setup checklist -->
        <div class="panel" style="grid-column: 1 / -1;" id="setup-panel">
            <h2>{{ icon("check-circle-outline") }}Setup
                <a href="/welcome" class="setup-link">{{ icon("restart", 16) }}Run setup again</a></h2>
            <div class="setup-items" id="setup-items"><span class="setup-d">Checking…</span></div>
        </div>

        <!-- Interaction Log -->
        <div class="panel log-container">
            <h2>{{ icon("format-list-bulleted") }}Interaction log</h2>
            <div class="log-entries" id="log-entries">
                <div class="log-empty">No interactions yet — waiting for visitors…</div>
            </div>
        </div>
    </div>

    {{ ui_icons_js() }}
    <script>
        // Signed out (PIN session expired / PIN reset)? Go to the login page.
        const _origFetch = window.fetch;
        window.fetch = async (...a) => {
            const r = await _origFetch(...a);
            if (r.status === 401) location.href = '/login';
            return r;
        };
        // --- Persona ---
        function setPersona(persona) {
            fetch('/api/set_persona', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({persona: persona}),
            })
            .then(r => r.json())
            .then(data => {
                if (data.ok) updatePersonaButtons(persona);
            });
        }

        function updatePersonaButtons(active) {
            document.querySelectorAll('.persona-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.persona === active);
            });
        }

        // --- AI Backend ---
        function setBackend(backend) {
            const info = document.getElementById('backend-info');
            info.textContent = 'Switching...';
            fetch('/api/backend', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({backend: backend}),
            })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    updateBackendButtons(data.backend);
                    info.textContent = 'Using ' + data.model;
                } else {
                    info.textContent = 'Error: ' + (data.error || 'unknown');
                }
                setTimeout(() => { info.textContent = ''; }, 4000);
            })
            .catch(err => { info.textContent = 'Error: ' + err; });
        }

        function updateBackendButtons(active) {
            document.querySelectorAll('.backend-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.backend === active);
            });
        }

        // --- Puppeteer Speak ---
        function speak() {
            const input = document.getElementById('speak-input');
            const btn = document.getElementById('speak-btn');
            const status = document.getElementById('speak-status');
            const text = input.value.trim();
            if (!text) return;

            btn.disabled = true;
            status.textContent = 'Speaking...';

            fetch('/api/speak', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({text: text}),
            })
            .then(r => r.json())
            .then(data => {
                status.textContent = data.ok ? 'Done' : ('Error: ' + (data.error || 'unknown'));
                if (data.ok) input.value = '';
                setTimeout(() => { status.textContent = ''; }, 3000);
            })
            .catch(err => {
                status.textContent = 'Error: ' + err;
            })
            .finally(() => {
                btn.disabled = false;
                input.focus();
            });
        }

        // --- Dictate (browser mic → text for Puppeteer) ---
        let _mediaRecorder = null;
        let _dictateChunks = [];
        let _dictating = false;

        async function toggleDictate() {
            if (_dictating) {
                stopDictate();
            } else {
                await startDictate();
            }
        }

        async function startDictate() {
            const status = document.getElementById('speak-status');
            const micBtn = document.getElementById('mic-btn');

            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                status.textContent = 'Voice input not supported by this browser.';
                setTimeout(() => { status.textContent = ''; }, 3000);
                return;
            }

            let stream;
            try {
                stream = await navigator.mediaDevices.getUserMedia({audio: true, video: false});
            } catch (err) {
                status.textContent = err.name === 'NotAllowedError'
                    ? 'Microphone permission denied.'
                    : 'Mic error: ' + err.message;
                setTimeout(() => { status.textContent = ''; }, 4000);
                return;
            }

            _dictateChunks = [];
            _mediaRecorder = new MediaRecorder(stream);

            _mediaRecorder.ondataavailable = e => {
                if (e.data && e.data.size > 0) _dictateChunks.push(e.data);
            };

            _mediaRecorder.onstop = async () => {
                stream.getTracks().forEach(t => t.stop());
                micBtn.innerHTML = mdi('microphone');
                micBtn.classList.remove('recording');

                const blob = new Blob(_dictateChunks,
                    {type: _mediaRecorder.mimeType || 'audio/webm'});
                if (blob.size < 500) {
                    status.textContent = 'No audio captured.';
                    setTimeout(() => { status.textContent = ''; }, 2000);
                    return;
                }

                status.textContent = 'Transcribing...';
                const form = new FormData();
                form.append('audio', blob, 'dictate.webm');

                try {
                    const res = await fetch('/api/dictate', {method: 'POST', body: form});
                    const data = await res.json();
                    if (data.ok && data.text) {
                        document.getElementById('speak-input').value = data.text;
                        status.textContent = 'Transcribed — press Speak or Enter to send.';
                        document.getElementById('speak-input').focus();
                        setTimeout(() => { status.textContent = ''; }, 5000);
                    } else {
                        status.textContent = data.error || 'No speech detected.';
                        setTimeout(() => { status.textContent = ''; }, 3000);
                    }
                } catch (err) {
                    status.textContent = 'Transcription failed.';
                    setTimeout(() => { status.textContent = ''; }, 3000);
                }
            };

            _mediaRecorder.start();
            _dictating = true;
            micBtn.innerHTML = mdi('stop');
            micBtn.classList.add('recording');
            status.textContent = 'Recording — tap the mic again to stop';
        }

        function stopDictate() {
            if (_mediaRecorder && _dictating) {
                _dictating = false;
                _mediaRecorder.stop();
            }
        }

        // --- Video: reconnect after a restart or a dropped stream ---
        let _startedAt = null, _wasDown = false, _videoRetry = null;
        function reloadVideo() {
            clearTimeout(_videoRetry);
            document.getElementById('video').src = '/video_feed?t=' + Date.now();
        }
        function videoBroken() {           // stream ended or failed to load: try again shortly
            clearTimeout(_videoRetry);
            _videoRetry = setTimeout(reloadVideo, 3000);
        }

        // --- Health Polling ---
        function pollHealth() {
            fetch('/api/health')
                .then(r => r.json())
                .then(data => {
                    // Scarecrow restarted (new start time) or we were cut off: the
                    // MJPEG stream is dead, so open a fresh one
                    if ((_startedAt !== null && data.started_at !== _startedAt) || _wasDown) reloadVideo();
                    _startedAt = data.started_at;
                    _wasDown = false;
                    // CPU temp
                    const tempEl = document.getElementById('h-cpu-temp');
                    tempEl.textContent = data.cpu_temp_c !== null
                        ? data.cpu_temp_c.toFixed(1) + '°C'
                        : '--';
                    tempEl.className = 'health-value';
                    if (data.cpu_temp_c > 75) tempEl.classList.add('hot');
                    else if (data.cpu_temp_c > 60) tempEl.classList.add('warn');

                    // Uptime
                    document.getElementById('h-uptime').textContent = data.uptime;

                    // Interaction count
                    document.getElementById('h-interactions').textContent = data.interaction_count;

                    // Last interaction
                    document.getElementById('h-last-interaction').textContent =
                        data.last_interaction || 'none';

                    // Persona
                    updatePersonaButtons(data.persona);

                    // AI backend
                    if (data.ai_backend) updateBackendButtons(data.ai_backend);

                    // Quiet hours
                    syncQuietHoursUI(data);

                    // Connection status
                    document.getElementById('connection-status').textContent = 'connected';
                })
                .catch(() => {
                    document.getElementById('connection-status').textContent = 'disconnected';
                    _wasDown = true;
                });
        }

        // --- Setup checklist ---
        const SETUP_ICONS = {ok: 'check-circle-outline', warn: 'information-outline', missing: 'stop', off: 'sleep'};
        function pollSetup() {
            fetch('/api/setup/status').then(r => r.json()).then(j => {
                document.getElementById('setup-items').innerHTML = j.items.map(i =>
                    `<a class="setup-item ${i.status}" ${i.fix && i.status !== 'ok' ? `href="${i.fix}"` : ''} title="${escapeHtml(i.detail)}">
                       ${mdi(SETUP_ICONS[i.status] || 'information-outline', 18)}
                       <span style="min-width:0"><span class="setup-t">${escapeHtml(i.label)}</span>
                       <span class="setup-d">${escapeHtml(i.detail || (i.status === 'ok' ? 'OK' : ''))}</span></span></a>`).join('');
            }).catch(() => {});
        }

        // --- Log Polling ---
        let lastLogId = 0;
        // source -> [icon, colour class, label]
        const LOG_KINDS = {
            AI: ['robot-outline', 'spoke', 'His line'],
            REPLY: ['reply', 'spoke', 'His reply'],
            TAUNT: ['emoticon-devil-outline', 'spoke', 'Taunt'],
            MANUAL: ['account-voice', 'spoke', 'Puppeteer'],
            PERSON: ['account', 'heard', 'Visitor'],
            DICTATE: ['microphone-outline', 'heard', 'Dictation'],
            SYSTEM: ['information-outline', '', 'System'],
        };

        function pollLog() {
            fetch('/api/log?since=' + lastLogId)
                .then(r => r.json())
                .then(data => {
                    if (!data.entries || data.entries.length === 0) return;

                    const container = document.getElementById('log-entries');

                    // Remove empty placeholder
                    const empty = container.querySelector('.log-empty');
                    if (empty) empty.remove();

                    data.entries.forEach(entry => {
                        const div = document.createElement('div');
                        div.className = 'log-entry';

                        const kind = LOG_KINDS[entry.source] || ['information-outline', '', entry.source];
                        let html = '<span class="log-icon ' + kind[1] + '">' + mdi(kind[0], 18) + '</span><div>';
                        html += '<div class="log-text">' + escapeHtml(entry.text) + '</div>';
                        if (entry.scene) {
                            html += '<div class="log-scene">' + escapeHtml(entry.scene) + '</div>';
                        }
                        html += '<div class="log-meta">' + entry.time + ' · ' + kind[2] + '</div></div>';
                        div.innerHTML = html;
                        container.appendChild(div);

                        lastLogId = Math.max(lastLogId, entry.id);
                    });

                    // Auto-scroll to bottom
                    container.scrollTop = container.scrollHeight;
                });
        }

        function escapeHtml(text) {
            const d = document.createElement('div');
            d.textContent = text;
            return d.innerHTML;
        }

        // --- Quiet Hours ---
        // Populate hour dropdowns
        (function() {
            const startSel = document.getElementById('quiet-start');
            const endSel = document.getElementById('quiet-end');
            for (let h = 0; h < 24; h++) {
                const label = h === 0 ? '12am' : h < 12 ? h + 'am' : h === 12 ? '12pm' : (h - 12) + 'pm';
                startSel.add(new Option(label, h));
                endSel.add(new Option(label, h));
            }
        })();

        let _quietDebounce = null;
        function updateQuietHours() {
            // Debounce so dragging the slider doesn't spam requests
            clearTimeout(_quietDebounce);
            const vol = document.getElementById('quiet-vol').value;
            document.getElementById('quiet-vol-label').textContent = vol + '%';

            _quietDebounce = setTimeout(() => {
                fetch('/api/quiet_hours', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        enabled: document.getElementById('quiet-enabled').checked,
                        start: parseInt(document.getElementById('quiet-start').value),
                        end: parseInt(document.getElementById('quiet-end').value),
                        volume: parseInt(vol) / 100,
                        scan: document.getElementById('quiet-scan').checked,
                    }),
                });
            }, 300);
        }

        function syncQuietHoursUI(data) {
            // Idle sweeps master switch lives on the Settings page (Behavior);
            // while it's off, the quiet-hours "Head sweeps" checkbox is moot.
            if (typeof data.idle_scan_enabled === 'boolean') {
                const qs = document.getElementById('quiet-scan');
                qs.disabled = !data.idle_scan_enabled;
                qs.parentElement.style.opacity = data.idle_scan_enabled ? '' : '0.45';
                qs.parentElement.title = data.idle_scan_enabled
                    ? 'Keep doing the idle look-around during quiet hours. Off = stays asleep but still wakes for people.'
                    : 'Idle head sweeps are switched off (Settings → Behavior), so this has no effect.';
            }
            document.getElementById('quiet-enabled').checked = data.quiet_hours_enabled;
            document.getElementById('quiet-start').value = data.quiet_hours_start;
            document.getElementById('quiet-end').value = data.quiet_hours_end;
            document.getElementById('quiet-scan').checked = !!data.quiet_hours_idle_scan;
            const volPct = Math.round(data.quiet_hours_volume * 100);
            document.getElementById('quiet-vol').value = volPct;
            document.getElementById('quiet-vol-label').textContent = volPct + '%';

            const statusEl = document.getElementById('quiet-status');
            if (data.current_volume < 1.0) {
                statusEl.textContent = 'Whisper mode · ' + Math.round(data.current_volume * 100) + '%';
                statusEl.className = 'quiet-status active';
            } else {
                statusEl.textContent = 'Inactive';
                statusEl.className = 'quiet-status inactive';
            }
        }

        // --- Manual Servo Control (joystick) ---
        let _servoRanges = null;
        let _lastServo = null;
        let _hintHoldUntil = 0;
        let _manualSendPending = false;
        let _manualSendTimer = null;
        let _joystickDragging = false;
        let _joystickRect = null;
        let _joystickActivePointer = null;

        function fetchServoState() {
            return fetch('/api/servo')
                .then(r => r.json())
                .then(data => {
                    if (!data.ok) return null;
                    if (data.ranges) {
                        _servoRanges = data.ranges;
                    }
                    syncManualUI(data);
                    return data;
                })
                .catch(() => null);
        }

        function syncManualUI(data) {
            const enabled = !!data.manual_mode;
            document.getElementById('manual-mode').checked = enabled;
            const pad = document.getElementById('joystick-pad');
            pad.classList.toggle('disabled', !enabled);
            document.getElementById('preset-center').disabled = !enabled;
            document.getElementById('preset-drooped').disabled = !enabled;
            document.getElementById('preset-voice').disabled = !enabled;
            document.getElementById('set-voice-look').disabled = !enabled;
            _lastServo = data;
            if (Date.now() > _hintHoldUntil) {
                document.getElementById('manual-hint').textContent = enabled
                    ? 'Manual mode active. Idle scanning + auto-engagement are paused.'
                    : 'Turn on manual mode to steer his head yourself.';
            }

            // Sync knob position from server unless the user is mid-drag or
            // has a debounced send still in flight.
            if (!_manualSendPending && !_joystickDragging) {
                joystickSyncFromAngles(data.target_pan, data.target_tilt);
            }
            document.getElementById('pan-target').textContent = data.target_pan.toFixed(0);
            document.getElementById('tilt-target').textContent = data.target_tilt.toFixed(0);
            document.getElementById('pan-actual').textContent = data.current_pan.toFixed(1);
            document.getElementById('tilt-actual').textContent = data.current_tilt.toFixed(1);
        }

        function toggleManualMode() {
            const enabled = document.getElementById('manual-mode').checked;
            fetch('/api/servo', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({manual_mode: enabled}),
            }).then(r => r.json()).then(syncManualUI);
        }

        // Throttle servo target updates (don't debounce). Debouncing held
        // back every send until the user paused dragging, which made the
        // head sit still during the drag and then snap to the final
        // position — feels jerky. Throttling sends the latest target at
        // a steady ~25Hz so the head follows the joystick smoothly during
        // continuous motion, with a final trailing send to land on the
        // exact release position.
        const MANUAL_SEND_INTERVAL_MS = 40;
        let _manualPendingPayload = null;

        function sendServoTarget(payload) {
            _manualPendingPayload = payload;
            _manualSendPending = true;
            if (_manualSendTimer) return;  // a send is already scheduled / in flight

            const doSend = () => {
                if (!_manualPendingPayload) {
                    _manualSendTimer = null;
                    _manualSendPending = false;
                    return;
                }
                const toSend = _manualPendingPayload;
                _manualPendingPayload = null;
                fetch('/api/servo', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(toSend),
                })
                .then(r => r.json())
                .then(syncManualUI)
                .catch(() => {})
                .finally(() => {
                    // If more drag updates arrived while we were waiting,
                    // schedule the next send. Otherwise stop until the
                    // next move event.
                    if (_manualPendingPayload) {
                        _manualSendTimer = setTimeout(doSend, MANUAL_SEND_INTERVAL_MS);
                    } else {
                        _manualSendTimer = null;
                        _manualSendPending = false;
                    }
                });
            };

            // Leading edge: fire the first send immediately so the head
            // starts moving the moment you grab the joystick.
            _manualSendTimer = setTimeout(doSend, 0);
        }

        // Map a knob (px, py) on the pad in [0..width, 0..height] to (pan, tilt)
        // angles. X is inverted so that "push knob right" -> "look right"
        // (PAN_MIN = looking right per config). Y is natural: top = looking up.
        // The pad is split at the servo's configured center, so the middle of the
        // pad is always "straight ahead" even when the limits aren't symmetric
        // (e.g. PAN_MAX trimmed to 120 with center 90 and PAN_MIN 0).
        // f in 0..1: 0 -> a, 0.5 -> c, 1 -> b
        function _splitLerp(f, a, c, b) {
            return f < 0.5 ? a + (f / 0.5) * (c - a) : c + ((f - 0.5) / 0.5) * (b - c);
        }
        function _splitInv(v, a, c, b) {
            const f = (Math.abs(c - a) > 1e-6 && ((v - c) * (a - c) > 0))
                ? 0.5 - 0.5 * (v - c) / (a - c)
                : (Math.abs(b - c) > 1e-6 ? 0.5 + 0.5 * (v - c) / (b - c) : 0.5);
            return Math.max(0, Math.min(1, f));
        }

        function joystickXYToAngles(px, py, w, h) {
            if (!_servoRanges) return null;
            const r = _servoRanges;
            const fx = px / w;  // 0..1, left to right
            const fy = py / h;  // 0..1, top to bottom
            const pan = _splitLerp(fx, r.pan_max, r.pan_center, r.pan_min);
            const tilt = _splitLerp(fy, r.tilt_min, r.tilt_center, r.tilt_max);
            return {pan: pan, tilt: tilt};
        }

        function joystickAnglesToXY(pan, tilt, w, h) {
            if (!_servoRanges) return null;
            const r = _servoRanges;
            const fx = _splitInv(pan, r.pan_max, r.pan_center, r.pan_min);
            const fy = _splitInv(tilt, r.tilt_min, r.tilt_center, r.tilt_max);
            return {x: fx * w, y: fy * h};
        }

        function joystickSyncFromAngles(pan, tilt) {
            if (!_servoRanges) return;
            const pad = document.getElementById('joystick-pad');
            const rect = pad.getBoundingClientRect();
            if (!rect.width || !rect.height) return;
            const xy = joystickAnglesToXY(pan, tilt, rect.width, rect.height);
            if (!xy) return;
            const knob = document.getElementById('joystick-knob');
            knob.style.left = xy.x + 'px';
            knob.style.top = xy.y + 'px';
        }

        function joystickStart(e) {
            const pad = document.getElementById('joystick-pad');
            if (pad.classList.contains('disabled')) return;
            _joystickDragging = true;
            _joystickActivePointer = e.pointerId;
            _joystickRect = pad.getBoundingClientRect();
            pad.classList.add('active');
            try { pad.setPointerCapture(e.pointerId); } catch (_) {}
            joystickMove(e);
            e.preventDefault();
        }

        function joystickMove(e) {
            if (!_joystickDragging || !_joystickRect) return;
            if (e.pointerId !== _joystickActivePointer) return;
            const r = _joystickRect;
            let x = Math.max(0, Math.min(r.width, e.clientX - r.left));
            let y = Math.max(0, Math.min(r.height, e.clientY - r.top));
            const knob = document.getElementById('joystick-knob');
            knob.style.left = x + 'px';
            knob.style.top = y + 'px';
            const angles = joystickXYToAngles(x, y, r.width, r.height);
            if (!angles) return;
            document.getElementById('pan-target').textContent = angles.pan.toFixed(0);
            document.getElementById('tilt-target').textContent = angles.tilt.toFixed(0);
            sendServoTarget({pan: angles.pan, tilt: angles.tilt});
        }

        function joystickEnd(e) {
            if (!_joystickDragging) return;
            if (e.pointerId !== _joystickActivePointer) return;
            _joystickDragging = false;
            _joystickActivePointer = null;
            const pad = document.getElementById('joystick-pad');
            pad.classList.remove('active');
            try { pad.releasePointerCapture(e.pointerId); } catch (_) {}
        }

        (function initJoystick() {
            const pad = document.getElementById('joystick-pad');
            pad.addEventListener('pointerdown', joystickStart);
            pad.addEventListener('pointermove', joystickMove);
            pad.addEventListener('pointerup', joystickEnd);
            pad.addEventListener('pointercancel', joystickEnd);
        })();

        function setPreset(name) {
            if (!_servoRanges) return;
            let payload = {};
            if (name === 'center') {
                payload = {pan: _servoRanges.pan_center, tilt: _servoRanges.tilt_center};
            } else if (name === 'drooped') {
                payload = {pan: _servoRanges.pan_center, tilt: _servoRanges.tilt_drooped};
            } else if (name === 'voice') {
                payload = {pan: _servoRanges.voice_look_pan, tilt: _servoRanges.voice_look_tilt};
            }
            sendServoTarget(payload);
        }

        function setVoiceLook() {
            if (!_lastServo) return;
            const pan = Math.round(_lastServo.target_pan), tilt = Math.round(_lastServo.target_tilt);
            const hint = document.getElementById('manual-hint');
            fetch('/api/settings/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({changes: {VOICE_LOOK_PAN: pan, VOICE_LOOK_TILT: tilt}}),
            }).then(r => r.json()).then(j => {
                _hintHoldUntil = Date.now() + 6000;
                if (j.ok) {
                    if (_servoRanges) { _servoRanges.voice_look_pan = pan; _servoRanges.voice_look_tilt = tilt; }
                    hint.textContent = `Voice look saved: pan ${pan}°, tilt ${tilt}°. ` +
                        (_servoRanges && _servoRanges.voice_look_enabled ? ''
                            : 'Turn on "Voice look" in Settings → Behavior to use it.');
                } else {
                    hint.textContent = 'Could not save voice look: ' + (j.error || 'unknown error');
                }
            }).catch(() => { _hintHoldUntil = Date.now() + 6000; hint.textContent = 'Could not save voice look.'; });
        }

        // --- Polling loops ---
        setInterval(pollHealth, 3000);
        pollSetup(); setInterval(pollSetup, 60000);
        setInterval(pollLog, 2000);
        setInterval(fetchServoState, 250);  // fast poll for live angle readback
        pollHealth();
        pollLog();
        fetchServoState();
    </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    # Fresh install: go through setup first (an install that already works is
    # marked done automatically, see readiness.auto_mark_done)
    if not config.SETUP_DONE:
        from flask import redirect
        return redirect('/welcome')
    return render_template_string(DASHBOARD_HTML)


@app.route('/video_feed')
def video_feed():
    return Response(
        _generate_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame',
    )


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------

@app.route('/api/set_persona', methods=['POST'])
def api_set_persona():
    """Change the persona at runtime."""
    data = request.get_json(silent=True) or {}
    persona = data.get('persona', '').lower()

    valid = ('friendly', 'snarky', 'creepy', 'menacing')
    if persona not in valid:
        return jsonify(ok=False, error=f"Invalid persona. Use one of: {', '.join(valid)}"), 400

    # Saved to settings.json and applied to config.PERSONA, which the brain
    # reads on every call (system prompt + offline fallback lines).
    config.SETTINGS.set('PERSONA', persona)

    log_event("SYSTEM", f"Persona changed to {persona}")
    return jsonify(ok=True, persona=persona)


@app.route('/api/backend', methods=['POST'])
def api_set_backend():
    """Switch AI backend (claude / ollama / openai) at runtime."""
    data = request.get_json(silent=True) or {}
    backend = data.get('backend', '').lower()

    if backend not in ('claude', 'ollama', 'openai'):
        return jsonify(ok=False, error='Invalid backend. Use: claude, ollama, openai'), 400

    if not _scarecrow_brain:
        return jsonify(ok=False, error='Brain not connected'), 503

    try:
        # The store runs brain.set_backend() first (hook registered in
        # register_scarecrow), so a failed switch isn't saved.
        config.SETTINGS.set('AI_BACKEND', backend)
        model = _scarecrow_brain.describe_backend(backend)
        log_event("SYSTEM", f"AI backend switched to {model}")
        return jsonify(ok=True, backend=backend, model=model)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route('/api/speak', methods=['POST'])
def api_speak():
    """Manually trigger the scarecrow to speak a line with jaw sync."""
    data = request.get_json(silent=True) or {}
    text = data.get('text', '').strip()

    if not text:
        return jsonify(ok=False, error='No text provided'), 400

    if not _speech_engine:
        return jsonify(ok=False, error='Speech engine not connected'), 503

    if _speech_engine.is_speaking:
        return jsonify(ok=False, error='Already speaking'), 409

    # Run in a thread so we don't block the request
    def _do_speak():
        try:
            if _servo_controller:
                _servo_controller.start_jaw_sync()
            _speech_engine.speak(text, blocking=True)
        finally:
            if _servo_controller:
                _servo_controller.stop_jaw_sync()

    log_event("MANUAL", text)
    threading.Thread(target=_do_speak, daemon=True).start()
    return jsonify(ok=True)


@app.route('/api/dictate', methods=['POST'])
def api_dictate():
    """Receive browser mic audio (WebM/OGG/MP4), transcribe via the STT
    pipeline, and return the text for the Puppeteer input field.

    The browser's MediaRecorder sends whatever format it produces (typically
    WebM/Opus on Chrome, OGG/Opus on Firefox, MP4/AAC on Safari). We decode
    it to 16 kHz mono WAV with ffmpeg, then run the same speech-to-text
    cascade (Settings → Hearing order) as the hardware mic listener.
    """
    if 'audio' not in request.files:
        return jsonify(ok=False, error='No audio file in request'), 400

    audio_bytes = request.files['audio'].read()
    if len(audio_bytes) < 100:
        return jsonify(ok=False, error='Audio too short or empty'), 400

    # Convert browser audio format → 16 kHz mono WAV
    try:
        proc = subprocess.run(
            ['ffmpeg', '-i', 'pipe:0', '-f', 'wav', '-ar', '16000',
             '-ac', '1', 'pipe:1'],
            input=audio_bytes, capture_output=True, timeout=15,
        )
        if not proc.stdout:
            err_msg = proc.stderr.decode(errors='replace')[:200]
            return jsonify(ok=False, error=f'ffmpeg error: {err_msg}'), 500
        wav_buffer = io.BytesIO(proc.stdout)
    except Exception as e:
        return jsonify(ok=False, error=f'Audio conversion error: {e}'), 500

    # Same speech-to-text cascade as the mic listener (stt_providers), so it
    # works even when listening is turned off
    import stt_providers
    try:
        text = stt_providers.transcribe(wav_buffer.getvalue())
        if text and _listener:
            text = _listener._filter_hallucinations(text)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

    if text:
        log_event("DICTATE", text)
        return jsonify(ok=True, text=text)
    return jsonify(ok=False, error='No speech detected')


@app.route('/api/health')
def api_health():
    """Return system health metrics."""
    # CPU temperature
    cpu_temp = None
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            cpu_temp = int(f.read().strip()) / 1000.0
    except Exception:
        pass

    # Uptime
    elapsed = time.time() - _start_time
    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)
    if hours > 0:
        uptime_str = f"{hours}h {minutes}m"
    else:
        uptime_str = f"{minutes}m"

    # Interaction count
    interaction_count = 0
    if _scarecrow_brain:
        interaction_count = _scarecrow_brain.conversation_count

    # Last interaction time
    last_str = None
    if _last_interaction_time > 0:
        ago = time.time() - _last_interaction_time
        if ago < 60:
            last_str = f"{int(ago)}s ago"
        elif ago < 3600:
            last_str = f"{int(ago // 60)}m ago"
        else:
            last_str = f"{int(ago // 3600)}h ago"

    return jsonify(
        started_at=_PROCESS_STARTED,
        cpu_temp_c=cpu_temp,
        uptime=uptime_str,
        persona=config.PERSONA,
        interaction_count=interaction_count,
        last_interaction=last_str,
        ai_backend=_scarecrow_brain.backend if _scarecrow_brain else config.AI_BACKEND,
        quiet_hours_enabled=config.QUIET_HOURS_ENABLED,
        quiet_hours_start=config.QUIET_HOURS_START,
        quiet_hours_end=config.QUIET_HOURS_END,
        quiet_hours_volume=config.QUIET_HOURS_VOLUME,
        quiet_hours_idle_scan=config.QUIET_HOURS_IDLE_SCAN,
        idle_scan_enabled=config.IDLE_SCAN_ENABLED,
        current_volume=_speech_engine.volume if _speech_engine else 1.0,
    )


@app.route('/api/servo', methods=['GET', 'POST'])
def api_servo():
    """Get or set servo state for the dashboard's manual control panel.

    GET → returns current angles, configured ranges, and manual_mode flag.
    POST → body may contain 'pan', 'tilt', and/or 'manual_mode'.
           Numeric values are clamped to the configured min/max.
    """
    if not _servo_controller or not _scarecrow_instance:
        return jsonify(ok=False, error='Servo controller not connected'), 503

    if request.method == 'POST':
        data = request.get_json(silent=True) or {}

        # Toggle manual mode if requested
        if 'manual_mode' in data:
            _scarecrow_instance._manual_mode = bool(data['manual_mode'])
            if not _scarecrow_instance._manual_mode:
                _servo_controller.calibrating = False  # leaving manual mode ends calibration's slow moves too
            log_event("SYSTEM",
                      f"Manual mode {'enabled' if _scarecrow_instance._manual_mode else 'disabled'}")

        # Set pan and/or tilt if requested. Clamp to configured ranges.
        target_pan = _servo_controller.target_pan
        target_tilt = _servo_controller.target_tilt
        moved = False
        if 'pan' in data:
            target_pan = max(config.PAN_MIN, min(config.PAN_MAX, float(data['pan'])))
            moved = True
        if 'tilt' in data:
            target_tilt = max(config.TILT_MIN, min(config.TILT_MAX, float(data['tilt'])))
            moved = True
        if moved:
            _servo_controller.set_target(target_pan, target_tilt)

    # Return current state (covers both GET and POST response)
    return jsonify(
        ok=True,
        manual_mode=_scarecrow_instance._manual_mode,
        target_pan=_servo_controller.target_pan,
        target_tilt=_servo_controller.target_tilt,
        current_pan=_servo_controller.pan_angle,
        current_tilt=_servo_controller.tilt_angle,
        ranges={
            'pan_min': config.PAN_MIN,
            'pan_max': config.PAN_MAX,
            'pan_center': config.PAN_CENTER,
            'tilt_min': config.TILT_MIN,
            'tilt_max': config.TILT_MAX,
            'tilt_center': config.TILT_CENTER,
            'tilt_drooped': config.TILT_DROOPED,
            'voice_look_pan': config.VOICE_LOOK_PAN,
            'voice_look_tilt': config.VOICE_LOOK_TILT,
            'voice_look_enabled': config.VOICE_LOOK_ENABLED,
        },
    )


@app.route('/api/log')
def api_log():
    """Return interaction log entries since a given ID."""
    since_id = request.args.get('since', 0, type=int)

    with _log_lock:
        entries = [e for e in _interaction_log if e['id'] > since_id]

    return jsonify(entries=entries)


@app.route('/api/quiet_hours')
def api_get_quiet_hours():
    """Return current quiet hours settings."""
    return jsonify(
        enabled=config.QUIET_HOURS_ENABLED,
        start=config.QUIET_HOURS_START,
        end=config.QUIET_HOURS_END,
        volume=config.QUIET_HOURS_VOLUME,
        scan=config.QUIET_HOURS_IDLE_SCAN,
    )


@app.route('/api/quiet_hours', methods=['POST'])
def api_set_quiet_hours():
    """Update quiet hours settings at runtime."""
    data = request.get_json(silent=True) or {}

    fields = {'enabled': 'QUIET_HOURS_ENABLED', 'start': 'QUIET_HOURS_START',
              'end': 'QUIET_HOURS_END', 'volume': 'QUIET_HOURS_VOLUME',
              'scan': 'QUIET_HOURS_IDLE_SCAN'}
    changes = {key: data[f] for f, key in fields.items() if f in data}
    for k in ('QUIET_HOURS_ENABLED', 'QUIET_HOURS_IDLE_SCAN'):
        if k in changes:
            changes[k] = bool(changes[k])
    try:
        config.SETTINGS.set_many(changes)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400

    log_event("SYSTEM",
              f"Quiet hours updated: {'ON' if config.QUIET_HOURS_ENABLED else 'OFF'} "
              f"{config.QUIET_HOURS_START}:00–{config.QUIET_HOURS_END}:00 "
              f"(vol {config.QUIET_HOURS_VOLUME:.0%}, head sweeps "
              f"{'on' if config.QUIET_HOURS_IDLE_SCAN else 'off'})")

    return jsonify(
        ok=True,
        enabled=config.QUIET_HOURS_ENABLED,
        start=config.QUIET_HOURS_START,
        end=config.QUIET_HOURS_END,
        volume=config.QUIET_HOURS_VOLUME,
        scan=config.QUIET_HOURS_IDLE_SCAN,
    )


# ---------------------------------------------------------------------------
# Shared functions — called from scarecrow.py
# ---------------------------------------------------------------------------

_log_counter = 0


def log_event(source: str, text: str, scene: str = None):
    """
    Add an entry to the interaction log.

    Args:
        source: Label like "AI", "MANUAL", "REPLY", "SYSTEM"
        text: The spoken line or event description
        scene: Optional scene description for AI interactions
    """
    global _log_counter, _last_interaction_time
    _log_counter += 1

    if source in ("AI", "REPLY", "MANUAL"):
        _last_interaction_time = time.time()

    entry = {
        'id': _log_counter,
        'time': time.strftime("%H:%M:%S"),
        'source': source,
        'text': text,
        'scene': scene,
    }

    with _log_lock:
        _interaction_log.append(entry)


def register_scarecrow(scarecrow_instance):
    """
    Register the main Scarecrow instance so the dashboard can access
    its subsystems (speech, servos, brain, listener).

    Call this from scarecrow.py after initializing all subsystems.
    """
    global _speech_engine, _servo_controller, _scarecrow_brain, _scarecrow_instance, _listener
    _scarecrow_instance = scarecrow_instance
    _speech_engine = scarecrow_instance.speech
    _servo_controller = scarecrow_instance.servos
    _scarecrow_brain = scarecrow_instance.brain
    _listener = getattr(scarecrow_instance, 'listener', None)

    # Switching backend needs the brain to set up its client; if that fails
    # (e.g. no API key) the store rejects the change and nothing is saved.
    def _switch_backend(backend):
        try:
            _scarecrow_brain.set_backend(backend)
        except Exception as e:
            raise ValueError(str(e))
    config.SETTINGS.add_hook('AI_BACKEND', _switch_backend)
    print("[web] Dashboard registered with scarecrow subsystems")


def update_frame(frame, detections=None, tracks=None, tracker=None):
    """
    Called from the main scarecrow loop to update the video feed.

    Args:
        frame: Raw camera frame (numpy array)
        detections: List of detection dicts from PersonDetector
        tracks: List of TrackedPerson objects from PersonTracker
        tracker: PersonTracker instance (for debug status)
    """
    global _current_frame, _latest_scene

    if frame is None:
        return

    annotated = frame.copy()

    # Draw detections
    if detections:
        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            conf = det["confidence"]

            # Pose skeleton (drawn first so bbox/HEAD sit on top of it)
            keypoints = det.get("keypoints")
            if keypoints is not None:
                # Limb lines: only draw an edge when BOTH endpoints are visible
                for a, b in SKELETON_EDGES:
                    va = keypoints[a, 2]
                    vb = keypoints[b, 2]
                    if va < KEYPOINT_CONFIDENCE or vb < KEYPOINT_CONFIDENCE:
                        continue
                    pa = (int(keypoints[a, 0]), int(keypoints[a, 1]))
                    pb = (int(keypoints[b, 0]), int(keypoints[b, 1]))
                    cv2.line(annotated, pa, pb, SKELETON_LINE_COLOR, 2)
                # Joint dots: draw any keypoint that meets the confidence bar
                for kx, ky, kv in keypoints:
                    if kv < KEYPOINT_CONFIDENCE:
                        continue
                    cv2.circle(annotated, (int(kx), int(ky)), 3,
                               SKELETON_POINT_COLOR, -1)

            # Bounding box
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)

            # Confidence label
            cv2.putText(annotated, f"{conf:.0%}", (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

            # Head target crosshair
            hx, hy = int(det["head"][0]), int(det["head"][1])
            cv2.drawMarker(annotated, (hx, hy), (0, 0, 255),
                           cv2.MARKER_CROSS, 20, 2)
            cv2.putText(annotated, "HEAD", (hx + 12, hy + 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)

    # Draw tracker info
    if tracks:
        for track in tracks:
            x1, y1, x2, y2 = track.bbox
            spoken = track.spoken_to

            # Color: orange if spoken to, cyan if new
            color = (0, 140, 255) if spoken else (255, 255, 0)
            label = f"#{track.track_id} {'DONE' if spoken else 'NEW'}"

            cv2.putText(annotated, label, (x1, y2 + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    # Tracker status overlay in top-left
    if tracker:
        status_lines = tracker.debug_status().split('\n')
        for i, line in enumerate(status_lines):
            cv2.putText(annotated, line, (10, 22 + i * 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1)

    # Timestamp
    timestamp = time.strftime("%H:%M:%S")
    cv2.putText(annotated, timestamp, (annotated.shape[1] - 100, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

    with _frame_lock:
        _current_frame = annotated
        _latest_scene = (frame.copy(), list(detections or []), tracks, tracker)


def _local_ips() -> list[str]:
    """Discover all of this host's local IPv4 addresses so we can include
    them in the self-signed cert's SubjectAltName.  Modern browsers may
    post-validate the cert against the address you typed (e.g. 192.168.x.y)
    even after you click through the warning — without the IP in SAN they
    silently abandon the connection."""
    import socket as _socket
    ips = {"127.0.0.1"}
    # All addresses associated with the hostname
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None):
            ip = info[4][0]
            if ip and ":" not in ip:  # skip IPv6
                ips.add(ip)
    except Exception:
        pass
    # Primary outbound interface (the "what IP does this box use to reach
    # the internet" trick — no packets actually sent, just routing lookup)
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ips)


def _generate_self_signed_cert(cert_path: str, key_path: str):
    """Generate a self-signed TLS cert with the host's local IPs and a
    handful of common DNS names baked into the SubjectAltName extension."""
    ips = _local_ips()
    san_parts = [f"IP:{ip}" for ip in ips] + [
        "DNS:scarecrow", "DNS:scarecrow.local", "DNS:localhost",
    ]
    san = ",".join(san_parts)

    base_cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", key_path, "-out", cert_path,
        "-days", "3650", "-nodes",
        "-subj", "/CN=scarecrow",
    ]
    # Try with -addext first (requires openssl 1.1.1+, which the Pi has)
    try:
        subprocess.run(
            base_cmd + ["-addext", f"subjectAltName={san}"],
            check=True, capture_output=True, timeout=30,
        )
        print(f"[web] Generated TLS cert (SAN: {san})")
        return
    except subprocess.CalledProcessError:
        pass
    except Exception as e:
        print(f"[web] openssl failed with -addext ({e}) — retrying without SAN")

    # Fallback: cert without SAN (older openssl)
    try:
        subprocess.run(base_cmd, check=True, capture_output=True, timeout=30)
        print("[web] Generated TLS cert (no SAN — older openssl)")
    except Exception as e:
        print(f"[web] openssl failed ({e}) — falling back to plain HTTP "
              "(mic voice input will not work)")


def start_web_server(port=5000):
    """Start the dashboard web server in a background thread, over HTTPS.

    Serves with a self-signed certificate so the browser allows microphone
    access — getUserMedia is only exposed in secure contexts.  Click
    'Advanced → Proceed' the first time to accept the cert.

    Uses Python's stdlib wsgiref + socketserver.ThreadingMixIn instead of
    Flask's app.run() or werkzeug's make_server().  Both werkzeug paths
    have proven flaky for embedded threaded HTTPS use on the Pi — after
    the browser click-throughs the cert warning and opens a fresh
    connection, werkzeug can hang on the second handshake.  The stdlib
    path is dumber and more reliable: each accepted connection gets its
    own daemon thread, including the SSL handshake.
    """
    import ssl as _ssl
    import socketserver as _ss
    from wsgiref.simple_server import (
        make_server as _make_server,
        WSGIServer as _WSGIServer,
        WSGIRequestHandler as _WSGIHandler,
    )

    _here = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(_here, "scarecrow.crt")
    key_path  = os.path.join(_here, "scarecrow.key")

    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        _generate_self_signed_cert(cert_path, key_path)

    use_ssl = os.path.exists(cert_path) and os.path.exists(key_path)

    class _ThreadingWSGIServer(_ss.ThreadingMixIn, _WSGIServer):
        daemon_threads = True
        allow_reuse_address = True

    class _QuietHandler(_WSGIHandler):
        def log_message(self, format, *args):
            return  # suppress per-request access logs

        def handle(self):
            # Trigger the deferred SSL handshake in this worker thread
            # rather than the accept() loop, so a slow handshake doesn't
            # block other incoming connections.
            try:
                if hasattr(self.connection, "do_handshake"):
                    self.connection.do_handshake()
            except Exception as e:
                # Silently drop bogus / unencrypted requests
                if config.DEBUG:
                    print(f"[web] TLS handshake failed: {e}")
                return
            super().handle()

    def _run():
        try:
            httpd = _make_server(
                "0.0.0.0", port, app,
                server_class=_ThreadingWSGIServer,
                handler_class=_QuietHandler,
            )
            if use_ssl:
                ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
                ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
                # Tell the browser we speak HTTP/1.1, not HTTP/2 — our WSGI
                # server has no HTTP/2 support and an unresolved ALPN
                # negotiation can hang the connection.
                try:
                    ctx.set_alpn_protocols(["http/1.1"])
                except (NotImplementedError, AttributeError):
                    pass
                ctx.load_cert_chain(cert_path, key_path)
                # do_handshake_on_connect=False defers the TLS handshake
                # until the request handler thread starts processing —
                # see _QuietHandler.handle above.
                httpd.socket = ctx.wrap_socket(
                    httpd.socket, server_side=True,
                    do_handshake_on_connect=False,
                )
            httpd.serve_forever()
        except Exception as e:
            print(f"[web] Server crashed: {e}")
            import traceback
            traceback.print_exc()

    server_thread = threading.Thread(target=_run, daemon=True)
    server_thread.start()
    proto = "https" if use_ssl else "http"
    print(f"[web] Dashboard → {proto}://0.0.0.0:{port}")
    if use_ssl:
        print("[web] First visit: click 'Advanced → Proceed' to accept the self-signed cert.")
    return server_thread


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _generate_frames():
    """Yield MJPEG frames for the video stream."""
    while True:
        with _frame_lock:
            frame = _current_frame

        if frame is not None:
            _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n'
            )
        time.sleep(0.05)  # ~20fps max
