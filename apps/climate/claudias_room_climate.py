import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class ClaudiasRoomClimate(hass.Hass):
    """
    Rooftop-door-2 frost economy for the Claudias Room thermostat.

    Ported from the HA automation 'Claudias Room climate control' (id 1743762170942):
      * door OPENS  -> remember the current setpoint, drop the thermostat to
                       ``frost_temp`` (heat mode) so we don't heat the rooftop.
      * door CLOSES -> restore the remembered setpoint (heat mode).
      * manual setpoint change while the door is CLOSED -> remember it as the
        value to restore after the next open/close cycle.
    """

    def initialize(self):
        self.door = self.args["door_sensor"]
        self.climate = self.args["climate_entity"]
        self.prev_temp_entity = self.args["previous_temp_entity"]
        self.frost_temp = float(self.args.get("frost_temp", 5))
        self.default_temp = float(self.args.get("default_temp", 20))
        self._suppress = False

        self.listen_state(self._on_door, self.door)
        self.listen_state(self._on_setpoint, self.climate, attribute="temperature")
        self.log(
            "ClaudiasRoomClimate ready: door=%s climate=%s frost=%.1f default=%.1f"
            % (self.door, self.climate, self.frost_temp, self.default_temp)
        )

    # --- helpers ---
    def _setpoint(self):
        try:
            return float(self.get_state(self.climate, attribute="temperature"))
        except (TypeError, ValueError):
            return None

    def _remember(self, temp):
        if temp is not None and temp > 0:
            self.call_service("input_number/set_value", entity_id=self.prev_temp_entity, value=temp)

    def _apply(self, temp):
        self._suppress = True
        self.call_service("climate/set_temperature", entity_id=self.climate, temperature=temp, hvac_mode="heat")
        self.run_in(self._unsuppress, 5)

    def _unsuppress(self, kwargs):
        self._suppress = False

    # --- handlers ---
    def _on_door(self, entity, attribute, old, new, kwargs):
        if new == old:
            return
        if new == "on":
            cur = self._setpoint()
            self._remember(cur)
            self._apply(self.frost_temp)
            self.log("Rooftop door 2 OPEN -> %s frost %.1f C (saved %s)" % (self.climate, self.frost_temp, cur))
        elif new == "off":
            try:
                prev = float(self.get_state(self.prev_temp_entity))
            except (TypeError, ValueError):
                prev = 0.0
            restore = prev if prev > 0 else self.default_temp
            self._apply(restore)
            self.log("Rooftop door 2 CLOSED -> restore %s to %.1f C" % (self.climate, restore))

    def _on_setpoint(self, entity, attribute, old, new, kwargs):
        if self._suppress:
            return
        if self.get_state(self.door) != "off":
            return
        try:
            t = float(new)
        except (TypeError, ValueError):
            return
        if t > 0 and abs(t - self.frost_temp) > 0.01:
            self._remember(t)
            self.log("Manual setpoint %.1f C (door closed) remembered" % t, level="DEBUG")
