#!/usr/bin/env python3
"""
Scarecrow — Main Control Script
================================
The Talking Halloween Scarecrow.

Ties together person detection, AI dialogue generation, text-to-speech,
servo head tracking, and jaw animation into one spooky package.

Usage:
    python scarecrow.py              # Normal operation
    python scarecrow.py --debug      # Show camera feed with detection boxes
    python scarecrow.py --test-voice # Test TTS without camera
    python scarecrow.py --scare high # Override scare level

Ctrl+C to stop.
"""

import os
import sys
import time
import signal
import random
import argparse
import threading
from datetime import datetime

# Suppress noisy ALSA/JACK warnings that flood stderr when PyAudio initializes
os.environ["JACK_NO_START_SERVER"] = "1"
try:
    import ctypes
    _ERROR_HANDLER = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                      ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
    def _alsa_noop(filename, line, function, err, fmt):
        pass
    _alsa_error_handler = _ERROR_HANDLER(_alsa_noop)
    ctypes.cdll.LoadLibrary('libasound.so.2').snd_lib_error_set_handler(_alsa_error_handler)
except Exception:
    pass  # Not on Linux or ALSA not available — no problem

import config
from person_detector import PersonDetector
from person_tracker import PersonTracker
from servo_control import ServoController
from ai_brain import ScarecrowBrain
from speech import SpeechEngine
from web_dashboard import update_frame, start_web_server, register_scarecrow, log_event
from listener import Listener
from voice_watch import VoiceWatcher
from radar import Radar


AI_PHOTOS_KEEP = 30  # conversations' worth of saved AI photos (when saving is on)
MIN_PHOTO_SPACING = 0.25  # s between photos sent together (closer ones are near-identical)
VOICE_SKIP_LOG_SECONDS = 30  # "heard voices, not glancing (why)" repeats the same reason at most this often


class Scarecrow:
    """Main scarecrow controller — orchestrates all subsystems."""

    def __init__(self, debug: bool = False):
        self.debug = debug
        self.running = False
        self._interaction_lock = threading.Lock()
        self._last_detection_time = time.time()
        self._returned_to_center = False
        self._last_interaction_time = 0
        self._manual_mode = False  # Dashboard manual servo control suspends idle behavior
        self._frame_buffer = []        # recent (frame, timestamp, people detected) tuples
        self._frame_buffer_max = 3.0   # seconds of frames to keep
        self._tracking_target_id = None  # sticky head tracking — current person's track ID

        print("=" * 50)
        print("  THE TALKING SCARECROW")
        print(f"  Persona: {config.PERSONA}")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 50)
        print()

        # Initialize subsystems
        print("Initializing subsystems...\n")

        print("[1/7] Person detector...")
        self.detector = PersonDetector()

        print("[2/7] Person tracker...")
        self.tracker = PersonTracker()

        print("[3/7] Servo controller...")
        self.servos = ServoController()

        print("[4/7] AI brain...")
        self.brain = ScarecrowBrain()

        print("[5/7] Speech engine...")
        self.speech = SpeechEngine(on_amplitude=self.servos.update_jaw_amplitude)

        print("[6/7] Listener (microphone)...")
        self.listener = Listener() if config.LISTEN_ENABLED else None
        if not config.LISTEN_ENABLED:
            print("  (disabled in config)")
        # Voice look: an idle ear on the same mic (pauses itself while he listens)
        self._glance_until = 0.0
        self._last_glance_time = 0.0
        self._voice_skip_logged = (None, 0.0)  # (reason, time) of the last "not glancing" line
        self.voice_watch = None
        if self.listener:
            self.voice_watch = VoiceWatcher(self.listener, self._on_voice_heard,
                                            is_muted=lambda: self.speech.is_speaking)

        print("[7/7] Radar (mmWave)...")
        self.radar = Radar()

        # Start web dashboard and register subsystems
        start_web_server(port=5000)
        register_scarecrow(self)

        print("\nAll systems ready.\n")

    def _is_quiet_hours(self) -> bool:
        """Check if current time falls within quiet hours."""
        if not config.QUIET_HOURS_ENABLED:
            return False
        hour = datetime.now().hour
        start = config.QUIET_HOURS_START
        end = config.QUIET_HOURS_END
        if start > end:
            # Wraps past midnight (e.g. 23–7)
            return hour >= start or hour < end
        else:
            return start <= hour < end

    def _update_quiet_hours(self):
        """Adjust speech volume based on quiet hours."""
        quiet = self._is_quiet_hours()
        target_vol = config.QUIET_HOURS_VOLUME if quiet else 1.0
        if self.speech.volume != target_vol:
            self.speech.volume = target_vol
            mode = "whisper" if quiet else "normal"
            print(f"[scarecrow] Volume → {target_vol:.0%} ({mode} mode)")
            log_event("SYSTEM", f"Switched to {mode} mode (volume {target_vol:.0%})")

    def run(self):
        """Main loop — scan for people, interact, repeat."""
        self.running = True
        self.servos.start_tracking_loop()

        print("Scarecrow is ACTIVE — watching for trick-or-treaters...")
        print("Press Ctrl+C to stop.\n")

        # Start with a slow idle scan
        idle_thread = threading.Thread(target=self._idle_behavior, daemon=True)
        idle_thread.start()
        if self.voice_watch:
            self.voice_watch.start()

        # An install that already works is marked set up — after the camera has
        # had time to deliver frames, so a working scarecrow is never kept quiet
        def _check_setup():
            time.sleep(20)
            try:
                import readiness
                readiness.auto_mark_done()
                if not config.SETUP_DONE:
                    print("[setup] Not set up yet — open the dashboard to finish setup (/welcome)")
                    log_event("SYSTEM", "Setup isn't finished — he won't talk to visitors until it is")
            except Exception as e:
                print(f"[setup] readiness check failed: {e}")
        threading.Thread(target=_check_setup, daemon=True).start()

        try:
            while self.running:
                self._update_quiet_hours()
                triggered, detections, frame = self.detector.check_for_people()

                # Update the tracker with new detections (every frame, even if
                # not triggered yet — this builds up tracking history)
                # Update every frame, even with nobody in view, so tracks expire
                # (TRACK_EXPIRE_SECONDS) instead of lingering on the overlay.
                tracks = self.tracker.update(detections or [])

                # If people are detected, point the head at someone.
                # "Sticky" tracking: once locked onto a person, stay with them
                # until they leave. Only switch if the current target is gone
                # or a significantly closer NEW person appears.
                # Only consider tracks seen enough frames to be credible.
                credible = [t for t in tracks if t.frames_seen >= config.TRACK_MIN_FRAMES]
                # Manual mode (dashboard joystick / calibration) owns the head:
                # no following people and no drifting back to center
                if credible and not self._manual_mode:
                    credible_by_id = {t.track_id: t for t in credible}

                    # Check if current target is still around
                    current_target = credible_by_id.get(self._tracking_target_id)

                    if current_target is None:
                        # Current target is gone — pick a new one
                        new_person = self.tracker.get_closest_new_person(credible)
                        if new_person:
                            target = new_person
                        else:
                            target = max(credible, key=lambda t: t.area_ratio)
                        self._tracking_target_id = target.track_id
                    else:
                        # Current target still here — stick with them unless
                        # a new (unspoken) person is MUCH closer (2x the area)
                        target = current_target
                        new_person = self.tracker.get_closest_new_person(credible)
                        if new_person and new_person.track_id != target.track_id:
                            if new_person.area_ratio > target.area_ratio * 2.0:
                                target = new_person
                                self._tracking_target_id = target.track_id

                    self.servos.track_person(
                        target.head[0], target.head[1],
                        config.CAMERA_WIDTH, config.CAMERA_HEIGHT,
                    )
                    self._last_detection_time = time.time()
                    self._returned_to_center = False
                elif (not self._manual_mode and not self._returned_to_center
                      and not self._glance_active()
                      and time.time() - self._last_detection_time > 3.0):
                    # Nobody detected for 3 seconds — drift back to center once
                    self.servos.center(slow=True)
                    self._returned_to_center = True

                # Buffer frames for multi-frame context
                if frame is not None:
                    now = time.time()
                    self._frame_buffer.append((frame.copy(), now, len(detections or [])))
                    # Trim buffer to last N seconds
                    self._frame_buffer = [
                        (f, t, n) for f, t, n in self._frame_buffer
                        if now - t < self._frame_buffer_max
                    ]

                # Update web debug view with annotated frame
                if frame is not None:
                    update_frame(frame, detections, tracks, self.tracker)

                # Check if there's a NEW person to interact with
                if triggered and self._can_interact():
                    new_person = self.tracker.get_closest_new_person(tracks)
                    if new_person is not None:
                        if config.DEBUG:
                            for d in detections:
                                print(f"[scarecrow] Detection: conf={d['confidence']:.0%} "
                                      f"area={d['area_ratio']:.1%} center=({d['center'][0]:.0f},{d['center'][1]:.0f})")
                        self.detector.reset_detection_count()
                        if config.DEBUG:
                            print(f"[scarecrow] TRIGGER: new_person=#{new_person.track_id} "
                                  f"active_tracks={[t.track_id for t in tracks]} "
                                  f"spoken_ids={self.tracker._spoken_ids}")
                        # Grab a spread of frames from the buffer for context
                        picked = self._pick_context_entries(4)
                        buffered_frames = [f for f, t, n in picked]
                        # Age (s before this trigger) + people detected, for the photo log line
                        trigger_t = time.time()
                        photo_info = [(round(trigger_t - t, 2), n) for f, t, n in picked]
                        # Track IDs to mark as spoken after we know what was said
                        all_track_ids = [t.track_id for t in tracks]
                        # Run interaction in a separate thread
                        interaction_thread = threading.Thread(
                            target=self._interact,
                            args=(detections, frame, tracks, buffered_frames,
                                  all_track_ids, photo_info, trigger_t),
                            daemon=True,
                        )
                        interaction_thread.start()

                time.sleep(config.SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("\n\nShutting down...")

        self.shutdown()

    def _pick_context_entries(self, count: int) -> list:
        """(frame, timestamp, people detected) for the photos sent to the AI.

        Only frames where a person was detected, spread over the time they've
        been in view and always ending with the newest one. Someone who just
        stepped into view gets one photo (a few near-identical frames add
        nothing); someone who's been there a while gets up to `count`, which
        shows how they're moving. With no detected person in the buffer, fall
        back to evenly spaced frames from the whole buffer.
        """
        buf = self._frame_buffer
        seen = [e for e in buf if e[2] > 0]
        if not seen:
            if len(buf) <= count:
                return list(buf)
            step = len(buf) / count
            return [buf[int(i * step)] for i in range(count)]
        newest_t = seen[-1][1]
        span = newest_t - seen[0][1]
        if span < MIN_PHOTO_SPACING or count == 1:
            return [seen[-1]]
        picks = []
        for k in range(count - 1, -1, -1):          # oldest target first, newest last
            target = newest_t - span * k / (count - 1)
            entry = min(seen, key=lambda e: abs(e[1] - target))
            if not picks or entry[1] - picks[-1][1] >= MIN_PHOTO_SPACING:
                picks.append(entry)
        if picks[-1] is not seen[-1]:
            picks[-1] = seen[-1]
        return picks

    def _fresh_photo(self, max_age: float = 1.0):
        """Newest frame with a person in it (from the last `max_age` s), else the
        newest frame — for replies, so he reacts to how they look right now."""
        buf = list(self._frame_buffer)
        if not buf:
            return None, None, 0
        now = time.time()
        for f, t, n in reversed(buf):
            if n and now - t <= max_age:
                return f, now - t, n
        f, t, n = buf[-1]
        return f, now - t, n

    def _log_photos(self, images_b64, photo_info, trigger_t, scene_desc):
        """One journal line about the photos going to the AI (always), and the
        photos themselves when Settings → Logging → Save the photos sent to the
        AI is on (kept for the last AI_PHOTOS_KEEP conversations)."""
        info = photo_info or []
        waited = time.time() - trigger_t if trigger_t else 0.0
        seen = sum(1 for age, n in info if n)
        print(f"[photos] {len(images_b64)} sent to the AI {waited:.1f}s after the trigger; taken "
              f"{', '.join(f'{age:.2f}' for age, n in info)}s before it; person detected in {seen}/{len(info)}")
        if not config.SAVE_AI_PHOTOS:
            return
        try:
            import base64, json, shutil
            root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_photos")
            folder = os.path.join(root, datetime.now().strftime("%Y%m%d-%H%M%S"))
            os.makedirs(folder, exist_ok=True)
            photos = []
            for i, b64 in enumerate(images_b64, 1):
                name = f"photo-{i}.jpg"
                with open(os.path.join(folder, name), "wb") as f:
                    f.write(base64.b64decode(b64))
                age, n = info[i - 1] if i - 1 < len(info) else (None, None)
                photos.append({"file": name, "seconds_before_trigger": age, "people_detected": n})
            with open(os.path.join(folder, "info.json"), "w") as f:
                json.dump({"scene": scene_desc, "sent_after_trigger_s": round(waited, 2), "photos": photos}, f, indent=2)
            for old in sorted(os.listdir(root))[:-AI_PHOTOS_KEEP]:
                shutil.rmtree(os.path.join(root, old), ignore_errors=True)
        except Exception as e:
            print(f"[photos] couldn't save: {e}")

    def _can_interact(self) -> bool:
        """Check if enough time has passed since the last interaction."""
        if not config.SETUP_DONE:
            return False  # Fresh install: finish the setup wizard (/welcome) first
        if self._manual_mode:
            return False  # Manual servo control via dashboard — don't auto-engage
        if self._interaction_lock.locked():
            return False
        if self.speech.is_speaking:
            return False
        now = time.time()
        if now - self._last_interaction_time < config.COOLDOWN_SECONDS:
            return False
        return True

    def _interact(self, detections: list[dict], frame, tracks=None,
                  context_frames=None, all_track_ids=None, photo_info=None, trigger_t=None):
        """Handle a full interaction: AI generation → speech → jaw animation."""
        try:
            with self._interaction_lock:
                self._last_interaction_time = time.time()

                num_people = len(detections)
                scene_desc = self.detector.describe_scene(
                    detections, tracks=tracks, tracker=self.tracker,
                    radar=self.radar,
                    head_still=self.servos.head_still_for(config.RADAR_SETTLE_SECONDS,
                                                          config.RADAR_SETTLE_DEGREES),
                )

                print(f"\n{'=' * 40}")
                print(f"INTERACTION #{self.brain.conversation_count + 1}")
                print(f"People: {num_people}")
                print(f"Scene: {scene_desc}")
                if getattr(self.detector, "last_radar_note", ""):
                    print(self.detector.last_radar_note)
                print(f"{'=' * 40}")

                # Ask the AI now, in the background, so an optional filler line
                # can play while it thinks instead of before it starts
                frames_to_send = context_frames if context_frames else [frame]
                ai = {}

                def ask_ai():
                    try:
                        ai["images"] = [self.detector.frame_to_base64(f) for f in frames_to_send]
                        self._log_photos(ai["images"], photo_info, trigger_t, scene_desc)
                        ai["asked_at"] = time.time()
                        ai["line"] = self.brain.generate_response(ai["images"], scene_desc, num_people)
                    except Exception as e:
                        ai["error"] = e
                    ai["done_at"] = time.time()

                ai_thread = threading.Thread(target=ask_ai, daemon=True)
                ai_thread.start()

                if random.random() < 0.3:
                    filler = random.choice(config.FILLER_LINES)
                    print(f"[filler] {filler}")
                    self.servos.start_jaw_sync()
                    self.speech.speak(filler, blocking=True)
                    self.servos.stop_jaw_sync()
                    time.sleep(0.5)
                ai_thread.join()

                # Get the AI-generated line — multiple frames for motion context
                try:
                    if "error" in ai:
                        raise ai["error"]
                    images_b64, line = ai["images"], ai["line"]
                    if trigger_t:
                        print(f"[timing] AI asked {ai['asked_at'] - trigger_t:.2f}s after the trigger, "
                              f"answered in {ai['done_at'] - ai['asked_at']:.1f}s; "
                              f"line ready to speak {time.time() - trigger_t:.1f}s after the trigger")
                    # Build user_content for conversation history (mirrors what
                    # generate_response sends to the API, so replies have full context)
                    user_content = []
                    for img_b64 in images_b64:
                        user_content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": img_b64,
                            },
                        })
                    user_content.append({
                        "type": "text",
                        "text": self.brain._build_context(
                            scene_desc, num_people,
                            num_frames=len(images_b64),
                        ),
                    })
                except Exception as e:
                    print(f"[scarecrow] AI/encoding error: {e}")
                    line = self.brain._get_fallback_line(num_people)
                    user_content = None  # No history available for fallback

                print(f"\nSCARECROW SAYS: \"{line}\"\n")
                log_event("AI", line, scene=scene_desc)

                # Speak it with jaw sync
                self.servos.start_jaw_sync()
                self.speech.speak(line, blocking=True)
                self.servos.stop_jaw_sync()

                # --- Multi-turn conversation loop ---
                if self.listener and config.LISTEN_ENABLED and user_content:
                    # Build conversation history starting from the initial exchange
                    # (mirrors the message format used by generate_response)
                    conversation_history = [
                        {"role": "user", "content": user_content},
                        {"role": "assistant", "content": line},
                    ]

                    for turn in range(config.MAX_CONVERSATION_TURNS):
                        is_final_turn = (turn == config.MAX_CONVERSATION_TURNS - 1)

                        time.sleep(config.LISTEN_POST_SPEAK_DELAY)  # Let speaker audio decay
                        person_said = self.listener.listen_for_response()

                        if not person_said:
                            # They didn't say anything — taunt or move on
                            if turn == 0 and config.SILENCE_TAUNTS:
                                taunt = random.choice(config.SILENCE_TAUNTS)
                                print(f"[scarecrow] No response — taunt: \"{taunt}\"")
                                log_event("TAUNT", taunt)
                                self.servos.start_jaw_sync()
                                self.speech.speak(taunt, blocking=True)
                                self.servos.stop_jaw_sync()
                            else:
                                print("[scarecrow] No response heard — moving on")
                            break

                        print(f"\nPERSON SAID (turn {turn + 1}): \"{person_said}\"")
                        log_event("PERSON", person_said)

                        # Generate a reply with full conversation context, plus a
                        # fresh photo so he reacts to how they look right now
                        fresh, age, n = self._fresh_photo()
                        fresh_b64 = self.detector.frame_to_base64(fresh) if fresh is not None else None
                        if fresh_b64:
                            print(f"[photos] reply {turn + 1}: fresh photo {age:.2f}s old, "
                                  f"person detected: {'yes' if n else 'no'}")
                        try:
                            reply = self.brain.generate_reply(
                                person_said=person_said,
                                conversation_history=conversation_history,
                                is_final_turn=is_final_turn,
                                image_b64=fresh_b64,
                            )
                        except Exception as e:
                            print(f"[scarecrow] Reply generation error: {e}")
                            reply = self.brain._get_reply_fallback(person_said)

                        print(f"SCARECROW REPLIES (turn {turn + 1}): \"{reply}\"\n")
                        log_event("REPLY", reply)

                        self.servos.start_jaw_sync()
                        self.speech.speak(reply, blocking=True)
                        self.servos.stop_jaw_sync()

                        # Append this exchange to history for next turn
                        conversation_history.append(
                            {"role": "user", "content": f"The person responded out loud: \"{person_said}\""}
                        )
                        conversation_history.append(
                            {"role": "assistant", "content": reply}
                        )

                # Mark everyone as spoken to — use BOTH the original track IDs
                # (in case they're still alive) AND whatever is currently active
                # (the person likely got new track IDs during the interaction
                # because they moved and IoU matching lost them)
                current_track_ids = [t.track_id for t in self.tracker.get_active_tracks()]
                all_ids_to_mark = set(all_track_ids) | set(current_track_ids)
                if all_ids_to_mark:
                    self.tracker.mark_spoken_to(list(all_ids_to_mark))

                # Small dramatic pause, then slowly return to center
                time.sleep(0.5)
                self.servos.center(slow=True)

                # Wait for the head to settle, then flush the detection counter
                # so stale frames from the pan don't trigger a false interaction.
                # (The camera is in the scarecrow's eye — panning changes the
                # entire field of view, and YOLO can false-positive on whatever
                # newly enters the frame.)
                time.sleep(2.0)
                self.detector.reset_detection_count()

                # Slumps back to the drooped resting pose — the scarecrow
                # "powers down" between visitors instead of staying alert.
                # We wait for the move to finish before restoring track speed,
                # otherwise the slow droop speed never gets to apply.
                # With idle sweeps off there is no "wake up" later, so he just
                # stays level, looking straight ahead (already centred above).
                if config.IDLE_SCAN_ENABLED:
                    self.servos.speed_override = config.IDLE_DROOP_SPEED
                    self.servos.set_target(config.PAN_CENTER, config.TILT_DROOPED)
                    self._wait_for_pose(config.PAN_CENTER, config.TILT_DROOPED, timeout=4.0)
                    self.servos.speed_override = None

                # Update timestamp so cooldown starts AFTER the interaction ends,
                # not from when it began (interactions can take 15+ seconds)
                self._last_interaction_time = time.time()

        except Exception as e:
            print(f"[scarecrow] Interaction error: {e}")
            self._last_interaction_time = time.time()  # Cooldown even on error
            self.servos.stop_jaw_sync()  # Ensure jaw closes on error

    def _lift_to_level_if_drooped(self):
        """With idle sweeps off, never leave him slumped: slowly lift to level,
        straight ahead. Skipped while someone else owns the head (manual mode,
        talking, just after a conversation)."""
        if abs(config.TILT_DROOPED - config.TILT_CENTER) < 2.0:
            return
        if abs(self.servos.tilt_angle - config.TILT_DROOPED) > 2.0 or not self._should_scan():
            return
        self.servos.speed_override = config.IDLE_LIFT_SPEED
        try:
            self.servos.set_target(config.PAN_CENTER, config.TILT_CENTER)
            self._wait_for_pose(config.PAN_CENTER, config.TILT_CENTER, abort_check=self._should_scan)
        finally:
            self.servos.speed_override = None

    def _should_scan(self) -> bool:
        """Check if conditions are right for idle scanning."""
        if self._manual_mode:
            return False  # Dashboard is driving the servos directly
        if self.speech.is_speaking or self._interaction_lock.locked():
            return False
        if self._glance_active():
            return False  # a voice-look glance owns the head right now
        # Don't scan too soon after an interaction
        if time.time() - self._last_interaction_time < config.COOLDOWN_SECONDS + 5:
            return False
        # Otherwise, scan freely. If a real person shows up, detection hold
        # frames + _can_interact() will trigger an interaction regardless of
        # whether the head is currently scanning or not.
        return True

    # --- Voice look ---------------------------------------------------------
    def _glance_active(self) -> bool:
        return time.time() < self._glance_until

    def _voice_look_blocker(self, now: float) -> str | None:
        """Why he should NOT glance right now (None = go ahead)."""
        if not config.VOICE_LOOK_ENABLED:
            return "off"
        if self._is_quiet_hours():
            return "quiet hours"
        if not self._should_scan():
            return "busy"  # manual mode, talking, mid-glance, or just after a conversation
        if now - self._last_detection_time < 5.0:
            return "someone in view"
        if now - self._last_glance_time < config.VOICE_LOOK_COOLDOWN_SECONDS:
            return "cooldown"
        return None

    def _on_voice_heard(self):
        """VoiceWatcher callback: voices nearby. Glance toward the blind spot if idle."""
        now = time.time()
        why_not = self._voice_look_blocker(now)
        if why_not:
            # Always logged, so "he didn't look" can be explained from the journal;
            # the same reason again only every VOICE_SKIP_LOG_SECONDS
            last_why, last_t = self._voice_skip_logged
            if why_not != last_why or now - last_t >= VOICE_SKIP_LOG_SECONDS:
                print(f"[voice] Heard voices — not glancing ({why_not})")
                self._voice_skip_logged = (why_not, now)
            return
        self._voice_skip_logged = (None, 0.0)
        # Claim the head now (before the thread starts) so nothing else moves it
        self._glance_until = now + config.VOICE_LOOK_HOLD_SECONDS + 10.0
        self._last_glance_time = now
        threading.Thread(target=self._do_glance, args=(now,), daemon=True).start()

    def _glance_ok(self, start: float) -> bool:
        """The glance carries on while nobody else needs the head."""
        return (self.running and not self._manual_mode
                and not self.speech.is_speaking and not self._interaction_lock.locked()
                and self._last_detection_time <= start)

    def _do_glance(self, start: float):
        print("[voice] Heard voices — glancing toward the blind spot")
        log_event("SYSTEM", "Heard voices nearby — glancing over")
        ok = lambda: self._glance_ok(start)
        try:
            self.servos.speed_override = None  # normal speed: a quick, alert turn
            pan = self.servos._clamp_pan(config.VOICE_LOOK_PAN)
            tilt = self.servos._clamp_tilt(config.VOICE_LOOK_TILT)
            self.servos.set_target(pan, tilt)
            self._wait_for_pose(pan, tilt, timeout=6.0, abort_check=ok)  # slow tilt from drooped: ~4 s
            hold_end = time.time() + config.VOICE_LOOK_HOLD_SECONDS
            while ok() and time.time() < hold_end:
                time.sleep(0.1)
            if not ok():
                # Someone showed up (tracking + talking take over) or the
                # dashboard/a conversation has the head — leave it to them.
                print("[voice] Glance ended — someone else has the head")
                return
            # Nobody came: quietly go back to resting
            rest_tilt = config.TILT_DROOPED if config.IDLE_SCAN_ENABLED else config.TILT_CENTER
            self.servos.speed_override = 0.5
            self.servos.set_target(config.PAN_CENTER, rest_tilt)
            self._wait_for_pose(config.PAN_CENTER, rest_tilt, timeout=10.0, abort_check=ok)
            self._returned_to_center = True
            print("[voice] Nobody there — back to rest")
        except Exception as e:
            print(f"[voice] Glance error: {e}")
        finally:
            self.servos.speed_override = None
            self._last_glance_time = time.time()
            self._glance_until = 0.0

    def _wait_for_pose(self, target_pan: float, target_tilt: float,
                       timeout: float = 8.0, abort_check=None):
        """Block until pan + tilt are both within 2° of their targets.

        Args:
            target_pan, target_tilt: angles to wait for
            timeout: max seconds to wait before giving up
            abort_check: optional callable; if provided and returns False,
                         exit early (used by the idle thread to bail out
                         when an interaction starts).
        """
        deadline = time.time() + timeout
        while self.running and time.time() < deadline:
            if abort_check is not None and not abort_check():
                return
            if (abs(self.servos.pan_angle - target_pan) < 2.0 and
                    abs(self.servos.tilt_angle - target_tilt) < 2.0):
                return
            time.sleep(0.1)

    def _idle_behavior(self):
        """
        Background idle behavior — the scarecrow's "asleep" pose.

        Default state: head drooped forward, looking at the ground, motionless.
        Periodically (every 60–90s, configurable) the head lifts slowly, sweeps
        side to side scanning the yard, then slumps back into the drooped pose.
        This trades constant idle motion (which wears servos and gives away the
        prop) for occasional dramatic awakening (eerier and far less wear).
        """

        # Resting pose at startup: slumped if he'll periodically wake and scan,
        # otherwise level and looking straight ahead.
        self.servos.set_target(config.PAN_CENTER,
                               config.TILT_DROOPED if config.IDLE_SCAN_ENABLED else config.TILT_CENTER)

        while self.running:
            if not config.IDLE_SCAN_ENABLED:
                # Sweeps off: no drooping or lifting at all. If he was left slumped
                # (switch flipped while asleep), lift him back to level once.
                self._lift_to_level_if_drooped()
                time.sleep(2.0)
                continue

            # Wait for the next wake-up, but react within ~1 s if sweeps get
            # switched off in the meantime.
            wake_at = time.time() + random.uniform(config.IDLE_SCAN_MIN_INTERVAL,
                                                   config.IDLE_SCAN_MAX_INTERVAL)
            while self.running and config.IDLE_SCAN_ENABLED and time.time() < wake_at:
                time.sleep(1.0)
            if not (self.running and config.IDLE_SCAN_ENABLED):
                continue

            try:
                # Quiet hours only gate *starting* a sweep; they are not part of the
                # mid-sweep abort check, so a sweep that began at 10:59pm finishes
                # and droops back to rest instead of freezing with the head raised.
                if self._is_quiet_hours() and not config.QUIET_HOURS_IDLE_SCAN:
                    if config.DEBUG:
                        print("[scan] Quiet hours — staying asleep, skipping cycle")
                    continue
                if not self._should_scan():
                    if config.DEBUG:
                        print("[scan] _should_scan=False, skipping cycle")
                    continue

                if config.DEBUG:
                    print("[scan] Awakening — lifting head from drooped pose")

                # Phase 1: slow lift from drooped to level (the "wake up" beat)
                self.servos.speed_override = config.IDLE_LIFT_SPEED
                self.servos.set_target(config.PAN_CENTER, config.TILT_CENTER)
                self._wait_for_pose(config.PAN_CENTER, config.TILT_CENTER, abort_check=self._should_scan)
                if not (self.running and self._should_scan()):
                    self.servos.speed_override = None
                    continue
                time.sleep(1.0)  # brief pause, "looking straight ahead"

                # Phase 2: slow sweep all the way to one side
                self.servos.speed_override = config.IDLE_SCAN_SPEED
                left_target = self.servos._clamp_pan(config.PAN_CENTER - config.IDLE_SCAN_RANGE)
                right_target = self.servos._clamp_pan(config.PAN_CENTER + config.IDLE_SCAN_RANGE)
                # Alternate which side we sweep first across calls, just so it
                # doesn't always start the same way
                first, second = (left_target, right_target) \
                    if random.random() < 0.5 else (right_target, left_target)

                if config.DEBUG:
                    print(f"[scan] Sweep → {first:.0f}° → {second:.0f}° → center")

                self.servos.set_target(first, config.TILT_CENTER)
                self._wait_for_pose(first, config.TILT_CENTER, abort_check=self._should_scan)
                if not (self.running and self._should_scan()):
                    self.servos.speed_override = None
                    continue
                time.sleep(1.5)  # pause at the end of the sweep

                # Phase 3: sweep all the way to the other side
                self.servos.set_target(second, config.TILT_CENTER)
                self._wait_for_pose(second, config.TILT_CENTER, abort_check=self._should_scan)
                if not (self.running and self._should_scan()):
                    self.servos.speed_override = None
                    continue
                time.sleep(1.5)

                # Phase 4: return to center, brief pause
                self.servos.set_target(config.PAN_CENTER, config.TILT_CENTER)
                self._wait_for_pose(config.PAN_CENTER, config.TILT_CENTER, abort_check=self._should_scan)
                if not (self.running and self._should_scan()):
                    self.servos.speed_override = None
                    continue
                time.sleep(0.8)

                # Phase 5: slow droop back to resting pose
                if config.DEBUG:
                    print("[scan] Drooping back to rest")
                self.servos.speed_override = config.IDLE_DROOP_SPEED
                self.servos.set_target(config.PAN_CENTER, config.TILT_DROOPED)
                self._wait_for_pose(config.PAN_CENTER, config.TILT_DROOPED, abort_check=self._should_scan)

                # Restore normal speed for any subsequent tracking
                self.servos.speed_override = None

            except Exception as e:
                print(f"[scan] ERROR in idle scan thread: {e}")
                self.servos.speed_override = None

    def shutdown(self):
        """Clean shutdown of all subsystems."""
        self.running = False
        print("\nShutting down subsystems...")
        self.radar.shutdown()
        if self.voice_watch:
            self.voice_watch.stop()
        self.servos.shutdown()
        self.detector.release()
        if self.listener:
            self.listener.release()
        if self.debug:
            try:
                import cv2
                cv2.destroyAllWindows()
            except Exception:
                pass
        print("Scarecrow is asleep. Happy Halloween.\n")


def test_voice():
    """Test the speech system without camera or servos."""
    from speech import SpeechEngine

    def amp_callback(amp):
        bars = int(amp * 30)
        print(f"  {'█' * bars}{'░' * (30 - bars)} {amp:.2f}", end='\r')

    engine = SpeechEngine(on_amplitude=amp_callback)

    test_lines = [
        "I see you. Yes, you. Don't look behind you.",
        "Happy Halloween. I've been waiting all year for this.",
        "Nice costume. I'd clap, but, well, straw hands.",
    ]

    for line in test_lines:
        print(f"\n\nSaying: \"{line}\"")
        engine.speak(line, blocking=True)
        time.sleep(1)

    print("\n\nVoice test complete.")


def main():
    parser = argparse.ArgumentParser(description="The Talking Halloween Scarecrow")
    parser.add_argument("--debug", action="store_true",
                        help="Show camera feed with detection boxes")
    parser.add_argument("--test-voice", action="store_true",
                        help="Test speech system only (no camera)")
    parser.add_argument("--persona", choices=["friendly", "snarky", "creepy", "menacing"],
                        help="Override persona")
    args = parser.parse_args()

    if args.persona:
        config.PERSONA = args.persona

    if args.debug:
        config.DEBUG = True

    if args.test_voice:
        test_voice()
        return

    scarecrow = Scarecrow(debug=args.debug)

    # Handle SIGTERM gracefully (for systemd or cron restarts)
    def handle_signal(sig, frame):
        scarecrow.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)

    scarecrow.run()


if __name__ == "__main__":
    main()
