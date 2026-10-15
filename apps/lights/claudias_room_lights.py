import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class ClaudiasRoomLights(hass.Hass):
    """Claudias Room lighting: manual control.

    The room is driven by the physical wall switch (handled here) and direct
    app/dashboard control of the light entities (which never routed through this app).
    The old automatic lighting (PIR occupancy + ``darkness_calculator`` room_state
    push) was disabled 2026-07-20 (user request); ``darkness_calculator`` still
    publishes ``sensor.room_state_claudias_room`` / ``sensor.darkness_claudias_room``,
    so restoring full automation is a straight revert of the commit that stripped it.

    Lights left on after Claudia leaves are turned off by ``resident_room_lights_off``.

    Switch map: ``press_1`` (top-left) toggles the ceiling light, ``press_3`` (top-right)
    the string lights. ``press_2``/``press_4`` (bottom row) are unmapped.
    """

    def initialize(self):
        self.ceiling_light = self.args["ceiling_light"]
        self.string_light = self.args["string_light"]
        self.log_level = self.args.get("verbosity_level", "normal")

        self.listen_state(self._on_switch_state, "event.claudias_room_switch_action", attribute="event_type")

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
