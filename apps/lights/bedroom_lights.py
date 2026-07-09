import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import lighting_actions
import room_state_darkness

class BedroomLights(hass.Hass):
    """
    Bedroom lighting with occupancy-based auto control.

    Effective occupancy (gate only - does not bypass bright shutoff, sleep mode, or blind-closed):
      bedroom PIR OR bathroom PIR (when bathroom door open) OR any configured in-bed sensor.

    Brightness: ``room_state_darkness`` (see LIGHTING_STANDARD.md) - refreshed every evaluation.

    Truth table (after the vacant/occupied gate):
      - No effective occupancy -> all lights OFF
      - Occupied + bright -> bed + ceiling OFF
      - Occupied + dark + sleep mode -> no auto-on
      - Occupied + dark + blind fully closed -> no auto-on
      - Occupied + dark + in_bed_entities configured + in bed -> bed ON, ceiling OFF
      - Occupied + dark + in_bed_entities configured + not in bed -> ceiling ON, bed OFF
      - Occupied + dark + no in_bed_entities + lights off -> bed ON (legacy fallback)

    Manual wall switch and remote are unchanged.
    """

    def initialize(self):
        # --- Configuration ---
        self.bed_lights = self.args.get("bed_lights")
        self.ceiling_lights = self.args.get("ceiling_lights")
        self.in_bed_entities = list(self.args.get("in_bed_entities") or [])

        self.switch_device_id = self.args.get("switch_device_id")
        self.remote_device_id = self.args.get("remote_device_id")

        self.raw_pir_sensor = self.args.get("raw_pir_sensor")
        self.raw_bathroom_pir_sensor = self.args.get("raw_bathroom_pir_sensor")
        self.bathroom_door_sensor = self.args.get("bathroom_door_sensor")

        self.mikkel_sleep_entity = self.args.get("mikkel_sleep_entity")
        self.bedroom_blind_entity = self.args.get("bedroom_blind_entity")
        self.room_state_text_entity = self.args.get(
            "room_state_text_entity", "sensor.room_state_bedroom_bathroom"
        )
        self.darkness_confirmed_sensor = self.args.get("darkness_confirmed_sensor_entity")

        self.log_level = self.args.get("verbosity_level", "normal")

        self._last_off_is_dark: bool | None = None

        if not all(
            [
                self.bed_lights,
                self.ceiling_lights,
                self.switch_device_id,
                self.remote_device_id,
                self.raw_pir_sensor,
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

        if self.raw_pir_sensor:
            try:
                self.listen_state(
                    self._on_raw_bedroom_pir_on,
                    self.raw_pir_sensor,
                    old="off",
                    new="on",
                )
            except Exception as e:
                self.log(f"Error registering bedroom PIR handler: {e}", level="ERROR")

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

        for ent in self.in_bed_entities:
            try:
                self.listen_state(self._on_in_bed_change, ent)
            except Exception as e:
                self.log(f"Failed to listen to in-bed sensor {ent}: {e}", level="ERROR")

        # Global mechanism: per-room manual override pauses ALL automatic light actions
        self.manual_override_entity = self.args.get("manual_override_boolean")
        if self.manual_override_entity:
            self.listen_state(self._on_manual_override_change, self.manual_override_entity)

        self.run_in(self._initial_check, 5)
        self.log("Bedroom: App initialized", level="INFO")

    def _initial_check(self, kwargs=None):
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
        return self.get_state(self.raw_pir_sensor) == "on"

    def _is_bathroom_door_open(self) -> bool:
        if not self.bathroom_door_sensor:
            return True
        return self.get_state(self.bathroom_door_sensor) == "on"

    def _get_bathroom_presence(self) -> bool:
        if not self._is_bathroom_door_open():
            return False
        return self.get_state(self.raw_bathroom_pir_sensor) == "on"

    def _anyone_in_bed(self) -> bool:
        for ent in self.in_bed_entities:
            try:
                if self.get_state(ent) == "on":
                    return True
            except Exception:
                pass
        return False

    def _get_effective_occupancy(self) -> bool:
        return (
            self._get_bedroom_presence()
            or self._get_bathroom_presence()
            or self._anyone_in_bed()
        )

    def _on_raw_bedroom_pir_on(self, entity, attribute, old, new, kwargs):
        try:
            self.log("Bedroom: PIR detected - fast evaluation", level="INFO")
            self._evaluate_lights("PIR_ON")
        except Exception as e:
            self.log(f"Error in bedroom PIR handler: {e}", level="ERROR")

    def _on_in_bed_change(self, entity, attribute, old, new, kwargs):
        try:
            self.log(f"Bedroom: In-bed state changed ({entity} {old} -> {new})", level="INFO")
            self._evaluate_lights("IN_BED_CHANGE")
        except Exception as e:
            self.log(f"Error in in-bed handler: {e}", level="ERROR")

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
            in_bed = self._anyone_in_bed()

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

            if self.in_bed_entities:
                if in_bed:
                    if self.get_state(self.ceiling_lights) == "on":
                        self.log("Bedroom: Ceiling lights OFF (someone in bed)", level="INFO")
                        self.turn_off(self.ceiling_lights)
                    if self.get_state(self.bed_lights) != "on":
                        self.log(
                            "Bedroom: Bed lights ON (dark + occupied, in bed)",
                            level="INFO",
                        )
                        self.turn_on(self.bed_lights)
                else:
                    if self.get_state(self.bed_lights) == "on":
                        self.log(
                            "Bedroom: Bed lights OFF (nobody in bed, using ceiling)",
                            level="INFO",
                        )
                        self.turn_off(self.bed_lights)
                    if self.get_state(self.ceiling_lights) != "on":
                        self.log(
                            "Bedroom: Ceiling lights ON (dark + occupied, not in bed)",
                            level="INFO",
                        )
                        self.turn_on(self.ceiling_lights)
            elif not lights_on:
                self.log(
                    "Bedroom: Bed lights ON (dark + occupied, no in-bed split)",
                    level="INFO",
                )
                self.turn_on(self.bed_lights)

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
        if not self.bedroom_blind_entity:
            return False
        try:
            blind_pos = int(
                self.get_state(self.bedroom_blind_entity, attribute="current_position") or 0
            )
            return blind_pos >= 100
        except (ValueError, TypeError):
            return False

    def _on_manual_override_change(self, entity, attribute, old, new, kwargs):
        """Global mechanism: manual override toggle - pause/resume automatic lighting."""
        if new == "on":
            self.log("Bedroom: manual override ON - automatic lighting paused", level="INFO")
        elif new == "off":
            self.log("Bedroom: manual override OFF - resuming automatic lighting", level="INFO")
            self._evaluate_lights("OVERRIDE_CLEARED")
