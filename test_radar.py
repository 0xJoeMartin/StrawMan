#!/usr/bin/env python3
"""
HLK-LD2450 mmWave radar diagnostic.
---------------------------------------------------------------
Opens the Pi's primary UART, syncs on radar frames, decodes up to
3 simultaneous targets, and prints their positions and speeds in
real time.

Use this to verify:
  1. Wiring is correct (data appears at all)
  2. Frames parse without sync errors
  3. Coordinate axes match physical reality (walk in known directions)
  4. Detection range / blind spots in your specific mounting

Run:
  python3 test_radar.py

If you get permission errors on /dev/ttyAMA0, make sure you've:
  - sudo raspi-config → Interfaces → Serial Port
    "No" to login shell, "Yes" to enable serial hardware
  - rebooted afterward
  - your user is in the `dialout` group:  sudo usermod -aG dialout $USER

Frame format (from LD2450 datasheet):
  AA FF 03 00 [t1 8B] [t2 8B] [t3 8B] 55 CC      = 30 bytes total

Each 8-byte target slot:
  bytes 0-1: X coord (mm, signed via MSB convention)
  bytes 2-3: Y coord (mm, signed)
  bytes 4-5: speed (cm/s, signed)
  bytes 6-7: distance resolution (mm, unsigned)

Sign convention: high bit (0x8000) of the LE int16 marks positive.
The lower 15 bits are the magnitude. (Not standard two's complement.)
"""

import sys
import time

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed.")
    print("Run:  pip install pyserial")
    sys.exit(1)


PORT = "/dev/ttyAMA0"   # primary UART on Pi 5 GPIO header
BAUD = 256000           # LD2450 default

FRAME_HEADER = b"\xaa\xff\x03\x00"
FRAME_FOOTER = b"\x55\xcc"
FRAME_LEN = 30
TARGET_BYTES = 8


def decode_signed(byte_pair: bytes) -> int:
    """Decode LD2450's quirky sign convention.

    The value is little-endian uint16. The high bit (0x8000) is a sign
    flag where 1 = positive, 0 = negative. The lower 15 bits are the
    magnitude. This is NOT standard two's complement.
    """
    raw = byte_pair[0] | (byte_pair[1] << 8)
    magnitude = raw & 0x7FFF
    return magnitude if (raw & 0x8000) else -magnitude


def decode_target(buf: bytes) -> dict | None:
    """Decode one 8-byte target slot. Returns None if the slot is empty."""
    if buf == b"\x00" * 8:
        return None
    return {
        "x_mm": decode_signed(buf[0:2]),       # lateral (sign convention TBD)
        "y_mm": decode_signed(buf[2:4]),       # forward distance from sensor
        "speed_cms": decode_signed(buf[4:6]),  # signed; sign convention TBD
        "dist_res_mm": buf[6] | (buf[7] << 8), # range resolution
    }


def find_frame(ser: serial.Serial) -> bytes | None:
    """Read bytes until the AA FF 03 00 header syncs, then read the rest of
    the frame. Returns 30 bytes on success, None on timeout/desync."""
    sync_pos = 0
    while True:
        b = ser.read(1)
        if not b:
            return None
        if b[0] == FRAME_HEADER[sync_pos]:
            sync_pos += 1
            if sync_pos == len(FRAME_HEADER):
                rest = ser.read(FRAME_LEN - len(FRAME_HEADER))
                if len(rest) != FRAME_LEN - len(FRAME_HEADER):
                    return None
                if not rest.endswith(FRAME_FOOTER):
                    sync_pos = 0
                    continue
                return FRAME_HEADER + rest
        else:
            # Restart the sync — but if the byte we just saw is the start
            # of a new header attempt, count it as the first match
            sync_pos = 1 if b[0] == FRAME_HEADER[0] else 0


def format_target_line(t: dict, slot: int, raw: bytes = b"") -> str:
    """One-line summary of a target for terminal display.

    If raw is supplied, also prints the 8 raw hex bytes for that slot — useful
    when you need to debug the decode logic by inspecting actual bytes.
    """
    range_mm = (t["x_mm"] ** 2 + t["y_mm"] ** 2) ** 0.5
    parts = [
        f"#{slot} ",
        f"x={t['x_mm']:>+5}mm y={t['y_mm']:>+5}mm ",
        f"range={range_mm:>5.0f}mm ",
        f"v={t['speed_cms']:>+4}cm/s",
    ]
    if raw:
        hex_str = " ".join(f"{b:02x}" for b in raw)
        parts.append(f"  raw=[{hex_str}]")
    return "".join(parts)


def main():
    # --raw flag enables hex byte dump alongside decoded values
    show_raw = "--raw" in sys.argv

    print(f"Opening {PORT} at {BAUD} baud...")
    if show_raw:
        print("Raw mode: each frame line will include the 8-byte target slot in hex.")
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.5)
    except serial.SerialException as e:
        print(f"ERROR: could not open {PORT}: {e}")
        print()
        print("Common causes:")
        print("  - Serial console still occupies the UART. Run:")
        print("      sudo raspi-config → Interfaces → Serial Port")
        print("      → 'No' to login shell, 'Yes' to enable serial hardware")
        print("      then reboot.")
        print("  - User not in 'dialout' group:")
        print("      sudo usermod -aG dialout $USER  (then log out/in)")
        sys.exit(1)

    print()
    print("Reading frames. Walk in front of the sensor — Ctrl+C to stop.")
    print()
    print("Coordinate frame (per datasheet — VERIFY by walking):")
    print("  X axis = lateral (left/right of sensor, sign TBD)")
    print("  Y axis = forward distance (positive = away from sensor)")
    print("  speed  = signed cm/s (sign convention TBD)")
    print()
    print("Sanity walks to do:")
    print("  - Stand directly in front, walk straight away → Y should grow")
    print("  - Walk back toward sensor       → Y shrinks, speed sign confirms approach")
    print("  - Stand to the LEFT of the sensor → note X sign")
    print("  - Stand to the RIGHT of the sensor → X sign should flip")
    print()
    print("=" * 76)

    frame_count = 0
    last_print = 0.0
    desync_count = 0
    start_time = time.time()

    while True:
        frame = find_frame(ser)
        if frame is None:
            desync_count += 1
            if desync_count % 10 == 0:
                print(f"\n[warning] {desync_count} desyncs/timeouts so far")
            continue

        frame_count += 1

        # Throttle terminal output to ~5 Hz so it stays readable
        now = time.time()
        if now - last_print < 0.2:
            continue
        last_print = now

        targets = []
        for slot in range(3):
            offset = 4 + slot * TARGET_BYTES
            slot_bytes = frame[offset:offset + TARGET_BYTES]
            t = decode_target(slot_bytes)
            if t is not None:
                targets.append((slot, t, slot_bytes))

        elapsed = now - start_time
        rate = frame_count / elapsed if elapsed > 0 else 0

        if not targets:
            print(f"\r[{rate:4.1f} fps] (no targets)" + " " * 60,
                  end="", flush=True)
        else:
            # Use a newline (not \r) when raw mode is on, so each line is
            # preserved for copy/paste during debugging.
            line = "  ".join(
                format_target_line(t, slot, slot_bytes if show_raw else b"")
                for slot, t, slot_bytes in targets
            )
            terminator = "\n" if show_raw else ""
            line_start = "" if show_raw else "\r"
            print(f"{line_start}[{rate:4.1f} fps] {line}" + " " * 5,
                  end=terminator, flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nStopped.")
