"""
Settings page for the dashboard (/settings)
-------------------------------------------
Every setting in settings_store.SETTINGS_SCHEMA except the "main" tier
(persona, AI backend, quiet hours — those have their own controls on the
dashboard home page and save through the same store), grouped, with search,
"modified" badges, per-setting and per-group reset, and an "Advanced" section
per group. "Live" settings apply immediately; "restart" settings raise a banner
with a Restart button (systemd only).

API keys (secrets_store.py) are write-only: the page only learns whether a
key is set. Each key is shown next to what uses it; the AI providers are
cards (PROVIDER_CARDS) holding their key, URL and model plus one Test button.

Storage, validation and reset semantics live in settings_store.py. Auth is
app-wide (dashboard_auth.py), so these routes need no extra checks.
"""

import os
import subprocess
import threading
import time

from flask import Blueprint, jsonify, render_template_string, request

import config
import llm
import stt_providers
import tts_providers
from secrets_store import SECRET_KEYS, SECRETS, SecretsStore, test_key
from settings_store import GROUPS, SCHEMA_BY_KEY, SETTINGS_SCHEMA

bp = Blueprint("settings", __name__)

SERVICE_NAME = "scarecrow.service"
_STARTED_AT = time.time()  # lets the page notice when a restart has completed
SECRETS_STORE = SecretsStore(config)
_MODELS_CACHE = {}
MODELS_CACHE_SECONDS = 120


def _wd():
    import web_dashboard  # lazy: web_dashboard imports this module
    return web_dashboard


# Provider cards: the key + settings with card=<id>, one Test for the lot.
# kind/name say which adapter the Test button (and "in use" badge) refers to.
PROVIDER_CARDS = [
    {"id": "claude", "group": "AI", "kind": "llm", "name": "claude", "title": "Claude",
     "help": "Anthropic's API (cloud, paid per use)."},
    {"id": "ollama", "group": "AI", "kind": "llm", "name": "ollama", "title": "Ollama",
     "help": "Your own Ollama server on the LAN (free)."},
    {"id": "openai", "group": "AI", "kind": "llm", "name": "openai", "title": "OpenAI-compatible",
     "help": "OpenAI, OpenRouter, Groq, Gemini, LM Studio… anything with an OpenAI-style API."},
    {"id": "tts-elevenlabs", "group": "Voice", "kind": "tts", "name": "elevenlabs", "title": "ElevenLabs",
     "help": "Cloud voice — the most natural."},
    {"id": "tts-openai", "group": "Voice", "kind": "tts", "name": "openai", "title": "OpenAI voice",
     "help": "Cloud voice. Uses the OpenAI API key under Hearing."},
    {"id": "tts-piper", "group": "Voice", "kind": "tts", "name": "piper", "title": "Piper",
     "help": "Runs on the Pi — works without internet."},
    {"id": "tts-espeak", "group": "Voice", "kind": "tts", "name": "espeak", "title": "espeak",
     "help": "Robotic, but runs on the Pi and works without internet."},
    {"id": "stt-faster-whisper", "group": "Hearing", "kind": "stt", "name": "faster-whisper",
     "title": "faster-whisper server", "help": "Your own server on the LAN — fast and free."},
    {"id": "stt-openai", "group": "Hearing", "kind": "stt", "name": "openai", "title": "OpenAI",
     "help": "Cloud transcription."},
    {"id": "stt-local", "group": "Hearing", "kind": "stt", "name": "local", "title": "Whisper on the Pi",
     "help": "Slow, but works without internet."},
]
_CARD = {c["id"]: c for c in PROVIDER_CARDS}
_KIND_MODULE = {"tts": tts_providers, "stt": stt_providers}

# "main" settings live on the dashboard home page, not here
PAGE_SCHEMA = [m for m in SETTINGS_SCHEMA if m["tier"] != "main"]


def _under_systemd() -> bool:
    return bool(os.environ.get("INVOCATION_ID"))


def _entry(meta):
    store = config.SETTINGS
    k = meta["key"]
    entry = {**meta, "value": store.get(k), "default": store.default(k),
             "overridden": store.is_overridden(k)}
    if meta.get("presets"):
        entry["presets"] = llm.PRESETS
    return entry


def _friendly(e: Exception) -> str:
    """Short error text for Test buttons (connection errors are very wordy)."""
    import requests
    if isinstance(e, requests.exceptions.ConnectionError):
        host = getattr(getattr(e, "request", None), "url", "") or ""
        return f"Couldn't connect{' to ' + host.split('/')[2] if host.count('/') >= 2 else ''} — is it running?"
    if isinstance(e, requests.exceptions.Timeout):
        return "Timed out — no answer within the time limit"
    return f"{type(e).__name__}: {e}"[:300]


def _secret_label(key):
    """'ElevenLabs API key' rather than just 'API key' for keys shown inside a card."""
    sec = next(x for x in SECRETS if x["key"] == key)
    card = _CARD.get(sec.get("card"))
    return f"{card['title']} {sec['label']}" if card and sec["label"] == "API key" else sec["label"]


def _cards():
    """Cards with why each voice / hearing provider can't be used right now (if so)."""
    out = []
    for c in PROVIDER_CARDS:
        mod = _KIND_MODULE.get(c["kind"])
        out.append({**c, "unavailable": mod.PROVIDERS[c["name"]].unavailable() if mod else None})
    return out


def _in_use(cards):
    """Card id per group that's doing the job: the AI backend picked on the dashboard;
    the voice that spoke last (or would speak first); the first usable hearing provider."""
    wd = _wd()
    brain = getattr(wd, "_scarecrow_brain", None)
    speech = getattr(wd, "_speech_engine", None)
    usable = {(c["kind"], c["name"]) for c in cards if not c["unavailable"]}
    first = lambda kind, order: next((f"{kind}-{n}" for n in order if (kind, n) in usable), None)
    voice = f"tts-{speech.engine}" if speech and speech.engine else first("tts", tts_providers.order())
    # Hearing: skip one that's currently failing (the cascade has moved past it)
    healthy = [n for n in stt_providers.order() if stt_providers._healthy.get(n, True)]
    return {"AI": brain.backend if brain else config.AI_BACKEND, "Voice": voice,
            "Hearing": first("stt", healthy) or first("stt", stt_providers.order())}


def _state():
    cards = _cards()
    return {"pending_restart": config.SETTINGS.pending_restart(),
            "secrets": SECRETS_STORE.status(),
            "cards": cards, "in_use": _in_use(cards),
            "can_restart": _under_systemd(), "started_at": _STARTED_AT}


def _label(meta):
    """'Ollama Model' rather than just 'Model' for settings inside a provider card."""
    card = next((c["title"] for c in PROVIDER_CARDS if c["id"] == meta.get("card")), None)
    return f"{card} {meta['label'].lower()}" if card else meta["label"]


def _fmt(meta, value):
    if meta["type"] == "lines":
        return f"{len(value)} line(s)"
    if meta["type"] == "bool":
        return "on" if value else "off"
    return str(value)


@bp.route("/settings")
def settings_page():
    return render_template_string(SETTINGS_HTML)


@bp.route("/api/settings")
def api_settings():
    return jsonify(ok=True, groups=GROUPS, settings=[_entry(m) for m in PAGE_SCHEMA],
                   **_state())


@bp.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    data = request.get_json(silent=True) or {}
    changes = data.get("changes")
    if not isinstance(changes, dict) or not changes:
        return jsonify(ok=False, error="Nothing to save"), 400
    if any(v is None for v in changes.values()):
        return jsonify(ok=False, error="Use Reset to go back to a default"), 400
    store = config.SETTINGS
    before = store.values()
    try:
        store.set_many(changes)
    except (ValueError, KeyError) as e:
        return jsonify(ok=False, error=str(e)), 400
    after = store.values()
    for k in changes:
        if after[k] != before[k]:
            meta = SCHEMA_BY_KEY[k]
            note = " (after restart)" if meta["apply"] == "restart" else ""
            _wd().log_event("SYSTEM", f"Setting '{_label(meta)}' → {_fmt(meta, after[k])}{note}")
    return jsonify(ok=True, settings=[_entry(SCHEMA_BY_KEY[k]) for k in changes], **_state())


@bp.route("/api/settings/reset", methods=["POST"])
def api_settings_reset():
    data = request.get_json(silent=True) or {}
    if data.get("group"):
        keys = [m["key"] for m in PAGE_SCHEMA if m["group"] == data["group"]]
        what = f"{data['group']} settings"
    else:
        keys = [k for k in (data.get("keys") or []) if k in SCHEMA_BY_KEY]
        what = ", ".join(f"'{_label(SCHEMA_BY_KEY[k])}'" for k in keys)
    if not keys:
        return jsonify(ok=False, error="Nothing to reset"), 400
    store = config.SETTINGS
    if not any(store.is_overridden(k) for k in keys):
        return jsonify(ok=True, settings=[_entry(SCHEMA_BY_KEY[k]) for k in keys], **_state())
    try:
        store.reset(keys)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    _wd().log_event("SYSTEM", f"Reset to default: {what}")
    return jsonify(ok=True, settings=[_entry(SCHEMA_BY_KEY[k]) for k in keys], **_state())


@bp.route("/api/secrets/save", methods=["POST"])
def api_secrets_save():
    data = request.get_json(silent=True) or {}
    key = data.get("key", "")
    if key not in SECRET_KEYS:
        return jsonify(ok=False, error="Unknown key"), 400
    try:
        if data.get("remove"):
            SECRETS_STORE.set(key, None)
            what = "removed"
        else:
            SECRETS_STORE.set(key, data.get("value"))
            what = "saved"
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except OSError as e:
        return jsonify(ok=False, error=f"Couldn't write .env: {e.strerror}"), 500
    # Never log the value
    _wd().log_event("SYSTEM", f"{_secret_label(key)} {what}")
    return jsonify(ok=True, **_state())


@bp.route("/api/secrets/test", methods=["POST"])
def api_secrets_test():
    """Test the typed key if one is given, otherwise the saved one."""
    data = request.get_json(silent=True) or {}
    key = data.get("key", "")
    if key not in SECRET_KEYS:
        return jsonify(ok=False, error="Unknown key"), 400
    value = data.get("value") or SECRETS_STORE.current(key)
    if not value:
        return jsonify(ok=False, error="No key to test — paste one first"), 400
    try:
        value = SECRETS_STORE.validate(key, value)
        return jsonify(ok=True, message=test_key(key, value, base_url=config.OPENAI_COMPAT_BASE_URL))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't reach the provider ({type(e).__name__})"), 502


@bp.route("/api/llm/test", methods=["POST"])
def api_llm_test():
    """Send one tiny picture to a backend with its current settings. Doesn't
    need to be the active backend; nothing is spoken or logged."""
    backend = (request.get_json(silent=True) or {}).get("backend", "")
    if backend not in llm.PROVIDERS:
        return jsonify(ok=False, error="Unknown backend"), 400
    provider = llm.PROVIDERS[backend]()
    t0 = time.time()
    try:
        provider.ensure_ready()
        msg = provider.test()
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"[:300]), 502
    return jsonify(ok=True, message=f"{msg} — {time.time() - t0:.1f}s")


@bp.route("/api/provider/test", methods=["POST"])
def api_provider_test():
    """Card Test button. AI: one tiny picture. Voice: synthesize a line (not
    played). Hearing: transcribe the bundled test phrase."""
    card = _CARD.get((request.get_json(silent=True) or {}).get("card", ""))
    if not card:
        return jsonify(ok=False, error="Unknown provider"), 400
    t0 = time.time()
    try:
        if card["kind"] == "llm":
            provider = llm.PROVIDERS[card["name"]]()
            provider.ensure_ready()
            msg = f"{provider.test()} — {time.time() - t0:.1f}s"
        else:
            msg = _KIND_MODULE[card["kind"]].test(card["name"])
    except Exception as e:
        return jsonify(ok=False, error=_friendly(e)), 502
    return jsonify(ok=True, message=msg)


@bp.route("/api/tts/say", methods=["POST"])
def api_tts_say():
    """Voice card 'Say it': speak a test line through the speaker with that voice only."""
    card = _CARD.get((request.get_json(silent=True) or {}).get("card", ""))
    if not card or card["kind"] != "tts":
        return jsonify(ok=False, error="Unknown voice"), 400
    speech = getattr(_wd(), "_speech_engine", None)
    if not speech:
        return jsonify(ok=False, error="Speech engine not running"), 503
    if speech.is_speaking:
        return jsonify(ok=False, error="He's talking — try again in a moment"), 409
    why = tts_providers.PROVIDERS[card["name"]].unavailable()
    if why:
        return jsonify(ok=False, error=why), 400
    threading.Thread(target=speech.speak, args=("Happy Halloween. I've been waiting for you.",),
                     kwargs={"provider": card["name"]}, daemon=True).start()
    return jsonify(ok=True)


@bp.route("/api/llm/models", methods=["POST"])
def api_llm_models():
    """The provider's model list, using its current URL + key."""
    backend = (request.get_json(silent=True) or {}).get("backend", "")
    if backend not in llm.PROVIDERS:
        return jsonify(ok=False, error="Unknown backend"), 400
    # Cached briefly per backend + whatever it depends on, so opening the
    # page doesn't hit three providers every time.
    ident = (backend, config.OLLAMA_BASE_URL, config.OPENAI_COMPAT_BASE_URL,
             hash((config.ANTHROPIC_API_KEY, config.OPENAI_API_KEY, config.OPENAI_COMPAT_API_KEY)))
    hit = _MODELS_CACHE.get(ident)
    if hit and time.time() - hit[0] < MODELS_CACHE_SECONDS:
        return jsonify(ok=True, models=hit[1])
    try:
        models = llm.PROVIDERS[backend]().list_models()
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"[:300]), 502
    _MODELS_CACHE[ident] = (time.time(), models)
    return jsonify(ok=True, models=models)


@bp.route("/api/settings/restart", methods=["POST"])
def api_settings_restart():
    if not _under_systemd():
        return jsonify(ok=False, error="Not running as the systemd service — restart it by hand."), 400
    _wd().log_event("SYSTEM", "Restart requested from the Settings page")

    def _restart():
        time.sleep(1.0)  # let the HTTP response go out first
        # --no-block: systemd owns the restart job, so it completes even
        # though this process (and this child) get stopped by it.
        subprocess.Popen(["systemctl", "--user", "--no-block", "restart", SERVICE_NAME],
                         start_new_session=True)

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify(ok=True)


SETTINGS_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<title>Settings · StrawMan</title>
{{ ui_head() }}
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 * { box-sizing: border-box; margin: 0; padding: 0; }
 body { background:#111; color:#e0e0e0; font-size:14px; padding:12px; }
 .wrap { max-width:860px; margin:0 auto; }
 header { display:flex; align-items:baseline; justify-content:space-between; gap:12px; padding:8px 0 14px; flex-wrap:wrap; }
 h1 { color:#ff8800; font-size:24px; font-weight:400; display:flex; align-items:center; gap:10px; }
 .logo { height:44px; width:auto; display:block; }
 nav { display:flex; gap:18px; }
 nav a { display:flex; align-items:center; gap:6px; color:#aaa; text-decoration:none; }
 nav a:hover { color:#ff8800; }
 .toolbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:6px; }
 .toolbar input[type=search] { flex:1; min-width:180px; }
 .switch { display:flex; align-items:center; gap:6px; color:#aaa; cursor:pointer; user-select:none; }
 .banner { position:sticky; top:0; z-index:5; background:#3a2a00; border:1px solid #a86000; color:#ffcf8a; border-radius:12px;
           padding:10px 12px; margin-bottom:10px; display:none; font-size:.9em; }
 .banner .actions { margin-top:6px; }
 .group-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin:22px 0 8px; }
 h2.group { display:flex; align-items:center; gap:10px; color:#e8e8e8; font-size:18px; font-weight:400; }
 h2.group .i { color:#ff8800; }
 .panel { background:#1c1c1c; border-radius:12px; padding:4px 16px; margin-bottom:8px; box-shadow:0 2px 2px rgba(0,0,0,.35),0 1px 5px rgba(0,0,0,.3); }
 .set { padding:10px 0; border-bottom:1px solid #262626; }
 .set:last-child { border-bottom:none; }
 .set-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
 .title { font-weight:500; color:#eee; }
 .help { color:#8a8a8a; font-size:13px; margin:3px 0 6px; line-height:1.4; }
 .ctl { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
 .badge { font-size:11px; font-weight:500; padding:2px 8px; border-radius:10px; }
 .b-mod { background:#3a2a00; color:#ffb340; }
 .b-restart { background:#222; color:#999; border:1px solid #3a3a3a; }
 .b-pending { background:#4a1d00; color:#ff8a3d; }
 .b-ok { background:#0f3320; color:#5ee08f; }
 .b-unset { background:#222; color:#888; }
 input, select, textarea { background:#141414; color:#e8e8e8; border:1px solid #3a3a3a; border-radius:8px; padding:7px 10px; font-size:14px; }
 input[type=number] { width:110px; }
 input.text, select.model { width:100%; max-width:440px; }
 select.model { text-overflow:ellipsis; }
 textarea { width:100%; min-height:110px; font:13px/1.45 ui-monospace,Menlo,Consolas,monospace; resize:vertical; }
 input:focus, select:focus, textarea:focus { outline:none; border-color:#ff8800; }
 input.bad, textarea.bad { border-color:#ff6b6b; }
 input[type=checkbox] { width:18px; height:18px; accent-color:#ff8800; }
 button { background:#262626; color:#ddd; border:1px solid #3a3a3a; border-radius:8px; padding:6px 12px; font-size:13px; font-weight:500; cursor:pointer;
          display:inline-flex; align-items:center; gap:6px; }
 button:hover:not(:disabled) { border-color:#ff8800; }
 button:disabled { opacity:.4; cursor:default; }
 button.primary { background:#ff8800; color:#111; border-color:#ff8800; }
 button.danger { color:#ff7070; border-color:#5a2a2a; }
 .msg { font-size:.78em; color:#888; }
 .msg.err { color:#ff6b6b; } .msg.ok, .msg.okstay { color:#5ee08f; }
 .range { font-size:.75em; color:#666; }
 details.adv > summary { cursor:pointer; color:#d9a24a; font-size:13px; font-weight:500; padding:10px 0 6px; }
 details.adv .warn { color:#9a8a6a; font-size:.78em; margin-bottom:4px; }
 .empty { color:#777; font-size:.85em; padding:20px 0; display:none; }
 .card { border:1px solid #2e2e2e; border-radius:12px; background:#181818; padding:4px 14px; margin:10px 0; }
 .card.active { border-color:#7a4a00; }
 .card-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:8px 0 2px; }
 .card-head .title { display:flex; align-items:center; gap:8px; font-size:16px; font-weight:500; color:#eee; }
 .card-head .title .i { color:#ff8800; }
 .card > .help { margin-bottom:2px; }
 .b-active { background:#0f3320; color:#5ee08f; }
 .order { display:flex; flex-direction:column; gap:4px; width:100%; max-width:440px; }
 .ord-row { display:flex; align-items:center; gap:8px; padding:4px 6px 4px 8px; border:1px solid #2e2e2e; border-radius:8px; background:#181818; }
 .ord-row .ord-label { flex:1; }
 .ord-row .ord-why { color:#777; font-size:12px; }
 .ord-row button { padding:2px 4px; border:none; background:none; color:#999; }
 .ord-row button:hover { color:#ff8800; }
 .ord-row:first-child button[data-act=up], .ord-row:last-child button[data-act=down] { visibility:hidden; }
</style>
</head>
<body>
<div class="wrap">
 <header>
  <h1><a href="/" title="Dashboard"><img class="logo" src="/static/strawman-logo.png" alt="StrawMan"></a>Settings</h1>
  <nav><a href="/">{{ icon("view-dashboard-outline", 18) }}Dashboard</a><a href="/prompts">{{ icon("script-text-outline", 18) }}Prompts</a><a href="/welcome">{{ icon("restart", 18) }}Run setup</a><a href="/logout">{{ icon("logout", 18) }}Log out</a></nav>
 </header>

 <div class="banner" id="banner">
  <div id="banner-text"></div>
  <div class="actions"><button class="primary" id="restart-btn" onclick="restartNow()">{{ icon("restart", 18) }}Restart now</button>
   <span class="msg" id="restart-msg"></span></div>
 </div>

 <div class="toolbar">
  <input type="search" id="q" placeholder="Search settings…" oninput="applyFilter()">
  <label class="switch"><input type="checkbox" id="show-adv" onchange="toggleAdvanced()"> Show advanced</label>
 </div>
 <div class="help">Most settings, including API keys, apply right away. Ones marked <span class="badge b-restart">restart</span> take effect after a restart.
  Defaults are built into the code and can always be restored.
  Persona, AI backend and quiet hours are on the <a href="/" style="color:#ff8800">dashboard</a>.</div>

 <div id="groups"></div>
 <div class="empty" id="empty">No settings match.</div>
</div>

{{ ui_icons_js() }}
<script>
const _origFetch = window.fetch;
window.fetch = async (...a) => { const r = await _origFetch(...a); if (r.status === 401) location.href = '/login'; return r; };
async function post(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  let j = {}; try { j = await r.json(); } catch (e) {}
  if (!r.ok || !j.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} } };

let S = null;              // last server state
const byKey = {};          // key -> setting entry

async function load() {
  const r = await fetch('/api/settings'); S = await r.json();
  for (const s of S.settings) byKey[s.key] = s;
  document.getElementById('show-adv').checked = store.get('settings.showAdvanced') === '1';
  render(); updateBanner(S);
}

function fmtDefault(s) {
  if (s.type === 'bool') return s.default ? 'on' : 'off';
  if (s.type === 'lines') return s.default.length + ' lines';
  if (s.type === 'hour') return hourLabel(s.default);
  return s.default;
}
function hourLabel(h) { return String(h).padStart(2, '0') + ':00'; }

// Which provider kind an order list ranks (for the "why not" notes)
const ORDER_KIND = {TTS_ORDER: 'tts', STT_ORDER: 'stt'};
function orderRows(s, value) {
  const names = value.concat(s.choices.filter(c => !value.includes(c)));
  return names.map(n => {
    const card = S.cards.find(c => c.kind === ORDER_KIND[s.key] && c.name === n), why = card && card.unavailable;
    return `<div class="ord-row" data-name="${n}">
      <input type="checkbox" data-act="ord" ${value.includes(n) ? 'checked' : ''}>
      <span class="ord-label">${esc(s.labels[n] || n)}</span>${why ? `<span class="ord-why">${esc(why)}</span>` : ''}
      <button data-act="up" title="Move up">${mdi('chevron-up', 18)}</button>
      <button data-act="down" title="Move down">${mdi('chevron-down', 18)}</button></div>`;
  }).join('');
}
function control(s) {
  const v = s.value;
  if (s.type === 'order') return `<div class="order">${orderRows(s, v)}</div>`;
  if (s.test) return `<select class="model"><option value="${esc(v)}" selected>${esc(v)}</option></select>
    <input class="text" data-act="other" type="text" spellcheck="false" placeholder="Type a model name, then Enter" style="display:none">`;
  switch (s.type) {
    case 'bool': return `<input type="checkbox" ${v ? 'checked' : ''}>`;
    case 'choice': case 'resolution':
      return `<select>${s.choices.map(c => `<option value="${esc(c)}" ${String(c) === String(v) ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select>`;
    case 'hour':
      return `<select>${Array.from({length:24}, (_, h) => `<option value="${h}" ${h === v ? 'selected' : ''}>${hourLabel(h)}</option>`).join('')}</select>`;
    case 'int': case 'float':
      return `<input type="number" value="${v}" min="${s.min}" max="${s.max}" step="${s.step || (s.type === 'int' ? 1 : 'any')}">
              <span class="range">${s.min} – ${s.max}</span>`;
    case 'lines':
      return `<textarea spellcheck="false">${esc(v.join('\n'))}</textarea>`;
    default:
      return `<input class="text" type="text" value="${esc(v)}" spellcheck="false">`;
  }
}

function rowHtml(s) {
  return `<div class="set" id="set-${s.key}" data-key="${s.key}">
    <div class="set-head"><span class="title">${esc(s.label)}</span>
      ${s.apply === 'restart' ? '<span class="badge b-restart">restart</span>' : ''}
      <span class="badge b-mod" style="display:none">modified</span>
      <span class="badge b-pending" style="display:none">restart pending</span></div>
    ${s.help ? `<div class="help">${esc(s.help)}</div>` : ''}
    <div class="ctl">${control(s)}
      ${s.type === 'lines' ? '<button class="primary" data-act="save" disabled>Save</button>' : ''}
      ${s.presets ? `<select data-act="preset"><option value="">Presets…</option>${s.presets.map((p, i) =>
          `<option value="${i}">${esc(p.name)}${p.tested ? '' : ' (untested)'}</option>`).join('')}</select>` : ''}
      <button data-act="reset" title="Reset to default (${esc(fmtDefault(s))})">Reset</button>
      <span class="msg"></span></div></div>`;
}

const GROUP_ICONS = {'AI': 'brain', 'Voice': 'speaker', 'Hearing': 'ear-hearing', 'Camera & detection': 'cctv',
  'Behavior': 'emoticon-devil-outline', 'Logging': 'text-box-outline', 'Servos': 'axis-arrow', 'Radar': 'radar'};
const CARD_ICONS = {claude: 'cloud-outline', ollama: 'server-network', openai: 'api',
  'tts-elevenlabs': 'cloud-outline', 'tts-openai': 'cloud-outline', 'tts-piper': 'memory', 'tts-espeak': 'memory',
  'stt-faster-whisper': 'server-network', 'stt-openai': 'cloud-outline', 'stt-local': 'memory'};
const GROUP_NOTES = {AI: 'The one marked “in use” is chosen on the dashboard.',
  Servos: 'Easiest on the <a href="/calibrate" style="color:#ff8800">calibration page</a>, where you can move each servo and save where it is.'};
function render() {
  const host = document.getElementById('groups'); host.innerHTML = '';
  for (const g of S.groups) {
    const items = S.settings.filter(s => s.group === g), keys = S.secrets.filter(k => k.group === g);
    if (!items.length && !keys.length) continue;
    const basic = items.filter(s => s.tier !== 'advanced'), adv = items.filter(s => s.tier === 'advanced');
    const cards = S.cards.filter(c => c.group === g);
    const cardHtml = c => `<div class="card" data-card="${c.id}">
        <div class="card-head"><span class="title">${mdi(CARD_ICONS[c.id] || 'brain')}${esc(c.title)}</span>
          <span class="badge b-active" data-role="inuse" style="display:none">in use</span>
          <span class="badge b-unset" data-role="why" style="display:none"></span>
          <button data-act="card-test">${mdi('test-tube', 16)}Test</button>
          ${c.kind === 'tts' ? `<button data-act="say" title="Say a test line through his speaker">${mdi('volume-high', 16)}Say it</button>` : ''}
          <span class="msg"></span></div>
        <div class="help">${esc(c.help)}</div>
        ${keys.filter(k => k.card === c.id).map(secretRowHtml).join('')}
        ${basic.filter(s => s.card === c.id).map(rowHtml).join('')}</div>`;
    const sec = document.createElement('section'); sec.className = 'grp'; sec.dataset.group = g;
    sec.innerHTML = `<div class="group-head"><h2 class="group">${mdi(GROUP_ICONS[g] || 'cog')}${esc(g)}</h2>
        <button class="danger" data-act="reset-group" title="API keys are never reset">${mdi('restore', 16)}Reset group</button></div>
      <div class="panel">
        ${GROUP_NOTES[g] ? `<div class="help" style="margin-top:8px">${GROUP_NOTES[g]}</div>` : ''}
        ${basic.filter(s => !s.card && s.type === 'order').map(rowHtml).join('')}
        ${cards.map(cardHtml).join('')}
        ${keys.filter(k => !k.card).map(secretRowHtml).join('')}
        ${basic.filter(s => !s.card && s.type !== 'order').map(rowHtml).join('')}
        ${adv.length ? `<details class="adv"><summary>Advanced (${adv.length})</summary>
          <div class="warn">Tuning values. Change one at a time and watch how he behaves.</div>
          ${adv.map(rowHtml).join('')}</details>` : ''}
      </div>`;
    host.appendChild(sec);
    sec.querySelector('[data-act=reset-group]').onclick = () => resetGroup(g);
    for (const c of cards) wireCard(sec.querySelector(`[data-card="${c.id}"]`), c);
  }
  for (const s of S.settings) wire(s);
  for (const k of S.secrets) wireSecret(k);
  toggleAdvanced(true); applyFilter(); refreshBadges(); updateCards();
  for (const s of S.settings) if (s.test) loadModels(s);
}

const TEST_WORDS = {llm: 'Sending a test picture…', tts: 'Making a test line…', stt: 'Transcribing a test phrase…'};
function wireCard(card, c) {
  const btn = card.querySelector('[data-act=card-test]'), say = card.querySelector('[data-act=say]');
  const m = card.querySelector('.card-head .msg');
  const show = (t, cls) => { m.textContent = t; m.className = 'msg ' + (cls || ''); };
  btn.onclick = async () => {
    btn.disabled = true; show(TEST_WORDS[c.kind]);
    try { show((await post('/api/provider/test', {card: c.id})).message, 'okstay'); }
    catch (e) { show(e.message, 'err'); }
    btn.disabled = false;
  };
  if (say) say.onclick = async () => {
    try { await post('/api/tts/say', {card: c.id}); show('Speaking…', 'ok'); } catch (e) { show(e.message, 'err'); }
  };
}
// "in use" and "can't be used because…" badges; also the reasons in the order lists
function updateCards() {
  for (const c of S.cards) {
    const card = document.querySelector(`[data-card="${c.id}"]`); if (!card) continue;
    const inuse = S.in_use && S.in_use[c.group] === c.id;
    card.classList.toggle('active', inuse);
    card.querySelector('[data-role=inuse]').style.display = inuse ? '' : 'none';
    const why = card.querySelector('[data-role=why]');
    why.textContent = c.unavailable || ''; why.style.display = c.unavailable ? '' : 'none';
  }
  for (const s of S.settings) if (s.type === 'order' && el(s.key)) setInput(s);
}
async function refreshCards() {
  try { const j = await (await fetch('/api/settings')).json(); S.cards = j.cards; S.in_use = j.in_use; S.secrets = j.secrets; updateCards(); }
  catch (e) {}
}

// --- model dropdowns: filled from the provider's own list ---
const OTHER = '__other__';
// Reload a model list when a setting it depends on changes
const MODEL_DEPS = {OLLAMA_BASE_URL: 'OLLAMA_MODEL', OPENAI_COMPAT_BASE_URL: 'OPENAI_COMPAT_MODEL'};
async function loadModels(s) {
  const sel = input(s.key), cur = s.value;
  setMsg(s.key, 'Loading models…');
  let models = null, err = null;
  try { models = (await post('/api/llm/models', {backend: s.test})).models; } catch (e) { err = e.message; }
  const opt = (value, text) => `<option value="${esc(value)}" ${value === cur ? 'selected' : ''}>${esc(text)}</option>`;
  let html = '', found = false;
  if (models) {
    const groups = [[true, 'Accept pictures'], [null, 'Not sure if they accept pictures — use Test'],
                    [false, "Text only — he couldn't see visitors"]];
    for (const [v, name] of groups) {
      const ms = models.filter(m => m.vision === v);
      if (!ms.length) continue;
      html += `<optgroup label="${name}">` + ms.map(m => {
        // Ollama lists "llava:latest" for a setting of "llava"
        const value = (m.id === cur || m.id === cur + ':latest') ? cur : m.id;
        if (value === cur) found = true;
        return opt(value, m.id + (m.label && m.label !== m.id ? ' · ' + m.label : '') + (m.thinking ? ' — thinking model, avoid' : ''));
      }).join('') + '</optgroup>';
    }
  }
  if (!found) html = `<optgroup label="Current setting">${opt(cur, cur)}</optgroup>` + html;
  sel.innerHTML = html + `<option value="${OTHER}">Other model (type a name)…</option>`;
  sel.value = cur;
  if (err) setMsg(s.key, `Couldn't load the model list (${err}). Pick “Other model…” to type a name.`, 'err');
  else {
    const seeing = models.filter(m => m.vision).length, thinking = models.filter(m => m.thinking).length;
    setMsg(s.key, `${models.length} models from the provider` + (seeing ? `, ${seeing} accept pictures` : '') +
      (thinking ? `; avoid the ${thinking} thinking model(s)` : '') + (found ? '' : '. Your current model isn\'t in its list.'), '');
  }
}
function reloadModels() { for (const s of S.settings) if (s.test) loadModels(s); }

function el(k) { return document.getElementById('set-' + k); }
function input(k) { return el(k).querySelector('.ctl .order, .ctl input:not([data-act]), .ctl select:not([data-act]), .ctl textarea'); }
function readInput(s) {
  const i = input(s.key);
  if (s.type === 'order') return [...i.querySelectorAll('.ord-row')].filter(r => r.querySelector('input').checked).map(r => r.dataset.name);
  if (s.type === 'bool') return i.checked;
  if (s.type === 'lines') return i.value.split('\n').map(x => x.trim()).filter(Boolean);
  if (s.type === 'hour') return parseInt(i.value);
  if (s.type === 'int' || s.type === 'float') return i.value === '' ? '' : Number(i.value);
  return i.value;
}
function setInput(s) {
  const i = input(s.key), v = s.value;
  if (s.type === 'order') { i.innerHTML = orderRows(s, v); i.classList.remove('bad'); return; }
  if (s.type === 'bool') i.checked = v;
  else if (s.type === 'lines') i.value = v.join('\n');
  else {
    if (i.tagName === 'SELECT' && ![...i.options].some(o => o.value === String(v)))
      i.insertAdjacentHTML('afterbegin', `<option value="${esc(v)}">${esc(v)}</option>`);
    i.value = v;
  }
  i.classList.remove('bad');
}
function setMsg(k, text, cls) {
  const m = el(k).querySelector('.msg'); m.textContent = text; m.className = 'msg ' + (cls || '');
  if (cls === 'ok') setTimeout(() => { if (m.textContent === text) m.textContent = ''; }, 2500);
}

function wire(s) {
  const i = input(s.key), row = el(s.key);
  if (s.type === 'order') {
    // One delegated handler survives the list being redrawn after each save
    i.addEventListener('click', e => {
      const b = e.target.closest('button'), r = e.target.closest('.ord-row');
      if (!b || !r) return;
      const sib = b.dataset.act === 'up' ? r.previousElementSibling : r.nextElementSibling;
      if (!sib) return;
      b.dataset.act === 'up' ? i.insertBefore(r, sib) : i.insertBefore(sib, r);
      save(s.key);
    });
    i.addEventListener('change', e => { if (e.target.dataset.act === 'ord') save(s.key); });
  } else if (s.type === 'lines') {
    const btn = row.querySelector('[data-act=save]');
    i.addEventListener('input', () => { btn.disabled = i.value === s.value.join('\n'); });
    btn.onclick = () => save(s.key);
  } else {
    i.addEventListener('change', () => {
      if (i.value === OTHER) {      // model dropdown: type a name instead
        i.value = s.value;
        const o = row.querySelector('[data-act=other]'); o.style.display = ''; o.value = ''; o.focus();
        return;
      }
      save(s.key);
    });
    if (i.tagName === 'INPUT' && i.type !== 'checkbox')
      i.addEventListener('keydown', e => { if (e.key === 'Enter') i.blur(); });
  }
  row.querySelector('[data-act=reset]').onclick = () => reset([s.key]);
  const tb = row.querySelector('[data-act=test]');
  if (tb) tb.onclick = async () => {
    tb.disabled = true; setMsg(s.key, 'Sending a test picture…');
    try { const j = await post('/api/llm/test', {backend: s.test}); setMsg(s.key, j.message, 'okstay'); }
    catch (e) { setMsg(s.key, e.message, 'err'); }
    tb.disabled = false;
  };
  const other = row.querySelector('[data-act=other]');
  if (other) {
    const commit = () => { const v = other.value.trim(); if (!v) return;
      setInput({...s, value: v}); other.style.display = 'none'; save(s.key); };
    other.addEventListener('keydown', e => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') other.style.display = 'none'; });
    other.addEventListener('change', commit);
  }
  const ps = row.querySelector('[data-act=preset]');
  if (ps) ps.onchange = async () => {
    const p = s.presets[ps.value]; ps.value = ''; if (!p) return;
    const changes = {OPENAI_COMPAT_BASE_URL: p.base_url};
    if (p.model) changes.OPENAI_COMPAT_MODEL = p.model;
    try {
      const j = await post('/api/settings/save', {changes}); applyEntries(j);
      setMsg(s.key, `${p.name}: ${p.key}.${p.tested ? '' : ' Not tested with this scarecrow yet — use Test.'}`, 'okstay');
    } catch (e) { setMsg(s.key, e.message, 'err'); }
  };
}

function applyEntries(j) {
  for (const e of j.settings) { Object.assign(byKey[e.key], e); setInput(byKey[e.key]); }
  applyState(j); refreshBadges();
  for (const e of j.settings) if (MODEL_DEPS[e.key]) loadModels(byKey[MODEL_DEPS[e.key]]);
}
function applyState(j) {
  if (j.pending_restart) S.pending_restart = j.pending_restart;
  if (j.cards) { S.cards = j.cards; S.in_use = j.in_use; updateCards(); }
  updateBanner(S);
}

// --- API keys (write-only: the server only says whether each is set) ---
function secretRowHtml(k) {
  return `<div class="set" data-secret="${k.key}">
    <div class="set-head"><span class="title" style="display:flex;align-items:center;gap:6px">${mdi('key-variant', 16)}${esc(k.label)}</span>
      <span class="badge ${k.set ? 'b-ok' : 'b-unset'}">${k.set ? 'set ✓' : 'not set'}</span></div>
    <div class="help">${esc(k.help)}</div>
    <div class="ctl"><input class="text" type="password" autocomplete="new-password" spellcheck="false"
        placeholder="${k.set ? 'Paste a new key to replace it' : 'Paste key'}">
      <button class="primary" data-act="save" disabled>Save</button>
      ${k.card ? '' : `<button data-act="test" ${k.set ? '' : 'disabled'}>Test</button>`}
      <button class="danger" data-act="remove" ${k.set ? '' : 'disabled'}>Remove</button>
      <span class="msg"></span></div></div>`;
}
function secretRow(key) { return document.querySelector(`[data-secret="${key}"]`); }
function wireSecret(k) {
  const row = secretRow(k.key), i = row.querySelector('input'), tb = row.querySelector('[data-act=test]');
  const msg = (t, c) => { const m = row.querySelector('.msg'); m.textContent = t; m.className = 'msg ' + (c || ''); };
  const redraw = (j, text) => {
    S.secrets = j.secrets; applyState(j);
    const fresh = S.secrets.find(x => x.key === k.key);
    row.outerHTML = secretRowHtml(fresh); wireSecret(fresh);
    const m = secretRow(k.key).querySelector('.msg'); m.textContent = text; m.className = 'msg ok';
    reloadModels();
  };
  i.addEventListener('input', () => {
    row.querySelector('[data-act=save]').disabled = !i.value.trim();
    if (tb) tb.disabled = !i.value.trim() && !k.set;
  });
  i.addEventListener('keydown', e => { if (e.key === 'Enter' && i.value.trim()) row.querySelector('[data-act=save]').click(); });
  row.querySelector('[data-act=save]').onclick = async () => {
    try { redraw(await post('/api/secrets/save', {key: k.key, value: i.value}), 'Saved — live now'); }
    catch (e) { msg(e.message, 'err'); }
  };
  if (tb) tb.onclick = async () => {
    msg('Testing…');
    try { const j = await post('/api/secrets/test', {key: k.key, value: i.value.trim()});
      msg(j.message + (i.value.trim() ? ' (the key you typed — not saved yet)' : ''), 'ok'); }
    catch (e) { msg(e.message, 'err'); }
  };
  row.querySelector('[data-act=remove]').onclick = async () => {
    if (!confirm(`Remove the ${k.label} from .env?`)) return;
    try { redraw(await post('/api/secrets/save', {key: k.key, remove: true}), 'Removed'); }
    catch (e) { msg(e.message, 'err'); }
  };
}
function refreshBadges() {
  for (const s of S.settings) {
    const row = el(s.key);
    row.querySelector('.b-mod').style.display = s.overridden ? 'inline' : 'none';
    row.querySelector('[data-act=reset]').disabled = !s.overridden;
    row.querySelector('.b-pending').style.display = S.pending_restart.includes(s.key) ? 'inline' : 'none';
    if (s.type === 'lines') row.querySelector('[data-act=save]').disabled = input(s.key).value === s.value.join('\n');
  }
  for (const sec of document.querySelectorAll('section.grp'))
    sec.querySelector('[data-act=reset-group]').disabled =
      !S.settings.some(s => s.group === sec.dataset.group && s.overridden);
}

async function save(k) {
  const s = byKey[k], v = readInput(s), i = input(k);
  try {
    const j = await post('/api/settings/save', {changes: {[k]: v}});
    applyEntries(j);
    setMsg(k, s.apply === 'restart' ? 'Saved — applies after restart' : 'Saved — live now', 'ok');
  } catch (e) { i.classList.add('bad'); setMsg(k, e.message + ' — not saved', 'err'); }
}
async function reset(keys) {
  try {
    const j = await post('/api/settings/reset', {keys}); applyEntries(j);
    for (const k of keys) setMsg(k, 'Reset to default', 'ok');
  } catch (e) { setMsg(keys[0], e.message, 'err'); }
}
async function resetGroup(g) {
  if (!confirm(`Reset every ${g} setting (including advanced) to its default?`)) return;
  try {
    const j = await post('/api/settings/reset', {group: g}); applyEntries(j);
  } catch (e) { alert(e.message); }
}

function toggleAdvanced(initial) {
  const on = document.getElementById('show-adv').checked;
  if (initial !== true) store.set('settings.showAdvanced', on ? '1' : '0');
  for (const d of document.querySelectorAll('details.adv')) d.open = on;
  applyFilter();
}
function applyFilter() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  let any = false;
  for (const sec of document.querySelectorAll('section.grp')) {
    let shown = 0;
    for (const row of sec.querySelectorAll('.set')) {
      const s = row.dataset.key ? byKey[row.dataset.key] : S.secrets.find(x => x.key === row.dataset.secret);
      const card = row.closest('.card'), cardName = card ? S.cards.find(c => c.id === card.dataset.card).title : '';
      const hay = (s.label + ' ' + s.key + ' ' + s.help + ' ' + s.group + ' ' + cardName + (row.dataset.secret ? ' api key secret' : '')).toLowerCase();
      const hit = !q || hay.includes(q);
      row.style.display = hit ? '' : 'none';
      if (hit) shown++;
    }
    for (const card of sec.querySelectorAll('.card'))
      card.style.display = [...card.querySelectorAll('.set')].some(r => r.style.display !== 'none') ? '' : 'none';
    const adv = sec.querySelector('details.adv');
    if (adv) {
      const advHits = [...adv.querySelectorAll('.set')].filter(r => r.style.display !== 'none').length;
      adv.style.display = advHits ? '' : 'none';
      if (q && advHits) adv.open = true;
      else if (!q) adv.open = document.getElementById('show-adv').checked;
    }
    sec.style.display = shown ? '' : 'none';
    any = any || shown > 0;
  }
  document.getElementById('empty').style.display = any ? 'none' : 'block';
}

function updateBanner(st) {
  const b = document.getElementById('banner');
  const names = (st.pending_restart || []).map(k => byKey[k] ? byKey[k].label : k);
  b.style.display = names.length ? 'block' : 'none';
  if (!names.length) return;
  document.getElementById('banner-text').innerHTML =
    `<b>Restart needed</b> for: ${names.map(esc).join(', ')}.` +
    (st.can_restart ? ' He\'ll be offline for about 30 seconds.' : ' Not running as the systemd service — restart it by hand.');
  document.getElementById('restart-btn').style.display = st.can_restart ? '' : 'none';
}
async function restartNow() {
  if (!confirm('Restart the scarecrow now? He goes quiet for about 30 seconds.')) return;
  const btn = document.getElementById('restart-btn'), msg = document.getElementById('restart-msg');
  btn.disabled = true;
  try { await post('/api/settings/restart', {}); } catch (e) { msg.textContent = e.message; msg.className = 'msg err'; btn.disabled = false; return; }
  msg.textContent = 'Restarting…'; msg.className = 'msg';
  const started = S.started_at, t0 = Date.now();
  const poll = async () => {
    try {
      const r = await _origFetch('/api/settings');
      if (r.ok) { const j = await r.json(); if (j.started_at !== started) { location.reload(); return; } }
    } catch (e) {}
    if (Date.now() - t0 > 120000) { msg.textContent = 'Still not back after 2 minutes — check the Pi.'; msg.className = 'msg err'; return; }
    msg.textContent = `Restarting… ${Math.round((Date.now() - t0) / 1000)}s`;
    setTimeout(poll, 2000);
  };
  setTimeout(poll, 3000);
}

window.addEventListener('beforeunload', e => {
  if (S && (S.settings.some(s => s.type === 'lines' && el(s.key) && input(s.key).value !== s.value.join('\n'))
            || [...document.querySelectorAll('[data-secret] input')].some(i => i.value))) {
    e.preventDefault(); e.returnValue = '';
  }
});
load();
</script>
</body>
</html>
"""
