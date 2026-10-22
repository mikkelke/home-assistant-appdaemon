"""
FridgeWatch - one push when a fridge compressor runs without a break for too long.

A fridge that cannot hold its temperature (door left ajar, failing compressor) never switches
off, which its own plug shows. A run starts when power reaches `running_w` and ends only once
power has stayed below it for `off_hold_s`, so a short dip does not split a run. A run that
reaches `alert_minutes` pushes the household once through MobileNotifier (home broadcast,
category "fridge"); a push that reached nobody is retried every RETRY_S. When the run ends the
fridge is "ok" again and the next long run pushes again.

Publishes `health_entity` (ok | long_run) with a `reason` attribute and the current run's start
and length. AppDaemon-published sensors vanish when Home Assistant restarts, so the entity is
re-published every REPUBLISH_S as well as on every change.

unavailable/unknown/non-numeric power readings are ignored: they neither start nor end a run.
On start-up the run in progress is rebuilt by replaying the recorder history through the same
rules as live readings, and a run whose entity still reads long_run does not push again; when
the history cannot be read, a fridge that is running now counts as starting now.

One yaml instance per fridge (fridge_watch.yaml). All timestamps are epoch seconds.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import washer_history as whist

REPUBLISH_S = 300
RETRY_S = 600
HISTORY_MARGIN_S = 3600


def _watts(value):
    """The reading as a float, or None for unavailable/unknown/non-numeric."""
    try:
        watts = float(value)
    except (TypeError, ValueError):
        return None
    return watts if math.isfinite(watts) else None


def _duration(minutes, hour_unit):
    hours, mins = divmod(int(minutes), 60)
    if not hours:
        return f"{mins} min"
    return f"{hours} {hour_unit} {mins} min" if mins else f"{hours} {hour_unit}"


class FridgeWatch(hass.Hass):
    def initialize(self):
        a = self.args
        self.power_sensor = a["power_sensor"]
        self.health_entity = a["health_entity"]
        self.display_name = a["display_name"]
        self.alert_minutes = a["alert_minutes"]
        self.running_w = float(a.get("running_w", 20))
        self.off_hold_s = float(a.get("off_hold_s", 120))
        self.category = a.get("category", "fridge")

        self.alert_s = float(self.alert_minutes) * 60
        self._tag = self.health_entity.split(".", 1)[-1]
        self._friendly_name = self._tag.replace("_", " ").capitalize()

        self._notifier = self.get_app("MobileNotifier")
        self._run_started = None
        self._below_since = None
        self._alerted = False
        self._next_push_at = 0.0
        self._published_key = None
        self._published_at = 0.0

        now = self._now()
        self._recover(now)
        self.listen_state(self._on_power, self.power_sensor)
        self.run_every(self._tick, "now+60", 60)
        self._refresh(now)

        run = "no run in progress" if self._run_started is None else f"run in progress for {self._elapsed_s(now) / 60:.0f} min"
        self.log(
            f"FridgeWatch {self.health_entity} initialized: {self.power_sensor}, alert after {self.alert_minutes} min, {run}",
            level="INFO",
        )

    # ---------- run tracking ----------

    def _on_power(self, entity, attribute, old, new, kwargs):
        watts = _watts(new)
        if watts is None:
            return
        now = self._now()
        self._observe(now, watts)
        self._refresh(now)

    def _tick(self, kwargs):
        now = self._now()
        self._settle(now)
        self._refresh(now)

    def _observe(self, now, watts):
        self._settle(now)
        if watts >= self.running_w:
            self._below_since = None
            if self._run_started is None:
                self._run_started = now
        elif self._run_started is not None and self._below_since is None:
            self._below_since = now

    def _settle(self, now):
        """End the run once power has stayed below running_w for off_hold_s."""
        if self._run_started is None or self._below_since is None:
            return
        if now - self._below_since < self.off_hold_s:
            return
        if self._is_long(now):
            minutes = self._elapsed_s(now) / 60
            self.log(f"FridgeWatch {self.health_entity}: long run ended after {minutes:.0f} min", level="INFO")
        self._run_started = None
        self._below_since = None
        self._alerted = False
        self._next_push_at = 0.0

    def _elapsed_s(self, now):
        """Length of the current run; a dip in progress does not count towards it."""
        end = now if self._below_since is None else self._below_since
        return end - self._run_started

    def _is_long(self, now):
        return self._run_started is not None and self._elapsed_s(now) >= self.alert_s

    # ---------- start-up recovery ----------

    def _recover(self, now):
        for t, watts in self._history_points(now):
            self._observe(t, watts)
        self._settle(now)
        current = _watts(self.get_state(self.power_sensor))
        if current is not None:
            self._observe(now, current)
        if self._run_started is not None:
            self._alerted = self.get_state(self.health_entity) == "long_run"

    def _history_points(self, now):
        """Chronological [(epoch, watts)] of the sensor's recorder history; [] when unreadable."""
        try:
            raw = self.get_history(
                entity_id=self.power_sensor,
                start_time=datetime.fromtimestamp(now - self.alert_s - HISTORY_MARGIN_S, timezone.utc),
                end_time=datetime.fromtimestamp(now, timezone.utc),
                minimal_response=True,
                no_attributes=True,
            )
            points = whist.parse_power_points(whist.flatten_history(raw, self.power_sensor))
        except Exception as e:
            self.log(f"FridgeWatch {self.health_entity}: history unreadable, starting from now: {e}", level="WARNING")
            return []
        return sorted((t.timestamp(), w) for t, w in points if math.isfinite(w))

    # ---------- alert ----------

    def _refresh(self, now):
        self._evaluate(now)
        self._publish(now)

    def _evaluate(self, now):
        if self._alerted or now < self._next_push_at or not self._is_long(now):
            return
        self._alerted = True
        self._next_push_at = now + RETRY_S
        minutes = self._elapsed_s(now) / 60
        self.log(f"FridgeWatch {self.health_entity}: compressor running {minutes:.0f} min, pushing", level="WARNING")
        self.create_task(self._push(minutes))

    async def _push(self, minutes):
        try:
            sent = await self._notifier.notify(
                title=f"{self.display_name} kører uafbrudt",
                message=f"Kompressoren har kørt i {_duration(minutes, 't')}. Står døren på klem?",
                target="home",
                category=self.category,
                data={"tag": self._tag},
            )
        except Exception as e:
            self.log(f"FridgeWatch {self.health_entity}: notify failed: {e}", level="WARNING")
            sent = 0
        if not sent:
            self._alerted = False
            self.log(f"FridgeWatch {self.health_entity}: push reached no one, will retry", level="WARNING")

    # ---------- semantic entity ----------

    def _snapshot(self, now):
        state = "long_run" if self._is_long(now) else "ok"
        attrs = {
            "friendly_name": self._friendly_name,
            "icon": "mdi:fridge-alert" if state == "long_run" else "mdi:fridge",
            "alert_minutes": self.alert_minutes,
        }
        if self._run_started is None:
            attrs["reason"] = "Compressor is off"
            return state, attrs
        minutes = self._elapsed_s(now) / 60
        attrs["run_started"] = datetime.fromtimestamp(self._run_started, timezone.utc).isoformat(timespec="seconds")
        attrs["run_minutes"] = int(minutes)
        alert_after = _duration(self.alert_minutes, "h")
        if state == "long_run":
            attrs["reason"] = f"Compressor has run for {_duration(minutes, 'h')} without a break (alert after {alert_after})"
        else:
            attrs["reason"] = f"Compressor has been running for {_duration(minutes, 'h')} (alert after {alert_after})"
        return state, attrs

    def _publish(self, now):
        state, attrs = self._snapshot(now)
        key = (state, self._run_started)
        if key == self._published_key and now - self._published_at < REPUBLISH_S:
            return
        try:
            self.set_state(self.health_entity, state=state, replace=True, attributes=attrs)
        except Exception as e:
            self.log(f"FridgeWatch {self.health_entity}: publish failed: {e}", level="WARNING")
            return
        self._published_key = key
        self._published_at = now

    def _now(self):
        return time.time()
