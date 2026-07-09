import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import lighting_actions
import room_state_darkness


class ClaudiasRoomLights(hass.Hass):
    """
    Claudias Room lighting: manual switches + global ``room_state`` push from ``darkness_calculator``.
    PIR feeds the calculator only; this app reacts when ``sensor.room_state_claudias_room`` updates.
    """

    def initialize(self):
        self.ceiling_light = self.args["ceiling_light"]
        self.floor_light = self.args["floor_light"]
        self.overnight_guest = self.args["overnight_guest"]
        self.room_state_text_entity = self.args.get("room_state_text_entity")
        self.darkness_confirmed_sensor = self.args.get("darkness_confirmed_sensor_entity")
        self.raw_pir_sensor = self.args.get("raw_pir_sensor")
        self.log_level = self.args.get("verbosity_level", "normal")
        self._last_off_is_dark = None

        self.listen_state(self._on_switch_state, "event.claudias_room_switch_action", attribute="event_type")

        if self.raw_pir_sensor:
            self.listen_state(
                self._on_raw_pir_on,
                self.raw_pir_sensor,
                old="off",
                new="on",
            )

        lighting_actions.register_room_state_push_listeners(
            self,
            self._on_room_state_push,
            room_state_entity=self.room_state_text_entity,
            darkness_sensor=self.darkness_confirmed_sensor,
        )

        # Global mechanism: per-room manual override pauses ALL automatic light actions
        self.manual_override_entity = self.args.get("manual_override_boolean")
        if self.manual_override_entity:
            self.listen_state(self._on_manual_override_change, self.manual_override_entity)

        self.run_in(lambda _: self._apply_lights("STARTUP"), 5)

    def _is_local_occupied(self) -> bool:
        if not self.raw_pir_sensor:
            return False
        try:
            return self.get_state(self.raw_pir_sensor) == "on"
        except Exception:
            return False

    def _on_raw_pir_on(self, entity, attribute, old, new, kwargs):
        """Fast path: react on PIR before darkness_calculator room_state push."""
        self._apply_lights("PIR_ON")

    def _on_room_state_push(self, entity, attribute, old, new, kwargs):
        """Brain A pushed - apply global dark/bright + occupancy."""
        try:
            trigger = attribute or "state"
            off_d = room_state_darkness.evaluate_auto_off(
                self,
                self.room_state_text_entity,
                default_dark=False,
                darkness_sensor=self.darkness_confirmed_sensor,
            )
            if self._last_off_is_dark is not None and off_d.is_dark != self._last_off_is_dark:
                self.log(
                    f"Claudias Room: Room is {'dark' if off_d.is_dark else 'bright'} [{off_d.rule}]",
                    level="INFO",
                )
            self._last_off_is_dark = off_d.is_dark
            self._apply_lights(trigger)
        except Exception as e:
            self.log(f"Claudias Room: room_state push handler: {e}", level="ERROR")

    def _apply_lights(self, trigger: str = "ROOM_STATE"):
        try:
            if lighting_actions.manual_override_active(self, self.manual_override_entity):
                return
            lights_on = self.get_state(self.ceiling_light) == "on"
            block = self.get_state(self.overnight_guest) == "on"
            occupied = self._is_local_occupied()
            if trigger == "PIR_ON":
                occupied = True
            action = lighting_actions.apply_global_lighting(
                self,
                room_state_entity=self.room_state_text_entity,
                darkness_sensor=self.darkness_confirmed_sensor,
                default_dark=False,
                lights_on=lights_on,
                turn_on=lambda: self.turn_on(self.ceiling_light),
                turn_off=lambda: self.turn_off(self.ceiling_light),
                occupied=occupied,
                block_auto_on=block,
                log_fn=lambda m: self._log_action("AUTO", m),
            )
            if self.log_level == "debug":
                self.log(f"Claudias Room: [{trigger}] action={action}", level="DEBUG")
        except Exception as e:
            self.log(f"Claudias Room: apply lights: {e}", level="ERROR")

    def _on_switch_state(self, entity, attribute, old, new, kwargs):
        try:
            event_type = new
            if event_type == "press_1":
                if self.get_state(self.ceiling_light) == "off":
                    self.turn_on(self.ceiling_light)
                    self._log_action("ON", "ceiling switch")
                else:
                    self.turn_off(self.ceiling_light)
                    self._log_action("OFF", "ceiling switch")
            elif event_type == "press_3":
                if self.get_state(self.floor_light) == "off":
                    self.turn_on(self.floor_light)
                    self._log_action("ON", "floor switch")
                else:
                    self.turn_off(self.floor_light)
                    self._log_action("OFF", "floor switch")
        except Exception as e:
            self.log(f"Error in switch event handler: {e}", level="ERROR")

    def _log_action(self, action, reason="", score=None):
        if self.log_level == "quiet":
            return
        score_str = f" (score: {score:.2f})" if score is not None else ""
        reason_str = f": {reason}" if reason else ""
        self.log(f"LIGHTS {action} [claudias_room]{reason_str}{score_str}", level="INFO")

    def _on_manual_override_change(self, entity, attribute, old, new, kwargs):
        """Global mechanism: manual override toggle - pause/resume automatic lighting."""
        if new == "on":
            self.log("Claudias Room: manual override ON - automatic lighting paused", level="INFO")
        elif new == "off":
            self.log("Claudias Room: manual override OFF - resuming automatic lighting", level="INFO")
            self._apply_lights("OVERRIDE_CLEARED")
