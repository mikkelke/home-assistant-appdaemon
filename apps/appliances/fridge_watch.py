"""
FridgeWatch - one push when a fridge compressor runs without a break for too long, and one when
the fridge has been warm for too long.

A fridge that cannot hold its temperature (door left ajar, failing compressor) never switches
off, which its own plug shows. A run starts when power reaches `running_w` and ends only once
power has stayed below it for `off_hold_s`, so a short dip does not split a run. A run that
reaches `alert_minutes` pushes `notify_target` once through MobileNotifier, home or away; a push
that reached nobody is retried every RETRY_S. When the run ends the fridge is "ok" again and the
next long run pushes again.

Publishes `health_entity` (ok | long_run) with a `reason` attribute and the current run's start
and length. AppDaemon-published sensors vanish when Home Assistant restarts, so the entity is
re-published every REPUBLISH_S as well as on every change.

unavailable/unknown/non-numeric power readings are ignored: they neither start nor end a run.
On start-up the run in progress is rebuilt by replaying the recorder history through the same
rules as live readings, and a run whose entity still reads long_run does not push again; when
the history cannot be read, a fridge that is running now counts as starting now.

Optional temperature rule (`temperature_sensor`): a warm spell starts at the first reading at or
above `warm_c` and is broken by any reading below it. A spell lasting `warm_minutes` pushes once;
no further warm push until a reading falls below `rearm_c`. The last reading counts until the next
one, so a sensor that reports only on change still accumulates the spell. Start-up replays the
temperature history the same way, and a spell whose entity already reads warm does not push again.

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


def _number(value):
    """The reading as a float, or None for unavailable/unknown/non-numeric."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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
        self.notify_target = a.get("notify_target", "mikkel")
        self.temperature_sensor = a.get("temperature_sensor")
        self.warm_c = float(a.get("warm_c", 8.0))
        self.warm_minutes = float(a.get("warm_minutes", 60))
        self.rearm_c = float(a.get("rearm_c", 7.0))

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
        self._temp = None
        self._warm_since = None
        self._warm_alerted = False
        self._warm_next_push_at = 0.0

        now = self._now()
        self._recover(now)
        self.listen_state(self._on_power, self.power_sensor)
        if self.temperature_sensor:
            self._recover_temperature(now)
            self.listen_state(self._on_temperature, self.temperature_sensor)
        self.run_every(self._tick, "now+60", 60)
        self._refresh(now)

        run = "no run in progress" if self._run_started is None else f"run in progress for {self._elapsed_s(now) / 60:.0f} min"
        self.log(
            f"FridgeWatch {self.health_entity} initialized: {self.power_sensor}, alert after {self.alert_minutes} min, {run}",
            level="INFO",
        )

    # ---------- run tracking ----------

    def _on_power(self, entity, attribute, old, new, kwargs):
        watts = _number(new)
        if watts is None:
            return
        now = self._now()
        self._observe(now, watts)
        self._refresh(now)

    def _on_temperature(self, entity, attribute, old, new, kwargs):
        celsius = _number(new)
        if celsius is None:
            return
        now = self._now()
        self._observe_temperature(now, celsius)
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

    # ---------- temperature ----------

    def _observe_temperature(self, now, celsius):
        self._temp = celsius
        if celsius < self.rearm_c and self._warm_alerted:
            self.log(f"FridgeWatch {self.health_entity}: back below {self.rearm_c} C ({celsius} C), warm alert re-armed", level="INFO")
            self._warm_alerted = False
            self._warm_next_push_at = 0.0
        if celsius >= self.warm_c:
            if self._warm_since is None:
                self._warm_since = now
        else:
            self._warm_since = None

    def _warm_s(self, now):
        return 0.0 if self._warm_since is None else now - self._warm_since

    def _is_warm(self, now):
        return self._warm_alerted or self._warm_s(now) >= self.warm_minutes * 60

    # ---------- start-up recovery ----------

    def _recover(self, now):
        for t, watts in self._history_points(self.power_sensor, now - self.alert_s - HISTORY_MARGIN_S, now):
            self._observe(t, watts)
        self._settle(now)
        current = _number(self.get_state(self.power_sensor))
        if current is not None:
            self._observe(now, current)
        if self._run_started is not None:
            self._alerted = self.get_state(self.health_entity) == "long_run"

    def _recover_temperature(self, now):
        start = now - self.warm_minutes * 60 - HISTORY_MARGIN_S
        for t, celsius in self._history_points(self.temperature_sensor, start, now):
            self._observe_temperature(t, celsius)
        current = _number(self.get_state(self.temperature_sensor))
        if current is not None:
            self._observe_temperature(now, current)
        if self.get_state(self.health_entity) == "warm" and self._temp is not None and self._temp >= self.rearm_c:
            self._warm_alerted = True

    def _history_points(self, entity, start, now):
        """Chronological [(epoch, value)] of the entity's recorder history; [] when unreadable."""
        try:
            raw = self.get_history(
                entity_id=entity,
                start_time=datetime.fromtimestamp(start, timezone.utc),
                end_time=datetime.fromtimestamp(now, timezone.utc),
                minimal_response=True,
                no_attributes=True,
            )
            points = whist.parse_power_points(whist.flatten_history(raw, entity))
        except Exception as e:
            self.log(f"FridgeWatch {self.health_entity}: {entity} history unreadable, starting from now: {e}", level="WARNING")
            return []
        return sorted((t.timestamp(), w) for t, w in points if math.isfinite(w))

    # ---------- alert ----------

    def _refresh(self, now):
        self._evaluate(now)
        self._publish(now)

    def _evaluate(self, now):
        self._evaluate_warm(now)
        if self._alerted or now < self._next_push_at or not self._is_long(now):
            return
        self._alerted = True
        self._next_push_at = now + RETRY_S
        minutes = self._elapsed_s(now) / 60
        self.log(f"FridgeWatch {self.health_entity}: compressor running {minutes:.0f} min, pushing", level="WARNING")
        self.create_task(self._push(minutes))

    def _evaluate_warm(self, now):
        if self._warm_alerted or now < self._warm_next_push_at or self._warm_s(now) < self.warm_minutes * 60:
            return
        self._warm_alerted = True
        self._warm_next_push_at = now + RETRY_S
        minutes = self._warm_s(now) / 60
        self.log(f"FridgeWatch {self.health_entity}: {self._temp} C, at or above {self.warm_c} C for {minutes:.0f} min, pushing", level="WARNING")
        self.create_task(self._push_warm(minutes, self._temp))

    async def _push(self, minutes):
        if not await self._send(
            f"{self.display_name} is running non-stop",
            f"The compressor has run for {_duration(minutes, 'h')}. Is the door ajar?",
            self._tag,
        ):
            self._alerted = False

    async def _push_warm(self, minutes, celsius):
        if not await self._send(
            f"{self.display_name} is warm",
            f"It reads {celsius:.1f} °C and has been at {self.warm_c:g} °C or above for {_duration(minutes, 'h')}.",
            f"{self._tag}_warm",
        ):
            self._warm_alerted = False

    async def _send(self, title, message, tag):
        """True when the push reached someone."""
        try:
            sent = await self._notifier.notify(title=title, message=message, target=self.notify_target, data={"tag": tag})
        except Exception as e:
            self.log(f"FridgeWatch {self.health_entity}: notify failed: {e}", level="WARNING")
            sent = 0
        if not sent:
            self.log(f"FridgeWatch {self.health_entity}: push reached no one, will retry", level="WARNING")
        return bool(sent)

    # ---------- semantic entity ----------

    def _snapshot(self, now):
        warm = self.temperature_sensor and self._is_warm(now)
        state = "warm" if warm else "long_run" if self._is_long(now) else "ok"
        attrs = {
            "friendly_name": self._friendly_name,
            "icon": "mdi:fridge" if state == "ok" else "mdi:fridge-alert",
            "alert_minutes": self.alert_minutes,
        }
        if self.temperature_sensor:
            attrs["temperature"] = self._temp
            attrs["warm_c"] = self.warm_c
            attrs["warm_minutes"] = self.warm_minutes
        if warm:
            if self._warm_since is None:
                attrs["reason"] = f"Was at {self.warm_c:g} °C or above for {_duration(self.warm_minutes, 'h')}; now {self._temp} °C, clears below {self.rearm_c:g} °C"
            else:
                attrs["reason"] = f"At {self.warm_c:g} °C or above for {_duration(self._warm_s(now) / 60, 'h')} (now {self._temp} °C)"
            return state, attrs
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
        key = (state, self._run_started, self._temp)
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
