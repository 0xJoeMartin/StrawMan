"""
HLK-LD2450 mmWave Radar Driver
-------------------------------
Reads UART frames from the LD2450 in a background thread, decodes up to 3
simultaneous targets, and applies a median filter to the X axis to handle
the radar's natural angular jitter (range is precise to mm, angle of arrival
typically ±5–10°).

Coordinate frame, validated empirically against this hardware/mounting:
  X positive = scarecrow's right
  X negative = scarecrow's left
  Y         = forward distance from sensor (mm), always positive in front
  Speed     = signed cm/s; negative = approaching scarecrow, positive = retreating

Frame format (from LD2450 datasheet):
  AA FF 03 00 [t1 8B] [t2 8B] [t3 8B] 55 CC      = 30 bytes total

Per-target slot (8 bytes):
  bytes 0-1: X coord (mm, sign-via-MSB encoding)
  bytes 2-3: Y coord (mm, sign-via-MSB)
  bytes 4-5: Speed   (cm/s, sign-via-MSB)
  bytes 6-7: Distance resolution (mm, unsigned)

Sign convention: high bit (0x8000) of the LE int16 marks positive; lower 15
bits are magnitude. Not standard two's complement.
"""

import time
import threading
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Optional

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    print("[radar] pyserial not available — radar disabled")

from config import (
    RADAR_ENABLED, RADAR_PORT, RADAR_BAUD,
    RADAR_X_FILTER_LEN, RADAR_TARGET_TIMEOUT,
)

FRAME_HEADER = b"\xaa\xff\x03\x00"
FRAME_FOOTER = b"\x55\xcc"
FRAME_LEN = 30
TARGET_BYTES = 8
NUM_TARGET_SLOTS = 3


def _decode_signed(byte_pair: bytes) -> int:
    """LD2450 sign convention: high bit = positive flag, low 15 bits = magnitude."""
    raw = byte_pair[0] | (byte_pair[1] << 8)
    magnitude = raw & 0x7FFF
    return magnitude if (raw & 0x8000) else -magnitude


@dataclass
class RadarTarget:
    """A single tracked person/object from the radar."""
    slot: int          # 0, 1, or 2 — radar's slot index
    x_mm: int          # lateral, signed (positive = scarecrow's right) — median-filtered
    y_mm: int          # forward distance from sensor, always positive in front
    speed_cms: int     # signed; negative = approaching scarecrow
    last_update: float # wall-clock timestamp of most recent update

    @property
    def range_mm(self) -> float:
        """Straight-line distance from sensor (combines x and y)."""
        return (self.x_mm ** 2 + self.y_mm ** 2) ** 0.5

    @property
    def side(self) -> str:
        """'left', 'right', or 'center' relative to the scarecrow."""
        if self.x_mm > 200:
            return "right"
        if self.x_mm < -200:
            return "left"
        return "center"


class Radar:
    """Background-threaded LD2450 reader with per-slot X smoothing.

    Use:
        radar = Radar()
        if radar.enabled:
            for t in radar.get_targets():
                print(t.y_mm, t.side, t.speed_cms)

    Graceful-degrades when pyserial is missing or the UART can't be opened —
    `enabled` will be False and all queries return empty/None. The scarecrow's
    other subsystems remain unaffected.
    """

    def __init__(self):
        self._enabled = False
        self._ser = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._x_history = [deque(maxlen=RADAR_X_FILTER_LEN) for _ in range(NUM_TARGET_SLOTS)]
        self._latest_targets: dict[int, RadarTarget] = {}
        self._frame_count = 0

        if not RADAR_ENABLED:
            print("[radar] disabled by config (RADAR_ENABLED=False)")
            return
        if not SERIAL_AVAILABLE:
            print("[radar] pyserial missing — radar disabled")
            return

        try:
            self._ser = serial.Serial(RADAR_PORT, RADAR_BAUD, timeout=0.5)
        except Exception as e:
            print(f"[radar] could not open {RADAR_PORT}: {e}")
            print("[radar] radar disabled — other subsystems unaffected")
            return

        self._enabled = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        print(f"[radar] running on {RADAR_PORT} at {RADAR_BAUD} baud")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def _read_loop(self):
        """Background thread: continuously read frames and update target snapshot."""
        while not self._stop.is_set():
            frame = self._read_frame()
            if frame is None:
                continue
            self._process_frame(frame)

    def _read_frame(self) -> Optional[bytes]:
        """Sync on header bytes, then read the rest of a 30-byte frame."""
        sync_pos = 0
        try:
            while not self._stop.is_set():
                b = self._ser.read(1)
                if not b:
                    return None
                if b[0] == FRAME_HEADER[sync_pos]:
                    sync_pos += 1
                    if sync_pos == len(FRAME_HEADER):
                        rest = self._ser.read(FRAME_LEN - len(FRAME_HEADER))
                        if len(rest) != FRAME_LEN - len(FRAME_HEADER):
                            return None
                        if not rest.endswith(FRAME_FOOTER):
                            sync_pos = 0
                            continue
                        return FRAME_HEADER + rest
                else:
                    sync_pos = 1 if b[0] == FRAME_HEADER[0] else 0
        except Exception:
            return None
        return None

    def _process_frame(self, frame: bytes):
        """Decode each target slot, apply X median filter, update snapshot."""
        now = time.time()
        new_targets = {}
        for slot in range(NUM_TARGET_SLOTS):
            offset = 4 + slot * TARGET_BYTES
            buf = frame[offset:offset + TARGET_BYTES]
            if buf == b"\x00" * 8:
                # Empty slot — drop any history so a target reappearing here gets a fresh filter
                self._x_history[slot].clear()
                continue

            x_raw = _decode_signed(buf[0:2])
            y = _decode_signed(buf[2:4])
            speed = _decode_signed(buf[4:6])

            self._x_history[slot].append(x_raw)
            x_smoothed = int(statistics.median(self._x_history[slot]))

            new_targets[slot] = RadarTarget(
                slot=slot,
                x_mm=x_smoothed,
                y_mm=y,
                speed_cms=speed,
                last_update=now,
            )

        with self._lock:
            self._latest_targets = new_targets
            self._frame_count += 1

    # --- Public query API ---

    def get_targets(self, max_age: Optional[float] = None) -> list[RadarTarget]:
        """Return current active targets, optionally filtered by max age (seconds)."""
        if not self._enabled:
            return []
        with self._lock:
            targets = list(self._latest_targets.values())
        if max_age is not None:
            now = time.time()
            targets = [t for t in targets if now - t.last_update <= max_age]
        return targets

    def closest_target(self) -> Optional[RadarTarget]:
        """The target with the smallest forward distance, or None."""
        targets = self.get_targets(max_age=RADAR_TARGET_TIMEOUT)
        if not targets:
            return None
        return min(targets, key=lambda t: t.y_mm)

    def shutdown(self):
        """Stop the background thread and close the UART."""
        if not self._enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            if self._ser is not None:
                self._ser.close()
        except Exception:
            pass
        print("[radar] shut down")


# --- Standalone test ---
if __name__ == "__main__":
    import sys

    radar = Radar()
    if not radar.enabled:
        print("Radar not enabled — exiting.")
        sys.exit(1)

    print("Radar running. Snapshots every 0.5s. Ctrl+C to stop.")
    print()
    try:
        while True:
            time.sleep(0.5)
            targets = radar.get_targets()
            if not targets:
                print(f"\r[{radar.frame_count} frames] (no targets)" + " " * 60,
                      end="", flush=True)
                continue
            lines = []
            for t in targets:
                direction = ("approaching" if t.speed_cms < -10
                             else "departing"   if t.speed_cms > 10
                             else "still")
                lines.append(
                    f"#{t.slot}: {t.y_mm}mm fwd, "
                    f"{abs(t.x_mm)}mm to my {t.side} "
                    f"({direction} @ {abs(t.speed_cms)}cm/s)"
                )
            print(f"\r[{radar.frame_count} frames] " + "  ".join(lines) + " " * 5,
                  end="", flush=True)
    except KeyboardInterrupt:
        print("\n\nStopped.")
        radar.shutdown()
