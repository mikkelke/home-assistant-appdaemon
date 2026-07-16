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
        # Gates the house-activity report: only a drop that interrupts REAL heating is worth
        # telling anyone about - in summer the radiator is idle and the setpoint dance is
        # both invisible and irrelevant. Fail-closed (sensor unavailable -> no report).
        self.heating_active_entity = self.args.get("heating_active_entity", "binary_sensor.claudias_room_heating_active")
        self._frost_reported = False
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

    def _frost_effective(self):
        """Frost target clamped to the thermostat's minimum - the device rejects
        anything below min_temp (validation error), which silently disabled frost
        mode when frost_temp was 5 and the Danfoss minimum is 15."""
        try:
            dev_min = float(self.get_state(self.climate, attribute="min_temp"))
        except (TypeError, ValueError):
            dev_min = 15.0
        return max(self.frost_temp, dev_min)

    def _apply(self, temp):
        self._suppress = True
        self.call_service("climate/set_temperature", entity_id=self.climate, temperature=temp, hvac_mode="heat")
        self.run_in(self._unsuppress, 5)

    def _unsuppress(self, kwargs):
        self._suppress = False

    def _report(self, cause, effect):
        """Explain the frost/restore decision to the dashboard's Home activity feed.
        Fire-and-forget; audience = Claudia (her room) + admins. NOTE: this room's
        automation is provisional - Claudia may not want it once she moves in - so
        this event doubles as the explanation that helps her decide."""
        try:
            self.fire_event(
                "house_events_report",
                cause=cause,
                effect=effect,
                icon="mdi:radiator",
                audience_users=["Claudia"],
            )
        except Exception:
            pass

    # --- handlers ---
    def _on_door(self, entity, attribute, old, new, kwargs):
        if new == old:
            return
        if new == "on":
            cur = self._setpoint()
            self._remember(cur)
            frost = self._frost_effective()
            self._apply(frost)
            self.log("Rooftop door 2 OPEN -> %s frost %.1f C (saved %s)" % (self.climate, frost, cur))
            if self.get_state(self.heating_active_entity) == "on":
                self._frost_reported = True
                self._report(
                    "Rooftop door opened",
                    "Claudias room radiator dropped to %g C to save heat" % frost,
                )
        elif new == "off":
            try:
                prev = float(self.get_state(self.prev_temp_entity))
            except (TypeError, ValueError):
                prev = 0.0
            restore = prev if prev > 0 else self.default_temp
            self._apply(restore)
            self.log("Rooftop door 2 CLOSED -> restore %s to %.1f C" % (self.climate, restore))
            # Only close the story we opened - an idle-season restore stays silent.
            if self._frost_reported:
                self._frost_reported = False
                self._report(
                    "Rooftop door closed",
                    "Claudias room radiator back to %g C" % restore,
                )

    def _on_setpoint(self, entity, attribute, old, new, kwargs):
        if self._suppress:
            return
        if self.get_state(self.door) != "off":
            return
        try:
            t = float(new)
        except (TypeError, ValueError):
            return
        if t > 0 and abs(t - self._frost_effective()) > 0.01:
            self._remember(t)
            self.log("Manual setpoint %.1f C (door closed) remembered" % t, level="DEBUG")
