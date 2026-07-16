"""GW2000A/WS90 weather-station watchdog - push notification when the outdoor feed dies.

The WS90 (roof) pairs by RF to the indoor GW2000A gateway. Known failure mode: RF
latch-up - the gateway keeps serving its local API but the outdoor sensors freeze or go
unavailable, and ONLY a hard power cycle of the gateway recovers it (a soft reboot does
not). Every consumer (darkness_calculator, wind monitors, weather_opening_alert, the AC
apps) silently degrades when that happens, so nothing surfaces the outage.

Notification-only BY USER DECISION (2026-07-16): this pushes Mikkel's phone and does NOT
write a house-activity feed entry - a dead sensor is maintenance, not house behavior.

Failure definition: EVERY watched entity is unavailable/unknown OR stale (last_updated
older than stale_minutes). All-of, not any-of - a single flaky sensor must not page.
One push per outage, one all-clear on recovery.
"""

import datetime

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class Gw2000aWatchdog(hass.Hass):
    def initialize(self):
        a = self.args.get
        # WS90-sourced (RF) entities - the ones that die in a latch-up. The gateway's own
        # indoor readings can stay healthy through it, so don't watch those.
        self.entities = list(
            a(
                "entities",
                [
                    "sensor.gw2000a_wind_speed",
                    "sensor.gw2000a_solar_lux",
                    "sensor.gw2000a_rain_rate_piezo",
                ],
            )
        )
        self.stale_min = float(a("stale_minutes", 20))
        self.check_min = float(a("check_minutes", 5))
        self.notify_target = a("notify_target", ["mikkel"])
        self._notified_down = False
        self.run_every(self._check, "now+30", self.check_min * 60)
        self.log(
            f"Gw2000aWatchdog: {len(self.entities)} entities, stale>{self.stale_min:.0f}min, "
            f"check every {self.check_min:.0f}min"
        )

    def _check(self, kwargs=None):
        try:
            now = self.get_now()
            healthy = False
            for ent in self.entities:
                state = self.get_state(ent)
                if state in (None, "unavailable", "unknown"):
                    continue
                last_updated = self.get_state(ent, attribute="last_updated")
                try:
                    lu_dt = datetime.datetime.fromisoformat(str(last_updated))
                    age_min = (now - lu_dt).total_seconds() / 60.0
                except (ValueError, TypeError):
                    # Can't read the age -> count the entity healthy. Fail-open: a parsing
                    # quirk must not fabricate an outage page at 3 AM.
                    age_min = 0.0
                if age_min < self.stale_min:
                    healthy = True
                    break
            if not healthy and not self._notified_down:
                self._notified_down = True
                self._notify(
                    f"Weather station stopped reporting - all outdoor sensors silent/stale "
                    f"> {self.stale_min:.0f} min. Known WS90 RF latch-up: needs a HARD power "
                    f"cycle of the gateway (soft reboot does not recover it)."
                )
                self.log("Outdoor feed DOWN - notified", level="WARNING")
            elif healthy and self._notified_down:
                self._notified_down = False
                self._notify("Weather station is reporting again.")
                self.log("Outdoor feed recovered - notified")
        except Exception as e:
            self.log(f"watchdog check failed: {e}", level="WARNING")

    def _notify(self, message):
        try:
            notifier = self.get_app("MobileNotifier")
            if notifier is None:
                self.log("MobileNotifier app not found - cannot push", level="WARNING")
                return
            self.create_task(notifier.notify(title="Weather station", message=message, target=self.notify_target))
        except Exception as e:
            self.log(f"notify failed: {e}", level="WARNING")
