import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class ClaudiasRoomLights(hass.Hass):
    """Claudias Room lighting: manual control, with a leave-home safety-off.

    The room is driven by the physical wall switch (handled here) and direct
    app/dashboard control of the light entities (which never routed through this app).
    The old automatic lighting (PIR occupancy + ``darkness_calculator`` room_state
    push) was disabled 2026-07-20 (user request); ``darkness_calculator`` still
    publishes ``sensor.room_state_claudias_room`` / ``sensor.darkness_claudias_room``,
    so restoring full automation is a straight revert of the commit that stripped it.

    The one automatic action kept: if Claudia leaves the apartment (``person.claudia``
    goes home -> away) with a light still on, that light is turned off - unless the
    room still reads occupied (PIR), so a sibling left behind keeps their light.

    Switch map: ``press_1`` (top-left) toggles the ceiling light, ``press_3`` (top-right)
    the string lights. ``press_2``/``press_4`` (bottom row) are unmapped.
    """

    def initialize(self):
        self.ceiling_light = self.args["ceiling_light"]
        self.string_light = self.args["string_light"]
        self.presence_entity = self.args.get("presence_entity")
        self.pir_sensor = self.args.get("pir_sensor")
        self.log_level = self.args.get("verbosity_level", "normal")

        self.listen_state(self._on_switch_state, "event.claudias_room_switch_action", attribute="event_type")

        # Safety-off: lights left on when Claudia leaves the apartment
        if self.presence_entity:
            self.listen_state(self._on_left_home, self.presence_entity, old="home")

    def _on_left_home(self, entity, attribute, old, new, kwargs):
        """Turn off any lights Claudia left on when she leaves the apartment.

        Fires on a real home -> away transition (``old='home'``); the guard drops
        flaps to ``unknown``/``unavailable`` (tracker dropout while she is still home)
        so those never kill the lights."""
        if new in (None, "home", "unknown", "unavailable"):
            return
        try:
            # Occupancy guard: only clean up an empty room. If the PIR still reads
            # occupied, someone else is in there (e.g. a sibling) - leave their light
            # alone. PIR unavailable -> can't confirm, so honour the shut-off.
            if self.pir_sensor and self.get_state(self.pir_sensor) == "on":
                self.log("Claudias Room: Claudia left but room still occupied - leaving lights on", level="INFO")
                return
            for name, light in (("ceiling", self.ceiling_light), ("string lights", self.string_light)):
                if self.get_state(light) == "on":
                    self.turn_off(light)
                    self._log_action("OFF", f"{name} - Claudia left home")
        except Exception as e:
            self.log(f"Error in leave-home handler: {e}", level="ERROR")

    def _on_switch_state(self, entity, attribute, old, new, kwargs):
        try:
            event_type = new
            if event_type == "press_1":
                self._toggle(self.ceiling_light, "ceiling")
            elif event_type == "press_3":
                self._toggle(self.string_light, "string lights")
        except Exception as e:
            self.log(f"Error in switch event handler: {e}", level="ERROR")

    def _toggle(self, light, label):
        self.toggle(light)
        self._log_action("TOGGLE", f"{label} switch")

    def _log_action(self, action, reason="", score=None):
        if self.log_level == "quiet":
            return
        score_str = f" (score: {score:.2f})" if score is not None else ""
        reason_str = f": {reason}" if reason else ""
        self.log(f"LIGHTS {action} [claudias_room]{reason_str}{score_str}", level="INFO")
