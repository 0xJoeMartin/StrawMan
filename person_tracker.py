"""
Person Tracker Module
---------------------
Tracks detected people across frames using bounding box position and overlap.
Assigns simple IDs and remembers who the scarecrow has already spoken to,
so it doesn't repeat itself at the same person standing on the porch.

This is a lightweight approach that doesn't require a separate camera or
complex multi-object tracking. It works by comparing new detections to
recently seen "tracks" using IoU (intersection over union).
"""

import time
from dataclasses import dataclass, field
import config
from config import (
    TRACK_IOU_THRESHOLD, TRACK_EXPIRE_SECONDS, TRACK_MAX_HISTORY,
)


@dataclass
class TrackedPerson:
    """A person being tracked across frames."""
    track_id: int
    bbox: tuple[int, int, int, int]     # (x1, y1, x2, y2)
    center: tuple[float, float]
    head: tuple[float, float]           # estimated head/face position
    area_ratio: float
    first_seen: float                    # timestamp
    last_seen: float                     # timestamp
    spoken_to: bool = False              # has the scarecrow addressed this person?
    frames_seen: int = 1
    center_history: list = field(default_factory=list)  # recent (cx, cy, timestamp) tuples


class PersonTracker:
    """
    Lightweight frame-to-frame person tracker using IoU (intersection over union).

    Each frame, new detections are matched against existing tracks:
    - If a detection overlaps enough with an existing track, it's the same person
    - If no match, it's a new person and gets a fresh ID
    - Tracks that haven't been seen recently expire and are removed
    """

    def __init__(self):
        self._tracks: dict[int, TrackedPerson] = {}
        self._next_id = 1
        self._spoken_ids: set[int] = set()  # IDs we've already talked to

        print("[tracker] Person tracker initialized")

    def update(self, detections: list[dict]) -> list[TrackedPerson]:
        """
        Match new detections to existing tracks and return updated track list.

        Args:
            detections: List of detection dicts from PersonDetector, each with
                        'bbox', 'center', 'confidence', 'area_ratio'

        Returns:
            List of TrackedPerson objects (both matched and new)
        """
        now = time.time()

        # Remove expired tracks (person left the scene)
        self._expire_tracks(now)

        # Match detections to existing tracks
        matched_track_ids = set()
        matched_det_indices = set()
        updated_tracks = []

        # Score all detection-track pairs by IoU, then greedily match best first
        pairs = []
        for det_idx, det in enumerate(detections):
            for track_id, track in self._tracks.items():
                iou = self._compute_iou(det["bbox"], track.bbox)
                if iou >= TRACK_IOU_THRESHOLD:
                    pairs.append((iou, det_idx, track_id))

        # Sort by IoU descending — match best overlaps first
        pairs.sort(key=lambda x: x[0], reverse=True)

        for iou, det_idx, track_id in pairs:
            if det_idx in matched_det_indices or track_id in matched_track_ids:
                continue  # Already matched

            # Update existing track with new detection position
            det = detections[det_idx]
            track = self._tracks[track_id]
            track.bbox = det["bbox"]
            track.center = det["center"]
            track.head = det["head"]
            track.area_ratio = det["area_ratio"]
            track.last_seen = now
            track.frames_seen += 1
            track.center_history.append((det["center"][0], det["center"][1], now))
            # Keep only last 3 seconds of history
            track.center_history = [
                h for h in track.center_history if now - h[2] < 3.0
            ]

            matched_track_ids.add(track_id)
            matched_det_indices.add(det_idx)
            updated_tracks.append(track)

        # Create new tracks for unmatched detections
        for det_idx, det in enumerate(detections):
            if det_idx in matched_det_indices:
                continue

            new_track = TrackedPerson(
                track_id=self._next_id,
                bbox=det["bbox"],
                center=det["center"],
                head=det["head"],
                area_ratio=det["area_ratio"],
                first_seen=now,
                last_seen=now,
                center_history=[(det["center"][0], det["center"][1], now)],
            )
            self._tracks[self._next_id] = new_track
            updated_tracks.append(new_track)
            if config.DEBUG:
                print(f"[tracker] NEW track #{self._next_id} "
                      f"conf={det.get('confidence', 0):.0%} area={det['area_ratio']:.1%} "
                      f"center=({det['center'][0]:.0f},{det['center'][1]:.0f}) "
                      f"spoken_ids={self._spoken_ids}")
            self._next_id += 1

        return updated_tracks

    def get_new_people(self, tracks: list[TrackedPerson]) -> list[TrackedPerson]:
        """
        Filter tracks to only people the scarecrow hasn't spoken to yet.
        These are the ones that should trigger a new interaction.
        """
        return [t for t in tracks if t.track_id not in self._spoken_ids]

    def mark_spoken_to(self, track_ids: list[int]):
        """Mark these track IDs as already spoken to."""
        for tid in track_ids:
            self._spoken_ids.add(tid)
            if tid in self._tracks:
                self._tracks[tid].spoken_to = True
                if config.DEBUG:
                    print(f"[tracker] Marked #{tid} spoken_to=True (track alive)")
            else:
                if config.DEBUG:
                    print(f"[tracker] Marked #{tid} in spoken_ids but TRACK ALREADY EXPIRED")
        if config.DEBUG:
            print(f"[tracker] spoken_ids now: {self._spoken_ids}")
        elif track_ids:
            print(f"[tracker] Marked as spoken to: {track_ids}")

    def get_active_tracks(self) -> list[TrackedPerson]:
        """Return all currently active (non-expired) tracks."""
        return list(self._tracks.values())

    def get_closest_new_person(self, tracks: list[TrackedPerson]) -> TrackedPerson | None:
        """Get the closest (largest bounding box) person who hasn't been spoken to."""
        new_people = self.get_new_people(tracks)
        if not new_people:
            return None
        return max(new_people, key=lambda t: t.area_ratio)

    def describe_motion(self, track: TrackedPerson) -> str:
        """
        Describe a person's motion in natural language from the scarecrow's perspective.
        Returns empty string if not enough data.
        """
        history = track.center_history
        if len(history) < 5:
            return ""

        # Compare first and last positions
        first_x, first_y, first_t = history[0]
        last_x, last_y, last_t = history[-1]
        dt = last_t - first_t
        if dt < 0.3:
            return ""

        dx = last_x - first_x

        # Horizontal movement (in pixels per second)
        x_speed = abs(dx) / dt

        # Horizontal direction
        if x_speed < 15:
            h_desc = ""
        elif dx > 0:
            h_desc = "moving to my right"
        else:
            h_desc = "moving to my left"

        # Approaching or retreating: we don't store area history, so use the
        # y position (lower in the frame = closer in a typical camera setup)
        if len(history) >= 8:
            early_y = sum(h[1] for h in history[:3]) / 3
            late_y = sum(h[1] for h in history[-3:]) / 3
            y_change = late_y - early_y
            if y_change > 20:
                d_desc = "getting closer"
            elif y_change < -20:
                d_desc = "backing away"
            else:
                d_desc = ""
        else:
            d_desc = ""

        # Standing still
        if not h_desc and not d_desc:
            if track.frames_seen > 15:
                return "standing still, watching"
            return ""

        parts = [p for p in [h_desc, d_desc] if p]
        return ", ".join(parts)

    def _expire_tracks(self, now: float):
        """Remove tracks that haven't been seen recently."""
        expired = [
            tid for tid, track in self._tracks.items()
            if now - track.last_seen > TRACK_EXPIRE_SECONDS
        ]
        for tid in expired:
            track = self._tracks.pop(tid)
            if config.DEBUG:
                age = now - track.first_seen
                print(f"[tracker] EXPIRED track #{tid} — spoken_to={track.spoken_to} "
                      f"frames={track.frames_seen} age={age:.1f}s area={track.area_ratio:.1%}")
            self._spoken_ids.discard(tid)
            if config.DEBUG:
                print(f"[tracker] Removed #{tid} from spoken_ids → {self._spoken_ids}")

        # Cap total spoken history to prevent memory growth over a long night
        if len(self._spoken_ids) > TRACK_MAX_HISTORY:
            self._spoken_ids = set(sorted(self._spoken_ids)[-TRACK_MAX_HISTORY:])

    @staticmethod
    def _compute_iou(bbox_a: tuple, bbox_b: tuple) -> float:
        """
        Compute Intersection over Union between two bounding boxes.
        Each bbox is (x1, y1, x2, y2).
        """
        x1 = max(bbox_a[0], bbox_b[0])
        y1 = max(bbox_a[1], bbox_b[1])
        x2 = min(bbox_a[2], bbox_b[2])
        y2 = min(bbox_a[3], bbox_b[3])

        intersection = max(0, x2 - x1) * max(0, y2 - y1)
        if intersection == 0:
            return 0.0

        area_a = (bbox_a[2] - bbox_a[0]) * (bbox_a[3] - bbox_a[1])
        area_b = (bbox_b[2] - bbox_b[0]) * (bbox_b[3] - bbox_b[1])
        union = area_a + area_b - intersection

        return intersection / union if union > 0 else 0.0

    def debug_status(self) -> str:
        """Return a human-readable status string."""
        active = len(self._tracks)
        spoken = len(self._spoken_ids)
        tracks = []
        for t in self._tracks.values():
            status = "SPOKEN" if t.track_id in self._spoken_ids else "NEW"
            tracks.append(f"  #{t.track_id}: {status}, seen {t.frames_seen}x, area {t.area_ratio:.1%}")
        lines = [f"[tracker] Active: {active}, Spoken to: {spoken}"]
        lines.extend(tracks)
        return "\n".join(lines)


# --- Standalone test ---
if __name__ == "__main__":
    print("Testing person tracker with simulated detections...\n")
    tracker = PersonTracker()

    # Simulate a person walking across the frame over several frames
    print("--- Person walks in from the left ---")
    for i in range(5):
        x_offset = i * 40
        fake_detections = [{
            "bbox": (50 + x_offset, 100, 200 + x_offset, 400),
            "center": (125.0 + x_offset, 250.0),
            "head": (125.0 + x_offset, 145.0),  # 15% down from top of bbox
            "confidence": 0.85,
            "area_ratio": 0.12,
        }]
        tracks = tracker.update(fake_detections)
        new_people = tracker.get_new_people(tracks)
        print(f"  Frame {i}: {len(tracks)} tracked, {len(new_people)} new")

    # Mark person as spoken to
    tracker.mark_spoken_to([1])
    print(f"\n{tracker.debug_status()}")

    # Same person still there
    print("\n--- Same person still standing there ---")
    tracks = tracker.update([{
        "bbox": (250, 100, 400, 400),
        "center": (325.0, 250.0),
        "head": (325.0, 145.0),
        "confidence": 0.85,
        "area_ratio": 0.12,
    }])
    new_people = tracker.get_new_people(tracks)
    print(f"  Tracked: {len(tracks)}, New (should be 0): {len(new_people)}")

    # Second person arrives
    print("\n--- Second person arrives ---")
    tracks = tracker.update([
        {"bbox": (250, 100, 400, 400), "center": (325.0, 250.0), "head": (325.0, 145.0), "confidence": 0.85, "area_ratio": 0.12},
        {"bbox": (450, 120, 580, 380), "center": (515.0, 250.0), "head": (515.0, 159.0), "confidence": 0.78, "area_ratio": 0.08},
    ])
    new_people = tracker.get_new_people(tracks)
    print(f"  Tracked: {len(tracks)}, New (should be 1): {len(new_people)}")

    print(f"\n{tracker.debug_status()}")
    print("\nDone!")
