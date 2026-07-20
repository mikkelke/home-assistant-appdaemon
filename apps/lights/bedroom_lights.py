import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import datetime
import time

import cover_util
import lighting_actions
import room_state_darkness

class BedroomLights(hass.Hass):
    """
    Bedroom lighting with occupancy-based auto control.

    Effective occupancy (gate only - does not bypass bright shutoff, sleep mode, or blind-closed):
      FP300 mmWave presence (``bedroom_presence_sensor``) OR any ``bedroom_presence_extra`` OR
      bathroom PIR (when bathroom door open) OR an active bed-light session (see below).

    Brightness: ``room_state_darkness`` (see LIGHTING_STANDARD.md) - refreshed every evaluation.

    Bed-light session (bed vs ceiling): a debounced latch, ``self._session``, mirrored to the
    HA helper ``bed_session_entity`` (single source of truth across app restarts/reloads) and
    read by ``wakeup_bedroom`` for the wake-ramp hand-off. Withings bedside sensors
    (``withings_in_bed_entities``) are used ONLY for their reliable ON edge to START a session -
    their OFF edge is ignored forever (Withings misses getting-up far too often to trust for
    ending one). A session also starts on sleep-mode ON, or on a manual bed-light-on while the
    room is dark. It ends only once the FP300 presence sensor has read "off" continuously for
    ``session_exit_debounce_sec`` while sleep mode is not active - the exit timer is armed on
    presence-off and cancelled the instant presence returns, so someone moving around the bed
    (FP300 briefly clearing) never flips the room to ceiling lights. Restart-safe:
    ``_reconcile_session`` rebuilds ``self._session`` on init from Withings/sleep-mode/the
    persisted helper + FP300's ``last_changed`` epoch (same pattern as
    ``manual_override_timeout``), before the first light evaluation runs.

    Truth table (after the vacant/occupied gate):
      - No effective occupancy -> all lights OFF
      - Occupied + bright -> bed + ceiling OFF
      - Occupied + dark + sleep mode -> no auto-on
      - Occupied + dark + blind at/above ``blind_closed_threshold`` -> no auto-on
      - Occupied + dark + bed session active -> bed ON, ceiling OFF
      - Occupied + dark + bed session ended -> ceiling ON, bed OFF

    Manual wall switch and remote are unchanged.
    """

    def initialize(self):
        # --- Configuration ---
        self.bed_lights = self.args.get("bed_lights")
        self.ceiling_lights = self.args.get("ceiling_lights")
        self.withings_in_bed_entities = list(
            self.args.get("withings_in_bed_entities") or self.args.get("in_bed_entities") or []
        )

        self.switch_device_id = self.args.get("switch_device_id")
        self.remote_device_id = self.args.get("remote_device_id")

        self.raw_bathroom_pir_sensor = self.args.get("raw_bathroom_pir_sensor")
        self.bathroom_door_sensor = self.args.get("bathroom_door_sensor")

        self.mikkel_sleep_entity = self.args.get("mikkel_sleep_entity")
        self.bedroom_blind_entity = self.args.get("bedroom_blind_entity")
        # "Closed" for a low-battery blind that parks at 99%; interpreted via cover_util.
        self.blind_closed_threshold = int(self.args.get("blind_closed_threshold", 95))
        self.room_state_text_entity = self.args.get(
            "room_state_text_entity", "sensor.room_state_bedroom_bathroom"
        )
        self.darkness_confirmed_sensor = self.args.get("darkness_confirmed_sensor_entity")

        # Bed-light session latch (see class docstring).
        self.bed_session_entity = self.args.get(
            "bed_session_entity", "input_boolean.bedroom_bed_session"
        )
        self.bedroom_presence_sensor = self.args.get(
            "bedroom_presence_sensor", "binary_sensor.bedroom_presence_presence"
        )
        self.bedroom_presence_extra = list(self.args.get("bedroom_presence_extra") or [])
        self.session_exit_debounce_sec = int(self.args.get("session_exit_debounce_sec", 90))

        self.log_level = self.args.get("verbosity_level", "normal")

        self._last_off_is_dark: bool | None = None
        self._session = False
        self._session_exit_timer = None

        if not all(
            [
                self.bed_lights,
                self.ceiling_lights,
                self.switch_device_id,
                self.remote_device_id,
                self.bedroom_presence_sensor,
                self.raw_bathroom_pir_sensor,
                self.mikkel_sleep_entity,
            ]
        ):
            self.error("One or more critical entities are missing. App may not function correctly.")

        self.listen_event(
            self._on_bed_lights_switch,
            "zwave_js_value_notification",
            device_id=self.switch_device_id,
            command_class=91,
            property_key="001",
            value="KeyPressed",
        )
        self.listen_event(
            self._on_ceiling_lights_switch,
            "zwave_js_value_notification",
            device_id=self.switch_device_id,
            command_class=91,
            property_key="002",
            value="KeyPressed",
        )
        self.listen_event(
            self._on_remote_control,
            "zwave_js_value_notification",
            device_id=self.remote_device_id,
            command_class=91,
            property_key="003",
            value="KeyPressed",
        )

        if self.raw_bathroom_pir_sensor:
            try:
                self.listen_state(self._on_raw_bathroom_pir_change, self.raw_bathroom_pir_sensor)
            except Exception as e:
                self.log(f"Error registering bathroom PIR/presence handler: {e}", level="ERROR")

        if self.bathroom_door_sensor:
            try:
                self.listen_state(self._on_bathroom_door_change, self.bathroom_door_sensor)
            except Exception as e:
                self.log(f"Error registering bathroom door handler: {e}", level="ERROR")

        lighting_actions.register_room_state_push_listeners(
            self,
            self._on_room_state_push,
            room_state_entity=self.room_state_text_entity,
            darkness_sensor=self.darkness_confirmed_sensor,
        )

        # Bed-light session latch: FP300 both edges start/arm-exit the session; Withings and
        # sleep-mode ON edges start one; a manual bed-light-on while dark also starts one.
        # See class docstring and _set_session/_reconcile_session.
        if self.bedroom_presence_sensor:
            try:
                self.listen_state(self._on_presence_change_session, self.bedroom_presence_sensor)
            except Exception as e:
                self.log(f"Error registering bedroom presence handler: {e}", level="ERROR")

        for ent in self.withings_in_bed_entities:
            try:
                self.listen_state(self._on_withings_in_bed_on, ent, old="off", new="on")
            except Exception as e:
                self.log(f"Failed to listen to Withings in-bed sensor {ent}: {e}", level="ERROR")

        if self.mikkel_sleep_entity:
            try:
                self.listen_state(
                    self._on_sleep_mode_on, self.mikkel_sleep_entity, old="off", new="on"
                )
            except Exception as e:
                self.log(f"Error registering sleep-mode session handler: {e}", level="ERROR")

        if self.bed_lights:
            try:
                self.listen_state(
                    self._on_bed_lights_on_manual, self.bed_lights, old="off", new="on"
                )
            except Exception as e:
                self.log(f"Error registering manual bed-light session handler: {e}", level="ERROR")

        # Global mechanism: per-room manual override pauses ALL automatic light actions
        self.manual_override_entity = self.args.get("manual_override_boolean")
        if self.manual_override_entity:
            self.listen_state(self._on_manual_override_change, self.manual_override_entity)

        self.run_in(self._initial_check, 5)
        self.log("Bedroom: App initialized", level="INFO")

    def _initial_check(self, kwargs=None):
        self._reconcile_session()
        self._evaluate_lights("INITIAL_CHECK")

    def _lighting_decisions(self, trigger: str = ""):
        """Brightness from darkness_calculator committed state (+ pending)."""
        sensor = getattr(self, "darkness_confirmed_sensor", None)
        on_d = room_state_darkness.evaluate_auto_on(
            self,
            self.room_state_text_entity,
            default_dark=True,
            darkness_sensor=sensor,
        )
        off_d = room_state_darkness.evaluate_auto_off(
            self,
            self.room_state_text_entity,
            default_dark=True,
            darkness_sensor=sensor,
        )
        if trigger == "PIR_PRESENCE_ON" and not on_d.is_dark:
            self.log(
                f"Bedroom: {room_state_darkness.format_decision_log(on_d)} - skip auto-on",
                level="INFO",
            )
        return on_d, off_d

    def _on_bed_lights_switch(self, event_name, data, kwargs):
        self.log("Bedroom: Bed lights switch pressed - toggling", level="INFO")
        self.toggle(self.bed_lights)

    def _on_ceiling_lights_switch(self, event_name, data, kwargs):
        self.log("Bedroom: Ceiling lights switch pressed - toggling", level="INFO")
        self.toggle(self.ceiling_lights)

    def _on_remote_control(self, event_name, data, kwargs):
        self.log("Bedroom: Remote pressed", level="INFO")
        bed_on = self.get_state(self.bed_lights) == "on"
        ceiling_on = self.get_state(self.ceiling_lights) == "on"
        if bed_on or ceiling_on:
            self.log("Bedroom: Turning off both lights", level="INFO")
            self.turn_off(self.bed_lights)
            self.turn_off(self.ceiling_lights)
        else:
            self.log("Bedroom: Turning on bed lights", level="INFO")
            self.turn_on(self.bed_lights)

    def _get_bedroom_presence(self) -> bool:
        return self.get_state(self.bedroom_presence_sensor) == "on"

    def _is_bathroom_door_open(self) -> bool:
        if not self.bathroom_door_sensor:
            return True
        return self.get_state(self.bathroom_door_sensor) == "on"

    def _get_bathroom_presence(self) -> bool:
        if not self._is_bathroom_door_open():
            return False
        return self.get_state(self.raw_bathroom_pir_sensor) == "on"

    def _get_effective_occupancy(self) -> bool:
        return (
            self._get_bedroom_presence()
            or any(self.get_state(e) == "on" for e in self.bedroom_presence_extra)
            or self._get_bathroom_presence()
            or self._session
        )

    def _set_session(self, on: bool, reason: str) -> None:
        if on == self._session:
            return
        self._session = on
        try:
            self.call_service(
                "input_boolean/turn_on" if on else "input_boolean/turn_off",
                entity_id=self.bed_session_entity,
            )
        except Exception as e:
            self.log(f"Bedroom: bed session helper mirror failed: {e}", level="WARNING")
        self.log(f"Bedroom: bed session {'START' if on else 'END'} ({reason})", level="INFO")
        self._evaluate_lights("SESSION_ON" if on else "SESSION_OFF")

    def _enter_session(self, reason: str) -> None:
        self._cancel_session_exit()
        self._set_session(True, reason)

    def _on_withings_in_bed_on(self, entity, attribute, old, new, kwargs):
        try:
            self._enter_session("withings in-bed")
        except Exception as e:
            self.log(f"Error in withings in-bed handler: {e}", level="ERROR")

    def _on_sleep_mode_on(self, entity, attribute, old, new, kwargs):
        try:
            self._enter_session("sleep mode on")
        except Exception as e:
            self.log(f"Error in sleep-mode session handler: {e}", level="ERROR")

    def _on_bed_lights_on_manual(self, entity, attribute, old, new, kwargs):
        try:
            if self._session:
                return
            try:
                _, off_d = self._lighting_decisions("BED_MANUAL")
            except Exception:
                return
            if off_d.is_dark:
                self._enter_session("manual bed light while dark")
        except Exception as e:
            self.log(f"Error in manual bed-light session handler: {e}", level="ERROR")

    def _on_presence_change_session(self, entity, attribute, old, new, kwargs):
        try:
            if new == "on":
                self._cancel_session_exit()
                self._evaluate_lights("PRESENCE_ON")
            elif new == "off":
                if self._session and not self._is_sleep_mode_active():
                    self._arm_session_exit()
            # any other value (unavailable/unknown): no-op (hold)
        except Exception as e:
            self.log(f"Error in bedroom presence session handler: {e}", level="ERROR")

    def _arm_session_exit(self) -> None:
        if self._session_exit_timer is None:
            self._session_exit_timer = self.run_in(
                self._session_exit_fire, self.session_exit_debounce_sec
            )

    def _cancel_session_exit(self) -> None:
        if self._session_exit_timer is not None:
            try:
                self.cancel_timer(self._session_exit_timer)
            except Exception:
                pass
            self._session_exit_timer = None

    def _session_exit_fire(self, kwargs):
        self._session_exit_timer = None
        if (
            self._session
            and self.get_state(self.bedroom_presence_sensor) != "on"
            and not self._is_sleep_mode_active()
        ):
            self._set_session(False, f"presence clear {self.session_exit_debounce_sec}s")

    def _reconcile_session(self) -> None:
        """Restart-safe rebuild of ``self._session`` (see class docstring). Modeled on
        ``manual_override_timeout._resync``'s epoch math (``last_changed`` -> elapsed)."""
        fp300_on = self.get_state(self.bedroom_presence_sensor) == "on"
        withings = self._withings_in_bed()
        sleep_on = self._is_sleep_mode_active()
        persisted = self.get_state(self.bed_session_entity) == "on"
        if withings or sleep_on:
            want = True
        elif persisted and fp300_on:
            want = True
        elif persisted and not fp300_on:
            clear_for = None
            lc = self.get_state(self.bedroom_presence_sensor, attribute="last_changed")
            if lc:
                try:
                    clear_for = time.time() - datetime.datetime.fromisoformat(str(lc)).timestamp()
                except (ValueError, TypeError):
                    clear_for = None
            want = clear_for is not None and clear_for < self.session_exit_debounce_sec
        else:
            want = False
        self._session = want
        if (self.get_state(self.bed_session_entity) == "on") != want:
            self.call_service(
                "input_boolean/turn_on" if want else "input_boolean/turn_off",
                entity_id=self.bed_session_entity,
            )

    def _withings_in_bed(self) -> bool:
        return any(self.get_state(b) == "on" for b in self.withings_in_bed_entities)

    def _on_raw_bathroom_pir_change(self, entity, attribute, old, new, kwargs):
        try:
            self.log(
                f"Bedroom: Bathroom presence {'detected' if new == 'on' else 'lost'}",
                level="INFO",
            )
            self._evaluate_lights("BATHROOM_PIR_CHANGE")
        except Exception as e:
            self.log(f"Error in bathroom PIR handler: {e}", level="ERROR")

    def _on_bathroom_door_change(self, entity, attribute, old, new, kwargs):
        try:
            state = "opened" if new == "on" else "closed"
            self.log(f"Bedroom: Bathroom door {state}", level="INFO")
            self._evaluate_lights("BATHROOM_DOOR")
        except Exception as e:
            self.log(f"Error in bathroom door handler: {e}", level="ERROR")

    def _on_room_state_push(self, entity, attribute, old, new, kwargs):
        try:
            _, off_d = self._lighting_decisions(attribute or "state")
            if self._last_off_is_dark is not None and off_d.is_dark != self._last_off_is_dark:
                self.log(
                    f"Bedroom: Room is {'dark' if off_d.is_dark else 'bright'} [{off_d.rule}]",
                    level="INFO",
                )
            self._last_off_is_dark = off_d.is_dark
            self._evaluate_lights(attribute or "state")
        except Exception as e:
            self.log(f"Bedroom: room_state push: {e}", level="ERROR")

    def _evaluate_lights(self, trigger="UNKNOWN"):
        try:
            if lighting_actions.manual_override_active(self, self.manual_override_entity):
                return
            on_d, off_d = self._lighting_decisions(trigger)
            occupied = self._get_effective_occupancy()
            sleep_mode = self._is_sleep_mode_active()
            blind_closed = self._is_blind_closed()
            lights_on = self._are_any_lights_on()

            if not occupied:
                self._turn_off_all()
                return

            if not off_d.is_dark:
                if lights_on:
                    self.log(
                        "Bedroom: Turning OFF (bright + occupied - sun is sufficient)",
                        level="INFO",
                    )
                    self.turn_off(self.bed_lights)
                    self.turn_off(self.ceiling_lights)
                return

            if sleep_mode or blind_closed or not on_d.is_dark:
                if sleep_mode and self.log_level == "debug":
                    self.log("Dark + occupied but sleep mode - no auto-on", level="DEBUG")
                if blind_closed and self.log_level == "debug":
                    self.log("Dark + occupied but blind closed - no auto-on", level="DEBUG")
                return

            if self._session:
                if self.get_state(self.ceiling_lights) == "on":
                    self.log("Bedroom: Ceiling OFF (bed-light session active)", level="INFO")
                    self.turn_off(self.ceiling_lights)
                if self.get_state(self.bed_lights) != "on":
                    self.log("Bedroom: Bed lights ON (dark + occupied, bed session)", level="INFO")
                    self.turn_on(self.bed_lights)
            else:
                if self.get_state(self.bed_lights) == "on":
                    self.log("Bedroom: Bed lights OFF (session ended, using ceiling)", level="INFO")
                    self.turn_off(self.bed_lights)
                if self.get_state(self.ceiling_lights) != "on":
                    self.log("Bedroom: Ceiling ON (dark + occupied, session ended)", level="INFO")
                    self.turn_on(self.ceiling_lights)

        except Exception as e:
            self.log(f"Error in light evaluation: {e}", level="ERROR")

    def _turn_off_all(self):
        try:
            bed_on = self.get_state(self.bed_lights) == "on"
            ceiling_on = self.get_state(self.ceiling_lights) == "on"
            if bed_on or ceiling_on:
                self.log(
                    "Bedroom: Turning OFF all lights (no effective occupancy)",
                    level="INFO",
                )
                self.turn_off(self.bed_lights)
                self.turn_off(self.ceiling_lights)
        except Exception as e:
            self.log(f"Error turning off lights: {e}", level="ERROR")

    def _are_any_lights_on(self) -> bool:
        try:
            if self.get_state(self.bed_lights) == "on":
                return True
            if self.get_state(self.ceiling_lights) == "on":
                return True
        except Exception:
            pass
        return False

    def _is_sleep_mode_active(self) -> bool:
        try:
            if self.get_state(self.mikkel_sleep_entity) == "on":
                return True
        except Exception:
            pass
        return False

    def _is_blind_closed(self) -> bool:
        return cover_util.is_closed(
            self, self.bedroom_blind_entity, threshold=self.blind_closed_threshold
        )

    def _on_manual_override_change(self, entity, attribute, old, new, kwargs):
        """Global mechanism: manual override toggle - pause/resume automatic lighting.

        On clearing, only re-evaluate an EMPTY room (cleanup: lights left burning). While
        someone is in the room, resuming instantly would apply the state machine to a scene
        the human just hand-arranged - e.g. the 12 h timeout expiring at night would flip
        the bed lights back ON with someone in bed (user-reported 2026-07-16). An occupied
        room resumes automatic control on the next natural trigger (PIR/in-bed/room-state)."""
        if new == "on":
            self.log("Bedroom: manual override ON - automatic lighting paused", level="INFO")
        elif new == "off":
            try:
                occupied = self._get_effective_occupancy()
            except Exception:
                occupied = True  # fail toward not touching the lights
            if occupied:
                self.log(
                    "Bedroom: manual override OFF - room occupied, resuming on next trigger",
                    level="INFO",
                )
            else:
                self.log("Bedroom: manual override OFF - resuming automatic lighting", level="INFO")
                self._evaluate_lights("OVERRIDE_CLEARED")
