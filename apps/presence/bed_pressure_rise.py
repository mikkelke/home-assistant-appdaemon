"""
BedPressureRise - publishes binary_sensor.bed_pressure_rise, a bed witness that fires on a
RISE of the pressure strip over the last known-empty level instead of on a fixed level (the
empty-bed level drifts with the bedding, so a fixed level misses someone sitting up).
Publishes one entity and controls nothing.

Per side (left and right independently):
  Baseline: while every room witness and both strip occupied sensors have read exactly "off"
  for empty_room_sec, the baseline follows the side's pressure on every event and tick, and
  both sides are forced off. Any other state (on, unknown, unavailable, missing) breaks that
  run and restarts its timer; the baseline then stays at the last empty level. No baseline
  yet (after a restart) means off.
  On: pressure >= baseline + rise_points continuously for hold_sec.
  Off: pressure < baseline + release_points at once, or the room empty again. An unreadable
  pressure counts as below the threshold.
The output is on when either side is on; the `side` attribute says which.

Everything is decided in _evaluate(now), run on every input change and on a TICK_SEC tick for
the two timers. The entity is published on every change of state or side and at least every
REPUBLISH_SEC, because HA drops entities set from AppDaemon when it restarts. AppDaemon's
set_state drops attributes equal to None, False or 0, so a baseline or pressure of exactly 0
is absent from the attributes.

All timestamps are epoch seconds via the single _now() helper (never naive local datetime
arithmetic), so tests can fake the clock directly.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

SIDES = ("left", "right")
TICK_SEC = 5
REPUBLISH_SEC = 60


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round1(value):
    return None if value is None else round(value, 1)


def _fmt(value):
    return f"{round(value, 1):g}"


def _duration(seconds):
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds / 60:g} min"
    return f"{seconds:g} s"


class BedPressureRise(hass.Hass):
    def initialize(self):
        a = self.args
        self.pressure_entities = {"left": a["left_pressure"], "right": a["right_pressure"]}
        self.occupied_entities = {"left": a["left_occupied"], "right": a["right_occupied"]}
        self.room_witnesses = list(a["room_witnesses"])
        self.output_entity = a["output"]
        self.rise_points = float(a.get("rise_points", 40))
        self.release_points = float(a.get("release_points", 32))
        self.hold_sec = float(a.get("hold_sec", 20))
        self.empty_room_sec = float(a.get("empty_room_sec", 300))

        self._empty_entities = [*self.room_witnesses, *self.occupied_entities.values()]
        self._empty_since = None
        self._learned = False
        self._baseline = dict.fromkeys(SIDES)
        self._baseline_at = None
        self._rise_since = dict.fromkeys(SIDES)
        self._on = dict.fromkeys(SIDES, False)
        self._on_note = dict.fromkeys(SIDES, "")
        self._out_state = "off"
        self._published_key = None
        self._published_at = None

        watched = [*self.pressure_entities.values(), *self._empty_entities]
        for entity in dict.fromkeys(watched):
            self.listen_state(self._on_change, entity)
        self.run_every(self._tick, f"now+{TICK_SEC}", TICK_SEC)

        self._evaluate(self._now())
        self.log("BedPressureRise initialized", level="INFO")

    # ---------- listeners ----------

    def _on_change(self, entity, attribute, old, new, kwargs):
        self._evaluate(self._now())

    def _tick(self, kwargs):
        self._evaluate(self._now())

    # ---------- evaluation ----------

    def _evaluate(self, now):
        quiet = all(self._read(entity) == "off" for entity in self._empty_entities)
        if not quiet:
            self._empty_since = None
        elif self._empty_since is None:
            self._empty_since = now
        empty = quiet and now - self._empty_since >= self.empty_room_sec

        pressures = {side: _number(self._read(self.pressure_entities[side])) for side in SIDES}
        notes = dict.fromkeys(SIDES)
        if empty:
            self._learn(now, pressures)
            for side in SIDES:
                self._on[side] = False
                self._rise_since[side] = None
        else:
            self._learned = False
            for side in SIDES:
                notes[side] = self._step_side(side, pressures[side], now)

        on_sides = [side for side in SIDES if self._on[side]]
        state = "on" if on_sides else "off"
        if len(on_sides) == len(SIDES):
            side = "both"
        else:
            side = on_sides[0] if on_sides else "none"
        reason = self._reason(empty, notes)
        attributes = {
            "friendly_name": "Bed pressure rise",
            "device_class": "occupancy",
            "side": side,
            "left_baseline": _round1(self._baseline["left"]),
            "right_baseline": _round1(self._baseline["right"]),
            "left_pressure": _round1(pressures["left"]),
            "right_pressure": _round1(pressures["right"]),
            "baseline_learned_at": self._iso(self._baseline_at),
            "reason": reason,
        }

        if state != self._out_state:
            self.log(f"BedPressureRise {state}: {reason}", level="INFO")
        self._out_state = state
        self._publish(now, state, side, attributes)
        return state, attributes

    def _learn(self, now, pressures):
        readable = [side for side in SIDES if pressures[side] is not None]
        for side in readable:
            self._baseline[side] = pressures[side]
        if not readable:
            return
        self._baseline_at = now
        if not self._learned:
            self._learned = True
            levels = ", ".join(f"{side} {_fmt(self._baseline[side])}" for side in readable)
            self.log(f"BedPressureRise baseline learned: {levels}", level="INFO")

    def _step_side(self, side, pressure, now):
        """Advance one side's hold/release state; returns a note for the reason, or None."""
        baseline = self._baseline[side]
        if baseline is None:
            self._on[side] = False
            self._rise_since[side] = None
            return f"{side} no baseline yet"
        if pressure is None:
            self._on[side] = False
            self._rise_since[side] = None
            return f"{side} pressure unreadable"
        if self._on[side]:
            if pressure >= baseline + self.release_points:
                return self._on_note[side]
            self._on[side] = False
            self._rise_since[side] = None
            return f"{side} {_fmt(pressure)} < baseline {_fmt(baseline)} + {self.release_points:g}"
        if pressure < baseline + self.rise_points:
            self._rise_since[side] = None
            return None
        since = self._rise_since[side]
        if since is None:
            since = self._rise_since[side] = now
        held = now - since
        text = f"{side} {_fmt(pressure)} >= baseline {_fmt(baseline)} + {self.rise_points:g} for {int(held)}"
        if held >= self.hold_sec:
            self._on[side] = True
            self._on_note[side] = f"{text} s"
            return self._on_note[side]
        return f"{text} of {self.hold_sec:g} s"

    def _reason(self, empty, notes):
        if all(baseline is None for baseline in self._baseline.values()):
            return "no baseline yet"
        if empty:
            return f"room empty {_duration(self.empty_room_sec)}: baseline learned"
        return "; ".join(note for note in notes.values() if note) or "no rise over baseline"

    def _publish(self, now, state, side, attributes):
        key = (state, side)
        if key == self._published_key and 0 <= now - self._published_at < REPUBLISH_SEC:
            return
        try:
            self.set_state(self.output_entity, state=state, attributes=attributes, replace=True)
        except Exception as e:
            self.log(f"BedPressureRise publish failed: {e}", level="WARNING")
            return
        self._published_key = key
        self._published_at = now

    # ---------- misc ----------

    def _read(self, entity):
        try:
            return self.get_state(entity)
        except Exception:
            return None

    @staticmethod
    def _iso(epoch):
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")

    def _now(self):
        """Epoch seconds via AppDaemon's own clock (so a scheduler time-travel plugin is
        honoured); falls back to the wall clock on a bare test instance."""
        try:
            return self.get_now().timestamp()
        except Exception:
            return time.time()
