"""
Servo calibration page for the dashboard (/calibrate)
-----------------------------------------------------
Jog pan, tilt and jaw one at a time, save the current position as a limit,
and run a slow test sweep through the saved limits.

  - Needs manual mode ("Take control"), which also stops him following
    people, so the head only moves when you move it.
  - Moves glide at servo_control.CALIBRATION_SPEED and may go past the saved
    limits (that's the point) but never outside 0–180°.
  - Saving goes through the settings store, so the usual rules apply
    (min < center < max, drooped between tilt min and max) and the new limits
    are live immediately.
"""

import threading
import time

from flask import Blueprint, jsonify, render_template_string, request

import config

bp = Blueprint("calibrate", __name__)

# axis -> [(setting key, button label)]; pan min = furthest right, tilt min = looking up
AXES = {
    "pan": [("PAN_MIN", "Far right"), ("PAN_CENTER", "Center"), ("PAN_MAX", "Far left")],
    "tilt": [("TILT_MIN", "Up"), ("TILT_CENTER", "Level"), ("TILT_MAX", "Down"), ("TILT_DROOPED", "Drooped")],
    "jaw": [("JAW_CLOSED", "Closed"), ("JAW_OPEN", "Open")],
}
_sweep_lock = threading.Lock()

# Forgotten tab safety: hand control back after this long with no calibration activity,
# so he doesn't sit in manual mode ignoring visitors
IDLE_RELEASE_SECONDS = 600
_last_activity = 0.0
_watchdog = None


def _touch():
    global _last_activity
    _last_activity = time.time()


def _release(servos, sc, reason):
    sc._manual_mode = False
    servos.calibrating = False
    servos.set_target(config.PAN_CENTER, config.TILT_CENTER)
    if not sc.speech.is_speaking:
        servos.calibrate_move("jaw", config.JAW_CLOSED)
    _wd().log_event("SYSTEM", reason)


def _watch():
    while True:
        time.sleep(5)
        servos, sc = _parts()
        if not servos or not servos.calibrating:
            return
        if not sc._manual_mode:          # turned off elsewhere (dashboard joystick)
            servos.calibrating = False
            return
        if time.time() - _last_activity > IDLE_RELEASE_SECONDS and not _sweep_lock.locked():
            _release(servos, sc, f"Servo calibration idle for {IDLE_RELEASE_SECONDS // 60} min — control handed back")
            return


def _wd():
    import web_dashboard
    return web_dashboard


def _parts():
    wd = _wd()
    return getattr(wd, "_servo_controller", None), getattr(wd, "_scarecrow_instance", None)


def _position(servos, axis):
    return {"pan": servos.pan_angle, "tilt": servos.tilt_angle, "jaw": servos.jaw_angle}[axis]


def _state():
    servos, sc = _parts()
    if not servos or not sc:
        return {"connected": False}
    return {
        "connected": True,
        "control": bool(sc._manual_mode),
        "sweeping": _sweep_lock.locked(),
        "idle_release_seconds": IDLE_RELEASE_SECONDS,
        "speaking": bool(getattr(sc, "speech", None) and sc.speech.is_speaking),
        "axes": {axis: {"angle": round(_position(servos, axis), 1),
                        "target": round({"pan": servos.target_pan, "tilt": servos.target_tilt,
                                         "jaw": servos.jaw_angle}[axis], 1),
                        "limits": [{"key": k, "label": label, "value": getattr(config, k),
                                    "default": config.SETTINGS.default(k)} for k, label in AXES[axis]]}
                 for axis in AXES},
    }


def _need_control():
    servos, sc = _parts()
    if not servos or not sc:
        return None, None, (jsonify(ok=False, error="Servos not connected"), 503)
    if not sc._manual_mode:
        return None, None, (jsonify(ok=False, error="Take control first"), 409)
    if _sweep_lock.locked():
        return None, None, (jsonify(ok=False, error="A test sweep is running"), 409)
    return servos, sc, None


@bp.route("/calibrate")
def calibrate_page():
    return render_template_string(CALIBRATE_HTML)


@bp.route("/api/calibrate")
def api_calibrate():
    return jsonify(ok=True, **_state())


@bp.route("/api/calibrate/control", methods=["POST"])
def api_calibrate_control():
    """Take control (manual mode + slow moves) or hand it back (and recenter)."""
    servos, sc = _parts()
    if not servos or not sc:
        return jsonify(ok=False, error="Servos not connected"), 503
    global _watchdog
    on = bool((request.get_json(silent=True) or {}).get("on"))
    if on:
        sc._manual_mode = True
        servos.calibrating = True
        _touch()
        if not (_watchdog and _watchdog.is_alive()):
            _watchdog = threading.Thread(target=_watch, daemon=True)
            _watchdog.start()
        _wd().log_event("SYSTEM", "Servo calibration started")
    else:
        _release(servos, sc, "Servo calibration finished")
    return jsonify(ok=True, **_state())


@bp.route("/api/calibrate/move", methods=["POST"])
def api_calibrate_move():
    servos, sc, err = _need_control()
    if err:
        return err
    _touch()
    data = request.get_json(silent=True) or {}
    axis = data.get("axis")
    if axis not in AXES:
        return jsonify(ok=False, error="Unknown servo"), 400
    try:
        if "angle" in data:
            angle = float(data["angle"])
        else:
            base = {"pan": servos.target_pan, "tilt": servos.target_tilt, "jaw": servos.jaw_angle}[axis]
            angle = base + float(data.get("delta", 0))
        servos.calibrate_move(axis, angle)
    except (TypeError, ValueError) as e:
        return jsonify(ok=False, error=str(e)), 400
    except RuntimeError as e:
        return jsonify(ok=False, error=str(e)), 409
    return jsonify(ok=True, **_state())


@bp.route("/api/calibrate/set", methods=["POST"])
def api_calibrate_set():
    """Save where the servo is pointing now as one of its limits."""
    servos, sc, err = _need_control()
    if err:
        return err
    _touch()
    data = request.get_json(silent=True) or {}
    axis, key = data.get("axis"), data.get("key")
    if axis not in AXES or key not in [k for k, _ in AXES[axis]]:
        return jsonify(ok=False, error="Unknown limit"), 400
    target = {"pan": servos.target_pan, "tilt": servos.target_tilt, "jaw": servos.jaw_angle}[axis]
    value = int(round(target))
    try:
        config.SETTINGS.set(key, value)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    label = dict(AXES[axis])[key]
    _wd().log_event("SYSTEM", f"Calibration: {axis} {label.lower()} = {value}°")
    return jsonify(ok=True, **_state())


# Servo midpoint: where every servo goes before its horn / linkage is fitted
ASSEMBLY_ANGLE = 90


@bp.route("/api/calibrate/pose", methods=["POST"])
def api_calibrate_pose():
    """'assembly': all servos to 90° (mid-travel) for fitting horns and linkages
    on a new build. 'center': the saved center (pan/tilt) and jaw closed."""
    servos, sc, err = _need_control()
    if err:
        return err
    _touch()
    pose = (request.get_json(silent=True) or {}).get("pose")
    if pose == "assembly":
        targets = {"pan": ASSEMBLY_ANGLE, "tilt": ASSEMBLY_ANGLE, "jaw": ASSEMBLY_ANGLE}
        note = f"Servos to assembly position ({ASSEMBLY_ANGLE}°)"
    elif pose == "center":
        targets = {"pan": config.PAN_CENTER, "tilt": config.TILT_CENTER, "jaw": config.JAW_CLOSED}
        note = "Servos to saved center"
    else:
        return jsonify(ok=False, error="Unknown pose"), 400
    try:
        for axis, angle in targets.items():
            servos.calibrate_move(axis, angle)
    except RuntimeError as e:
        return jsonify(ok=False, error=str(e)), 409
    _wd().log_event("SYSTEM", f"Calibration: {note}")
    return jsonify(ok=True, **_state())


@bp.route("/api/calibrate/sweep", methods=["POST"])
def api_calibrate_sweep():
    """Slowly visit each saved limit of one servo, then come back."""
    servos, sc, err = _need_control()
    if err:
        return err
    _touch()
    axis = (request.get_json(silent=True) or {}).get("axis")
    if axis not in AXES:
        return jsonify(ok=False, error="Unknown servo"), 400

    def wait_for(attr_angle, target, timeout=15.0):  # full tilt range takes ~8 s at TILT_MAX_SPEED 8
        t0 = time.time()
        while time.time() - t0 < timeout and abs(getattr(servos, attr_angle) - target) > 1.0:
            if not sc._manual_mode:
                return False
            time.sleep(0.05)
        return sc._manual_mode

    def run():
        with _sweep_lock:
            if axis == "jaw":
                for _ in range(3):
                    for k in ("JAW_OPEN", "JAW_CLOSED"):
                        if not sc._manual_mode or sc.speech.is_speaking:
                            return
                        servos.calibrate_move("jaw", getattr(config, k))
                        time.sleep(0.6)
                return
            attr = "pan_angle" if axis == "pan" else "tilt_angle"
            stops = [k for k, _ in AXES[axis]] + [AXES[axis][1][0]]  # every limit, then back to center
            for k in stops:
                servos.calibrate_move(axis, getattr(config, k))
                if not wait_for(attr, getattr(config, k)):
                    return
                time.sleep(0.8)

    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True)


CALIBRATE_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<title>Calibrate · StrawMan</title>
{{ ui_head() }}
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 * { box-sizing: border-box; margin: 0; padding: 0; }
 body { background:#111; color:#e0e0e0; font-size:14px; padding:12px; }
 .wrap { max-width:760px; margin:0 auto; }
 header { display:flex; align-items:baseline; justify-content:space-between; gap:12px; padding:8px 0 14px; flex-wrap:wrap; }
 h1 { color:#ff8800; font-size:24px; font-weight:400; display:flex; align-items:center; gap:10px; }
 .logo { height:44px; width:auto; display:block; }
 nav { display:flex; gap:18px; }
 nav a { display:flex; align-items:center; gap:6px; color:#aaa; text-decoration:none; }
 nav a:hover { color:#ff8800; }
 .panel { background:#1c1c1c; border-radius:12px; padding:14px 16px; margin-bottom:12px; box-shadow:0 2px 2px rgba(0,0,0,.35),0 1px 5px rgba(0,0,0,.3); }
 h2 { display:flex; align-items:center; gap:10px; color:#e8e8e8; font-size:18px; font-weight:400; margin-bottom:10px; }
 h2 .i { color:#ff8800; }
 h2 .angle { margin-left:auto; font-size:22px; color:#ff8800; font-variant-numeric:tabular-nums; }
 .help { color:#8a8a8a; font-size:13px; line-height:1.45; }
 .row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:10px; }
 button { background:#262626; color:#ddd; border:1px solid #3a3a3a; border-radius:8px; padding:7px 12px; font-size:13px;
          font-weight:500; cursor:pointer; display:inline-flex; align-items:center; gap:6px; }
 button:hover:not(:disabled) { border-color:#ff8800; }
 button:disabled { opacity:.4; cursor:default; }
 button.primary { background:#ff8800; color:#111; border-color:#ff8800; }
 button.jog { min-width:52px; justify-content:center; font-variant-numeric:tabular-nums; }
 .track { position:relative; height:34px; margin:14px 4px 4px; }
 .track .bar { position:absolute; left:0; right:0; top:15px; height:4px; background:#2e2e2e; border-radius:2px; }
 .track .range { position:absolute; top:15px; height:4px; background:rgba(255,136,0,.35); border-radius:2px; }
 .track .mark { position:absolute; top:6px; width:2px; height:22px; background:#777; }
 .track .mark span { position:absolute; top:24px; left:50%; transform:translateX(-50%); font-size:11px; color:#888; white-space:nowrap; }
 .track .pos { position:absolute; top:9px; width:16px; height:16px; margin-left:-8px; border-radius:50%; background:#ff8800; border:2px solid #fff; }
 .scale { display:flex; justify-content:space-between; font-size:11px; color:#666; margin:16px 4px 0; }
 .limits { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
 .limit { display:flex; flex-direction:column; gap:2px; border:1px solid #2e2e2e; border-radius:12px; padding:8px 12px; background:#181818; min-width:110px; }
 .limit .v { font-size:16px; color:#e8e8e8; font-variant-numeric:tabular-nums; }
 .limit .d { font-size:11px; color:#777; }
 .limit button { margin-top:4px; justify-content:center; }
 .msg { font-size:13px; color:#888; min-height:1.2em; margin-top:8px; }
 .msg.err { color:#ff6b6b; } .msg.ok { color:#5ee08f; }
 .control { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
 .status { font-size:12px; font-weight:500; padding:3px 10px; border-radius:10px; background:#232323; color:#888; }
 .status.on { background:rgba(255,136,0,.16); color:#ffb35c; }
 .locked .axis-ctl { opacity:.4; pointer-events:none; }
</style>
</head>
<body>
<div class="wrap" id="wrap">
 <header>
  <h1><a href="/" title="Dashboard"><img class="logo" src="/static/strawman-logo.png" alt="StrawMan"></a>Calibrate servos</h1>
  <nav><a href="/">{{ icon("view-dashboard-outline", 18) }}Dashboard</a><a href="/settings">{{ icon("cog", 18) }}Settings</a></nav>
 </header>

 <div class="panel">
  <div class="control">
   <button class="primary" id="take" onclick="control(true)">{{ icon("gamepad-variant-outline", 18) }}Take control</button>
   <button id="done" onclick="control(false)" style="display:none">{{ icon("check-circle-outline", 18) }}Done — give control back</button>
   <span class="status" id="status">He's in charge</span>
  </div>
  <div class="row axis-ctl">
   <button onclick="pose('center')">{{ icon("crosshairs-gps", 18) }}Move to saved center</button>
   <button onclick="pose('assembly')">{{ icon("axis-arrow", 18) }}Assembly position (all servos to 90°)</button>
  </div>
  <p class="help axis-ctl">Building him? Put the servos in the assembly position <b>before</b> fitting the servo horns and
   linkages, then attach them so his head faces straight ahead, roughly level, with the jaw about half open.
   Calibrate afterwards.</p>
  <p class="help" style="margin-top:10px">Taking control pauses everything else (talking to visitors, idle scans, following people)
   until you press Done — or after 10 minutes without a move, so a forgotten tab can't leave him switched off. Moves are slow, may go past the saved limits, and never leave 0–180°.
   Move one step at a time near the ends — if a servo buzzes or strains, it's at its stop: back off a few degrees.
   Saved limits apply immediately.</p>
  <div class="msg" id="msg"></div>
 </div>

 <div id="axes"></div>
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
const AX = {
  pan:  {title: 'Pan (turn left / right)', icon: 'axis-arrow',
         help: 'Find how far he can turn each way without straining, then straight ahead.'},
  tilt: {title: 'Tilt (look up / down)', icon: 'axis-arrow',
         help: 'Smaller numbers look up. “Drooped” is his asleep pose, head slumped forward — it must be between up and down.'},
  jaw:  {title: 'Jaw', icon: 'emoticon-devil-outline',
         help: 'Find fully closed (no gap, no strain) and fully open. The jaw jumps straight to each position.'},
};
let S = null, msgTimer = null;
function say(t, cls) { const m = document.getElementById('msg'); m.textContent = t; m.className = 'msg ' + (cls || '');
  clearTimeout(msgTimer); if (cls === 'ok') msgTimer = setTimeout(() => { m.textContent = ''; }, 3000); }

function axisHtml(a) {
  const steps = a === 'jaw' ? [-5, -1, 1, 5] : [-10, -5, -1, 1, 5, 10];
  return `<div class="panel" id="ax-${a}">
    <h2>${mdi(AX[a].icon)}${AX[a].title}<span class="angle" data-role="angle">--°</span></h2>
    <p class="help">${AX[a].help}</p>
    <div class="axis-ctl">
      <div class="track" data-role="track"><div class="bar"></div><div class="range" data-role="range"></div></div>
      <div class="scale"><span>0°</span><span>90°</span><span>180°</span></div>
      <div class="row">${steps.map(d => `<button class="jog" onclick="jog('${a}', ${d})">${d > 0 ? '+' : '−'}${Math.abs(d)}°</button>`).join('')}
        <button onclick="sweep('${a}')">${mdi('restart', 16)}Test sweep</button></div>
      <div class="limits" data-role="limits"></div>
    </div></div>`;
}
function render() {
  document.getElementById('axes').innerHTML = Object.keys(AX).map(axisHtml).join('');
}
function update() {
  if (!S || !S.connected) { say('Servos not connected — is the scarecrow running?', 'err'); return; }
  document.getElementById('wrap').classList.toggle('locked', !S.control);
  document.getElementById('take').style.display = S.control ? 'none' : '';
  document.getElementById('done').style.display = S.control ? '' : 'none';
  const st = document.getElementById('status');
  st.textContent = S.control ? (S.sweeping ? 'Test sweep running…' : 'You have control') : "He's in charge";
  st.className = 'status' + (S.control ? ' on' : '');
  for (const [a, ax] of Object.entries(S.axes)) {
    const el = document.getElementById('ax-' + a);
    el.querySelector('[data-role=angle]').textContent = Math.round(ax.angle) + '°';
    const pct = v => (v / 180 * 100) + '%';
    const vals = ax.limits.map(l => l.value), lo = Math.min(...vals), hi = Math.max(...vals);
    const track = el.querySelector('[data-role=track]');
    track.querySelectorAll('.mark, .pos').forEach(n => n.remove());
    const r = el.querySelector('[data-role=range]'); r.style.left = pct(lo); r.style.width = ((hi - lo) / 180 * 100) + '%';
    for (const l of ax.limits) track.insertAdjacentHTML('beforeend', `<div class="mark" style="left:${pct(l.value)}"><span>${l.label}</span></div>`);
    track.insertAdjacentHTML('beforeend', `<div class="pos" style="left:${pct(ax.angle)}"></div>`);
    el.querySelector('[data-role=limits]').innerHTML = ax.limits.map(l => `<div class="limit">
        <span class="help">${l.label}</span><span class="v">${l.value}°</span>
        <span class="d">${l.value === l.default ? 'default' : 'default ' + l.default + '°'}</span>
        <button onclick="setLimit('${a}', '${l.key}', '${l.label}')">Set to ${Math.round(ax.target)}°</button></div>`).join('');
  }
}
async function poll() {
  try { const r = await fetch('/api/calibrate'); S = await r.json(); update(); } catch (e) {}
  setTimeout(poll, S && S.control ? 400 : 1500);
}
async function pose(p) {
  if (p === 'assembly' && !confirm('Assembly position moves every servo to 90° — the middle of its travel.\n\n' +
      'Only for assembling. If his head and jaw are already attached, this can push them past their limits ' +
      '(a jaw that works between 63° and 83°, say, would be forced open to 90°).\n\nMove all servos to 90°?')) return;
  try { S = await post('/api/calibrate/pose', {pose: p}); update();
    say(p === 'assembly' ? 'All servos at 90° — fit the horns now.' : 'Back at his saved center.', 'ok'); }
  catch (e) { say(e.message, 'err'); }
}
async function control(on) {
  try { S = await post('/api/calibrate/control', {on}); update(); say(on ? 'You have control.' : 'Control handed back — he\'s centering.', 'ok'); }
  catch (e) { say(e.message, 'err'); }
}
async function jog(axis, delta) {
  try { S = await post('/api/calibrate/move', {axis, delta}); update(); } catch (e) { say(e.message, 'err'); }
}
async function setLimit(axis, key, label) {
  try { S = await post('/api/calibrate/set', {axis, key}); update(); say(`${label} saved — live now.`, 'ok'); }
  catch (e) { say(e.message + ' — not saved', 'err'); }
}
async function sweep(axis) {
  try { await post('/api/calibrate/sweep', {axis}); say('Sweeping through the saved limits…'); } catch (e) { say(e.message, 'err'); }
}
render(); poll();
</script>
</body>
</html>
"""
