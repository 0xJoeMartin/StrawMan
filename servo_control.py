"""
Servo Control Module
--------------------
Manages the PCA9685 servo driver for head pan/tilt and jaw movement.
Provides smooth movement, audio-reactive jaw sync, and safe shutdown.
"""

import time
import threading
from collections import deque
import numpy as np
import config
from config import (
    PAN_CHANNEL, TILT_CHANNEL, JAW_CHANNEL, JAW_UPDATE_RATE,
)

# Degrees per 50 Hz step while calibrating (~50°/s) — slow enough to stop before a hard stop
CALIBRATION_SPEED = 1.0

try:
    from adafruit_servokit import ServoKit
    SERVOS_AVAILABLE = True
except (ImportError, NotImplementedError):
    SERVOS_AVAILABLE = False
    print("[servo_control] ServoKit not available — running in simulation mode")


class ServoController:
    """Controls pan, tilt, and jaw servos with smooth movement."""

    def __init__(self):
        self.kit = None
        if SERVOS_AVAILABLE:
            try:
                self.kit = ServoKit(channels=16)
                # Set pulse width ranges for typical hobby servos
                for ch in [PAN_CHANNEL, TILT_CHANNEL, JAW_CHANNEL]:
                    self.kit.servo[ch].set_pulse_width_range(500, 2500)
            except Exception as e:
                print(f"[servo_control] Failed to init PCA9685: {e}")
                print("[servo_control] Continuing in simulation mode")

        # Current positions
        self.pan_angle = config.PAN_CENTER
        self.tilt_angle = config.TILT_CENTER
        self.jaw_angle = config.JAW_CLOSED
        self._pose_log = deque(maxlen=500)  # (t, pan, tilt) each tracking step, ~10 s

        # Target positions (for smooth tracking)
        self.target_pan = config.PAN_CENTER
        self.speed_override = None   # temporary slow-move speed (deg/step); None = HEAD_TRACK_SPEED
        self.target_tilt = config.TILT_CENTER

        # Velocity estimation — degrees per second the target is moving
        self._vel_pan = 0.0
        self._vel_tilt = 0.0
        self._last_target_pan = config.PAN_CENTER
        self._last_target_tilt = config.TILT_CENTER
        self._last_target_time = time.time()

        # Jaw animation state
        self._jaw_active = False
        self._jaw_thread = None
        self._audio_amplitude = 0.0

        # Tracking thread
        self._tracking_active = False
        self._track_thread = None

        # Calibration (dashboard /calibrate): slow moves that may go past the saved limits
        self.calibrating = False

        # Initialize to center
        self._set_servo(PAN_CHANNEL, config.PAN_CENTER)
        self._set_servo(TILT_CHANNEL, config.TILT_CENTER)
        self._set_servo(JAW_CHANNEL, config.JAW_CLOSED)

        print("[servo_control] Initialized — servos centered")

    def _set_servo(self, channel: int, angle: float):
        """Set a servo to a specific angle."""
        angle = max(0, min(180, angle))
        if self.kit:
            try:
                self.kit.servo[channel].angle = angle
            except Exception as e:
                print(f"[servo_control] Error setting channel {channel}: {e}")

    def _clamp_pan(self, angle: float) -> float:
        return max(config.PAN_MIN, min(config.PAN_MAX, angle))

    def _clamp_tilt(self, angle: float) -> float:
        return max(config.TILT_MIN, min(config.TILT_MAX, angle))

    # --- Head Tracking ---

    def set_target(self, pan: float, tilt: float):
        """Set the target pan/tilt angles. The tracking loop will smoothly move toward them."""
        self.target_pan = self._clamp_pan(pan)
        self.target_tilt = self._clamp_tilt(tilt)

    def track_person(self, head_x: float, head_y: float,
                     frame_width: int, frame_height: int):
        """
        Proportional feedback loop with velocity prediction.

        Each detection frame: correct toward centering the face AND estimate
        how fast the target is moving. Between detections, the tracking loop
        continues moving at the predicted velocity so motion stays smooth.
        """
        # Error = how far off-center the face is, normalized to -0.5..+0.5
        error_x = (head_x / frame_width) - 0.5    # positive = face is right
        error_y = (head_y / frame_height) - 0.5    # positive = face is below

        # Proportional gain — degrees of correction per unit of error
        GAIN_PAN = 18.0
        GAIN_TILT = 14.0

        # Dead zone — don't correct if the face is close enough to center
        DEADZONE = 0.04

        # Calculate new target from error correction
        new_pan = self.pan_angle
        new_tilt = self.tilt_angle

        if abs(error_x) > DEADZONE:
            new_pan = self._clamp_pan(self.pan_angle - error_x * GAIN_PAN)

        if abs(error_y) > DEADZONE:
            new_tilt = self._clamp_tilt(self.tilt_angle + error_y * GAIN_TILT)

        # Estimate velocity (degrees per second) from how the target changed
        now = time.time()
        dt = now - self._last_target_time
        if dt > 0.01 and dt < 1.0:  # Sane range — ignore stale or tiny intervals
            raw_vel_pan = (new_pan - self._last_target_pan) / dt
            raw_vel_tilt = (new_tilt - self._last_target_tilt) / dt
            # Smooth the velocity estimate to avoid spikes
            self._vel_pan = self._vel_pan * 0.5 + raw_vel_pan * 0.5
            self._vel_tilt = self._vel_tilt * 0.5 + raw_vel_tilt * 0.5
        else:
            self._vel_pan = 0.0
            self._vel_tilt = 0.0

        self._last_target_pan = new_pan
        self._last_target_tilt = new_tilt
        self._last_target_time = now

        self.target_pan = self._clamp_pan(new_pan)
        self.target_tilt = self._clamp_tilt(new_tilt)

    def start_tracking_loop(self):
        """Start background thread that smoothly moves servos toward targets."""
        if self._tracking_active:
            return
        self._tracking_active = True
        self._track_thread = threading.Thread(target=self._tracking_loop, daemon=True)
        self._track_thread.start()

    def stop_tracking_loop(self):
        self._tracking_active = False
        if self._track_thread:
            self._track_thread.join(timeout=1.0)

    def _tracking_loop(self):
        """Smoothly interpolate current angles toward target angles."""
        last = time.time()
        while self._tracking_active:
            now = time.time()
            dt = min(max(now - last, 0.0), 0.1)  # real loop period (sleep + work), capped
            last = now
            # Slow choreographed moves set speed_override; it never touches the
            # configured HEAD_TRACK_SPEED, so overlapping slow moves can't leave
            # him stuck slow (they used to save/restore the global setting).
            base = self.speed_override if self.speed_override is not None else config.HEAD_TRACK_SPEED
            speed = min(base, CALIBRATION_SPEED) if self.calibrating else base
            # Per-axis caps in degrees per second, measured against real time, on
            # every move (tracking, idle lift/droop, glances, joystick,
            # calibration). Tilt carries the whole head's weight, so it's slow.
            pan_speed = min(speed, config.PAN_MAX_SPEED * dt) if config.PAN_MAX_SPEED > 0 else speed
            tilt_speed = min(speed, config.TILT_MAX_SPEED * dt) if config.TILT_MAX_SPEED > 0 else speed
            # Move pan toward target
            diff_pan = self.target_pan - self.pan_angle
            if abs(diff_pan) > 0.5:
                step = min(abs(diff_pan), pan_speed) * (1 if diff_pan > 0 else -1)
                self.pan_angle += step
                self._set_servo(PAN_CHANNEL, self.pan_angle)

            # Move tilt toward target
            diff_tilt = self.target_tilt - self.tilt_angle
            if abs(diff_tilt) > 0.5:
                step = min(abs(diff_tilt), tilt_speed) * (1 if diff_tilt > 0 else -1)
                self.tilt_angle += step
                self._set_servo(TILT_CHANNEL, self.tilt_angle)

            self._pose_log.append((time.time(), self.pan_angle, self.tilt_angle))
            time.sleep(0.02)  # ~50Hz update rate

    def head_still_for(self, seconds: float, tolerance: float = 8.0) -> bool:
        """True if pan and tilt each stayed within `tolerance` degrees over the
        last `seconds`. The radar is on his face, so after a real head turn its
        targets are stale or slide between objects for a couple of seconds."""
        cutoff = time.time() - seconds
        pts = [(p, t) for ts, p, t in list(self._pose_log) if ts >= cutoff]
        if not pts:
            return True
        pans = [p for p, _ in pts]
        tilts = [t for _, t in pts]
        return max(pans) - min(pans) <= tolerance and max(tilts) - min(tilts) <= tolerance

    # --- Calibration ---

    def calibrate_move(self, axis: str, angle: float) -> float:
        """Move one servo for calibration. Ignores the saved limits (that's what's
        being calibrated) but never leaves 0–180°. Pan/tilt glide at
        CALIBRATION_SPEED via the tracking loop; the jaw is set directly (it's
        never moved while he's speaking). Returns the angle actually used."""
        angle = float(max(0.0, min(180.0, angle)))
        if axis == "pan":
            self.target_pan = angle
        elif axis == "tilt":
            self.target_tilt = angle
        elif axis == "jaw":
            if self._jaw_active:
                raise RuntimeError("He's talking — try again in a moment")
            self.jaw_angle = angle
            self._set_servo(JAW_CHANNEL, angle)
        else:
            raise ValueError(f"Unknown servo {axis!r}")
        return angle

    # --- Jaw Animation ---

    def update_jaw_amplitude(self, amplitude: float):
        """Update the current audio amplitude for jaw sync. Call this from the audio thread."""
        self._audio_amplitude = amplitude

    def start_jaw_sync(self):
        """Start jaw movement synced to audio amplitude."""
        if self._jaw_active:
            return
        self._jaw_active = True
        self._jaw_thread = threading.Thread(target=self._jaw_loop, daemon=True)
        self._jaw_thread.start()

    def stop_jaw_sync(self):
        """Stop jaw movement and close mouth."""
        self._jaw_active = False
        if self._jaw_thread:
            self._jaw_thread.join(timeout=1.0)
        self._set_servo(JAW_CHANNEL, config.JAW_CLOSED)
        self.jaw_angle = config.JAW_CLOSED

    def _jaw_loop(self):
        """
        Map audio amplitude to jaw position with syllable-aware bouncing.

        Uses two signals combined:
        1. Amplitude envelope — controls overall openness (loud = wide open)
        2. Syllable bounce — detects amplitude dips between syllables and forces
           the jaw to close briefly, so it bounces open-close even during
           sustained loud speech.
        """
        prev_amp = 0.0
        smoothed = 0.0
        slow_avg = 0.0        # Slow-moving average to detect relative changes
        bounce_phase = 0.0    # Tracks the bounce state (0 = no bounce)
        jaw_range = config.JAW_OPEN - config.JAW_CLOSED

        while self._jaw_active:
            amp = self._audio_amplitude

            # Fast smoothing for responsiveness
            smoothed = smoothed * 0.5 + amp * 0.5

            # Slow average tracks the overall level
            slow_avg = slow_avg * 0.95 + amp * 0.05

            if smoothed > config.JAW_THRESHOLD:
                # --- Syllable detection ---
                # When amplitude drops below the slow average, we're in a
                # gap between syllables. When it rises back above, that's
                # a new syllable onset — trigger a bounce.
                amp_delta = smoothed - prev_amp

                if amp_delta > 0.02:
                    # Rising edge — new syllable starting, snap jaw open
                    bounce_phase = 1.0
                elif amp_delta < -0.02:
                    # Falling edge — between syllables, close toward shut
                    bounce_phase = max(bounce_phase - 0.3, 0.0)

                # Combine amplitude with bounce:
                # - Base position from amplitude (how loud = how open)
                # - Bounce multiplier forces periodic close-open cycles
                base_openness = min(smoothed * 1.5, 1.0)  # Boost quiet speech
                bounce_multiplier = 0.4 + 0.6 * bounce_phase  # Range: 0.4 to 1.0

                openness = base_openness * bounce_multiplier
                jaw_pos = config.JAW_CLOSED + openness * jaw_range
                jaw_pos = max(config.JAW_CLOSED, min(config.JAW_OPEN, jaw_pos))

                # Decay the bounce so it naturally closes between syllables
                bounce_phase = max(bounce_phase - 0.08, 0.0)
            else:
                jaw_pos = config.JAW_CLOSED
                bounce_phase = 0.0

            prev_amp = smoothed

            # Update servo if position changed enough to matter
            if abs(jaw_pos - self.jaw_angle) > 0.5:
                self.jaw_angle = jaw_pos
                self._set_servo(JAW_CHANNEL, jaw_pos)

            time.sleep(JAW_UPDATE_RATE)

    def center(self, slow=False):
        """Return to center position. If slow=True, temporarily reduce speed."""
        if slow:
            self.speed_override = 0.5
            try:
                self.set_target(config.PAN_CENTER, config.TILT_CENTER)
                # Wait until we're close to center (max 10 s), then back to normal speed
                deadline = time.time() + 10.0
                while (abs(self.pan_angle - config.PAN_CENTER) > 2.0 or
                       abs(self.tilt_angle - config.TILT_CENTER) > 2.0) and time.time() < deadline:
                    time.sleep(0.05)
            finally:
                self.speed_override = None
        else:
            self.set_target(config.PAN_CENTER, config.TILT_CENTER)

    # --- Cleanup ---

    def shutdown(self):
        """Stop all movement and center servos."""
        self.stop_jaw_sync()
        self.stop_tracking_loop()
        self._set_servo(PAN_CHANNEL, config.PAN_CENTER)
        self._set_servo(TILT_CHANNEL, config.TILT_CENTER)
        self._set_servo(JAW_CHANNEL, config.JAW_CLOSED)
        print("[servo_control] Shutdown complete — servos centered")


# --- Standalone test ---
if __name__ == "__main__":
    print("Testing servo control...")
    ctrl = ServoController()
    ctrl.start_tracking_loop()

    print("Centering...")
    ctrl.center()
    time.sleep(2)

    print("Panning left...")
    ctrl.set_target(config.PAN_MAX, config.TILT_CENTER)
    time.sleep(2)

    print("Panning right...")
    ctrl.set_target(config.PAN_MIN, config.TILT_CENTER)
    time.sleep(2)

    print("Centering...")
    ctrl.center()
    time.sleep(2)

    print("Tilting up...")
    ctrl.set_target(config.PAN_CENTER, config.TILT_MAX)
    time.sleep(2)

    print("Tilting down...")
    ctrl.set_target(config.PAN_CENTER, config.TILT_MIN)
    time.sleep(2)

    print("Testing jaw...")
    ctrl.start_jaw_sync()
    for i in range(50):
        # Simulate audio amplitude
        amp = abs(np.sin(i * 0.3)) * 0.8
        ctrl.update_jaw_amplitude(amp)
        time.sleep(0.05)
    ctrl.stop_jaw_sync()

    print("Centering and shutting down...")
    ctrl.shutdown()
    print("Done!")
