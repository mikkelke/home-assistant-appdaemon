import appdaemon.plugins.hass.hassapi as hass  # type: ignore
from datetime import datetime


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

        # Messages
        self.msg_front = self.args.get("tts_message_front", "Someone is at the front door")
        self.msg_back = self.args.get("tts_message_back", "Someone is at the back door")
        self.msg_apt = self.args.get("tts_message_apt", "Someone is at the apartment door")
        self.msg_open_front = self.args.get("door_open_message_front", "I opened the front door")
        self.msg_open_back = self.args.get("door_open_message_back", "I opened the back door")

        self.sonos_notifier = self._get_notifier()
        self.mobile_notifier = self._get_mobile_notifier()
        self.last_trigger_at = {}
        self.pending_unlocks = {}  # Track scheduled unlock callbacks by entity
        self.unlock_outcomes = {}  # Per trigger entity: did any attempt of the current ring succeed

        # Validate entities exist
        self._validate_entities()

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
            self.listen_state(self._handle_trigger, self.front_sensor, new="on", old="off")
        if self.back_sensor:
            self.trigger_map[self.back_sensor] = {
                "message": self.msg_back,
                "lock": self.back_lock,
                "followup": self.msg_open_back,
                "door_sensor": self.back_door_sensor,
                "ring_label": "back door",
            }
            self.listen_state(self._handle_trigger, self.back_sensor, new="on", old="off")
        if self.apt_sensor:
            self.trigger_map[self.apt_sensor] = {
                "message": self.msg_apt,
                "lock": None,
                "followup": None,
                "ring_label": "apartment door",
            }
            self.listen_state(self._handle_trigger, self.apt_sensor, new="on", old="off")

        if not self.trigger_map:
            self.log("CRITICAL: No intercom sensors configured; app will be idle.", level="ERROR")
        else:
            sensors = ", ".join(self.trigger_map.keys())
            self.log(f"Intercom initialized; listening for rings on: {sensors}", level="INFO")

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

        for name, entity_id in entities_to_check:
            state = self.get_state(entity_id)
            if state is None or state in ["unknown", "unavailable"]:
                self.log(f"WARNING: Entity {entity_id} ({name}) not found or unavailable (state: {state})", level="WARNING")
            else:
                self.log(f"Validated entity {entity_id} ({name})", level="DEBUG")

    def _debounced(self, entity):
        """Check if entity trigger should be debounced."""
        last_ts = self.last_trigger_at.get(entity)
        if not last_ts:
            return False
        elapsed = (datetime.now() - last_ts).total_seconds()
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

    def _handle_trigger(self, entity, attr, old, new, kwargs):
        """Handle doorbell sensor trigger."""
        # Only process transitions from off to on
        if old != "off" or new != "on":
            self.log(f"Ignoring non-transition trigger from {entity} (old={old}, new={new})", level="DEBUG")
            return

        info = self.trigger_map.get(entity)
        if not info:
            return

        if self._debounced(entity):
            self.log(f"Debounced trigger from {entity}", level="DEBUG")
            return

        self.last_trigger_at[entity] = datetime.now()
        self.log(f"Ring detected from {entity}", level="INFO")

        # Cancel any pending unlocks for this entity (new trigger takes precedence)
        self._cancel_pending_unlocks(entity)

        # Check if auto-open is enabled before deciding which message to send
        auto_open_enabled = False
        lock_entity = None
        
        if self.auto_open_entity and info.get("lock"):
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
            # No follow-up TTS needed since the combined message covers it
            self.pending_unlocks[entity] = []
            # Track whether ANY attempt of THIS ring succeeds; ring_ts guards
            # against a stale verify from a cancelled ring escalating falsely.
            ring_ts = self.last_trigger_at[entity]
            self.unlock_outcomes[entity] = {
                "ring_ts": ring_ts,
                "succeeded": False,
                "ring_label": info.get("ring_label", "door"),
            }
            for i in range(self.unlock_repeat_count):
                delay = self.unlock_delay_s + i * self.unlock_repeat_interval_s

                # Store handle reference in a list to allow modification in closure
                handle_ref = [None]  # Use list to allow modification in closure

                # Capture loop variables in closure to avoid late binding issues
                trigger_ent = entity
                attempt_num = i + 1

                def unlock_callback(kwargs_inner):
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

                # Get door sensor for this trigger
                door_sensor = info.get("door_sensor") if info else None

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

        # Send TTS - combined message if auto-open enabled, otherwise just initial message
        if self.sonos_notifier:
            try:
                if auto_open_enabled and info.get("followup"):
                    # Combine messages: "Someone is at the front door and I opened the front door"
                    combined_message = f"{info['message']} and {info['followup']}"
                    self.sonos_notifier.notify(message=combined_message)
                    self.log(f"Sent combined TTS message for {entity} (auto-open enabled)", level="DEBUG")
                else:
                    # Just send the initial message
                    self.sonos_notifier.notify(message=info["message"])
            except Exception as e:
                self.log(f"Error sending TTS for {entity}: {e}", level="ERROR")
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
            try:
                self.create_task(self.mobile_notifier.notify(
                    title="Intercom auto-opened",
                    message=f"Someone rang the {ring_label} and the door was unlocked automatically.",
                    target=self.notify_target,
                ))
            except Exception as e:
                self.log(f"Auto-open success notification failed: {e}", level="WARNING")

        # House feed entry for the CONFIRMED unlock - distinct from the optimistic
        # ring-time "opening the door" line fired in _handle_trigger before verification.
        try:
            self.fire_event(
                "house_events_report",
                cause=f"Someone rang the {ring_label}",
                effect="Auto-open confirmed unlocked",
                icon="mdi:lock-open-variant",
            )
        except Exception as e:
            self.log(f"house_events_report failed: {e}", level="DEBUG")

    def _report_auto_open_failure(self, trigger_entity, lock_entity, outcome):
        """All unlock attempts for a ring failed: log, mobile-notify, house feed."""
        ring_label = outcome.get("ring_label", "door")
        self.unlock_outcomes.pop(trigger_entity, None)
        self.log(
            f"AUTO-OPEN FAILED: {self.unlock_repeat_count} unlock attempt(s) on {lock_entity} got no response after {ring_label} ring",
            level="ERROR",
        )

        if self.mobile_notifier:
            try:
                self.create_task(self.mobile_notifier.notify(
                    title="Intercom auto-open failed",
                    message=(
                        f"Someone rang the {ring_label} but the door did not open: "
                        f"{self.unlock_repeat_count} unlock attempts got no response from the intercom."
                    ),
                    target=self.notify_target,
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

