#!/usr/bin/env python3
"""
LLM payload equivalence check — no network, no hardware, no API keys.

Runs the brain through a fixed set of scenarios (first line, multi-frame line,
replies, final reply, dashboard Test button) for each backend, with the
network layer faked, and compares every request it would have sent against
test_llm_payloads.golden.json byte for byte.

    python test_llm_payloads.py            # check
    python test_llm_payloads.py --update   # re-record the golden file

Only re-record when a payload change is intended, and say why in the commit.
"""

import io
import json
import os
import sys
import tempfile
import types
import urllib.request
from datetime import datetime as _real_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "test_llm_payloads.golden.json")
CALLS = []


# --- fakes ------------------------------------------------------------------

class _FakeMessages:
    def create(self, **kwargs):
        CALLS.append({"transport": "anthropic-sdk", "kwargs": kwargs})
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=' "Boo." ')])


class _FakeAnthropic:
    def __init__(self, api_key=None, **kw):
        self.messages = _FakeMessages()


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    CALLS.append({"transport": "urllib", "url": req.full_url, "method": req.get_method(),
                  "headers": dict(sorted(req.header_items())), "timeout": timeout,
                  "body": req.data.decode("utf-8")})
    return _FakeResp(json.dumps({"message": {"content": "Boo."}}).encode())


class _FakeRequestsResp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "Boo."}}]}


def _fake_requests_post(url, json=None, headers=None, timeout=None):
    import json as _json
    CALLS.append({"transport": "requests", "url": url, "headers": dict(sorted((headers or {}).items())),
                  "timeout": timeout, "body": _json.dumps(json)})
    return _FakeRequestsResp()


class _FrozenDatetime(_real_datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 14, 19, 30, 0)


# --- scenarios --------------------------------------------------------------

SCENE_1 = "One person is standing at the bottom of the porch steps, about two meters away."
SCENE_2 = "2 people detected.\n  Person 1: center, close\n  Person 2: left, mid-range"


def _anthropic_user_turn(images, text):
    """History format scarecrow.py builds (Anthropic-style blocks)."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
               for b in images]
    content.append({"type": "text", "text": text})
    return {"role": "user", "content": content}


def run_backend(ai_brain, config, backend):
    calls = {}

    def record(name, fn):
        CALLS.clear()
        out = fn()
        calls[name] = {"returned": out, "requests": list(CALLS)}

    config.AI_BACKEND = backend
    brain = ai_brain.ScarecrowBrain()

    record("first_line", lambda: brain.generate_response(["IMG_A"], SCENE_1, 1))
    record("multi_frame", lambda: brain.generate_response(["IMG_A", "IMG_B", "IMG_C"], SCENE_2, 2))
    history = [_anthropic_user_turn(["IMG_A", "IMG_B"], brain._build_context(SCENE_2, 2, num_frames=2)),
               {"role": "assistant", "content": "Boo."}]
    record("reply", lambda: brain.generate_reply("Are you real?", history, is_final_turn=False))
    history2 = history + [{"role": "user", "content": 'The person responded out loud: "Are you real?"'},
                          {"role": "assistant", "content": "Boo."}]
    record("final_reply", lambda: brain.generate_reply("Bye!", history2, is_final_turn=True))
    record("preview_draft", lambda: brain.preview_response(
        ["IMG_A"], SCENE_1, 1, persona="menacing",
        draft={"shared_rules": "DRAFT RULES", "ollama.output_format": "DRAFT FORMAT",
               "context.line_instruction": "DRAFT LINE"}))
    return calls


def capture():
    sys.path.insert(0, HERE)
    sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=_FakeAnthropic)
    urllib.request.urlopen = _fake_urlopen
    import requests
    requests.post = _fake_requests_post

    import config
    # Fixed values, independent of any settings.json / .env on this machine
    config.ANTHROPIC_API_KEY = "test-key"
    config.AI_MODEL = "claude-test-model"
    config.AI_MAX_TOKENS = 60
    config.OLLAMA_BASE_URL = "http://ollama.test:11434"
    config.OLLAMA_MODEL = "llava"
    config.PERSONA = "creepy"
    config.LOG_CONVERSATIONS = False
    for k, v in {"OPENAI_COMPAT_BASE_URL": "https://compat.test/v1", "OPENAI_COMPAT_MODEL": "compat-model",
                 "OPENAI_COMPAT_API_KEY": "compat-key"}.items():
        setattr(config, k, v)

    import ai_brain
    import prompt_store
    ai_brain.PROMPT_STORE = prompt_store.PromptStore(
        ai_brain.PROMPT_DEFAULTS, ai_brain._FALLBACK_LINES,
        path=os.path.join(tempfile.mkdtemp(), "none.json"))
    ai_brain.ANTHROPIC_API_KEY = "test-key"
    ai_brain.datetime = _FrozenDatetime

    backends = ["claude", "ollama"]
    if "openai" in getattr(__import__("settings_store"), "SCHEMA_BY_KEY")["AI_BACKEND"]["choices"]:
        backends.append("openai")
    return {b: run_backend(ai_brain, config, b) for b in backends}


def main():
    got = capture()
    if "--update" in sys.argv:
        with open(GOLDEN, "w") as f:
            json.dump(got, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print(f"Recorded {sum(len(v) for v in got.values())} scenarios to {os.path.basename(GOLDEN)}")
        return
    want = json.load(open(GOLDEN))
    bad = 0
    for backend in sorted(set(want) | set(got)):
        for name in sorted(set(want.get(backend, {})) | set(got.get(backend, {}))):
            w, g = want.get(backend, {}).get(name), got.get(backend, {}).get(name)
            same = json.dumps(w, ensure_ascii=False) == json.dumps(g, ensure_ascii=False)
            print(f"  {'ok ' if same else 'DIFF'}  {backend}/{name}")
            if not same:
                bad += 1
                if w and g:
                    for i, (wr, gr) in enumerate(zip(w["requests"], g["requests"])):
                        if wr != gr:
                            for k in sorted(set(wr) | set(gr)):
                                if wr.get(k) != gr.get(k):
                                    print(f"        request {i} '{k}':\n          want {str(wr.get(k))[:300]}\n"
                                          f"          got  {str(gr.get(k))[:300]}")
                    if w["returned"] != g["returned"]:
                        print(f"        returned: want {w['returned']!r} got {g['returned']!r}")
    if bad:
        print(f"\n{bad} scenario(s) differ from the golden payloads.")
        sys.exit(1)
    print("\nAll LLM payloads match the golden file.")


if __name__ == "__main__":
    main()
