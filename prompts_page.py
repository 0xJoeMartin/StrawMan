"""
Prompts page for the dashboard (/prompts)
-----------------------------------------
View, edit and reset every prompt section and the offline fallback lines.
Edits apply on the scarecrow's very next line (prompts are composed per call).
The Test button generates a line from the current camera view using whatever
is in the editor — including unsaved edits — without speaking or logging it.

Storage + reset semantics live in prompt_store.py. Auth is app-wide
(dashboard_auth.py), so these routes need no extra checks.
"""

from flask import Blueprint, jsonify, render_template_string, request

import config

bp = Blueprint("prompts", __name__)

MAX_PROMPT_CHARS = 20000
MAX_FALLBACK_LINES = 60
MAX_FALLBACK_CHARS = 300


def _wd():
    import web_dashboard  # lazy: web_dashboard imports this module
    return web_dashboard


def _ai():
    import ai_brain
    return ai_brain


@bp.route("/prompts")
def prompts_page():
    return render_template_string(PROMPTS_HTML)


@bp.route("/api/prompts")
def api_prompts():
    ai = _ai()
    store = ai.PROMPT_STORE
    brain = _wd()._scarecrow_brain
    sections = []
    for meta in ai.PROMPT_SECTIONS:
        k = meta["key"]
        sections.append({**meta, "text": store.get(k), "default": store.default(k),
                         "overridden": store.is_overridden(k)})
    personas = list(ai.DEFAULT_PERSONA_PROMPTS.keys())
    fallbacks = {}
    for p in personas:
        fallbacks[p] = {
            "lines": {kind: store.fallback(p, kind) for kind in ("solo", "group", "reply")},
            "defaults": store.fallback_defaults(p),
            "overridden": store.fallback_overridden(p),
        }
    return jsonify(ok=True, sections=sections, personas=personas,
                   active_persona=brain.persona if brain else None,
                   backend=brain.backend if brain else None,
                   fallbacks=fallbacks)


def _section_meta(key):
    for m in _ai().PROMPT_SECTIONS:
        if m["key"] == key:
            return m
    return None


@bp.route("/api/prompts/save", methods=["POST"])
def api_prompts_save():
    data = request.get_json(silent=True) or {}
    key, text = data.get("key", ""), data.get("text", "")
    meta = _section_meta(key)
    if not meta:
        return jsonify(ok=False, error="Unknown section"), 400
    if not isinstance(text, str) or not text.strip():
        return jsonify(ok=False, error="Prompt can't be empty — use Reset to default instead."), 400
    if len(text) > MAX_PROMPT_CHARS:
        return jsonify(ok=False, error=f"Too long (max {MAX_PROMPT_CHARS} characters)."), 400
    missing = [ph for ph in meta.get("placeholders", []) if ph not in text]
    if missing:
        return jsonify(ok=False, error=f"Must contain {', '.join(missing)} — that's where "
                                       f"what the person said gets inserted."), 400
    store = _ai().PROMPT_STORE
    store.set(key, text)
    overridden = store.is_overridden(key)
    _wd().log_event("SYSTEM", f"Prompt '{meta['label']}' "
                              f"{'edited' if overridden else 'saved (matches default)'}")
    return jsonify(ok=True, overridden=overridden)


@bp.route("/api/prompts/reset", methods=["POST"])
def api_prompts_reset():
    data = request.get_json(silent=True) or {}
    store = _ai().PROMPT_STORE
    if data.get("all"):
        store.reset_all_prompts()
        _wd().log_event("SYSTEM", "All prompts reset to defaults")
        return jsonify(ok=True)
    meta = _section_meta(data.get("key", ""))
    if not meta:
        return jsonify(ok=False, error="Unknown section"), 400
    store.reset(meta["key"])
    _wd().log_event("SYSTEM", f"Prompt '{meta['label']}' reset to default")
    return jsonify(ok=True, text=store.get(meta["key"]))


@bp.route("/api/fallbacks/save", methods=["POST"])
def api_fallbacks_save():
    data = request.get_json(silent=True) or {}
    persona, lines = data.get("persona", ""), data.get("lines") or {}
    store = _ai().PROMPT_STORE
    if persona not in _ai().DEFAULT_PERSONA_PROMPTS:
        return jsonify(ok=False, error="Unknown persona"), 400
    for kind, ls in lines.items():
        if not isinstance(ls, list) or len(ls) > MAX_FALLBACK_LINES:
            return jsonify(ok=False, error=f"'{kind}': max {MAX_FALLBACK_LINES} lines"), 400
        if any(len(str(l)) > MAX_FALLBACK_CHARS for l in ls):
            return jsonify(ok=False, error=f"'{kind}': lines max {MAX_FALLBACK_CHARS} characters"), 400
    try:
        store.set_fallbacks(persona, lines)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    _wd().log_event("SYSTEM", f"Fallback lines for {persona} "
                              f"{'edited' if store.fallback_overridden(persona) else 'saved (match defaults)'}")
    return jsonify(ok=True, overridden=store.fallback_overridden(persona))


@bp.route("/api/fallbacks/reset", methods=["POST"])
def api_fallbacks_reset():
    data = request.get_json(silent=True) or {}
    store = _ai().PROMPT_STORE
    if data.get("all"):
        store.reset_fallbacks(None)
        _wd().log_event("SYSTEM", "All fallback lines reset to defaults")
        return jsonify(ok=True)
    persona = data.get("persona", "")
    if persona not in _ai().DEFAULT_PERSONA_PROMPTS:
        return jsonify(ok=False, error="Unknown persona"), 400
    store.reset_fallbacks(persona)
    _wd().log_event("SYSTEM", f"Fallback lines for {persona} reset to defaults")
    return jsonify(ok=True)


@bp.route("/api/prompts/test", methods=["POST"])
def api_prompts_test():
    """One line from the current camera view, using the editor's (possibly unsaved)
    text. Not spoken, not logged, not counted."""
    wd = _wd()
    brain, sc = wd._scarecrow_brain, wd._scarecrow_instance
    if not brain or not sc:
        return jsonify(ok=False, error="Scarecrow not running"), 503
    data = request.get_json(silent=True) or {}
    persona = data.get("persona") or brain.persona
    if persona not in _ai().DEFAULT_PERSONA_PROMPTS:
        return jsonify(ok=False, error="Unknown persona"), 400
    draft = {k: v for k, v in (data.get("draft") or {}).items()
             if isinstance(v, str) and v.strip() and _section_meta(k)}

    frame, dets, tracks, tracker = wd.get_latest_scene()
    if frame is None:
        return jsonify(ok=False, error="No camera frame yet"), 503
    if dets:
        scene = sc.detector.describe_scene(
            dets, tracks=tracks, tracker=tracker, radar=getattr(sc, "radar", None),
            head_still=sc.servos.head_still_for(config.RADAR_SETTLE_SECONDS, config.RADAR_SETTLE_DEGREES))
        num_people, pretend = len(dets), False
    else:
        scene = ("One person is standing at the bottom of the porch steps, "
                 "about two meters away, facing you.")
        num_people, pretend = 1, True
    try:
        result = brain.preview_response([sc.detector.frame_to_base64(frame)], scene,
                                        num_people, persona=persona, draft=draft)
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 502
    return jsonify(ok=True, persona=persona, scene=scene, pretend_visitor=pretend,
                   used_unsaved=sorted(draft.keys()), **result)


PROMPTS_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<title>Prompts · StrawMan</title>
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
 h2.group { display:flex; align-items:center; gap:10px; color:#e8e8e8; font-size:18px; font-weight:400; margin:22px 0 8px; }
 h2.group .i { color:#ff8800; }
 .panel { background:#1c1c1c; border-radius:12px; padding:14px 16px; margin-bottom:12px; box-shadow:0 2px 2px rgba(0,0,0,.35),0 1px 5px rgba(0,0,0,.3); }
 .panel.dirty { border-color:#a86000; }
 .row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
 .title { font-weight:600; color:#eee; }
 .help { color:#888; font-size:.8em; margin:4px 0 8px; line-height:1.4; }
 .badge { font-size:11px; font-weight:500; padding:2px 8px; border-radius:10px; }
 .b-mod { background:#3a2a00; color:#ffb340; }
 .b-unsaved { background:#4a1d00; color:#ff8a3d; }
 .b-active { background:#0f3320; color:#5ee08f; }
 textarea { width:100%; min-height:120px; background:#0d0d0d; color:#e8e8e8; border:1px solid #3a3a3a; border-radius:8px;
            padding:10px; font:13px/1.45 ui-monospace,Menlo,Consolas,monospace; resize:vertical; }
 textarea:focus, input:focus, select:focus { outline:none; border-color:#ff8800; }
 .actions { display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; align-items:center; }
 button { background:#262626; color:#ddd; border:1px solid #3a3a3a; border-radius:8px; padding:6px 12px; font-size:13px; font-weight:500; cursor:pointer;
          display:inline-flex; align-items:center; gap:6px; }
 button:hover:not(:disabled) { border-color:#ff8800; }
 button:disabled { opacity:.4; cursor:default; }
 button.primary { background:#ff8800; color:#111; border-color:#ff8800; }
 button.danger { color:#ff7070; border-color:#5a2a2a; }
 .msg { font-size:.8em; color:#888; }
 .msg.err { color:#ff6b6b; } .msg.ok { color:#5ee08f; }
 details.adv > summary { cursor:pointer; color:#e8e8e8; font-size:18px; margin:22px 0 8px; }
 details.adv .warn { color:#d9a24a; font-size:.8em; margin-bottom:10px; }
 /* test panel */
 .test select { background:#0d0d0d; color:#eee; border:1px solid #444; border-radius:5px; padding:6px; }
 .result { margin-top:10px; display:none; }
 .line { font-size:1.15em; color:#fff; background:#0d0d0d; border-left:3px solid #ff8800; padding:10px 12px; border-radius:4px; }
 .meta { color:#888; font-size:.78em; margin-top:6px; line-height:1.5; }
 /* fallback editor */
 .tabs { display:flex; gap:6px; margin-bottom:10px; flex-wrap:wrap; }
 .tabs button.on { border-color:#ff8800; color:#ff8800; }
 .fb-cols { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
 @media (max-width:760px) { .fb-cols { grid-template-columns:1fr; } }
 .fb-col h3 { font-size:.75em; color:#aaa; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; }
 .fb-line { display:flex; gap:4px; margin-bottom:4px; }
 .fb-line input { flex:1; min-width:0; background:#0d0d0d; color:#e8e8e8; border:1px solid #3a3a3a; border-radius:4px; padding:5px 7px; font-size:.82em; }
 .fb-line button { padding:2px 8px; }
 .danger-zone { border-color:#4a2222; }
</style>
</head>
<body>
<div class="wrap">
 <header>
  <h1><a href="/" title="Dashboard"><img class="logo" src="/static/strawman-logo.png" alt="StrawMan"></a>Prompts</h1>
  <nav><a href="/">{{ icon("view-dashboard-outline", 18) }}Dashboard</a><a href="/settings">{{ icon("cog", 18) }}Settings</a><a href="/logout">{{ icon("logout", 18) }}Log out</a></nav>
 </header>

 <div class="panel test">
  <div class="row"><span class="title">Test a line</span>
   <span class="help" style="margin:0">Uses the current camera view and what's in the boxes below — including unsaved edits. Nothing is spoken unless you press Speak.</span></div>
  <div class="actions">
   <label class="msg">Persona</label><select id="test-persona"></select>
   <button class="primary" id="test-btn" onclick="runTest()">Generate test line</button>
   <span class="msg" id="test-msg"></span>
  </div>
  <div class="result" id="test-result">
   <div class="line" id="test-line"></div>
   <div class="meta" id="test-meta"></div>
   <div class="actions"><button id="speak-btn" onclick="speakTest()">🔊 Speak it</button></div>
  </div>
 </div>

 <div id="sections"></div>

 <h2 class="group">{{ icon("robot-outline") }}Offline fallback lines</h2>
 <div class="panel" id="fb-panel">
  <div class="help">Canned lines used when the AI can't be reached. <b>Solo</b> = one visitor, <b>Group</b> = several, <b>Reply</b> = when the AI fails mid-conversation. Each list needs at least one line.</div>
  <div class="tabs" id="fb-tabs"></div>
  <div class="fb-cols" id="fb-cols"></div>
  <div class="actions">
   <button class="primary" id="fb-save" onclick="saveFallbacks()" disabled>Save</button>
   <button id="fb-revert" onclick="revertFallbacks()" disabled>Undo unsaved changes</button>
   <button class="danger" id="fb-reset" onclick="resetFallbacks()">Reset to defaults</button>
   <span class="badge b-mod" id="fb-mod" style="display:none">modified</span>
   <span class="msg" id="fb-msg"></span>
  </div>
 </div>

 <h2 class="group">{{ icon("restore") }}Start over</h2>
 <div class="panel danger-zone">
  <div class="actions" style="margin:0">
   <button class="danger" onclick="resetAll('prompts')">Reset ALL prompts</button>
   <button class="danger" onclick="resetAll('fallbacks')">Reset ALL fallback lines</button>
   <span class="msg">Defaults are built into the code and can always be restored.</span>
  </div>
 </div>
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
const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

let S = null;            // server state
const saved = {};        // key -> last saved text
let fbPersona = null, fbDraft = {};
let lastTestLine = '';

async function load() {
  const r = await fetch('/api/prompts'); S = await r.json();
  const sel = document.getElementById('test-persona');
  sel.innerHTML = S.personas.map(p => `<option value="${p}" ${p===S.active_persona?'selected':''}>${p}${p===S.active_persona?' (active)':''}</option>`).join('');
  renderSections(); fbPersona = fbPersona || S.active_persona || S.personas[0]; renderFallbacks();
}

const PROMPT_GROUP_ICONS = {Shared: 'format-list-bulleted', Personas: 'drama-masks', Scene: 'eye-outline'};
function renderSections() {
  const host = document.getElementById('sections'); host.innerHTML = '';
  let group = null, advBox = null;
  for (const s of S.sections) {
    saved[s.key] = s.text;
    if (s.group !== group) {
      group = s.group;
      if (group === 'Advanced') {
        const d = document.createElement('details'); d.className = 'adv';
        d.innerHTML = '<summary>Advanced</summary><div class="warn">These control output format and conversation turns. Small mistakes here make him ramble, add stage directions, or ignore what people say.</div>';
        host.appendChild(d); advBox = d;
      } else {
        const h = document.createElement('h2'); h.className = 'group'; h.innerHTML = mdi(PROMPT_GROUP_ICONS[group] || 'script-text-outline') + esc(group); host.appendChild(h); advBox = null;
      }
    }
    const isActive = s.key === 'persona.' + S.active_persona;
    const el = document.createElement('div'); el.className = 'panel'; el.id = 'sec-' + s.key;
    el.innerHTML = `
      <div class="row"><span class="title">${esc(s.label)}</span>
        ${isActive ? '<span class="badge b-active">active</span>' : ''}
        <span class="badge b-mod" style="display:${s.overridden?'inline':'none'}">modified</span>
        <span class="badge b-unsaved" style="display:none">unsaved</span></div>
      ${s.help ? `<div class="help">${esc(s.help)}</div>` : ''}
      <textarea spellcheck="false"></textarea>
      <div class="actions">
        <button class="primary" data-act="save" disabled>Save</button>
        <button data-act="revert" disabled>Undo unsaved changes</button>
        <button class="danger" data-act="reset" ${s.overridden?'':'disabled'}>Reset to default</button>
        <span class="msg"></span></div>`;
    const ta = el.querySelector('textarea'); ta.value = s.text; fit(ta);
    ta.addEventListener('input', () => { fit(ta); refresh(s.key); });
    el.querySelector('[data-act=save]').onclick = () => saveSection(s.key);
    el.querySelector('[data-act=revert]').onclick = () => { ta.value = saved[s.key]; fit(ta); refresh(s.key); };
    el.querySelector('[data-act=reset]').onclick = () => resetSection(s.key);
    (advBox || host).appendChild(el);
  }
}
function fit(ta) { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight + 4, 640) + 'px'; }
function secEl(k) { return document.getElementById('sec-' + k); }
function refresh(k) {
  const el = secEl(k), dirty = el.querySelector('textarea').value !== saved[k];
  el.classList.toggle('dirty', dirty);
  el.querySelector('.b-unsaved').style.display = dirty ? 'inline' : 'none';
  el.querySelector('[data-act=save]').disabled = !dirty;
  el.querySelector('[data-act=revert]').disabled = !dirty;
}
function setMsg(el, text, cls) { el.textContent = text; el.className = 'msg ' + (cls||''); if (cls==='ok') setTimeout(()=>{ if (el.textContent===text) el.textContent=''; }, 2500); }

async function saveSection(k) {
  const el = secEl(k), msg = el.querySelector('.msg'), text = el.querySelector('textarea').value;
  try {
    const j = await post('/api/prompts/save', {key:k, text});
    saved[k] = text; refresh(k);
    el.querySelector('.b-mod').style.display = j.overridden ? 'inline' : 'none';
    el.querySelector('[data-act=reset]').disabled = !j.overridden;
    setMsg(msg, j.overridden ? 'Saved — live on his next line' : 'Saved (same as default)', 'ok');
  } catch (e) { setMsg(msg, e.message, 'err'); }
}
async function resetSection(k) {
  const label = S.sections.find(s => s.key === k).label;
  if (!confirm(`Reset "${label}" to the default text? Your edits to this section will be lost.`)) return;
  const el = secEl(k), msg = el.querySelector('.msg');
  try {
    const j = await post('/api/prompts/reset', {key:k});
    const ta = el.querySelector('textarea'); ta.value = j.text; saved[k] = j.text; fit(ta); refresh(k);
    el.querySelector('.b-mod').style.display = 'none'; el.querySelector('[data-act=reset]').disabled = true;
    setMsg(msg, 'Reset to default', 'ok');
  } catch (e) { setMsg(msg, e.message, 'err'); }
}
async function resetAll(what) {
  const txt = what === 'prompts' ? 'Reset ALL prompt sections to their defaults?' : 'Reset ALL fallback lines (every persona) to their defaults?';
  if (!confirm(txt + ' This cannot be undone.')) return;
  try { await post(what === 'prompts' ? '/api/prompts/reset' : '/api/fallbacks/reset', {all:true}); fbDraft = {}; await load(); }
  catch (e) { alert(e.message); }
}

// --- test ---
async function runTest() {
  const btn = document.getElementById('test-btn'), msg = document.getElementById('test-msg');
  const draft = {};
  for (const s of S.sections) { const v = secEl(s.key).querySelector('textarea').value; if (v !== saved[s.key]) draft[s.key] = v; }
  btn.disabled = true; setMsg(msg, 'Thinking…');
  try {
    const j = await post('/api/prompts/test', {persona: document.getElementById('test-persona').value, draft});
    lastTestLine = j.line;
    document.getElementById('test-line').textContent = j.line;
    document.getElementById('test-meta').innerHTML =
      `${esc(j.persona)} · ${esc(j.backend)} · ${j.elapsed}s` +
      (j.used_unsaved.length ? ` · <b>using unsaved:</b> ${j.used_unsaved.map(esc).join(', ')}` : ' · saved prompts') +
      `<br>${j.pretend_visitor ? '<b>No one in view</b> — used a pretend visitor: ' : 'Scene: '}${esc(j.scene)}`;
    document.getElementById('test-result').style.display = 'block'; setMsg(msg, '');
  } catch (e) { setMsg(msg, e.message, 'err'); }
  btn.disabled = false;
}
async function speakTest() {
  if (!lastTestLine) return;
  try { await post('/api/speak', {text:lastTestLine}); setMsg(document.getElementById('test-msg'), 'Speaking…', 'ok'); }
  catch (e) { setMsg(document.getElementById('test-msg'), e.message, 'err'); }
}

// --- fallbacks ---
const KINDS = [['solo','Solo'],['group','Group'],['reply','Reply']];
function fbCurrent() { return fbDraft[fbPersona] || JSON.parse(JSON.stringify(S.fallbacks[fbPersona].lines)); }
function fbDirty(p) { return !!fbDraft[p] && JSON.stringify(fbDraft[p]) !== JSON.stringify(S.fallbacks[p].lines); }
function renderFallbacks() {
  document.getElementById('fb-tabs').innerHTML = S.personas.map(p =>
    `<button class="${p===fbPersona?'on':''}" onclick="fbPersona='${p}';renderFallbacks()">${p}${fbDirty(p)?' •':''}${S.fallbacks[p].overridden?' ✎':''}</button>`).join('');
  const cur = fbCurrent();
  document.getElementById('fb-cols').innerHTML = KINDS.map(([k, label]) => `
    <div class="fb-col"><h3>${label} (${cur[k].length})</h3>
      ${cur[k].map((l,i) => `<div class="fb-line"><input value="${esc(l)}" oninput="fbEdit('${k}',${i},this.value)"><button title="Delete" onclick="fbDel('${k}',${i})">✕</button></div>`).join('')}
      <button onclick="fbAdd('${k}')">+ Add line</button></div>`).join('');
  const dirty = fbDirty(fbPersona);
  document.getElementById('fb-save').disabled = !dirty;
  document.getElementById('fb-revert').disabled = !dirty;
  document.getElementById('fb-reset').disabled = !S.fallbacks[fbPersona].overridden;
  document.getElementById('fb-mod').style.display = S.fallbacks[fbPersona].overridden ? 'inline' : 'none';
  document.getElementById('fb-panel').classList.toggle('dirty', dirty);
}
function fbEnsure() { if (!fbDraft[fbPersona]) fbDraft[fbPersona] = fbCurrent(); return fbDraft[fbPersona]; }
function fbEdit(k, i, v) { fbEnsure()[k][i] = v; const d = fbDirty(fbPersona);
  document.getElementById('fb-save').disabled = !d; document.getElementById('fb-revert').disabled = !d; document.getElementById('fb-panel').classList.toggle('dirty', d); }
function fbDel(k, i) { fbEnsure()[k].splice(i, 1); renderFallbacks(); }
function fbAdd(k) { fbEnsure()[k].push(''); renderFallbacks();
  const inputs = document.querySelectorAll(`#fb-cols .fb-col:nth-child(${KINDS.findIndex(x=>x[0]===k)+1}) input`); inputs[inputs.length-1].focus(); }
function revertFallbacks() { delete fbDraft[fbPersona]; renderFallbacks(); }
async function saveFallbacks() {
  const msg = document.getElementById('fb-msg'), lines = {};
  for (const [k] of KINDS) lines[k] = fbEnsure()[k].map(s => s.trim()).filter(Boolean);
  try {
    const j = await post('/api/fallbacks/save', {persona: fbPersona, lines});
    S.fallbacks[fbPersona].lines = lines; S.fallbacks[fbPersona].overridden = j.overridden; delete fbDraft[fbPersona];
    renderFallbacks(); setMsg(msg, 'Saved', 'ok');
  } catch (e) { setMsg(msg, e.message, 'err'); }
}
async function resetFallbacks() {
  if (!confirm(`Reset the ${fbPersona} fallback lines to the defaults?`)) return;
  try { await post('/api/fallbacks/reset', {persona: fbPersona}); delete fbDraft[fbPersona];
    S.fallbacks[fbPersona].lines = JSON.parse(JSON.stringify(S.fallbacks[fbPersona].defaults)); S.fallbacks[fbPersona].overridden = false;
    renderFallbacks(); setMsg(document.getElementById('fb-msg'), 'Reset to defaults', 'ok');
  } catch (e) { setMsg(document.getElementById('fb-msg'), e.message, 'err'); }
}

window.addEventListener('beforeunload', e => {
  const dirtyPrompt = S && S.sections.some(s => secEl(s.key).querySelector('textarea').value !== saved[s.key]);
  const dirtyFb = S && S.personas.some(fbDirty);
  if (dirtyPrompt || dirtyFb) { e.preventDefault(); e.returnValue = ''; }
});
load();
</script>
</body>
</html>
"""
