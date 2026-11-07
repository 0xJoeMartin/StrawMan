"""
Secrets Store
-------------
Write-only API keys for the dashboard's Settings page, kept in .env.

  - Values are never returned to the browser: the page only learns whether a
    key is set. The dashboard log only says that a key changed.
  - .env is rewritten atomically (tmp file + os.replace) with its original
    file mode. Every line the dashboard doesn't own — comments, blank lines,
    other variables, ELEVENLABS_VOICE_ID — is kept exactly as it was. The
    previous file is kept as .env.bak (same mode).
  - A saved key is put in config / os.environ right away; the code that uses
    each key reads it from config on every call, so changes apply live.
  - Each key is shown on the Settings page next to what uses it: `group` is
    the settings group, `card` the AI provider card (if any).
"""

import os
import re
import shutil
import threading

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

SECRETS = [
    {"key": "ANTHROPIC_API_KEY", "label": "Anthropic API key", "group": "AI", "card": "claude",
     "help": "console.anthropic.com → API keys."},
    {"key": "OPENAI_COMPAT_API_KEY", "label": "API key", "group": "AI", "card": "openai",
     "help": "For OpenRouter, Groq, Gemini… Not needed for local servers. For OpenAI itself, "
             "leave it empty to use the OpenAI key under Hearing."},
    {"key": "ELEVENLABS_API_KEY", "label": "API key", "group": "Voice", "card": "tts-elevenlabs",
     "help": "Without it he uses the next voice in the order."},
    {"key": "OPENAI_API_KEY", "label": "OpenAI API key", "group": "Hearing", "card": "stt-openai",
     "help": "Used for OpenAI speech-to-text and the OpenAI voice, and by the OpenAI-compatible AI "
             "when it points at OpenAI and has no key of its own."},
]
SECRET_KEYS = [s["key"] for s in SECRETS]
MAX_LEN = 300
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_SAFE = re.compile(r"^[A-Za-z0-9_\-.:/+=]+$")


class SecretsStore:
    def __init__(self, config_module, path: str = ENV_PATH):
        self._config = config_module
        self._path = path
        self._lock = threading.Lock()

    def current(self, key: str) -> str:
        return getattr(self._config, key, "") or ""

    def status(self) -> list:
        return [{**s, "set": bool(self.current(s["key"]))} for s in SECRETS]

    @staticmethod
    def validate(key: str, value: str) -> str:
        if key not in SECRET_KEYS:
            raise ValueError("Unknown key")
        if not isinstance(value, str):
            raise ValueError("Key must be text")
        value = value.strip()
        if not value:
            raise ValueError("Key is empty — use Remove to delete it")
        if len(value) > MAX_LEN or any(c.isspace() for c in value) or not value.isprintable():
            raise ValueError("That doesn't look like an API key (no spaces, under 300 characters)")
        if "'" in value or '"' in value or "\\" in value or "$" in value:
            raise ValueError("API keys can't contain quotes, backslashes or $")
        return value

    def set(self, key: str, value):
        """value=None removes the key from .env."""
        if value is not None:
            value = self.validate(key, value)
        elif key not in SECRET_KEYS:
            raise ValueError("Unknown key")
        with self._lock:
            self._rewrite(key, value)
            setattr(self._config, key, value or "")
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    def _rewrite(self, key: str, value):
        lines, mode = [], 0o600
        if os.path.exists(self._path):
            mode = os.stat(self._path).st_mode & 0o777
            with open(self._path) as f:
                lines = f.read().splitlines(keepends=True)
        new_line = None
        if value is not None:
            new_line = f"{key}={value}\n" if _SAFE.match(value) else f'{key}="{value}"\n'
        out, done = [], False
        for line in lines:
            m = _LINE.match(line)
            if m and m.group(1) == key:
                if new_line and not done:
                    out.append(new_line)
                done = True          # drop any later duplicates
                continue
            out.append(line)
        if new_line and not done:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(new_line)
        tmp = self._path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as f:
            f.writelines(out)
        os.chmod(tmp, mode)
        if os.path.exists(self._path):
            shutil.copy2(self._path, self._path + ".bak")
        os.replace(tmp, self._path)


def test_key(key: str, value: str, base_url: str = None) -> str:
    """Cheapest authenticated call per provider (no tokens spent). Returns a
    short success message or raises ValueError with the provider's reason."""
    import requests
    if key == "ANTHROPIC_API_KEY":
        r = requests.get("https://api.anthropic.com/v1/models", timeout=10,
                         headers={"x-api-key": value, "anthropic-version": "2023-06-01"})
    elif key == "OPENAI_API_KEY":
        r = requests.get("https://api.openai.com/v1/models", timeout=10,
                         headers={"Authorization": f"Bearer {value}"})
    elif key == "OPENAI_COMPAT_API_KEY":
        if not base_url:
            raise ValueError("Set the OpenAI-compatible URL first")
        r = requests.get(f"{base_url.rstrip('/')}/models", timeout=10,
                         headers={"Authorization": f"Bearer {value}"})
    elif key == "ELEVENLABS_API_KEY":
        r = requests.get("https://api.elevenlabs.io/v1/user", timeout=10,
                         headers={"xi-api-key": value})
    else:
        raise ValueError("Unknown key")
    if r.status_code == 200:
        return "Key works"
    reason = ""
    try:
        body = r.json()
        err = body.get("error") or body.get("detail") or {}
        reason = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        pass
    reason = (reason or "").replace(value, "…")
    if key == "ELEVENLABS_API_KEY" and r.status_code == 401 and "permission" in reason.lower():
        # Restricted keys can be fine for speech but not allowed to read the account
        return "Key accepted (restricted key — can't read account info, which is fine for speech)"
    raise ValueError(f"Rejected (HTTP {r.status_code}){': ' + reason[:160] if reason else ''}")
