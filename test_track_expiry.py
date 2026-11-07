"""Tracks expire when the scene is empty. Run: venv/bin/python test_track_expiry.py"""
import config, person_tracker
from person_tracker import PersonTracker
clock = [1000.0]; person_tracker.time.time = lambda: clock[0]
tr = PersonTracker()
det = {"bbox": (100, 100, 200, 300), "center": (150, 200), "head": (150, 130), "confidence": 0.8, "area_ratio": 0.06}
for _ in range(20): clock[0] += 0.1; tr.update([det])
n0 = len(tr.get_active_tracks())
clock[0] += config.TRACK_EXPIRE_SECONDS - 1; tr.update([]); n1 = len(tr.get_active_tracks())
clock[0] += 2; tr.update([]); n2 = len(tr.get_active_tracks())
ok = (n0, n1, n2) == (1, 1, 0)
print(f"{'PASS' if ok else 'FAIL'}  active: while seen={n0}, {config.TRACK_EXPIRE_SECONDS-1:.0f}s after={n1}, {config.TRACK_EXPIRE_SECONDS+1:.0f}s after={n2}")
raise SystemExit(0 if ok else 1)
