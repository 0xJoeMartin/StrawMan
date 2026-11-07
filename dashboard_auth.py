"""
Dashboard PIN protection
------------------------
Every dashboard page, API and the video feed require a signed-in session.

  - PIN is stored only as a salted PBKDF2 hash in dashboard_pin.json.
  - No PIN set (first run, or after a reset) → every page sends you to /setup
    to choose one.
  - Sessions last 30 days. Changing or resetting the PIN signs out every
    browser (the session carries the PIN's "set_at" stamp).
  - Brute force: 5 wrong PINs from one IP within 10 minutes locks that IP
    out for 5 minutes.
  - Cookie: HttpOnly, Secure (dashboard is HTTPS-only), SameSite=Strict
    (blocks cross-site POSTs to the APIs).

Reset a forgotten PIN (on the Pi):
    cd ~/scarecrow && venv/bin/python dashboard_auth.py --reset
then open the dashboard and choose a new one. No restart needed.
"""

import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
PIN_PATH = os.path.join(_DIR, "dashboard_pin.json")
SECRET_PATH = os.path.join(_DIR, ".dashboard_secret")

PIN_MIN_LEN = 4
PIN_MAX_LEN = 12
MAX_FAILURES = 5
FAILURE_WINDOW_S = 600
LOCKOUT_S = 300

# Paths reachable without a session
_PUBLIC_PATHS = {"/login", "/setup", "/logout", "/static/strawman-logo.png", "/static/favicon.png",
                 "/static/fonts/roboto-latin-400.woff2", "/static/fonts/roboto-latin-500.woff2"}

_fail_lock = threading.Lock()
_failures = {}      # ip -> [timestamps]
_locked_until = {}  # ip -> epoch seconds


# --- PIN storage -------------------------------------------------------------

def _read_pin():
    try:
        with open(PIN_PATH) as f:
            data = json.load(f)
        return data if data.get("hash") else None
    except (OSError, ValueError):
        return None


def pin_is_set() -> bool:
    return _read_pin() is not None


def pin_stamp():
    d = _read_pin()
    return d.get("set_at") if d else None


def validate_pin(pin: str):
    if not pin.isdigit():
        return "PIN must be digits only."
    if not (PIN_MIN_LEN <= len(pin) <= PIN_MAX_LEN):
        return f"PIN must be {PIN_MIN_LEN}–{PIN_MAX_LEN} digits."
    return None


def set_pin(pin: str):
    from werkzeug.security import generate_password_hash
    err = validate_pin(pin)
    if err:
        raise ValueError(err)
    data = {"hash": generate_password_hash(pin, method="pbkdf2:sha256:600000"),
            "set_at": datetime.now().isoformat(timespec="seconds")}
    tmp = PIN_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PIN_PATH)


def check_pin(pin: str) -> bool:
    from werkzeug.security import check_password_hash
    d = _read_pin()
    return bool(d) and check_password_hash(d["hash"], pin)


def clear_pin() -> bool:
    try:
        os.remove(PIN_PATH)
        return True
    except FileNotFoundError:
        return False


def _secret_key() -> bytes:
    try:
        with open(SECRET_PATH, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    with open(SECRET_PATH, "wb") as f:
        f.write(key)
    os.chmod(SECRET_PATH, 0o600)
    return key


# --- brute-force limiter -----------------------------------------------------

def _locked_for(ip: str) -> int:
    with _fail_lock:
        remaining = _locked_until.get(ip, 0) - time.time()
        return int(remaining) + 1 if remaining > 0 else 0


def _record_failure(ip: str):
    now = time.time()
    with _fail_lock:
        recent = [t for t in _failures.get(ip, []) if now - t < FAILURE_WINDOW_S]
        recent.append(now)
        _failures[ip] = recent
        if len(recent) >= MAX_FAILURES:
            _locked_until[ip] = now + LOCKOUT_S
            _failures[ip] = []


def _clear_failures(ip: str):
    with _fail_lock:
        _failures.pop(ip, None)
        _locked_until.pop(ip, None)


# --- pages -------------------------------------------------------------------

_PAGE = """<!DOCTYPE html>
<html><head><title>{{ title }} · StrawMan</title>{{ ui_head() }}
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 * { box-sizing: border-box; margin: 0; padding: 0; }
 body { background:#111; color:#e0e0e0; font-size:14px;
        min-height:100vh; display:flex; align-items:center; justify-content:center; padding:16px; }
 .card { background:#1c1c1c; border-radius:12px; padding:24px; width:100%; max-width:340px;
         box-shadow:0 2px 2px rgba(0,0,0,.35),0 1px 5px rgba(0,0,0,.3); }
 h1 { color:#ff8800; font-size:1.3em; text-align:center; margin-bottom:4px; }
 .logo { display:block; width:150px; height:auto; margin:0 auto 6px; }
 p.sub { color:#888; font-size:.85em; text-align:center; margin-bottom:18px; }
 label { display:block; font-size:13px; color:#aaa; margin:10px 0 4px; }
 input { width:100%; padding:12px; font-size:1.4em; letter-spacing:6px; text-align:center; background:#0d0d0d;
         color:#fff; border:1px solid #3a3a3a; border-radius:8px; }
 input:focus { outline:none; border-color:#ff8800; }
 button { width:100%; margin-top:18px; padding:12px; font-size:15px; background:#ff8800; color:#111; border:0;
          border-radius:24px; font-weight:500; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px; }
 .err { color:#ff5555; font-size:.9em; text-align:center; margin-top:12px; }
 .note { color:#777; font-size:.75em; text-align:center; margin-top:16px; line-height:1.4; }
</style></head><body>
<form class="card" method="post" autocomplete="off">
 <img class="logo" src="/static/strawman-logo.png" alt="StrawMan">
 <p class="sub">{{ subtitle }}</p>
 {% if setup %}
  <label>New PIN</label>
  <input name="pin" type="password" inputmode="numeric" pattern="[0-9]*" autofocus required>
  <label>Confirm PIN</label>
  <input name="pin2" type="password" inputmode="numeric" pattern="[0-9]*" required>
  <button type="submit">{{ icon("key-variant") }}Set PIN</button>
  <p class="note">{{ min }}–{{ max }} digits. 6 or more recommended.<br>
   Forgot it later? On the Pi: <code>venv/bin/python dashboard_auth.py --reset</code></p>
 {% else %}
  <input name="pin" type="password" inputmode="numeric" pattern="[0-9]*" autofocus required {% if locked %}disabled{% endif %}>
  <button type="submit" {% if locked %}disabled{% endif %}>{{ icon("lock-open-variant-outline") }}Unlock</button>
 {% endif %}
 {% if error %}<p class="err">{{ error }}</p>{% endif %}
</form></body></html>"""


def init_app(app):
    from flask import (request, session, redirect, jsonify,
                       render_template_string, url_for)

    app.secret_key = _secret_key()
    app.config.update(
        SESSION_COOKIE_NAME="scarecrow_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )

    def _authed() -> bool:
        stamp = pin_stamp()
        return bool(stamp) and session.get("auth") is True and session.get("pin_at") == stamp

    def _start_session():
        session.clear()
        session.permanent = True
        session["auth"] = True
        session["pin_at"] = pin_stamp()

    def _wants_json() -> bool:
        return request.path.startswith("/api/") or request.path == "/video_feed"

    @app.before_request
    def _require_pin():
        if request.path in _PUBLIC_PATHS:
            return None
        if _authed():
            return None
        if _wants_json():
            return jsonify(ok=False, error="Not signed in", login=True), 401
        return redirect(url_for("_setup") if not pin_is_set() else url_for("_login"))

    def _render(**kw):
        return render_template_string(_PAGE, min=PIN_MIN_LEN, max=PIN_MAX_LEN, **kw)

    @app.route("/setup", methods=["GET", "POST"], endpoint="_setup")
    def _setup():
        if pin_is_set():
            return redirect(url_for("_login"))
        error = None
        if request.method == "POST":
            pin = request.form.get("pin", "")
            pin2 = request.form.get("pin2", "")
            error = validate_pin(pin) or (None if pin == pin2 else "PINs don't match.")
            if not error:
                set_pin(pin)
                _start_session()
                try:
                    from web_dashboard import log_event
                    log_event("SYSTEM", f"Dashboard PIN set from {request.remote_addr}")
                except Exception:
                    pass
                return redirect("/")
        return _render(title="Set PIN", subtitle="Choose a PIN for the dashboard", setup=True, error=error)

    @app.route("/login", methods=["GET", "POST"], endpoint="_login")
    def _login():
        if not pin_is_set():
            return redirect(url_for("_setup"))
        if _authed():
            return redirect("/")
        ip = request.remote_addr or "?"
        error = None
        locked = _locked_for(ip)
        if request.method == "POST" and not locked:
            if check_pin(request.form.get("pin", "")):
                _clear_failures(ip)
                _start_session()
                return redirect("/")
            _record_failure(ip)
            locked = _locked_for(ip)
            error = "Wrong PIN."
            try:
                from web_dashboard import log_event
                log_event("SYSTEM", f"Wrong dashboard PIN from {ip}")
            except Exception:
                pass
        if locked:
            error = f"Too many wrong PINs. Try again in {(locked + 59) // 60} min."
        return _render(title="Unlock", subtitle="Enter the dashboard PIN", setup=False,
                       error=error, locked=bool(locked))

    @app.route("/logout", endpoint="_logout")
    def _logout():
        session.clear()
        return redirect(url_for("_login"))


# --- CLI ---------------------------------------------------------------------

if __name__ == "__main__":
    if "--reset" in sys.argv:
        print("PIN cleared. Open the dashboard to choose a new one."
              if clear_pin() else "No PIN was set. Open the dashboard to choose one.")
    elif "--set" in sys.argv:
        import getpass
        p1 = getpass.getpass("New PIN: ")
        p2 = getpass.getpass("Confirm: ")
        if p1 != p2:
            sys.exit("PINs don't match.")
        try:
            set_pin(p1)
        except ValueError as e:
            sys.exit(str(e))
        print("PIN set. All browsers are signed out.")
    elif "--status" in sys.argv:
        d = _read_pin()
        print(f"PIN set at {d['set_at']}" if d else "No PIN set (dashboard shows the setup page).")
    else:
        print(__doc__.split("Reset a forgotten PIN")[0].strip())
        print("\nUsage: dashboard_auth.py --reset | --set | --status")
