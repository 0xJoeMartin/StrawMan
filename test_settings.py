#!/usr/bin/env python3
"""
Settings store checks — no hardware needed, never touches the real
settings.json.

    python test_settings.py
"""

import json
import os
import tempfile
import types

import settings_store
from settings_store import SETTINGS_SCHEMA, SettingsStore, coerce

HERE = os.path.dirname(os.path.abspath(__file__))


def fresh_config():
    """config.py's defaults as a new module, without applying settings.json."""
    src = open(os.path.join(HERE, "config.py")).read()
    src = src.split("# --- Dashboard overrides")[0]
    mod = types.ModuleType("cfg")
    exec(compile(src, "config.py", "exec"), mod.__dict__)
    return mod


def store_at(path, cfg=None):
    cfg = cfg or fresh_config()
    return SettingsStore(cfg, path=path), cfg


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'FAIL'}  {name}{'' if cond or not extra else '  ' + extra}")
    if not cond:
        raise SystemExit(1)


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return True
    return False


def main():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "settings.json")

    print("schema")
    cfg = fresh_config()
    for m in SETTINGS_SCHEMA:
        if not all(hasattr(cfg, t) for t in m.get("targets", [m["key"]])):
            check(f"{m['key']} exists in config.py", False)
    defaults = SettingsStore(cfg, path=os.path.join(tmp, "unused.json"))
    for m in SETTINGS_SCHEMA:
        try:
            coerce(m, defaults.default(m["key"]))
        except ValueError as e:
            check(f"default for {m['key']} is valid ({e})", False)
    check("every key exists in config.py and its default validates", True)
    check("keys are unique", len({m["key"] for m in SETTINGS_SCHEMA}) == len(SETTINGS_SCHEMA))
    check("every group is listed", all(m["group"] in settings_store.GROUPS for m in SETTINGS_SCHEMA))
    from secrets_store import SECRETS
    check("every API key has a group", all(s["group"] in settings_store.GROUPS for s in SECRETS))

    print("no settings.json")
    s, cfg = store_at(path)
    check("values == config defaults", s.get("COOLDOWN_SECONDS") == cfg.COOLDOWN_SECONDS)
    check("nothing pending", s.pending_restart() == [])
    check("no file written", not os.path.exists(path))

    print("live setting")
    s.set("COOLDOWN_SECONDS", 25)
    check("config updated", cfg.COOLDOWN_SECONDS == 25)
    check("overridden", s.is_overridden("COOLDOWN_SECONDS"))
    check("written to disk", json.load(open(path))["settings"] == {"COOLDOWN_SECONDS": 25})
    s.set("COOLDOWN_SECONDS", "30")
    check("numeric string accepted", cfg.COOLDOWN_SECONDS == 30)
    check(".bak kept", os.path.exists(path + ".bak"))
    s.set("COOLDOWN_SECONDS", s.default("COOLDOWN_SECONDS"))
    check("saving the default removes the override", not s.is_overridden("COOLDOWN_SECONDS"))
    check("config back to default", cfg.COOLDOWN_SECONDS == s.default("COOLDOWN_SECONDS"))

    print("restart setting")
    s.set("WHISPER_MODEL", "base")
    check("config NOT updated", cfg.WHISPER_MODEL == "tiny")
    check("pending restart", s.pending_restart() == ["WHISPER_MODEL"])
    s.reset(["WHISPER_MODEL"])
    check("reset clears pending", s.pending_restart() == [])

    print("validation")
    check("out of range rejected", raises(lambda: s.set("AI_MAX_TOKENS", 5000)))
    check("confidence can't go into the noise floor", raises(lambda: s.set("DETECTION_CONFIDENCE", 0.5)))
    check("confidence 0.52 and 0.60 allowed", s.set("DETECTION_CONFIDENCE", 0.52) == 0.52
          and s.set("DETECTION_CONFIDENCE", 0.6) == 0.6)
    check("bad choice rejected", raises(lambda: s.set("PERSONA", "goofy")))
    check("bool must be bool", raises(lambda: s.set("LOG_CONVERSATIONS", "yes")))
    check("int must be whole", raises(lambda: s.set("MAX_CONVERSATION_TURNS", 2.5)))
    check("url needs scheme", raises(lambda: s.set("OLLAMA_BASE_URL", "192.168.1.50:11434")))
    check("empty text rejected", raises(lambda: s.set("AI_MODEL", "  ")))
    check("empty line list rejected", raises(lambda: s.set("SILENCE_TAUNTS", ["", " "])))
    check("unknown key rejected", raises(lambda: s.set("NOPE", 1)))
    check("pan min > center rejected", raises(lambda: s.set("PAN_MIN", 120)))
    check("duplicate channel rejected", raises(lambda: s.set("JAW_CHANNEL", 0)))
    check("idle min > max rejected", raises(lambda: s.set("IDLE_SCAN_MIN_INTERVAL", 500)))
    check("all-or-nothing: valid half not saved",
          raises(lambda: s.set_many({"COOLDOWN_SECONDS": 12, "AI_MAX_TOKENS": 9999}))
          and cfg.COOLDOWN_SECONDS != 12 and not s.is_overridden("COOLDOWN_SECONDS"))
    check("idle min+max moved together is fine",
          s.set_many({"IDLE_SCAN_MIN_INTERVAL": 200, "IDLE_SCAN_MAX_INTERVAL": 300}) is not None)
    check("url trailing slash stripped", s.set("OLLAMA_BASE_URL", "http://10.0.0.5:11434/") == "http://10.0.0.5:11434")
    check("lines from text", s.set("SILENCE_TAUNTS", "Boo.\n\n  Well?  \n") == ["Boo.", "Well?"])

    print("hooks")
    def no(v):
        raise ValueError("nope")
    s.add_hook("AI_BACKEND", no)
    other = "claude" if cfg.AI_BACKEND == "ollama" else "ollama"
    before = cfg.AI_BACKEND
    check("hook can reject", raises(lambda: s.set("AI_BACKEND", other)))
    check("rejected change not applied or saved",
          cfg.AI_BACKEND == before and not s.is_overridden("AI_BACKEND"))
    seen = []
    s.add_hook("AI_BACKEND", seen.append)
    s.set("AI_BACKEND", other)
    check("hook called with new value", seen == [other] and cfg.AI_BACKEND == other)

    print("reload from disk")
    s.set("CAMERA_RESOLUTION", "1280x720")
    s.set("PERSONA", "snarky")
    s2, cfg2 = store_at(path)
    check("live override applied at startup", cfg2.PERSONA == "snarky")
    check("restart override applied at startup", (cfg2.CAMERA_WIDTH, cfg2.CAMERA_HEIGHT) == (1280, 720))
    check("nothing pending after 'restart'", s2.pending_restart() == [])
    check("other overrides survive", cfg2.SILENCE_TAUNTS == ["Boo.", "Well?"]
          and cfg2.OLLAMA_BASE_URL == "http://10.0.0.5:11434")

    print("bad files")
    data = json.load(open(path))
    data["settings"].update({"AI_MAX_TOKENS": 99999, "GONE_KEY": 1, "PAN_MIN": 150})
    json.dump(data, open(path, "w"))
    s3, cfg3 = store_at(path)
    check("bad value dropped", not s3.is_overridden("AI_MAX_TOKENS"))
    check("unknown key dropped", "GONE_KEY" not in s3.values())
    check("cross-rule violation dropped", not s3.is_overridden("PAN_MIN") and cfg3.PAN_MIN == s3.default("PAN_MIN"))
    check("good values kept", cfg3.PERSONA == "snarky")
    open(path, "w").write("{not json")
    s4, cfg4 = store_at(path)
    check("corrupt file moved aside", os.path.exists(path + ".corrupt") and not os.path.exists(path))
    check("corrupt file -> defaults", cfg4.PERSONA == s4.default("PERSONA"))

    print("secrets (.env)")
    from secrets_store import SecretsStore
    env = os.path.join(tmp, ".env")
    original = ("# my keys\nANTHROPIC_API_KEY=sk-ant-old\n\nELEVENLABS_VOICE_ID=abc123\n"
                "export OPENAI_API_KEY='sk-old'\nOTHER=1\nANTHROPIC_API_KEY=dupe")
    open(env, "w").write(original)
    os.chmod(env, 0o640)
    cfg = types.SimpleNamespace(ANTHROPIC_API_KEY="sk-ant-old", OPENAI_API_KEY="sk-old",
                                ELEVENLABS_API_KEY="")
    sec = SecretsStore(cfg, path=env)
    check("status says set / not set, no values",
          [(x["key"], x["set"]) for x in sec.status()] ==
          [("ANTHROPIC_API_KEY", True), ("OPENAI_COMPAT_API_KEY", False), ("ELEVENLABS_API_KEY", False),
           ("OPENAI_API_KEY", True)]
          and all("value" not in x for x in sec.status()))
    sec.set("ANTHROPIC_API_KEY", "  sk-ant-new  ")
    text = open(env).read()
    check("key replaced in place, duplicate dropped",
          text == "# my keys\nANTHROPIC_API_KEY=sk-ant-new\n\nELEVENLABS_VOICE_ID=abc123\n"
                  "export OPENAI_API_KEY='sk-old'\nOTHER=1\n", repr(text))
    check("file mode kept", os.stat(env).st_mode & 0o777 == 0o640)
    check(".bak is the previous file", open(env + ".bak").read() == original)
    check("config + environ updated", cfg.ANTHROPIC_API_KEY == "sk-ant-new"
          and os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-new")
    sec.set("ELEVENLABS_API_KEY", "el_new")
    check("missing key appended", open(env).read().endswith("OTHER=1\nELEVENLABS_API_KEY=el_new\n"))
    sec.set("OPENAI_API_KEY", None)
    check("remove deletes the export line", "OPENAI_API_KEY" not in open(env).read()
          and cfg.OPENAI_API_KEY == "")
    check("other lines untouched", "ELEVENLABS_VOICE_ID=abc123\n" in open(env).read()
          and open(env).read().startswith("# my keys\n"))
    for bad in ["", "has space", "x" * 400, 'quo"te', "line\nbreak", "$HOME"]:
        check(f"rejects {bad[:12]!r}", raises(lambda: sec.set("ANTHROPIC_API_KEY", bad)))
    check("unknown key rejected", raises(lambda: sec.set("PATH", "x")))
    sec.set("ANTHROPIC_API_KEY", "sk-ant-old")
    from dotenv import dotenv_values
    check("python-dotenv still parses it", dotenv_values(env) ==
          {"ANTHROPIC_API_KEY": "sk-ant-old", "ELEVENLABS_VOICE_ID": "abc123",
           "OTHER": "1", "ELEVENLABS_API_KEY": "el_new"})
    for k in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(k, None)

    print("\nAll settings checks passed.")


if __name__ == "__main__":
    main()
