"""
Prompt Store
------------
Runtime-editable prompt text and fallback lines, layered over the defaults
that live in code (ai_brain.py).

  - Defaults are NEVER written to disk. They live in ai_brain.py, so a reset
    can always get back to them, and a code update ships new defaults.
  - Edits made from the dashboard are stored in prompts_override.json (next
    to this file). "Reset to default" just deletes the override for that key.
  - Saving text identical to the default removes the override, so the
    "modified" badge on the dashboard stays honest.
  - Writes are atomic (tmp file + os.replace) and the previous override file
    is kept as prompts_override.json.bak.
  - A corrupt override file is moved aside to *.corrupt and ignored, so a bad
    edit can never stop the scarecrow from starting.

File format:
  {"version": 1,
   "prompts":   {"<key>": "<text>", ...},
   "fallbacks": {"<persona>": {"solo": [...], "group": [...], "reply": [...]}, ...}}
"""

import copy
import json
import os
import shutil
import threading

FALLBACK_KINDS = ("solo", "group", "reply")
OVERRIDE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "prompts_override.json")


class PromptStore:
    def __init__(self, prompt_defaults: dict, fallback_defaults: dict,
                 path: str = OVERRIDE_PATH):
        self._defaults = dict(prompt_defaults)
        self._fb_defaults = copy.deepcopy(fallback_defaults)
        self._path = path
        self._lock = threading.RLock()
        self._prompts = {}
        self._fallbacks = {}
        self._load()

    # --- persistence -------------------------------------------------------

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            prompts = data.get("prompts", {})
            fallbacks = data.get("fallbacks", {})
            # Drop keys the code no longer knows about rather than crash
            self._prompts = {k: v for k, v in prompts.items()
                             if k in self._defaults and isinstance(v, str)}
            self._fallbacks = {}
            for persona, kinds in fallbacks.items():
                if persona not in self._fb_defaults or not isinstance(kinds, dict):
                    continue
                clean = {k: [str(l) for l in v if str(l).strip()]
                         for k, v in kinds.items()
                         if k in FALLBACK_KINDS and isinstance(v, list)}
                clean = {k: v for k, v in clean.items() if v}
                if clean:
                    self._fallbacks[persona] = clean
            print(f"[prompts] Loaded overrides: {len(self._prompts)} prompt(s), "
                  f"{len(self._fallbacks)} fallback set(s)")
        except Exception as e:
            bad = self._path + ".corrupt"
            try:
                shutil.move(self._path, bad)
            except OSError:
                pass
            print(f"[prompts] Override file unreadable ({e}) — moved to {bad}, using defaults")
            self._prompts, self._fallbacks = {}, {}

    def _save(self):
        data = {"version": 1, "prompts": self._prompts, "fallbacks": self._fallbacks}
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        if os.path.exists(self._path):
            shutil.copy2(self._path, self._path + ".bak")
        os.replace(tmp, self._path)

    # --- prompt sections ---------------------------------------------------

    def keys(self):
        return list(self._defaults.keys())

    def get(self, key: str) -> str:
        with self._lock:
            return self._prompts.get(key, self._defaults[key])

    def default(self, key: str) -> str:
        return self._defaults[key]

    def is_overridden(self, key: str) -> bool:
        with self._lock:
            return key in self._prompts

    def set(self, key: str, text: str):
        if key not in self._defaults:
            raise KeyError(key)
        text = text.replace("\r\n", "\n")
        with self._lock:
            if text.strip() == self._defaults[key].strip():
                self._prompts.pop(key, None)
            else:
                self._prompts[key] = text
            self._save()

    def reset(self, key: str):
        with self._lock:
            if self._prompts.pop(key, None) is not None:
                self._save()

    def reset_all_prompts(self):
        with self._lock:
            self._prompts = {}
            self._save()

    # --- fallback lines ----------------------------------------------------

    def fallback(self, persona: str, kind: str) -> list:
        with self._lock:
            over = self._fallbacks.get(persona, {}).get(kind)
            if over:
                return list(over)
            base = self._fb_defaults.get(persona) or self._fb_defaults["creepy"]
            return list(base[kind])

    def fallback_defaults(self, persona: str) -> dict:
        return copy.deepcopy(self._fb_defaults[persona])

    def fallback_overridden(self, persona: str) -> bool:
        with self._lock:
            return persona in self._fallbacks

    def set_fallbacks(self, persona: str, lines_by_kind: dict):
        """Replace all three lists for a persona. Every list must keep >= 1 line,
        otherwise the offline fallback would have nothing to say."""
        if persona not in self._fb_defaults:
            raise KeyError(persona)
        clean = {}
        for kind in FALLBACK_KINDS:
            lines = [str(l).strip() for l in lines_by_kind.get(kind, []) if str(l).strip()]
            if not lines:
                raise ValueError(f"'{kind}' needs at least one line")
            clean[kind] = lines
        with self._lock:
            if clean == {k: self._fb_defaults[persona][k] for k in FALLBACK_KINDS}:
                self._fallbacks.pop(persona, None)
            else:
                self._fallbacks[persona] = clean
            self._save()

    def reset_fallbacks(self, persona: str = None):
        with self._lock:
            if persona is None:
                self._fallbacks = {}
            else:
                self._fallbacks.pop(persona, None)
            self._save()
