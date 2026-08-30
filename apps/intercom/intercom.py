import json
import os
from datetime import datetime
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

# How recent a `button` entity's state (an ISO timestamp - HA reports the last
# press time as the button's state) must be, relative to our own clock, to
# count as a live press happening right now. Generous enough to absorb normal
# HA/AppDaemon dispatch jitter; short enough to reject the classic restart/
# restore replay of a stale last-pressed timestamp (typically minutes to hours
# old, not seconds). Used by _on_abb_button_state for both freshness checks:
# "is this a live press at all" and "is it OUR own press" (see
# _own_abb_press_at).
ABB_PRESS_FRESHNESS_S = 10

# ---------------------------------------------------------------------------
# Pure module-level function - unit-testable without an AppDaemon runtime (see
# tests/test_intercom.py).
# ---------------------------------------------------------------------------


def resume_decision(record, age_s, max_age_s):
    """Pure decision for one persisted ring record found at Intercom startup (see
    _resume_pending_rings). `age_s` is (now - ring_ts) in seconds, computed by the
    caller since only it knows "now".

    "discard_succeeded": the ring already confirmed an unlock before the restart -
        nothing to report, nothing to do.
    "discard_stale": too old to still matter, or ring_ts is somehow in the future
        (equally untrustworthy) - stay silent, just drop it.
    "alert": within the resume window and never confirmed - the house already
        announced "I opened it" but we don't know that it did; tell Mikkel once.
    """
    if record.get("succeeded"):
        return "discard_succeeded"
    if age_s < 0 or age_s > max_age_s:
        return "discard_stale"
    return "alert"


class Intercom(hass.Hass):
    def initialize(self):
        # Config
        self.front_sensor = self.args.get("front_sensor")
        self.back_sensor = self.args.get("back_sensor")
        self.apt_sensor = self.args.get("apt_sensor")
        self.front_lock = self.args.get("front_lock")
        self.back_lock = self.args.get("back_lock")
        self.front_door_sensor = self.args.get("front_door_sensor")  # Optional door state sensor
        self.back_door_sensor = self.args.get("back_door_sensor")  # Optional door state sensor
        self.auto_open_entity = self.args.get("auto_open_boolean")
        self.unlock_delay_s = int(self.args.get("unlock_delay_s", 1))
        self.unlock_repeat_count = int(self.args.get("unlock_repeat_count", 2))
        self.unlock_repeat_interval_s = int(self.args.get("unlock_repeat_interval_s", 7))
        self.debounce_s = int(self.args.get("debounce_s", 5))
        self.notify_target = self.args.get("notify_target", "mikkel")
        # How stale a persisted "rang but auto-open unlock was never confirmed"
        # record can be at startup and still be worth alerting on - older than this
        # and the visitor is long gone, so alerting would just be noise (see
        # _resume_pending_rings).
        self.resume_alert_max_age_s = int(self.args.get("resume_alert_max_age_s", 60))

        # ABB-native unlock experiment (see abb_unlock_doors in intercom.yaml):
        # door label -> ABB Welcome button entity to press INSTEAD of starting
        # the ESP unlock sequence for that door. Default {} - unconfigured is a
        # no-op, byte-for-byte today's behaviour (see _handle_trigger below and
        # the door_lock_info/abb_button_to_door wiring further down).
        raw_abb_unlock_doors = self.args.get("abb_unlock_doors", {}) or {}
        self.abb_unlock_doors = (
            {str(k): str(v) for k, v in raw_abb_unlock_doors.items()}
            if isinstance(raw_abb_unlock_doors, dict) else {}
        )
        # How long to wait for the ESP lock's own unlocking/unlocked ack after
        # an ABB press before giving up on it and falling back to the ESP
        # sequence (see _arm_abb_watchdog).
        self.abb_unlock_ack_timeout_s = float(self.args.get("abb_unlock_ack_timeout_s", 2.5))

        # Messages
        self.msg_front = self.args.get("tts_message_front", "Someone is at the front door")
        self.msg_back = self.args.get("tts_message_back", "Someone is at the back door")
        self.msg_apt = self.args.get("tts_message_apt", "Someone is at the apartment door")
        self.msg_open_front = self.args.get("door_open_message_front", "I opened the front door")
        self.msg_open_back = self.args.get("door_open_message_back", "I opened the back door")

        self.sonos_notifier = self._get_notifier()
        self.mobile_notifier = self._get_mobile_notifier()
        # Optional ABB Welcome bridge (apps/intercom/abb_welcome_bridge.py) for
        # attaching the doorbell snapshot to pushes. Resolved LAZILY at push time
        # (see _abb_ring_attachment), never via yaml dependencies: a broken/missing
        # bridge must cost the photo, never this app's load or its unlock path.
        self.abb_bridge = None
        self.last_trigger_at = {}
        self.pending_unlocks = {}  # Track scheduled unlock callbacks by entity
        self.unlock_outcomes = {}  # Per trigger entity: did any attempt of the current ring succeed
        self._abb_watchdogs = {}  # door label -> pending ABB-ack watchdog record (see _arm_abb_watchdog)
        self._own_abb_press_at = {}  # ABB button entity -> when WE last pressed it (see _start_abb_unlock)
        # ring -> recording -> announcement -> unlock (Mikkel 2026-08-26). The
        # visitor is only in front of the camera until the door opens, and this
        # gateway needs ~2 s to produce its first video frame while the unlock
        # fires at ~+0.45 s - so opening immediately guarantees an empty clip.
        # Speaking first buys the recording the seconds it needs, and a couple
        # of seconds is invisible against the 15-30 s a human answering would
        # have taken. NOT a tuned delay: it is what the sequence costs.
        raw_voice = self.args.get("voice_before_unlock", {}) or {}
        self.voice_before_unlock = (
            {str(k): str(v) for k, v in raw_voice.items()} if isinstance(raw_voice, dict) else {}
        )
        self.voice_before_unlock_message = str(
            self.args.get("voice_before_unlock_message", "Door is opening.")
        )
        self.voice_before_unlock_tts = str(self.args.get("voice_before_unlock_tts", "tts.piper"))
        # CEILING, not the schedule. The door opens when the bridge reports the
        # sentence has finished playing; this timer only fires when that report
        # never comes - the station's talkback never became ready and every
        # retry failed - so a rejected, silent or hung announcement still
        # cannot leave the door shut. It costs these seconds and nothing else,
        # and there is exactly one unlock path either way.
        self.voice_before_unlock_ceiling_s = float(
            self.args.get("voice_before_unlock_ceiling_s", 8.0)
        )
        # ring_label -> the ring whose door is being held for the sentence.
        # Both releasers (the announcement event and the ceiling timer) run on
        # this app's pinned thread, so the dict pop below IS the interlock:
        # whichever arrives first takes the record, the other finds nothing.
        self._voice_unlock_pending = {}
        self._state_file = Path(__file__).with_name("intercom_state.json")

        # Validate entities exist
        self._validate_entities()

        # Resume any ring that was persisted but never confirmed unlocked before a
        # restart - alert-only, see _resume_pending_rings. Must run before any
        # listen_state registration below: ZERO lock/unlock calls from initialize -
        # the intercom bus is SHARED across all 18-19 apartments in the building, so
        # unlocking from here with no fresh visitor context would be a real security
        # risk, not just a UX one.
        self._resume_pending_rings()

        # Build trigger map
        self.trigger_map = {}
        if self.front_sensor:
            self.trigger_map[self.front_sensor] = {
                "message": self.msg_front,
                "lock": self.front_lock,
                "followup": self.msg_open_front,
                "door_sensor": self.front_door_sensor,
                "ring_label": "front door",
            }
            self.listen_state(self._handle_trigger, self.front_sensor, new="on")
        if self.back_sensor:
            self.trigger_map[self.back_sensor] = {
                "message": self.msg_back,
                "lock": self.back_lock,
                "followup": self.msg_open_back,
                "door_sensor": self.back_door_sensor,
                "ring_label": "back door",
            }
            self.listen_state(self._handle_trigger, self.back_sensor, new="on")
        if self.apt_sensor:
            self.trigger_map[self.apt_sensor] = {
                "message": self.msg_apt,
                "lock": None,
                "followup": None,
                "ring_label": "apartment door",
            }
            self.listen_state(self._handle_trigger, self.apt_sensor, new="on")

        if not self.trigger_map:
            self.log("CRITICAL: No intercom sensors configured; app will be idle.", level="ERROR")
        else:
            sensors = ", ".join(self.trigger_map.keys())
            self.log(f"Intercom initialized; listening for rings on: {sensors}", level="INFO")

        # ABB-native unlock wiring (see abb_unlock_doors in intercom.yaml), built
        # from trigger_map so it only ever wires up doors that already have a
        # configured ESP lock. Empty abb_unlock_doors (the default) means none of
        # this runs: no extra listen_state registrations, no new branch touched by
        # _handle_trigger below - today's behaviour, byte for byte.
        self.door_lock_info = {}
        for trig_info in self.trigger_map.values():
            label = trig_info.get("ring_label")
            if label and trig_info.get("lock"):
                self.door_lock_info[label] = {"lock": trig_info["lock"], "door_sensor": trig_info.get("door_sensor")}
        self.abb_button_to_door = {}
        for door_label, abb_button in self.abb_unlock_doors.items():
            lock_info = self.door_lock_info.get(door_label)
            if not lock_info:
                self.log(
                    f"WARNING: abb_unlock_doors has {door_label!r} but no matching door/lock "
                    f"is configured for it - ignoring", level="WARNING",
                )
                continue
            self.abb_button_to_door[abb_button] = door_label
            self.listen_state(self._on_esp_lock_ack, lock_info["lock"], door_label=door_label)
            self.listen_state(self._on_abb_button_state, abb_button)
            self.log(f"ABB-native unlock enabled for {door_label} via {abb_button}", level="INFO")

        # The bridge speaks the door sentence into the call HA dialled and fires
        # this the instant play_audio returns - which the integration only does
        # once the last 20 ms frame has been sent. Registered unconditionally:
        # an event for a door we are not holding is simply dropped.
        self.listen_event(self._on_announcement_spoken, "abb_announcement_spoken")

    def _get_notifier(self):
        try:
            notifier = self.get_app("SonosNotifier")
            if not notifier:
                self.log("CRITICAL: SonosNotifier app not found; TTS will not be sent.", level="ERROR")
            return notifier
        except Exception as e:
            self.log(f"CRITICAL: Error getting SonosNotifier app: {e}.", level="ERROR")
            return None

    def _get_mobile_notifier(self):
        # get_app must be resolved in sync init - async context returns a Task.
        try:
            notifier = self.get_app("MobileNotifier")
            if not notifier:
                self.log("MobileNotifier app not found; auto-open failure alerts will only be logged.", level="WARNING")
            return notifier
        except Exception as e:
            self.log(f"Error getting MobileNotifier app: {e}. Failure alerts will only be logged.", level="WARNING")
            return None

    def _abb_ring_attachment(self, ring_label=None):
        """Optional doorbell-snapshot payload for mobile pushes, from the ABB
        Welcome bridge app. STRICTLY ADDITIVE: returns None on ANY problem
        (bridge app absent, not loaded yet, or throwing), and the callers pass
        the result straight through as MobileNotifier's `data=` argument -
        data=None is byte-for-byte today's push. Never raises.

        ring_label lets the bridge check the photo is THIS visitor's rather than
        the last one's; omitting it keeps the old unchecked behaviour for callers
        that have no door in hand."""
        try:
            if self.abb_bridge is None:
                self.abb_bridge = self.get_app("AbbWelcomeBridge")
            if self.abb_bridge:
                return self.abb_bridge.ring_attachment_data(ring_label)
        except Exception as e:
            try:
                self.log(f"ABB snapshot attachment unavailable: {e}", level="DEBUG")
            except Exception:
                pass
        return None

    def _abb_defer_ring_push(self, ring_label, title, message):
        """Hand the auto-open push to the ABB bridge so it can wait for a photo.

        True means the bridge OWNS the push and this app must not send it; False
        means send it now, exactly as before. Same contract as
        _abb_ring_attachment: any problem at all is a False, because a household
        that loses the notification is worse off than one that gets it without a
        picture. Never raises."""
        try:
            if self.abb_bridge is None:
                self.abb_bridge = self.get_app("AbbWelcomeBridge")
            if self.abb_bridge:
                return bool(self.abb_bridge.defer_ring_push(
                    ring_label, title, message, self.notify_target))
        except Exception as e:
            try:
                self.log(f"ABB deferred ring push unavailable: {e}", level="DEBUG")
            except Exception:
                pass
        return False

    def _validate_entities(self):
        """Validate that configured entities exist in Home Assistant."""
        entities_to_check = []
        if self.front_sensor:
            entities_to_check.append(("front_sensor", self.front_sensor))
        if self.back_sensor:
            entities_to_check.append(("back_sensor", self.back_sensor))
        if self.apt_sensor:
            entities_to_check.append(("apt_sensor", self.apt_sensor))
        if self.front_lock:
            entities_to_check.append(("front_lock", self.front_lock))
        if self.back_lock:
            entities_to_check.append(("back_lock", self.back_lock))
        if self.front_door_sensor:
            entities_to_check.append(("front_door_sensor", self.front_door_sensor))
        if self.back_door_sensor:
            entities_to_check.append(("back_door_sensor", self.back_door_sensor))
        if self.auto_open_entity:
            entities_to_check.append(("auto_open_boolean", self.auto_open_entity))
        for door_label, abb_button in self.abb_unlock_doors.items():
            entities_to_check.append((f"abb_unlock_doors[{door_label}]", abb_button))

        for name, entity_id in entities_to_check:
            state = self.get_state(entity_id)
            if state is None or state in ["unknown", "unavailable"]:
                self.log(f"WARNING: Entity {entity_id} ({name}) not found or unavailable (state: {state})", level="WARNING")
            else:
                self.log(f"Validated entity {entity_id} ({name})", level="DEBUG")

    def _resume_pending_rings(self):
        """Startup-only: alert on any ring whose auto-open outcome was never
        confirmed before a restart (see the state schema in _persist_ring).
        ALERT-ONLY (Option A) - this never touches lock/unlock. The intercom bus is
        shared across every apartment in the building; issuing an unlock from here
        with no fresh visitor context would be a real security risk, not a UX one."""
        records = self._load_ring_state()
        now = self.get_now()
        pruned = {}
        for entity, record in records.items():
            ring_ts = self._parse_ts(record.get("ring_ts"))
            if ring_ts is None:
                continue  # unparseable - drop
            self.last_trigger_at[entity] = ring_ts
            age_s = (now - ring_ts).total_seconds()
            decision = resume_decision(record, age_s, self.resume_alert_max_age_s)
            if decision == "alert":
                outcome = {
                    "ring_ts": ring_ts,
                    "succeeded": False,
                    "ring_label": record.get("ring_label", "door"),
                }
                self.log(
                    f"Resuming unconfirmed ring on {entity} from before restart "
                    f"({age_s:.0f}s old) - alerting only, never unlocking from init",
                    level="INFO",
                )
                self._report_auto_open_failure(entity, record.get("lock_entity"), outcome)
            # "discard_succeeded" / "discard_stale": nothing to do. Every branch here
            # is terminal (alert also ends by discarding, via _report_auto_open_failure
            # below), so `pruned` never gains an entry - this always ends up writing
            # back an empty file, which is what makes the resume pass idempotent.
        self._save_ring_state(pruned)

    # ---------- ring persistence (reboot-survival; alert-only, never unlock from init) ----------
    def _load_ring_state(self):
        try:
            return json.loads(self._state_file.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.log(f"intercom state load failed: {e}", level="WARNING")
            return {}

    def _save_ring_state(self, records):
        try:
            tmp = self._state_file.with_name(self._state_file.name + ".tmp")
            tmp.write_text(json.dumps(records))
            os.replace(tmp, self._state_file)
        except Exception as e:
            self.log(f"intercom state save failed: {e}", level="WARNING")

    def _persist_ring(self, entity, ring_ts, ring_label, lock_entity):
        """Write point (a): a ring just entered the auto-open branch. Called AFTER
        unlock scheduling and BEFORE TTS (see _handle_trigger) so the latency-
        sensitive unlock timers are never delayed by a disk write."""
        records = self._load_ring_state()
        records[entity] = {
            "ring_ts": ring_ts.isoformat(),
            "ring_label": ring_label,
            "lock_entity": lock_entity,
            "succeeded": False,
        }
        self._save_ring_state(records)

    def _mark_ring_succeeded(self, entity):
        """Write point (b): the first successful unlock verification of this ring."""
        records = self._load_ring_state()
        if entity in records:
            records[entity]["succeeded"] = True
            self._save_ring_state(records)

    def _forget_ring(self, entity):
        """Write point (c), terminal: all unlock attempts exhausted with no success
        (real-time failure, or a discarded resume-time record). Idempotent - safe to
        call even if no record exists for `entity`."""
        records = self._load_ring_state()
        records.pop(entity, None)
        self._save_ring_state(records)

    @staticmethod
    def _parse_ts(raw):
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def _debounced(self, entity):
        """Check if entity trigger should be debounced."""
        last_ts = self.last_trigger_at.get(entity)
        if not last_ts:
            return False
        elapsed = (self.get_now() - last_ts).total_seconds()
        return elapsed < self.debounce_s

    def _cancel_pending_unlocks(self, entity):
        """Cancel any pending unlock callbacks for the given entity."""
        if entity in self.pending_unlocks:
            cancelled_count = 0
            invalid_count = 0
            # Make a copy of the list to iterate over, as we may modify it
            handles_to_cancel = list(self.pending_unlocks[entity])
            for handle in handles_to_cancel:
                # AppDaemon logs WARNING on cancel_timer(stale_handle); it does not raise.
                # timer_running() is False for unknown/already-fired handles - skip cancel.
                try:
                    if handle and self.timer_running(handle):
                        self.cancel_timer(handle)
                        cancelled_count += 1
                    else:
                        invalid_count += 1
                except Exception as e:
                    self.log(f"Unexpected error cancelling unlock timer for {entity}: {e}", level="DEBUG")
                    invalid_count += 1
            if cancelled_count > 0:
                self.log(f"Cancelled {cancelled_count} pending unlock(s) for {entity}", level="DEBUG")
            if invalid_count > 0:
                self.log(f"Skipped {invalid_count} already-fired timer(s) for {entity}", level="DEBUG")
            del self.pending_unlocks[entity]

    def _schedule_esp_unlock_sequence(self, entity, lock_entity, ring_ts, door_sensor):
        """The original (pre-ABB) unlock ladder: unlock_repeat_count attempts,
        unlock_delay_s then +unlock_repeat_interval_s apart, each verified 2s
        later by _verify_unlock. No follow-up TTS needed since the ring-time
        combined message already covers it (see _handle_trigger).

        Used both directly (no abb_unlock_doors entry for this door) and as the
        ABB-ack-timeout fallback (see _arm_abb_watchdog) - unchanged either way:
        same retries, same verification, same failure alerting. `entity` is the
        key into pending_unlocks/unlock_outcomes: the ring's own sensor entity
        for a real ring, or a synthetic per-door key for an externally observed
        ABB button press with no ring context (see _on_abb_button_state) - in
        which case unlock_outcomes has no entry for it and _verify_unlock's
        notification/house-feed branches simply stay dark, by design.
        """
        self.pending_unlocks[entity] = []
        for i in range(self.unlock_repeat_count):
            delay = self.unlock_delay_s + i * self.unlock_repeat_interval_s

            # Store handle reference in a list to allow modification in closure
            handle_ref = [None]  # Use list to allow modification in closure

            # Bind loop variables via default args - a plain closure over these
            # would capture them by reference, so every callback would see the
            # LAST iteration's values (attempt_num == unlock_repeat_count, and the
            # last handle_ref). That mislabelled every attempt as the final one,
            # which tripped the "last attempt failed" report on the first verify
            # instead of after the genuine last attempt. Default args are evaluated
            # at def time, so each callback captures its own attempt_num/handle_ref.
            trigger_ent = entity
            attempt_num = i + 1

            def unlock_callback(kwargs_inner, trigger_ent=trigger_ent, attempt_num=attempt_num, handle_ref=handle_ref):
                # Remove handle from pending_unlocks when this timer fires
                # This prevents "Invalid callback handle" warnings when trying to cancel
                # an already-fired timer
                if trigger_ent in self.pending_unlocks and handle_ref[0]:
                    try:
                        self.pending_unlocks[trigger_ent].remove(handle_ref[0])
                        if not self.pending_unlocks[trigger_ent]:
                            del self.pending_unlocks[trigger_ent]
                    except (ValueError, KeyError):
                        pass  # Handle already removed or doesn't exist
                # Call the actual unlock function
                kwargs_inner["trigger_entity"] = trigger_ent
                kwargs_inner["unlock_attempt"] = attempt_num
                self._perform_unlock(kwargs_inner)

            handle = self.run_in(
                unlock_callback,
                delay,
                lock_entity=lock_entity,
                followup=None,
                door_sensor=door_sensor,
                ring_ts=ring_ts,
            )
            handle_ref[0] = handle  # Store handle for removal in callback
            self.pending_unlocks[entity].append(handle)

    def _handle_trigger(self, entity, attr, old, new, kwargs):
        """Handle doorbell sensor trigger."""
        # Any edge landing on "on" is a ring worth announcing - including a replay
        # from unavailable/unknown/None (e.g. a Zigbee/network blip, or an AD/HA
        # restart replaying retained state). Only a CLEAN off->on edge is trusted
        # enough to schedule an unlock (see clean_edge below and the auto_open_enabled
        # gate); a replay edge still gets the TTS/house-feed announcement, just never
        # the unlock.
        if new != "on":
            self.log(f"Ignoring non-'on' trigger from {entity} (old={old}, new={new})", level="DEBUG")
            return
        clean_edge = (old == "off")

        info = self.trigger_map.get(entity)
        if not info:
            return

        if self._debounced(entity):
            self.log(f"Debounced trigger from {entity}", level="DEBUG")
            return

        self.last_trigger_at[entity] = self.get_now()
        self.log(f"Ring detected from {entity} (clean_edge={clean_edge}, old={old})", level="INFO")

        # Cancel any pending unlocks for this entity (new trigger takes precedence)
        self._cancel_pending_unlocks(entity)

        if not clean_edge:
            self.log(f"Replay edge on {entity} (old={old} -> on) - announcing only, never "
                     f"scheduling an unlock from a non-clean edge", level="INFO")

        # Check if auto-open is enabled before deciding which message to send.
        # clean_edge gates this: auto-open UNLOCK scheduling only ever happens on a
        # genuine off->on edge (see the guard above).
        auto_open_enabled = False
        lock_entity = None

        if clean_edge and self.auto_open_entity and info.get("lock"):
            auto_open_state = self.get_state(self.auto_open_entity)
            if auto_open_state in ["on", True]:
                lock_entity = info.get("lock")
                lock_state = self.get_state(lock_entity)
                if lock_state not in [None, "unknown", "unavailable"]:
                    auto_open_enabled = True

        # Schedule unlocks BEFORE any TTS: chime_tts/say blocks this callback
        # thread for 5-10s, which was the dominant share of the measured 6-11s
        # ring->buzz latency (every daytime ring 2026-07-03..07-16; the one
        # quiet-hours ring, where notify() returns early, buzzed in 2.0s).
        # The visitor is already standing at the door - open first, talk after.
        if auto_open_enabled:
            # Track whether ANY attempt of THIS ring succeeds; ring_ts guards
            # against a stale verify from a cancelled ring escalating falsely.
            ring_ts = self.last_trigger_at[entity]
            self.unlock_outcomes[entity] = {
                "ring_ts": ring_ts,
                "succeeded": False,
                "ring_label": info.get("ring_label", "door"),
            }
            door_sensor = info.get("door_sensor")
            ring_label = info.get("ring_label")
            voice_camera = self.voice_before_unlock.get(ring_label)
            if voice_camera:
                # Speak first, then unlock. The unlock is scheduled here and
                # cannot be cancelled by anything the voice does.
                try:
                    self.submit_to_executor(
                        self._speak_before_unlock, ring_label, voice_camera
                    )
                except Exception as e:
                    self.log(
                        f"VOICE-BEFORE-UNLOCK dispatch failed for {ring_label}: {e}",
                        level="WARNING",
                    )
                unlock_args = {
                    "entity": entity,
                    "ring_ts": ring_ts,
                    "lock_entity": lock_entity,
                    "door_sensor": door_sensor,
                    "ring_label": ring_label,
                }
                self.log(
                    f"VOICE-BEFORE-UNLOCK door={ring_label} camera={voice_camera} "
                    f"- unlock waits for the sentence, ceiling "
                    f"{self.voice_before_unlock_ceiling_s}s",
                    level="INFO",
                )
                # Arm the ceiling FIRST, then record the hold: if run_in were to
                # raise, the door must not be left waiting on an event with no
                # backstop behind it.
                handle = self.run_in(
                    self._deferred_unlock,
                    self.voice_before_unlock_ceiling_s,
                    **unlock_args,
                )
                self._voice_unlock_pending[ring_label] = {
                    "ring_ts": ring_ts,
                    "handle": handle,
                    "args": unlock_args,
                }
            else:
                self._dispatch_unlock(entity, ring_ts, lock_entity, door_sensor, ring_label)

            # Persist AFTER scheduling, BEFORE TTS (same latency-first ordering as
            # above - see _persist_ring) so a restart before any attempt is verified
            # can still alert on resume instead of silently forgetting the ring.
            self._persist_ring(entity, ring_ts, info.get("ring_label", "door"), lock_entity)


        # Send TTS - combined message if auto-open enabled, otherwise just initial message.
        # Offloaded to AppDaemon's executor pool (submit_to_executor): SonosNotifier.notify()
        # ends in a blocking call_service("chime_tts/say") that waits ~5-10s on the HA
        # websocket ack. This app is pinned to ONE thread (pin_apps default), so calling
        # notify() inline here delayed the +1/+4/+7s unlock timers queued above until it
        # returned - every daytime ring opened at ~ring+8s instead of ~ring+1s, and wasted
        # attempts 1&2. notify() touches only SonosNotifier's own state + AD sync APIs (safe
        # cross-thread), never Intercom's pending_unlocks/unlock_outcomes/last_trigger_at, so
        # offloading it introduces no race on those dicts. The Future is AD-tracked/cancelled
        # on reload; notify() self-logs its own errors to sonos_notifier_log.
        if self.sonos_notifier:
            try:
                if auto_open_enabled and info.get("followup"):
                    # Combine messages: "Someone is at the front door and I opened the front door"
                    combined_message = f"{info['message']} and {info['followup']}"
                    self.submit_to_executor(self.sonos_notifier.notify, message=combined_message)
                    self.log(f"Queued combined TTS message for {entity} (auto-open enabled)", level="DEBUG")
                else:
                    # Just send the initial message
                    self.submit_to_executor(self.sonos_notifier.notify, message=info["message"])
            except Exception as e:
                self.log(f"Error queueing TTS for {entity}: {e}", level="ERROR")
        else:
            self.log("Skipping TTS; SonosNotifier unavailable.", level="ERROR")

        # Tell the house feed (dashboard's activity log). Guarded and after the TTS attempt:
        # the feed is cosmetic, the intercom is not - a feed problem must never block a ring.
        try:
            effect = "Announcing on the speakers and opening the door" if auto_open_enabled else "Announcing on the speakers"
            self.fire_event(
                "house_events_report",
                cause=f"Someone rang the {info.get('ring_label', 'door')}",
                effect=effect,
                icon="mdi:bell-ring",
            )
        except Exception as e:
            self.log(f"house_events_report failed: {e}", level="DEBUG")

    def _dispatch_unlock(self, entity, ring_ts, lock_entity, door_sensor, ring_label):
        """Start the actual unlock: ABB button if this door has one, else ESP.

        Extracted so it can run either immediately or after the door voice has
        had its say - the two callers must not drift apart, because this is the
        only code path that opens a door on a ring.
        """
        abb_button = self.abb_unlock_doors.get(ring_label)
        if abb_button:
            # ABB-native unlock (see abb_unlock_doors in intercom.yaml): press
            # the integration's button INSTEAD of starting the ESP ladder, then
            # watch for the ESP lock's own ack. Ack in time -> reported exactly
            # like an ESP success. No ack in time -> falls through to the
            # unchanged ESP sequence (see _arm_abb_watchdog's timeout closure).
            self._start_abb_unlock(
                entity, ring_ts, lock_entity, door_sensor, ring_label, abb_button
            )
        else:
            self._schedule_esp_unlock_sequence(entity, lock_entity, ring_ts, door_sensor)

    def _on_announcement_spoken(self, event_name, data, kwargs):
        """The bridge finished playing the door sentence - open the door now.

        Carries no ring_ts: it means "whatever ring is currently holding this
        door, its sentence is done", which is the only ring that can be waiting
        on this door at this moment.
        """
        door = str((data or {}).get("door") or "")
        self._release_voice_unlock(door, "announcement")

    def _deferred_unlock(self, kwargs):
        """Ceiling for a held door (see voice_before_unlock_ceiling_s).

        Fires only when the announcement never reported finishing - the station
        never became ready, or every attempt failed. Whatever the announcement
        did, the door still opens here; failure costs the wait and nothing else.
        """
        self._release_voice_unlock(
            kwargs.get("ring_label"), "ceiling", ring_ts=kwargs.get("ring_ts")
        )

    def _release_voice_unlock(self, ring_label, reason, ring_ts=None):
        """Open a door that was being held for its sentence, exactly once.

        The announcement and the ceiling race by design, and both land on this
        app's pinned thread, so popping the record IS the interlock - no lock
        needed, and the loser simply finds nothing.

        ring_ts guards a stale ceiling: if a second ring replaced the record
        while the first one's timer was still armed, that timer must not open
        the door on behalf of a ring that is no longer being held. The newer
        ring has its own ceiling and its own sentence.
        """
        record = self._voice_unlock_pending.get(ring_label)
        if record is None:
            return
        if ring_ts is not None and record.get("ring_ts") != ring_ts:
            return  # A newer ring took this door; that ring's own hold applies.
        self._voice_unlock_pending.pop(ring_label, None)
        handle = record.get("handle")
        if handle is not None:
            try:
                self.cancel_timer(handle)
            except Exception:
                pass  # Already fired, or gone with a restart - either is fine.
        self.log(f"VOICE-UNLOCK door={ring_label} released by {reason}", level="INFO")
        args = record.get("args", {})
        try:
            self._dispatch_unlock(
                args.get("entity"),
                args.get("ring_ts"),
                args.get("lock_entity"),
                args.get("door_sensor"),
                args.get("ring_label"),
            )
        except Exception as e:
            self.log(f"Deferred unlock failed for {ring_label}: {e}", level="ERROR")

    def _speak_before_unlock(self, ring_label, camera):
        """Executor-thread body: say the line into the station's OWN ring call.

        Runs on the executor because call_service blocks (sync_decorator) and
        this app is pinned to one thread. It deliberately cannot influence the
        unlock - _deferred_unlock is already scheduled - so nothing here needs
        to be fast or even to succeed.

        Uses play_audio rather than announce: at this moment the station is in
        a call with US, and announce dials a NEW call, which the gateway's one
        call slot refuses. Whether the station renders audio on a call it
        originated is exactly the open question (2026-08-26: 55 clean packets,
        nothing audible), so this may be silent - the recording benefit of the
        sequence stands either way.
        """
        if not self.voice_before_unlock_message:
            # Empty message = hold the door but say nothing here. The bridge
            # owns the sentence now: it speaks into the call HA dialled, which
            # is the only one the station renders. Two speakers would either
            # collide on the single call slot or talk over each other.
            return
        try:
            from urllib.parse import quote

            media_id = (
                f"media-source://tts/{self.voice_before_unlock_tts}"
                f"?message={quote(self.voice_before_unlock_message)}"
            )
            result = self.call_service(
                "abb_welcome/play_audio",
                entity_id=camera,
                media={"media_content_id": media_id, "media_content_type": "music"},
            )
            if isinstance(result, dict) and result.get("success") is False:
                err = result.get("error") or {}
                self.log(
                    f"VOICE-BEFORE-UNLOCK REJECTED door={ring_label} "
                    f"{err.get('code')}: {err.get('message')}",
                    level="WARNING",
                )
                return
            self.log(
                f"VOICE-BEFORE-UNLOCK accepted door={ring_label} "
                f"message={self.voice_before_unlock_message!r}",
                level="INFO",
            )
        except Exception as e:
            self.log(
                f"VOICE-BEFORE-UNLOCK failed for {ring_label}: {e}", level="WARNING"
            )

    def _start_abb_unlock(self, entity, ring_ts, lock_entity, door_sensor, door_label, abb_button):
        """Ring-triggered ABB-native unlock (see abb_unlock_doors in
        intercom.yaml): press `abb_button` INSTEAD of starting the ESP unlock
        ladder, then arm the ack watchdog (_arm_abb_watchdog).

        The press itself is offloaded to the executor exactly like
        sonos_notifier.notify() above (see that comment): button/press is a
        network round trip into the ABB integration's SIP client with no
        measured latency bound from this app's point of view, and this app is
        pinned to ONE thread - a blocking call_service here would stall the
        watchdog's own timeout timer and every other Intercom callback. The
        self-press marker (_own_abb_press_at) is written HERE, on the pinned
        thread, BEFORE the offloaded call goes out, so _on_abb_button_state can
        never observe the resulting state change before knowing to ignore it.
        """
        press_ts = self.get_now()
        self._own_abb_press_at[abb_button] = press_ts
        self._arm_abb_watchdog(door_label, lock_entity, door_sensor, press_ts, trigger_entity=entity, ring_ts=ring_ts)
        self.log(
            f"ABB-UNLOCK attempt door={door_label} button={abb_button} "
            f"(ack timeout {self.abb_unlock_ack_timeout_s}s)", level="INFO",
        )
        self.submit_to_executor(self._press_abb_button, abb_button)

    def _press_abb_button(self, abb_button):
        """Executor-thread body of the ABB button press (see _start_abb_unlock) -
        self-logs any failure, the same self-contained pattern
        sonos_notifier.notify() and abb_welcome_bridge.py's _dial_clip use for
        their own offloaded call_service calls."""
        try:
            self.call_service("button/press", entity_id=abb_button)
        except Exception as e:
            self.log(f"ABB-UNLOCK press failed for {abb_button}: {e}", level="ERROR")

    def _arm_abb_watchdog(self, door_label, lock_entity, door_sensor, press_ts, trigger_entity=None, ring_ts=None):
        """Arm the ack-timeout-then-ESP-fallback machinery for one ABB press on
        `door_label`, superseding any watchdog already pending for it. Shared by
        the app's own auto-open press (_start_abb_unlock) and an externally
        observed one (_on_abb_button_state, called when the wall dashboard
        presses the same button directly).

        trigger_entity/ring_ts identify the ring this press belongs to, or are
        None for a press with no ring context (a dashboard press with nobody
        having rung) - in which case _on_esp_lock_ack's success bookkeeping and
        _schedule_esp_unlock_sequence's failure bookkeeping both stay dark
        (unlock_outcomes has no entry to update), since there is no ring to
        report on. debounce_s normally keeps two presses on the same door far
        enough apart that only one watchdog is ever pending at a time (it is
        bigger than abb_unlock_ack_timeout_s by default) - the identity check in
        on_timeout below keeps this correct even if that invariant is ever
        misconfigured.
        """
        watchdog = {
            "press_ts": press_ts,
            "lock_entity": lock_entity,
            "door_sensor": door_sensor,
            "trigger_entity": trigger_entity,
            "ring_ts": ring_ts,
        }
        self._abb_watchdogs[door_label] = watchdog

        def on_timeout(kwargs_inner, door_label=door_label, watchdog=watchdog):
            if self._abb_watchdogs.get(door_label) is not watchdog:
                return  # already resolved (ack) or superseded by a newer press
            del self._abb_watchdogs[door_label]
            self.log(
                f"ABB-UNLOCK timeout door={door_label} - no ESP ack within "
                f"{self.abb_unlock_ack_timeout_s}s, falling back to ESP unlock sequence",
                level="WARNING",
            )
            fallback_entity = watchdog["trigger_entity"] or f"abb_manual:{door_label}"
            self._schedule_esp_unlock_sequence(
                fallback_entity, watchdog["lock_entity"], watchdog["ring_ts"], watchdog["door_sensor"]
            )

        watchdog["timeout_handle"] = self.run_in(on_timeout, self.abb_unlock_ack_timeout_s)

    def _on_esp_lock_ack(self, entity, attribute, old, new, kwargs):
        """Permanent listener (one per abb_unlock_doors-configured lock, see
        initialize) for the ESP's own unlocking/unlocked ack - the only
        physical confirmation a door actually opened. Resolves a pending ABB
        watchdog for that door as a success, reported exactly like an ESP
        success (see _verify_unlock's own success branch, mirrored here)."""
        try:
            if new not in ("unlocking", "unlocked"):
                return
            door_label = kwargs.get("door_label")
            watchdog = self._abb_watchdogs.get(door_label)
            if watchdog is None:
                return  # no pending ABB attempt for this door right now
            del self._abb_watchdogs[door_label]
            try:
                handle = watchdog.get("timeout_handle")
                if handle and self.timer_running(handle):
                    self.cancel_timer(handle)
            except Exception as e:
                self.log(f"Unexpected error cancelling ABB watchdog timer for {door_label}: {e}", level="DEBUG")

            elapsed = (self.get_now() - watchdog["press_ts"]).total_seconds()
            self.log(f"ABB-UNLOCK ok door={door_label} +{elapsed:.1f}s", level="INFO")

            trigger_entity = watchdog.get("trigger_entity")
            ring_ts = watchdog.get("ring_ts")
            # Same outcome bookkeeping as _verify_unlock's success branch, so the
            # ABB path reports EXACTLY like an ESP success - one report per ring,
            # regardless of which side opened the door. trigger_entity is None
            # for an externally observed press with no ring context, so outcome
            # stays None and nothing is reported (there is no ring to report on).
            outcome = self.unlock_outcomes.get(trigger_entity) if trigger_entity else None
            if outcome is not None and ring_ts is not None and outcome.get("ring_ts") != ring_ts:
                outcome = None
            first_success = outcome is not None and not outcome.get("succeeded")
            if outcome is not None:
                outcome["succeeded"] = True
            if first_success:
                self._mark_ring_succeeded(trigger_entity)
                self._report_auto_open_success(trigger_entity, watchdog["lock_entity"], outcome, "abb")
        except Exception as e:
            self.log(f"ABB unlock ack handling failed for {entity}: {e}", level="WARNING")

    def _on_abb_button_state(self, entity, attribute, old, new, kwargs):
        """Fires on every press of a configured abb_unlock_doors button - ours
        (via _start_abb_unlock) or external (the wall dashboard is expected to
        press this same button directly). HA `button` entities report an ISO
        timestamp as their state, so ANY press is a state change - including:
        - our own: _start_abb_unlock already armed a watchdog for that press
          directly; recognized here via _own_abb_press_at and ignored, or this
          would arm a second, redundant watchdog for the same underlying press.
        - a restart/restore replaying a stale last-pressed timestamp: rejected
          by the freshness check below (ABB_PRESS_FRESHNESS_S), same reasoning
          as the replay-edge guard in _handle_trigger.
        Anything else is a genuine external press: arm the SAME ack watchdog +
        ESP fallback as the app's own auto-open path, just with no ring context
        to report against (see _arm_abb_watchdog).
        """
        try:
            press_ts = self._parse_ts(new)
            if press_ts is None:
                self.log(f"Ignoring unparseable state on ABB button {entity}: {new!r}", level="DEBUG")
                return
            now = self.get_now()
            age_s = (now - press_ts).total_seconds()
            if age_s < 0 or age_s > ABB_PRESS_FRESHNESS_S:
                self.log(f"Ignoring stale ABB button state on {entity}: {new} ({age_s:.1f}s old)", level="DEBUG")
                return
            own_press_at = self._own_abb_press_at.get(entity)
            if own_press_at is not None and abs((now - own_press_at).total_seconds()) <= ABB_PRESS_FRESHNESS_S:
                return  # our own press - _start_abb_unlock already armed its watchdog
            door_label = self.abb_button_to_door.get(entity)
            lock_info = self.door_lock_info.get(door_label) if door_label else None
            if not lock_info:
                return
            self.log(f"ABB-UNLOCK external press detected door={door_label} button={entity}", level="INFO")
            self._arm_abb_watchdog(door_label, lock_info["lock"], lock_info.get("door_sensor"), press_ts)
        except Exception as e:
            self.log(f"ABB button state handling failed for {entity}: {e}", level="WARNING")

    def _is_door_open(self, door_sensor):
        """Check if door is physically open based on door sensor."""
        if not door_sensor:
            return None  # No door sensor configured, can't determine
        
        door_state = self.get_state(door_sensor)
        if door_state is None or door_state in ["unknown", "unavailable"]:
            return None  # Can't determine state
        
        # Door sensors typically use "on" for open, "off" for closed
        # Some may use "open"/"closed" - check both
        return door_state in ["on", "open"]
    
    def _perform_unlock(self, kwargs):
        """Perform lock unlock operation."""
        lock_entity = kwargs.get("lock_entity")
        followup = kwargs.get("followup")
        trigger_entity = kwargs.get("trigger_entity")
        unlock_attempt = kwargs.get("unlock_attempt", 1)
        door_sensor = kwargs.get("door_sensor")

        if not lock_entity:
            return

        # Check if door is already physically open
        door_open = self._is_door_open(door_sensor)
        if door_open is True:
            self.log(f"Door is already open (sensor: {door_sensor}), skipping unlock attempt {unlock_attempt} for {lock_entity}", level="INFO")
            # Door is open, no need to unlock - reliability is more important than speed
            return
        elif door_open is None and door_sensor:
            # Door sensor exists but state is unknown/unavailable - log but proceed with caution
            self.log(f"Door sensor {door_sensor} state unknown/unavailable, proceeding with unlock check", level="DEBUG")

        # Check if lock is already unlocked
        current_state = self.get_state(lock_entity)
        if current_state is None or current_state in ["unknown", "unavailable"]:
            self.log(f"WARNING: Cannot check lock state for {lock_entity} (state: {current_state})", level="WARNING")
            return

        # Always attempt unlock - even if already unlocked, this triggers the relay action
        # which helps delivery people notice the door is open (they hear/see the unlock happen)
        state_before = self.get_state(lock_entity)
        if current_state == "unlocked":
            self.log(f"Lock {lock_entity} already unlocked, but sending unlock command again (attempt {unlock_attempt}) so delivery person notices", level="INFO")
        else:
            self.log(f"Attempting to unlock {lock_entity} (current state: {state_before}, attempt {unlock_attempt})", level="INFO")

        # Attempt to unlock - always send command so relay action is visible/audible
        try:
            # ABB relay can handle multiple unlock commands - this helps delivery people notice
            self.call_service("lock/unlock", entity_id=lock_entity)
            
            # Wait a moment for the lock to respond, then verify state changed
            self.run_in(
                self._verify_unlock,
                delay=2,
                lock_entity=lock_entity,
                state_before=state_before,
                unlock_attempt=unlock_attempt,
                followup=followup,
                door_sensor=door_sensor,
                trigger_entity=trigger_entity,
                ring_ts=kwargs.get("ring_ts"),
            )
            
            self.log(f"Unlock service called for {lock_entity} (attempt {unlock_attempt})", level="INFO")

        except Exception as e:
            self.log(f"Error unlocking {lock_entity} (attempt {unlock_attempt}): {e}", level="ERROR")
            # Don't send follow-up if unlock failed

    def _verify_unlock(self, kwargs):
        """Verify that the unlock actually succeeded by checking state change."""
        lock_entity = kwargs.get("lock_entity")
        state_before = kwargs.get("state_before")
        unlock_attempt = kwargs.get("unlock_attempt", 1)
        followup = kwargs.get("followup")
        door_sensor = kwargs.get("door_sensor")
        trigger_entity = kwargs.get("trigger_entity")
        ring_ts = kwargs.get("ring_ts")

        if not lock_entity:
            return

        # Outcome record for the ring this verify belongs to (None if a newer
        # ring has replaced it - then this verify neither marks nor escalates)
        outcome = self.unlock_outcomes.get(trigger_entity)
        if outcome is not None and ring_ts is not None and outcome.get("ring_ts") != ring_ts:
            outcome = None

        current_state = self.get_state(lock_entity)

        # ESP32 publishes "unlocking" for 3 seconds, then "locked"
        # Accept both "unlocked" and "unlocking" as success
        if current_state in ["unlocked", "unlocking"]:
            first_success = outcome is not None and not outcome.get("succeeded")
            if outcome is not None:
                outcome["succeeded"] = True
            if first_success:
                self._mark_ring_succeeded(trigger_entity)
            self.log(f"OK: Successfully unlocked {lock_entity} (attempt {unlock_attempt})", level="INFO")
            # Check door state after unlock for additional verification
            if door_sensor:
                door_open = self._is_door_open(door_sensor)
                if door_open is True:
                    self.log(f"OK: Door confirmed open after unlock (sensor: {door_sensor})", level="INFO")
                elif door_open is False:
                    self.log(f"WARN: Door still appears closed after unlock (sensor: {door_sensor}) - may need time to open", level="DEBUG")
            # Send follow-up TTS only on first successful unlock
            if unlock_attempt == 1 and followup and self.sonos_notifier:
                try:
                    self.sonos_notifier.notify(message=followup)
                    self.log(f"Sent follow-up TTS for {lock_entity}", level="DEBUG")
                except Exception as e:
                    self.log(f"Error sending follow-up TTS for {lock_entity}: {e}", level="ERROR")
            # Notify + house feed once per ring, on the attempt that first confirms success
            if first_success:
                self._report_auto_open_success(trigger_entity, lock_entity, outcome, unlock_attempt)
        elif current_state == state_before:
            self.log(f"FAIL: Unlock failed for {lock_entity} (attempt {unlock_attempt}): state unchanged ({current_state})", level="WARNING")
        else:
            self.log(f"WARN: Unlock state unclear for {lock_entity} (attempt {unlock_attempt}): was {state_before}, now {current_state}", level="WARNING")

        # After the LAST attempt of a ring: if no attempt succeeded, tell Mikkel.
        # The house already announced "I opened the door" - a silent failure
        # leaves a visitor stranded while everyone believes the door is open.
        if (
            outcome is not None
            and unlock_attempt >= self.unlock_repeat_count
            and not outcome.get("succeeded")
        ):
            self._report_auto_open_failure(trigger_entity, lock_entity, outcome)

    def _report_auto_open_success(self, trigger_entity, lock_entity, outcome, unlock_attempt):
        """First confirmed unlock of a ring: notify Mikkel, log to the house feed."""
        ring_label = outcome.get("ring_label", "door")
        self.log(
            f"AUTO-OPEN: confirmed unlock of {lock_entity} after {ring_label} ring (attempt {unlock_attempt})",
            level="INFO",
        )

        if self.mobile_notifier:
            title = "Intercom auto-opened"
            message = f"Someone rang the {ring_label} and the door was unlocked automatically."
            # Offer the push to the ABB bridge first: it can hold it the few
            # seconds the gateway needs to produce THIS visitor's photo, instead
            # of sending now with the previous visitor's (Mikkel, 2026-08-27).
            # Strictly additive - a bridge that is absent, reloading or broken
            # declines, and the immediate push below is byte-for-byte today's.
            if not self._abb_defer_ring_push(ring_label, title, message):
                # ABB doorbell snapshot, if the bridge can offer one - computed
                # OUTSIDE the try so a (impossible-by-contract) failure could never
                # skip the push itself; None simply reproduces today's text-only
                # notification.
                attachment = self._abb_ring_attachment(ring_label)
                try:
                    self.create_task(self.mobile_notifier.notify(
                        title=title,
                        message=message,
                        target=self.notify_target,
                        data=attachment,
                    ))
                except Exception as e:
                    self.log(f"Auto-open success notification failed: {e}", level="WARNING")

        # No feed entry for a CONFIRMED auto-open (removed 2026-07-24): the ring-time
        # "Announcing on the speakers and opening the door" line already told the story,
        # and success is the expected outcome. Only FAILURE is feed-worthy - see
        # _report_auto_open_failure, which has its own entry.

    def _report_auto_open_failure(self, trigger_entity, lock_entity, outcome):
        """All unlock attempts for a ring failed: log, mobile-notify, house feed."""
        ring_label = outcome.get("ring_label", "door")
        self.unlock_outcomes.pop(trigger_entity, None)
        self._forget_ring(trigger_entity)  # write point (c), terminal - idempotent
        self.log(
            f"AUTO-OPEN FAILED: {self.unlock_repeat_count} unlock attempt(s) on {lock_entity} got no response after {ring_label} ring",
            level="ERROR",
        )

        if self.mobile_notifier:
            # Same strictly-additive ABB snapshot as the success push (None = today's push).
            attachment = self._abb_ring_attachment()
            try:
                self.create_task(self.mobile_notifier.notify(
                    title="Intercom auto-open failed",
                    message=(
                        f"Someone rang the {ring_label} but the door did not open: "
                        f"{self.unlock_repeat_count} unlock attempts got no response from the intercom."
                    ),
                    target=self.notify_target,
                    data=attachment,
                ))
            except Exception as e:
                self.log(f"Auto-open failure notification failed: {e}", level="WARNING")

        # House feed entry - same guarded, cosmetic-only contract as the ring report
        try:
            self.fire_event(
                "house_events_report",
                cause=f"Someone rang the {ring_label}",
                effect=f"Auto-open FAILED - {self.unlock_repeat_count} unlock attempts got no response",
                icon="mdi:alert-circle",
            )
        except Exception as e:
            self.log(f"house_events_report failed: {e}", level="DEBUG")

