#!/usr/bin/env python3
"""
Which photos go to the AI — no camera, no network. (Needs the Pi's packages.)

    python test_photos.py

The picker must send frames where a person was actually detected, newest
included; replies get a fresh photo; payloads without one are unchanged.
"""

import time
import types

import numpy as np

import scarecrow


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'FAIL'}  {name}{'' if cond or not extra else '  ' + str(extra)}")
    if not cond:
        raise SystemExit(1)


def buffer(person_from: float, flicker=False, seconds=3.0, fps=30):
    """Frames for the last `seconds`; a person detected from `person_from` s ago."""
    now = time.time()
    out = []
    for i in range(int(seconds * fps)):
        t = now - seconds + i / fps
        age = now - t
        n = 1 if age <= person_from and not (flicker and i % 7 == 0) else 0
        out.append((np.zeros((4, 4, 3), np.uint8), t, n))
    return out


def main():
    sc = scarecrow.Scarecrow.__new__(scarecrow.Scarecrow)

    print("photo picker")
    sc._frame_buffer = buffer(person_from=0.17)            # just stepped into view
    p = sc._pick_context_entries(4)
    check("just arrived: one photo, the newest, with the person", len(p) == 1 and p[0] is sc._frame_buffer[-1] and p[0][2])

    sc._frame_buffer = buffer(person_from=2.0)             # walking up for 2 s
    p = sc._pick_context_entries(4)
    gaps = [b[1] - a[1] for a, b in zip(p, p[1:])]
    check("in view 2 s: 4 photos, all with the person", len(p) == 4 and all(e[2] for e in p), [e[2] for e in p])
    check("…oldest first, newest last", p[-1] is sc._frame_buffer[-1] and all(g > 0 for g in gaps))
    check("…spread out (≥ 0.25 s apart)", all(g >= scarecrow.MIN_PHOTO_SPACING - 1e-6 for g in gaps), gaps)
    check("…covering the time in view", time.time() - p[0][1] > 1.5)

    sc._frame_buffer = buffer(person_from=0.6)
    p = sc._pick_context_entries(4)
    check("in view 0.6 s: 2-3 photos, no near-duplicates", 2 <= len(p) <= 3 and all(e[2] for e in p), len(p))

    sc._frame_buffer = buffer(person_from=2.0, flicker=True)
    check("detector flicker: still only frames with the person", all(e[2] for e in sc._pick_context_entries(4)))

    sc._frame_buffer = buffer(person_from=-1)              # nobody detected at all
    p = sc._pick_context_entries(4)
    check("nobody detected: old even spread (4 frames)", len(p) == 4 and not any(e[2] for e in p))

    print("fresh photo for replies")
    sc._frame_buffer = buffer(person_from=2.0)
    f, age, n = sc._fresh_photo()
    check("newest frame with a person", n and age < 0.1)
    sc._frame_buffer = buffer(person_from=-1)
    f, age, n = sc._fresh_photo()
    check("nobody there: newest frame anyway", f is not None and not n)

    print("reply payloads")
    import config
    import ai_brain
    calls = []

    class Msgs:
        def create(self, **kw):
            calls.append(kw)
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Boo.")])

    config.AI_BACKEND, config.AI_MODEL, config.ANTHROPIC_API_KEY = "claude", "claude-test-model", "k"
    import sys
    sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda api_key=None: types.SimpleNamespace(messages=Msgs()))
    brain = ai_brain.ScarecrowBrain()
    hist = [{"role": "user", "content": "scene"}, {"role": "assistant", "content": "Hi."}]
    brain.generate_reply("Are you real?", hist, image_b64="FRESH")
    last = calls[-1]["messages"][-1]["content"]
    check("reply turn carries the fresh photo first", isinstance(last, list) and last[0]["source"]["data"] == "FRESH")
    check("…with the note and the reply instruction", ai_brain.FRESH_PHOTO_NOTE in last[1]["text"] and "Are you real?" in last[1]["text"])
    brain.generate_reply("Are you real?", hist)
    check("no photo -> plain text turn as before", isinstance(calls[-1]["messages"][-1]["content"], str))
    check("history isn't modified", len(hist) == 2)

    folded = brain._ollama_fold("sys", hist + [ai_brain.user_turn(["FRESH"], "Are you real?")])
    check("Ollama keeps its 'reply directly' reminder with the photo",
          "Reply directly to what they said" in folded[-1]["content"][-1]["text"] and folded[-1]["content"][0]["type"] == "image")

    print("\nAll photo checks passed.")


if __name__ == "__main__":
    main()
