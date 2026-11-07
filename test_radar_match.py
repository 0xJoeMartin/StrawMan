"""Scene description: radar matched to people by direction, ignored after head turns,
'facing me' never paired with 'away', tracker motion looked up by bbox, head_still_for.
Run: venv/bin/python test_radar_match.py"""
import time
from collections import deque

import numpy as np

import config
import person_detector as pd
from person_tracker import PersonTracker
from radar import RadarTarget
from servo_control import ServoController

ok = True


def check(name, good, detail=""):
    global ok
    ok &= bool(good)
    print(f"{'PASS' if good else 'FAIL'}  {name}  {detail}")


W = config.CAMERA_WIDTH
config.CAMERA_HFOV_DEG = 60.0
config.RADAR_MATCH_DEGREES = 15.0


def det(cx, area=0.02, keypoints=None):
    w = 40
    return {"center": (cx, 240), "bbox": (cx - w, 140, cx + w, 340), "head": (cx, 160),
            "area_ratio": area, "confidence": 0.7, "keypoints": keypoints}


def tgt(slot, x, y, v):
    return RadarTarget(slot=slot, x_mm=x, y_mm=y, speed_cms=v, last_update=time.time())


class FakeRadar:
    enabled = True
    def __init__(self, targets):
        self.t = targets
    def get_targets(self, max_age=None):
        return self.t


def facing_kp():
    kp = np.zeros((17, 3), np.float32)
    for i, (x, y) in enumerate([(320, 150), (310, 140), (330, 140)]):  # nose, eyes
        kp[i] = (x, y, 0.9)
    return kp


D = pd.PersonDetector.__new__(pd.PersonDetector)

# Case seen on a real porch: one person far right; radar target straight ahead and very close (the pillar)
scene = D.describe_scene([det(600)], radar=FakeRadar([tgt(0, 50, 900, 40)]))
check("radar target in another direction isn't pinned on the person",
      "very close" not in scene and "walking away" not in scene, repr(scene))
check("radar note lists raw targets + no match", "s0 x+50 y900" in D.last_radar_note
      and "matched: none" in D.last_radar_note, D.last_radar_note)

# Same direction: radar used
scene = D.describe_scene([det(320)], radar=FakeRadar([tgt(0, 0, 1500, -50)]))
check("matching direction -> radar distance + motion", "very close" in scene and "walking toward me" in scene,
      repr(scene))

# Head just turned: radar ignored entirely
scene = D.describe_scene([det(320)], radar=FakeRadar([tgt(0, 0, 1500, 50)]), head_still=False)
check("head moved -> radar ignored", "very close" not in scene and "walking away" not in scene
      and "ignored (head moved)" in D.last_radar_note, repr(scene))

# Two people, two targets: each gets the one in its direction
# camera bearings: cx=100 -> -20.6°, cx=540 -> +20.6°; radar: atan2(-700,1900)=-20°, atan2(1200,3400)=+19°
scene = D.describe_scene([det(100), det(540)],
                         radar=FakeRadar([tgt(0, 1200, 3400, 0), tgt(1, -700, 1900, -50)]))
lines = scene.splitlines()
check("two people: left one gets the near target", "very close" in lines[1] and "walking toward" in lines[1],
      lines[1])
check("two people: right one gets the far target", "a few feet away" in lines[2] and "standing still" in lines[2],
      lines[2])

# Facing me + walking away -> motion dropped, facing kept
scene = D.describe_scene([det(320, keypoints=facing_kp())], radar=FakeRadar([tgt(0, 0, 1500, 50)]))
check("'facing me' drops 'walking away'", "facing me" in scene and "away" not in scene, repr(scene))
scene = D.describe_scene([det(320, keypoints=facing_kp())], radar=FakeRadar([tgt(0, 0, 1500, -50)]))
check("'facing me' keeps 'walking toward me'", "facing me" in scene and "walking toward me" in scene, repr(scene))

# Head-turn speeds (±24 cm/s in the sweep test) read as standing still
for v in (-24, 16, 24):
    check(f"{v:+d} cm/s -> standing still", D._radar_motion_desc(tgt(0, 0, 1500, v)) == "standing still")
check("+30 cm/s -> drifting away", D._radar_motion_desc(tgt(0, 0, 1500, 30)) == "drifting away")
check("+50 cm/s -> walking away", D._radar_motion_desc(tgt(0, 0, 1500, 50)) == "walking away")

# Radar on but no targets: camera estimate, not "out by the street"
scene = D.describe_scene([det(320, area=0.2)], radar=FakeRadar([]))
check("no radar targets -> camera distance", "very close" in scene and "street" not in scene, repr(scene))

# Tracker returns known tracks first, then new ones: motion must follow the bbox
import types
import person_tracker as ptm
clock = [time.time()]
ptm.time = types.SimpleNamespace(time=lambda: clock[0])
tr = PersonTracker()
for k in range(10):  # person A walks right across the frame for ~1 s
    a = det(100 + 30 * k)
    clock[0] += 0.1
    tr.update([a])
b = det(540)
clock[0] += 0.1
tracks = tr.update([b, a])   # B is new and listed first in detections; A's track comes first in tracks
check("tracker order differs from detection order", tracks[0].bbox is a["bbox"])
scene = D.describe_scene([b, a], tracks=tracks, tracker=tr)
lines = scene.splitlines()
check("walker's motion stays with the walker", "moving to my right" in lines[2] and "moving" not in lines[1],
      repr(lines[1:]))

# head_still_for
s = ServoController.__new__(ServoController)
s._pose_log = deque(maxlen=500)
now = time.time()
for k in range(150):  # 3 s still at 90°
    s._pose_log.append((now - 3 + k * 0.02, 90.0, 100.0))
check("still head -> still", s.head_still_for(3.0, 8.0))
s._pose_log.append((now, 60.0, 100.0))
check("30° turn just now -> not still", not s.head_still_for(3.0, 8.0))
s._pose_log.clear()
for k in range(150):  # small tracking corrections, ±3°
    s._pose_log.append((now - 3 + k * 0.02, 90.0 + 3 * np.sin(k / 10), 100.0))
check("small tracking corrections -> still", s.head_still_for(3.0, 8.0))

print("ALL PASS" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
