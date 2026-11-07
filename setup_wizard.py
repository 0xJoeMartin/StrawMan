"""
First-run setup wizard (/welcome)
---------------------------------
Walks a new builder through the minimum he needs to work — an AI that can see,
a voice, a camera picture — plus the optional parts (hearing, head and jaw,
personality), testing each one as it goes. Re-runnable from Settings.

Until setup is finished (SETUP_DONE) he runs but doesn't start conversations,
and the dashboard sends you here. readiness.auto_mark_done() marks an install
that already works as done, so existing scarecrows never see this.

Saving and testing reuse the Settings page APIs (/api/settings/save,
/api/secrets/save, /api/provider/test, /api/tts/say, /api/llm/models,
/api/backend, /api/set_persona, /api/quiet_hours); the only new endpoints are
the status, a camera snapshot, a mic test and "done".
"""

import cv2
from flask import Blueprint, Response, jsonify, render_template_string, request

import config
import readiness

bp = Blueprint("setup_wizard", __name__)


def _wd():
    import web_dashboard
    return web_dashboard


@bp.route("/welcome")
def welcome_page():
    return render_template_string(WELCOME_HTML)


@bp.route("/api/setup/status")
def api_setup_status():
    frame, dets, tracks, tracker = _wd().get_latest_scene()
    return jsonify(ok=True, **readiness.status(), hardware=readiness.hardware(),
                   people_in_view=len(dets or []))


@bp.route("/api/setup/snapshot.jpg")
def api_setup_snapshot():
    """Latest camera frame, as he sees it (no overlays)."""
    frame = _wd().get_latest_scene()[0]
    if frame is None:
        return Response(status=404)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return Response(buf.tobytes(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


def list_cameras() -> list:
    """Cameras that can actually deliver a picture: V4L2 devices whose driver is
    a USB webcam (uvcvideo) with video-capture capability. That skips a USB
    camera's metadata twin and the Pi 5's built-in decoder / image-processor
    nodes. Read-only (VIDIOC_QUERYCAP), safe while a camera is in use."""
    import fcntl
    import glob
    import os
    import re
    import struct
    VIDIOC_QUERYCAP = 0x80685600          # _IOR('V', 0, struct v4l2_capability) (104 bytes)
    CAP_VIDEO_CAPTURE = 0x00000001
    cams = []
    for path in sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0)):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            buf = fcntl.ioctl(fd, VIDIOC_QUERYCAP, bytes(104))
        except OSError:
            continue
        finally:
            os.close(fd)
        driver, card, bus = (x.split(b"\0")[0].decode(errors="replace") for x in struct.unpack_from("16s32s32s", buf))
        device_caps = struct.unpack_from("I", buf, 88)[0]   # after version (80) and capabilities (84)
        if driver == "uvcvideo" and device_caps & CAP_VIDEO_CAPTURE:
            index = int(re.sub(r"\D", "", path))
            cams.append({"index": index, "path": path, "name": card.split(":")[0].strip() or card, "bus": bus})
    return cams


@bp.route("/api/setup/cameras")
def api_setup_cameras():
    return jsonify(ok=True, cameras=list_cameras(), current=config.CAMERA_INDEX)


@bp.route("/api/setup/mic-test", methods=["POST"])
def api_setup_mic_test():
    """Listen once through his own mic (up to LISTEN_DURATION) and transcribe."""
    sc = getattr(_wd(), "_scarecrow_instance", None)
    lst = getattr(sc, "listener", None) if sc else None
    if not lst:
        return jsonify(ok=False, error="Listening is off or there's no mic (it needs a restart after turning on)"), 409
    if sc.speech.is_speaking or not sc._interaction_lock.acquire(blocking=False):
        return jsonify(ok=False, error="He's busy talking to someone — try again in a moment"), 409
    try:
        text = lst.listen_for_response()
    finally:
        sc._interaction_lock.release()
    if not text:
        return jsonify(ok=False, error="He didn't hear any words. Speak up, closer to him, or check the mic "
                                       "threshold under Settings → Hearing → Advanced.")
    return jsonify(ok=True, text=text)


@bp.route("/api/setup/done", methods=["POST"])
def api_setup_done():
    data = request.get_json(silent=True) or {}
    changes = {}
    if "done" in data:
        changes["SETUP_DONE"] = bool(data["done"])
    if "servos_calibrated" in data:
        changes["SERVOS_CALIBRATED"] = bool(data["servos_calibrated"])
    if changes:
        config.SETTINGS.set_many(changes)
    if changes.get("SETUP_DONE"):
        _wd().log_event("SYSTEM", "Setup finished — he'll talk to visitors now")
    return jsonify(ok=True, **readiness.status())


WELCOME_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<title>Set up · StrawMan</title>
{{ ui_head() }}
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 * { box-sizing: border-box; margin: 0; padding: 0; }
 body { background:#111; color:#e0e0e0; font-size:14px; padding:12px; }
 .wrap { max-width:720px; margin:0 auto; }
 header { display:flex; align-items:center; gap:12px; padding:8px 0 14px; }
 header img { height:56px; }
 h1 { color:#ff8800; font-size:24px; font-weight:400; }
 header .sub { color:#888; font-size:13px; }
 .steps { display:flex; gap:4px; flex-wrap:wrap; margin-bottom:12px; }
 .steps a { display:flex; align-items:center; gap:4px; font-size:12px; padding:4px 10px; border-radius:12px; background:#1c1c1c;
            color:#888; text-decoration:none; border:1px solid #2a2a2a; }
 .steps a.on { border-color:#ff8800; color:#ffb35c; }
 .steps a.done { color:#5ee08f; }
 .panel { background:#1c1c1c; border-radius:12px; padding:16px 18px; margin-bottom:12px; box-shadow:0 2px 2px rgba(0,0,0,.35),0 1px 5px rgba(0,0,0,.3); }
 h2 { display:flex; align-items:center; gap:10px; font-size:20px; font-weight:400; color:#eee; margin-bottom:8px; }
 h2 .i { color:#ff8800; }
 p, .help { color:#9a9a9a; line-height:1.5; margin:6px 0; }
 .help { font-size:13px; }
 .tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:8px; margin:12px 0; }
 .tile { display:flex; flex-direction:column; align-items:center; gap:4px; padding:12px 8px; border:1px solid #333; border-radius:12px;
         background:#232323; color:#ccc; cursor:pointer; font:500 13px var(--ui-font); text-align:center; }
 .tile .i { color:#9a9a9a; }
 .tile small { color:#777; font-weight:400; font-size:11px; }
 .tile.on { border-color:#ff8800; background:rgba(255,136,0,.16); color:#ffb35c; }
 .tile.on .i { color:#ff8800; }
 .tile.dim { opacity:.55; }
 label.f { display:block; margin:12px 0 4px; color:#ccc; font-weight:500; }
 input, select { background:#141414; color:#e8e8e8; border:1px solid #3a3a3a; border-radius:8px; padding:8px 10px; font-size:14px; width:100%; max-width:440px; }
 input:focus, select:focus { outline:none; border-color:#ff8800; }
 button { background:#262626; color:#ddd; border:1px solid #3a3a3a; border-radius:8px; padding:8px 14px; font-size:14px; font-weight:500;
          cursor:pointer; display:inline-flex; align-items:center; gap:6px; }
 button:hover:not(:disabled) { border-color:#ff8800; }
 button:disabled { opacity:.4; cursor:default; }
 button.primary { background:#ff8800; color:#111; border-color:#ff8800; }
 .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:14px; }
 .nav { display:flex; justify-content:space-between; margin-top:8px; }
 .result { margin-top:10px; font-size:13px; min-height:1.2em; }
 .result.ok { color:#5ee08f; } .result.err { color:#ff6b6b; } .result.wait { color:#aaa; }
 .found { display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:8px; margin-top:10px; }
 .chk { display:flex; align-items:center; gap:10px; padding:8px 0; }
 .chk .dot { width:30px; height:30px; border-radius:50%; display:flex; align-items:center; justify-content:center; flex:none; }
 .s-ok .dot { background:rgba(94,224,143,.14); color:#5ee08f; }
 .s-warn .dot { background:rgba(255,170,0,.14); color:#ffaa00; }
 .s-missing .dot { background:rgba(255,107,107,.14); color:#ff6b6b; }
 .s-off .dot { background:#262626; color:#888; }
 .chk .t { font-weight:500; color:#ddd; } .chk .d { color:#888; font-size:12px; }
 .snap { width:100%; border-radius:12px; border:2px solid #333; background:#000; aspect-ratio:4/3; object-fit:cover; }
 .heard { font-size:18px; color:#fff; border-left:3px solid #ff8800; padding:8px 12px; margin-top:10px; background:#141414; border-radius:4px; }
 .hidden { display:none; }
</style>
</head>
<body>
<div class="wrap">
 <header><img src="/static/strawman-logo.png" alt="StrawMan">
  <div><h1>Let's get StrawMan talking</h1><div class="sub">A few minutes. Everything can be changed later in Settings.</div></div></header>
 <div class="steps" id="steps"></div>
 <div id="step"></div>
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
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const $ = id => document.getElementById(id);
const val = k => (S.settings.find(s => s.key === k) || {}).value;
const meta = k => S.settings.find(s => s.key === k) || {};
const keySet = k => (S.secrets.find(s => s.key === k) || {}).set;
function result(t, cls) { const r = $('result'); if (r) { r.textContent = t; r.className = 'result ' + (cls || ''); } }

let S = null, R = null, cur = 0;
const passed = {};           // step id -> true once tested OK this visit
const STEPS = [
  {id: 'hello', title: 'Welcome', icon: 'ghost-outline'},
  {id: 'ai', title: 'Brain', icon: 'brain'},
  {id: 'voice', title: 'Voice', icon: 'speaker'},
  {id: 'hearing', title: 'Hearing', icon: 'ear-hearing'},
  {id: 'camera', title: 'Eyes', icon: 'cctv'},
  {id: 'servos', title: 'Head & jaw', icon: 'axis-arrow'},
  {id: 'persona', title: 'Personality', icon: 'drama-masks'},
  {id: 'finish', title: 'Finish', icon: 'check-circle-outline'},
];

async function load() {
  S = await (await fetch('/api/settings')).json();
  R = await (await fetch('/api/setup/status')).json();
  try { S.persona = (await (await fetch('/api/health')).json()).persona; } catch (e) {}
}
function stepBar() {
  $('steps').innerHTML = STEPS.map((s, i) => `<a href="#${s.id}" class="${i === cur ? 'on' : ''} ${passed[s.id] ? 'done' : ''}">
    ${mdi(passed[s.id] ? 'check-circle-outline' : s.icon, 14)}${s.title}</a>`).join('');
}
function nav(nextLabel) {
  return `<div class="nav">${cur > 0 ? `<button onclick="go(${cur - 1})">Back</button>` : '<span></span>'}
    <button class="primary" onclick="go(${cur + 1})">${nextLabel || 'Next'}</button></div>`;
}
async function go(i) {
  cur = Math.max(0, Math.min(STEPS.length - 1, i));
  history.replaceState(null, '', '#' + STEPS[cur].id);
  await load(); stepBar();
  $('step').innerHTML = `<div class="panel">${RENDER[STEPS[cur].id]()}<div class="result" id="result"></div></div>${nav(cur === STEPS.length - 1 ? null : undefined)}`;
  if (cur === STEPS.length - 1) $('step').querySelector('.nav').innerHTML = `<button onclick="go(${cur - 1})">Back</button>`;
  (AFTER[STEPS[cur].id] || (() => {}))();
  window.scrollTo(0, 0);
}
function tiles(options, chosen, onpick) {
  return `<div class="tiles">${options.map(o => `<button class="tile ${o.id === chosen ? 'on' : ''} ${o.why ? 'dim' : ''}"
      onclick="${onpick}('${o.id}')">${mdi(o.icon, 24)}${esc(o.label)}${o.why ? `<small>${esc(o.why)}</small>` : (o.note ? `<small>${esc(o.note)}</small>` : '')}</button>`).join('')}</div>`;
}
const keyField = (k, label, help) => `<label class="f">${label}</label>
  <input type="password" id="k-${k}" autocomplete="new-password" placeholder="${keySet(k) ? 'Saved ✓ — paste a new one to replace it' : 'Paste key'}">
  ${help ? `<div class="help">${help}</div>` : ''}`;
async function saveKeyIfTyped(k) {
  const i = $('k-' + k); if (i && i.value.trim()) { await post('/api/secrets/save', {key: k, value: i.value.trim()}); i.value = ''; }
}
async function modelSelect(id, backend, current) {
  const sel = $(id); if (!sel) return;
  try {
    const j = await post('/api/llm/models', {backend});
    const ok = j.models.filter(m => m.vision !== false && !m.thinking);
    const list = ok.length ? ok : j.models;
    sel.innerHTML = list.map(m => `<option ${m.id === current || m.id === current + ':latest' ? 'selected' : ''} value="${esc(m.id)}">${esc(m.id)}${m.label && m.label !== m.id ? ' · ' + esc(m.label) : ''}</option>`).join('');
    if (![...sel.options].some(o => o.selected)) sel.insertAdjacentHTML('afterbegin', `<option selected value="${esc(current)}">${esc(current)}</option>`);
  } catch (e) {
    sel.outerHTML = `<input id="${id}" value="${esc(current)}"><div class="help">Couldn't list models yet (${esc(e.message)}) — type a name, or save the key/URL and try again.</div>`;
  }
}

// ---------------- steps ----------------
const RENDER = {}, AFTER = {};

RENDER.hello = () => {
  const h = R.hardware || {};
  const f = (ok, icon, label, detail) => `<div class="chk ${ok === true ? 's-ok' : ok === 'warn' ? 's-warn' : 's-off'}"><span class="dot">${mdi(icon, 18)}</span>
     <div><div class="t">${label}</div><div class="d">${detail}</div></div></div>`;
  if (!h.running) return `<h2>${mdi('ghost-outline')}Welcome</h2><p>The scarecrow program isn't running yet, so nothing can be checked. Start it on the Pi, then reload this page.</p>`;
  return `<h2>${mdi('ghost-outline')}Welcome</h2>
    <p>This walks you through what he needs to work, testing each part as you go. Until you finish, he'll run but won't start conversations.</p>
    <p class="help">Here's what he found when he started:</p>
    <div class="found">
      ${f(h.camera, 'cctv', 'Camera', h.camera ? 'Picture OK' : 'No picture')}
      ${f(h.detector === 'hailo' ? true : h.detector === 'cpu' ? 'warn' : false, 'eye-outline', 'Person detector',
          h.detector === 'hailo' ? 'Hailo accelerator' : h.detector === 'cpu' ? 'CPU (works, a bit choppy)' : 'None installed')}
      ${f(h.servo_board, 'axis-arrow', 'Servo board', h.servo_board ? 'Found' : 'Not found — head and jaw won\'t move')}
      ${f(!!h.mic, 'microphone', 'Microphone', h.mic ? esc(h.mic) : (h.listening ? 'Default mic' : 'None / listening off'))}
      ${f(h.radar, 'radar', 'Radar (optional)', h.radar ? 'Found' : 'Not found')}
    </div>`;
};

// --- AI ---
let aiPick = null;
const AI_OPTS = [
  {id: 'claude', label: 'Claude', icon: 'cloud-outline', note: 'Best lines. Needs an Anthropic key.'},
  {id: 'openai', label: 'OpenAI & co.', icon: 'api', note: 'OpenAI, OpenRouter, Groq, Gemini…'},
  {id: 'ollama', label: 'Ollama', icon: 'server-network', note: 'Free, on your own computer'},
];
RENDER.ai = () => {
  aiPick = aiPick || S.in_use.AI || 'claude';
  let fields = '';
  if (aiPick === 'claude') fields = keyField('ANTHROPIC_API_KEY', 'Anthropic API key', 'console.anthropic.com → API keys.') +
    `<label class="f">Model</label><select id="m-ai"><option>${esc(val('AI_MODEL'))}</option></select>`;
  if (aiPick === 'ollama') fields = `<label class="f">Ollama server URL</label><input id="u-ai" value="${esc(val('OLLAMA_BASE_URL'))}">
    <div class="help">The computer running Ollama, e.g. http://192.168.1.50:11434</div>
    <label class="f">Model</label><select id="m-ai"><option>${esc(val('OLLAMA_MODEL'))}</option></select>`;
  if (aiPick === 'openai') fields = `<label class="f">Service</label><select id="preset">${(meta('OPENAI_COMPAT_BASE_URL').presets || [])
      .map((p, i) => `<option value="${i}" ${p.base_url === val('OPENAI_COMPAT_BASE_URL') ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}
      <option value="custom">Other (type the URL)</option></select>
    <label class="f">URL</label><input id="u-ai" value="${esc(val('OPENAI_COMPAT_BASE_URL'))}">
    ${keyField('OPENAI_COMPAT_API_KEY', 'API key', 'For OpenAI itself you can use the OpenAI key below instead.')}
    ${keyField('OPENAI_API_KEY', 'OpenAI key (also used for the OpenAI voice and hearing)', '')}
    <label class="f">Model</label><select id="m-ai"><option>${esc(val('OPENAI_COMPAT_MODEL'))}</option></select>`;
  return `<h2>${mdi('brain')}Brain</h2><p>He looks at a photo of each visitor and an AI writes what he says. Pick where the AI runs.</p>
    ${tiles(AI_OPTS, aiPick, 'pickAI')}${fields}
    <div class="row"><button class="primary" onclick="saveAI()">${mdi('test-tube', 18)}Save and test</button>
    <span class="help">Sends one small picture to check it can see.</span></div>`;
};
AFTER.ai = () => {
  const map = {claude: 'AI_MODEL', ollama: 'OLLAMA_MODEL', openai: 'OPENAI_COMPAT_MODEL'};
  modelSelect('m-ai', aiPick, val(map[aiPick]));
  const p = $('preset');
  if (p) p.onchange = () => { const pr = meta('OPENAI_COMPAT_BASE_URL').presets[p.value]; if (pr) { $('u-ai').value = pr.base_url; } };
};
function pickAI(id) { aiPick = id; go(cur); }
async function saveAI() {
  result('Saving…', 'wait');
  try {
    const changes = {};
    if (aiPick === 'claude') { await saveKeyIfTyped('ANTHROPIC_API_KEY'); changes.AI_MODEL = $('m-ai').value; }
    if (aiPick === 'ollama') { changes.OLLAMA_BASE_URL = $('u-ai').value.trim(); changes.OLLAMA_MODEL = $('m-ai').value; }
    if (aiPick === 'openai') {
      await saveKeyIfTyped('OPENAI_COMPAT_API_KEY'); await saveKeyIfTyped('OPENAI_API_KEY');
      changes.OPENAI_COMPAT_BASE_URL = $('u-ai').value.trim();
      const p = $('preset'), pr = p && meta('OPENAI_COMPAT_BASE_URL').presets[p.value];
      changes.OPENAI_COMPAT_MODEL = $('m-ai').value || (pr && pr.model) || val('OPENAI_COMPAT_MODEL');
    }
    if (Object.values(changes).some(v => v)) await post('/api/settings/save', {changes});
    await post('/api/backend', {backend: aiPick});
    result('Sending a test picture…', 'wait');
    const j = await post('/api/provider/test', {card: aiPick});
    passed.ai = true; stepBar(); result('✓ ' + j.message, 'ok');
  } catch (e) { result(e.message, 'err'); }
}

// --- Voice ---
let voicePick = null;
const VOICE_ICONS = {'tts-elevenlabs': 'cloud-outline', 'tts-openai': 'cloud-outline', 'tts-piper': 'memory', 'tts-espeak': 'memory'};
RENDER.voice = () => {
  const cards = S.cards.filter(c => c.kind === 'tts');
  voicePick = voicePick || S.in_use.Voice || 'tts-elevenlabs';
  const opts = cards.map(c => ({id: c.id, label: c.title, icon: VOICE_ICONS[c.id], why: c.id === voicePick ? '' : c.unavailable, note: c.help}));
  let fields = '';
  if (voicePick === 'tts-elevenlabs') fields = keyField('ELEVENLABS_API_KEY', 'ElevenLabs API key', '') +
    `<label class="f">Voice ID</label><input id="v-id" value="${esc(val('ELEVENLABS_VOICE_ID'))}"><div class="help">From your ElevenLabs voice library.</div>`;
  if (voicePick === 'tts-openai') fields = (keySet('OPENAI_API_KEY') ? '' : keyField('OPENAI_API_KEY', 'OpenAI API key', '')) +
    `<label class="f">Voice</label><select id="v-oa">${meta('OPENAI_TTS_VOICE').choices.map(v => `<option ${v === val('OPENAI_TTS_VOICE') ? 'selected' : ''}>${v}</option>`).join('')}</select>`;
  if (voicePick === 'tts-piper') fields = `<label class="f">Piper voice model</label><input id="v-piper" value="${esc(val('PIPER_MODEL'))}">`;
  const card = cards.find(c => c.id === voicePick);
  const offline = cards.some(c => (c.id === 'tts-piper' || c.id === 'tts-espeak') && !c.unavailable);
  return `<h2>${mdi('speaker')}Voice</h2><p>Pick his main voice. If it ever fails, he uses the next one that works (order in Settings → Voice).</p>
    ${tiles(opts, voicePick, 'pickVoice')}${fields}
    ${card && card.unavailable && !['tts-elevenlabs', 'tts-openai'].includes(card.id) ? `<p class="help">${esc(card.unavailable)}</p>` : ''}
    <div class="row"><button class="primary" onclick="saveVoice()">${mdi('volume-high', 18)}Save and say hello</button>
      <span class="help">He'll speak through his speaker.</span></div>
    <div class="row hidden" id="heard-q"><span>Did you hear him?</span>
      <button onclick="voiceHeard(true)">Yes</button><button onclick="voiceHeard(false)">No</button></div>
    ${offline ? '' : '<p class="help" style="margin-top:14px">Tip: none of his voices work without internet. <code>sudo apt install espeak-ng</code> on the Pi adds a robotic backup voice.</p>'}`;
};
function pickVoice(id) { voicePick = id; go(cur); }
async function saveVoice() {
  result('Saving…', 'wait');
  try {
    const changes = {};
    if (voicePick === 'tts-elevenlabs') { await saveKeyIfTyped('ELEVENLABS_API_KEY'); changes.ELEVENLABS_VOICE_ID = $('v-id').value.trim(); }
    if (voicePick === 'tts-openai') { await saveKeyIfTyped('OPENAI_API_KEY'); changes.OPENAI_TTS_VOICE = $('v-oa').value; }
    if (voicePick === 'tts-piper') changes.PIPER_MODEL = $('v-piper').value.trim();
    const name = voicePick.slice(4), order = val('TTS_ORDER');
    changes.TTS_ORDER = [name].concat(order.filter(n => n !== name));
    await post('/api/settings/save', {changes});
    await post('/api/tts/say', {card: voicePick});
    result('Speaking…', 'wait'); $('heard-q').classList.remove('hidden');
  } catch (e) { result(e.message, 'err'); }
}
function voiceHeard(yes) {
  if (yes) { passed.voice = true; stepBar(); result('✓ Voice works.', 'ok'); }
  else result('Check the speaker is plugged in and the volume is up (alsamixer on the Pi), then try again.', 'err');
  $('heard-q').classList.add('hidden');
}

// --- Hearing ---
let hearPick = null;
const HEAR_ICONS = {'stt-faster-whisper': 'server-network', 'stt-openai': 'cloud-outline', 'stt-local': 'memory'};
RENDER.hearing = () => {
  const cards = S.cards.filter(c => c.kind === 'stt');
  hearPick = hearPick || S.in_use.Hearing || 'stt-openai';
  const on = val('LISTEN_ENABLED');
  let fields = '';
  if (hearPick === 'stt-faster-whisper') fields = `<label class="f">Server URL</label><input id="h-url" value="${esc(val('WHISPER_SERVER_URL'))}">
    <div class="help">Your own faster-whisper server, e.g. http://192.168.1.50:8178</div>`;
  if (hearPick === 'stt-openai') fields = keySet('OPENAI_API_KEY') ? '<p class="help">Uses your OpenAI key ✓</p>' : keyField('OPENAI_API_KEY', 'OpenAI API key', '');
  if (hearPick === 'stt-local') fields = '<p class="help">Runs on the Pi with no internet. Slower (a few seconds per answer).</p>';
  return `<h2>${mdi('ear-hearing')}Hearing</h2><p>He can listen for answers and talk back. This part is optional.</p>
    <label class="chk"><input type="checkbox" id="listen" ${on ? 'checked' : ''} style="width:auto" onchange="toggleListen(this.checked)">
      <span>Listen for answers ${R.hardware && !R.hardware.listening && on ? '<span class="help">(needs a restart to start listening)</span>' : ''}</span></label>
    ${on ? tiles(cards.map(c => ({id: c.id, label: c.title, icon: HEAR_ICONS[c.id], why: c.id === hearPick ? '' : c.unavailable, note: c.help})), hearPick, 'pickHear') + fields + `
    <div class="row"><button onclick="saveHear(false)">${mdi('test-tube', 18)}Save and test with a recording</button>
      <button class="primary" onclick="saveHear(true)" ${R.hardware && R.hardware.listening ? '' : 'disabled'}>${mdi('microphone', 18)}Talk to him</button></div>
    <div id="heard"></div>` : '<p class="help">He\'ll say his line and move on.</p>'}`;
};
function pickHear(id) { hearPick = id; go(cur); }
async function toggleListen(on) {
  try { await post('/api/settings/save', {changes: {LISTEN_ENABLED: on}}); if (!on) passed.hearing = true; go(cur); }
  catch (e) { result(e.message, 'err'); }
}
async function saveHear(talk) {
  result('Saving…', 'wait');
  try {
    const changes = {};
    if (hearPick === 'stt-faster-whisper') changes.WHISPER_SERVER_URL = $('h-url').value.trim();
    if (hearPick === 'stt-openai') await saveKeyIfTyped('OPENAI_API_KEY');
    const name = hearPick.slice(4), order = val('STT_ORDER');
    changes.STT_ORDER = [name].concat(order.filter(n => n !== name));
    await post('/api/settings/save', {changes});
    if (!talk) {
      result('Transcribing a test phrase…', 'wait');
      const j = await post('/api/provider/test', {card: hearPick});
      passed.hearing = true; stepBar(); result('✓ ' + j.message, 'ok'); return;
    }
    result('Listening — say something to him now (up to 6 seconds)…', 'wait');
    const j = await post('/api/setup/mic-test', {});
    $('heard').innerHTML = `<div class="heard">“${esc(j.text)}”</div>`;
    passed.hearing = true; stepBar(); result('✓ That\'s what he heard through his own mic.', 'ok');
  } catch (e) { result(e.message, 'err'); }
}

// --- Camera ---
let snapTimer = null;
RENDER.camera = () => `<h2>${mdi('cctv')}Eyes</h2>
  <p>This is what he sees right now. Stand where visitors will, and check he notices you.</p>
  <img class="snap" id="snap" alt="Camera">
  <div class="row"><span id="people" class="help"></span></div>
  <label class="f">Camera</label><select id="cam" onchange="setCam(this.value)"><option>Looking for cameras…</option></select>
  <div class="help" id="cam-help">Wrong camera or a black picture? Pick another and restart him (Settings shows a Restart button).</div>`;
async function listCameras() {
  const cur = val('CAMERA_INDEX'), sel = $('cam');
  try {
    const j = await (await fetch('/api/setup/cameras')).json();
    const cams = j.cameras.slice();
    if (!cams.some(c => c.index === cur)) cams.unshift({index: cur, path: '/dev/video' + cur, name: 'not detected as a camera'});
    sel.innerHTML = cams.map(c => `<option value="${c.index}" ${c.index === cur ? 'selected' : ''}>${esc(c.name)} (${esc(c.path)})${c.index === cur ? ' — current' : ''}</option>`).join('');
    if (j.cameras.length === 1 && j.cameras[0].index === cur) $('cam-help').textContent = 'That\'s the only camera connected.';
  } catch (e) { sel.innerHTML = `<option value="${cur}">/dev/video${cur} — current</option>`; }
}
AFTER.camera = () => {
  listCameras();
  clearInterval(snapTimer);
  const tick = async () => {
    if (STEPS[cur].id !== 'camera') { clearInterval(snapTimer); return; }
    $('snap').src = '/api/setup/snapshot.jpg?t=' + Date.now();
    try {
      const j = await (await fetch('/api/setup/status')).json();
      const n = j.people_in_view;
      $('people').innerHTML = n ? `${mdi('account', 16)} He can see <b>${n}</b> ${n === 1 ? 'person' : 'people'} ✓` : 'Nobody in view — step in front of him.';
      if (n) { passed.camera = true; stepBar(); }
    } catch (e) {}
  };
  tick(); snapTimer = setInterval(tick, 1500);
};
async function setCam(i) {
  try { await post('/api/settings/save', {changes: {CAMERA_INDEX: parseInt(i)}}); result('Saved — restart him to switch cameras.', 'ok'); }
  catch (e) { result(e.message, 'err'); }
}

// --- Servos ---
RENDER.servos = () => {
  const board = R.hardware && R.hardware.servo_board;
  if (!board) return `<h2>${mdi('axis-arrow')}Head & jaw</h2><p>No servo board was found, so his head and jaw won't move. Everything else still works.</p>
    <p class="help">If you have one, check its wiring and that I²C is turned on (sudo raspi-config → Interface Options), then restart.</p>`;
  return `<h2>${mdi('axis-arrow')}Head & jaw</h2>
    <div class="panel" style="background:#181818;box-shadow:none;border:1px solid #2e2e2e;margin:10px 0">
      <div class="t" style="font-weight:500;color:#ddd">Assembling him? Do this first</div>
      <p class="help">Put every servo at the middle of its travel <b>before</b> fitting the servo horns and linkages,
        then attach them so his head faces straight ahead, roughly level, with the jaw about half open.
        Skip this if he's already built.</p>
      <div class="row" style="margin-top:8px"><button onclick="assembly(true)">${mdi('axis-arrow', 18)}Put servos in assembly position (90°)</button>
        <button onclick="assembly(false)" id="asm-done" class="hidden">${mdi('check-circle-outline', 18)}Done assembling</button></div>
    </div>
    <p>Then tell him how far his head can turn and tilt, and how far his jaw opens — without straining the servos.</p>
    <div class="row"><a href="/calibrate" target="_blank"><button class="primary">${mdi('gamepad-variant-outline', 18)}Open calibration</button></a>
      <button onclick="servosDone()">${mdi('check-circle-outline', 18)}I've calibrated them</button></div>
    <p class="help">Calibration opens in a new tab. Come back here when you've pressed Done there.</p>`;
};
async function assembly(start) {
  try {
    if (start) {
      if (!confirm('This moves every servo to 90° — the middle of its travel. Only for assembling: if his head and ' +
                   'jaw are already attached, it can push them past their limits.\n\nMove all servos to 90°?')) return;
      await post('/api/calibrate/control', {on: true});
      await post('/api/calibrate/pose', {pose: 'assembly'});
      $('asm-done').classList.remove('hidden');
      result('All servos at 90°. Fit the horns and linkages now, then press Done assembling.', 'ok');
    } else {
      await post('/api/calibrate/control', {on: false});
      $('asm-done').classList.add('hidden');
      result('Control handed back. Next: calibrate his limits.', 'ok');
    }
  } catch (e) { result(e.message, 'err'); }
}
async function servosDone() {
  try { await post('/api/setup/done', {servos_calibrated: true}); passed.servos = true; stepBar(); result('✓ Noted.', 'ok'); }
  catch (e) { result(e.message, 'err'); }
}

// --- Personality ---
const PERSONAS = [
  {id: 'friendly', label: 'Friendly', icon: 'emoticon-happy-outline', note: 'Warm, for little kids'},
  {id: 'snarky', label: 'Snarky', icon: 'emoticon-wink-outline', note: 'Teasing, funny'},
  {id: 'creepy', label: 'Creepy', icon: 'ghost-outline', note: 'Unsettling, clever'},
  {id: 'menacing', label: 'Menacing', icon: 'skull-outline', note: 'Quiet and threatening'},
];
RENDER.persona = () => {
  const hours = Array.from({length: 24}, (_, h) => `<option value="${h}">${h === 0 ? '12am' : h < 12 ? h + 'am' : h === 12 ? '12pm' : (h - 12) + 'pm'}</option>`).join('');
  return `<h2>${mdi('drama-masks')}Personality</h2><p>How should he come across? You can change this any time from the dashboard.</p>
    ${tiles(PERSONAS, S.persona || 'creepy', 'pickPersona')}
    <label class="chk"><input type="checkbox" id="qh" style="width:auto" ${val('QUIET_HOURS_ENABLED') ? 'checked' : ''}>
      <span>Quiet hours — whisper late at night</span></label>
    <div class="row"><select id="qs" style="width:auto">${hours}</select> to <select id="qe" style="width:auto">${hours}</select>
      <button onclick="saveQuiet()">Save quiet hours</button></div>`;
};
AFTER.persona = () => { $('qs').value = val('QUIET_HOURS_START'); $('qe').value = val('QUIET_HOURS_END'); };
async function pickPersona(p) {
  try { await post('/api/set_persona', {persona: p}); passed.persona = true; go(cur); } catch (e) { result(e.message, 'err'); }
}
async function saveQuiet() {
  try { await post('/api/quiet_hours', {enabled: $('qh').checked, start: parseInt($('qs').value), end: parseInt($('qe').value)});
    passed.persona = true; stepBar(); result('✓ Saved.', 'ok'); } catch (e) { result(e.message, 'err'); }
}

// --- Finish ---
const STATUS_ICON = {ok: 'check-circle-outline', warn: 'information-outline', missing: 'stop', off: 'sleep'};
RENDER.finish = () => `<h2>${mdi('check-circle-outline')}Ready?</h2>
  <p>${R.core_ok ? 'He has everything he needs.' : 'Some essentials still need setting up — he can\'t work without them.'}</p>
  ${R.items.map(i => `<div class="chk s-${i.status}"><span class="dot">${mdi(STATUS_ICON[i.status] || 'information-outline', 18)}</span>
     <div><div class="t">${esc(i.label)}</div><div class="d">${esc(i.detail)}</div></div></div>`).join('')}
  <div class="row"><button class="primary" onclick="finish()">${mdi('ghost-outline', 18)}Start StrawMan</button></div>`;
async function finish() {
  if (!R.core_ok && !confirm('Some essentials are missing, so he may not be able to talk. Finish anyway?')) return;
  try { await post('/api/setup/done', {done: true}); location.href = '/'; } catch (e) { result(e.message, 'err'); }
}

window.addEventListener('hashchange', () => {
  const i = STEPS.findIndex(s => '#' + s.id === location.hash);
  if (i >= 0 && i !== cur) go(i);
});
(async () => {
  const i = STEPS.findIndex(s => '#' + s.id === location.hash);
  go(i >= 0 ? i : 0);
})();
</script>
</body>
</html>
"""
