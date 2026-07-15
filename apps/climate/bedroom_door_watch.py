"""
Bedroom door watch - one alert if the door is left open too long while
SmartCooling is armed.

Family-room air is warmer than the pre-cooled bedroom; an open door lets that
heat flood straight in and undoes the pre-cool the same way an open bathroom
window helps the condenser vent (user, 2026-07-15). Passing through is fine -
only a door left open past `open_alert_minutes` triggers a notification, and
only while there is an actual cooling effort to protect (SmartCooling armed +
AC deployed). One alert per continuous open episode - closing and reopening
the door re-arms it; no repeat nagging while it stays open after the first.
"""

from datetime import datetime, timedelta

import appdaemon.plugins.hass.hassapi as hass


class BedroomDoorWatch(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.door_sensor = a("door_sensor", "binary_sensor.bedroom_door_contact")
        self.smart_cooling_entity = a("smart_cooling_entity", "input_boolean.smart_cooling")
        self.ac_climate_entity = a("ac_climate_entity", "climate.air_conditioner_thermostat")
        self.open_alert_minutes = float(a("open_alert_minutes", 5))
        self.notify_target = a("notify_target", "mikkel")

        self._notifier = self.get_app("MobileNotifier")
        self._alert_handle = None
        self._alerted_this_episode = False

        self.listen_state(self._on_door, self.door_sensor)

        if self.get_state(self.door_sensor) == "on":
            self._arm(int(self.open_alert_minutes * 60))

    def _cooling_active(self):
        armed = self.get_state(self.smart_cooling_entity) == "on"
        deployed = self.get_state(self.ac_climate_entity) not in (None, "unavailable", "unknown")
        return armed and deployed

    def _on_door(self, entity, attribute, old, new, kwargs):
        if new == "on":
            self._alerted_this_episode = False
            self._arm(int(self.open_alert_minutes * 60))
        elif new == "off":
            self._disarm()

    def _arm(self, delay_s):
        self._disarm()
        self._alert_handle = self.run_in(self._maybe_alert, delay_s)

    def _disarm(self):
        if self._alert_handle:
            try:
                if self.timer_running(self._alert_handle):
                    self.cancel_timer(self._alert_handle)
            except Exception:
                pass
            self._alert_handle = None

    def _maybe_alert(self, kwargs):
        self._alert_handle = None
        if self._alerted_this_episode:
            return
        if self.get_state(self.door_sensor) != "on":
            return
        if not self._cooling_active():
            return
        self._alerted_this_episode = True
        self.create_task(self._notify())

    async def _notify(self):
        try:
            await self._notifier.notify(
                title="Bedroom door open",
                message=(f"Been open {self.open_alert_minutes:.0f}+ min while cooling the room - "
                         f"closing it keeps the cool air in."),
                target=self.notify_target,
            )
            self.log(f"Alerted: bedroom door open {self.open_alert_minutes:.0f}+ min during active cooling")
        except Exception as e:
            self.log(f"notify failed: {e}", level="WARNING")
