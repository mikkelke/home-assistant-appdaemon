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

Restart survival (2026-07-27): an HA restart coinciding with a check would otherwise page
a false RF-latch-up (every entity briefly unavailable during the reconnect), so paging
requires TWO consecutive failed checks (an in-memory counter - worst case a restart delays
a real page by one check_minutes cycle). ``_notified_down`` itself is persisted to a tiny
state file so an AD restart mid-outage can't re-page, and so a recovery that happened while
AD was down still gets its all-clear (checked once at init).
"""

import datetime
import json
import os

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
        self.state_file = a("state_file", "/conf/apps/weather/gw2000a_watchdog_state.json")

        self._notified_down = False
        self._consec_fail = 0  # NOT persisted - a restart mid-flap just costs one extra cycle
        self._load_state()

        if self._notified_down and self._is_healthy(self.get_now()):
            # Recovered while AD itself was down (or reloading) - the scheduled _check
            # below would eventually notice too, but there's no reason to sit on a
            # known-stale "down" page a moment longer than necessary. Deferred one tick
            # (like every other create_task-via-_notify call in this codebase) so
            # initialize() is guaranteed to have fully returned first.
            self._notified_down = False
            self._save_state()
            self.run_in(lambda kw: self._notify("Weather station is reporting again."), 0)
            self.log("Outdoor feed recovered while AD was down - notified", level="INFO")

        self.run_every(self._check, "now+30", self.check_min * 60)
        self.log(
            f"Gw2000aWatchdog: {len(self.entities)} entities, stale>{self.stale_min:.0f}min, "
            f"check every {self.check_min:.0f}min"
        )

    def _is_healthy(self, now):
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
                return True
        return False

    def _check(self, kwargs=None):
        try:
            now = self.get_now()
            healthy = self._is_healthy(now)
            self._consec_fail = 0 if healthy else self._consec_fail + 1

            if not healthy and self._consec_fail >= 2 and not self._notified_down:
                self._notified_down = True
                self._save_state()
                self._notify(
                    f"Weather station stopped reporting - all outdoor sensors silent/stale "
                    f"> {self.stale_min:.0f} min. Known WS90 RF latch-up: needs a HARD power "
                    f"cycle of the gateway (soft reboot does not recover it)."
                )
                self.log("Outdoor feed DOWN - notified", level="WARNING")
            elif healthy and self._notified_down:
                self._notified_down = False
                self._save_state()
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

    # -- state persistence (notified_down only; see module docstring) --------------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                d = json.load(f)
            self._notified_down = bool(d.get("notified_down", False))
        except Exception:
            pass

    def _save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"notified_down": self._notified_down}, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"state save failed ({e}) - continuing in-memory", level="WARNING")
