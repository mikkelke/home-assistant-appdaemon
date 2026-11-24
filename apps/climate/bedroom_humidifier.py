"""
BedroomHumidifier - runs the bedroom's SwitchBot evaporative humidifier before bed and through the
night, and dries its wick in the morning.

NIGHT. Only while `sleep_mode` is on and `window` is closed. Below `on_below` % of the bedroom
humidity the app starts the humidifier in `run_mode` (sleep, its quietest), from `off_at` % it stops
it, and in between it keeps doing what it does. An open window stops it; once the window is closed
the app decides afresh. An unreadable humidity, window or sleep-mode reading leaves the humidifier
as it is. While sleep mode is on the app owns the humidifier: it sets another mode back to `run_mode`
and never dries.

BEFORE BED. The evening's window runs from `preload_from` to `preload_until` (house time, past
midnight when `preload_until` is the earlier clock time) and ends as soon as sleep mode turns on.
While sleep mode is off inside it, `person` is home and `window` is closed, the app runs the same
`on_below`/`off_at` rule as at night, on the same humidity. The mode is `preload_mode` while
`occupancy` reads off and `run_mode` while it reads on or cannot be read, so the humidifier is quiet
when someone is in the room. Leaving home or an open window stops it; once back or closed the app
decides afresh. Inside the window the app owns the humidifier as it does at night: another mode is
set back to the wanted one and a run by hand is not manual use. When sleep mode turns on the night
rule takes over without turning the humidifier off. If sleep mode has not turned on by
`preload_until`, the evening ends like a night (the wick is dried, see MORNING) and nothing starts
before the next `preload_from`. The window opens once per evening: after sleep mode has been on in
it, turning off again starts nothing before bed. A drying that is under way is not interrupted; the
phase waits for it. An empty `preload_from` turns the phase off.

HUMIDITY. `humidity_sources` lists the sensors in order of preference. The bedroom humidity is the
first one that reads a number and reported within `humidity_max_age_min` minutes (last_reported,
else last_updated), rounded to 0.1 %; the sources are never averaged. When none qualifies the
humidity is unreadable. The stamps come from AppDaemon's cached state, which moves only when a state
or attribute changes, so a sensor that repeats its value looks as old as its last change.

MORNING. A night in which the humidifier ran leaves the wick wet, and so does a run before bed. When
sleep mode turns off, or the evening's window ends without sleep mode, the app sets `drying_mode`
(drying_filter) and, `drying_hours` later, turns the humidifier off if it is still drying. Sleep mode
turning on again cancels the drying; a drying that could not be started is not retried until the
next morning. Either way the wick stays marked wet, so the next morning dries it even after a night
without a run. A night in which the humidifier never ran starts nothing.

BY HAND. With sleep mode off and outside the evening's window, a humidifier that is on is left alone
and published as manual.

AUTO-DRY. After it is turned off the device dries its wick by itself and then reports state off with
mode drying_filter. That is off, not a run, not manual use and not a drying: whether the humidifier is
running is read from its state and action (its mode only when the action is missing), and the morning
drying below is sent regardless of the mode the device reports.

WATER. The water level reads empty, low, medium or high (PySwitchbot's HumidifierWaterLevel). An
empty tank pushes `notify_target` once while `person` is home, or on arrival when not; the push is
re-armed when the level reads medium or high, so a level that flickers between empty and low does
not push twice. PySwitchbot refuses set_mode with an empty tank, so nothing that needs it is sent
meanwhile.

COMMANDS. They go over Bluetooth through a proxy and can fail or be slow. A command that has not got
the humidifier where it should be is sent once more RETRY_S later; after two tries in a row the app
sends nothing for COOLDOWN_S and logs an error. Every evaluation compares the humidifier's real state
with the wanted one, so a command that was reported done but did not take is noticed the same way.

Publishes `health_entity` (off | humidifying | drying | manual | no_water | unknown) with a
`reason` attribute, the control `humidity` and the `humidity_source` that gave it. While the night
or the evening's window is in charge it also carries `phase` (night | preload), and a running
humidifier's reason names it: "night" or "before bed". AppDaemon-published sensors vanish when Home
Assistant restarts, so the entity is re-published at every five-minute check as well as on every
change.

Each run of the humidifier before bed or while sleep mode is on, and each drying, appends one JSON
line to `log_file` when it ends: {night, kind (humidify | dry), phase (preload | night, null for a
drying), start, stop, minutes, humidity_start, humidity_source_start, humidity_stop,
humidity_source_stop, stop_reason}. A run that carries on from before bed into the night is two
lines. `night` is the date of the evening the night began.

The state that must outlive a restart (the last sleep-mode reading, the wick, the drying deadline,
the open run, the water push, the evening whose window has opened and whether it is still open)
lives in `state_file`. All timestamps are epoch seconds.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

HOUSE_TZ = ZoneInfo("Europe/Copenhagen")

CHECK_S = 300
RETRY_S = 30
COOLDOWN_S = 900
MAX_ATTEMPTS = 2
CALL_TIMEOUT_S = 45
PUSH_RETRY_S = 600

WICK_DRY, WICK_WET, WICK_DUE, WICK_DRYING = "dry", "wet", "due", "drying"
WICKS = (WICK_DRY, WICK_WET, WICK_DUE, WICK_DRYING)

START, SET_MODE, STOP, DRY, DRY_STOP = "start", "set_mode", "stop", "dry", "dry_stop"

PHASE_PRELOAD, PHASE_NIGHT = "preload", "night"

WATER_EMPTY = "empty"
WATER_BACK = ("medium", "high")
WATER_TAG = "bedroom_humidifier_water"

ICONS = {
    "off": "mdi:air-humidifier-off",
    "humidifying": "mdi:air-humidifier",
    "drying": "mdi:fan",
    "manual": "mdi:air-humidifier",
    "no_water": "mdi:water-off",
    "unknown": "mdi:air-humidifier-off",
}


def _number(value):
    """The reading as a float, or None for unavailable/unknown/non-numeric."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _epoch(value):
    """Epoch seconds from an ISO string or a datetime, or None."""
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _iso(ts):
    return datetime.fromtimestamp(ts, HOUSE_TZ).isoformat(timespec="seconds")


def _clock(ts):
    return datetime.fromtimestamp(ts, HOUSE_TZ).strftime("%H:%M")


def night_of(ts):
    """The date of the evening the night around `ts` began (noon to noon, house time)."""
    return (datetime.fromtimestamp(ts, HOUSE_TZ) - timedelta(hours=12)).date().isoformat()


def _clock_time(name, value):
    """The option `name`, written HH:MM, as a time of day."""
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError:
        raise ValueError(f"BedroomHumidifier: {name} must be a time written HH:MM, got {value!r}") from None


def _identity(run):
    """(kind, phase) of a run that goes to the run log; a run saved before the phases existed was a
    night run."""
    kind = run["kind"]
    return kind, run.get("phase") or (PHASE_NIGHT if kind == "humidify" else None)


def _error_text(reply):
    """Why a service call did not succeed, from AppDaemon's answer envelope."""
    if not isinstance(reply, dict):
        return "no answer from Home Assistant"
    error = reply.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    return str(reply.get("ad_status") or "unsuccessful")


class BedroomHumidifier(hass.Hass):
    def initialize(self):
        a = self.args
        self.humidifier = a["humidifier"]
        self.water_entity = a["water_level"]
        self.humidity_sources = a["humidity_sources"]
        self.humidity_max_age_s = float(a.get("humidity_max_age_min", 30)) * 60
        self.sleep_entity = a["sleep_mode"]
        self.window_entity = a["window"]
        self.person_entity = a["person"]
        self.health_entity = a["health_entity"]
        self.notify_target = a.get("notify_target", "mikkel")
        self.run_mode = a.get("run_mode", "sleep")
        self.drying_mode = a.get("drying_mode", "drying_filter")
        self.preload_mode = a.get("preload_mode", "medium")
        self.occupancy_entity = a.get("occupancy", "binary_sensor.bedroom_presence_presence")
        before_bed = a.get("preload_from", "21:00")
        self.preload_from = _clock_time("preload_from", before_bed) if before_bed else None
        self.preload_until = _clock_time("preload_until", a.get("preload_until", "02:00")) if before_bed else None
        self.on_below = float(a.get("on_below", 40))
        self.off_at = float(a.get("off_at", 42))
        self.drying_s = float(a.get("drying_hours", 2)) * 3600
        here = Path(__file__).resolve().parent
        self.state_file = Path(a.get("state_file") or here / "bedroom_humidifier_state.json")
        self.log_file = Path(a.get("log_file") or here / "bedroom_humidifier_log.jsonl")
        if not self.on_below < self.off_at:
            raise ValueError("BedroomHumidifier: on_below must be below off_at")
        if self.drying_s <= 0:
            raise ValueError("BedroomHumidifier: drying_hours must be positive")
        if not (
            isinstance(self.humidity_sources, list)
            and self.humidity_sources
            and all(isinstance(source, str) for source in self.humidity_sources)
        ):
            raise ValueError("BedroomHumidifier: humidity_sources must be a non-empty list of entity ids")
        if self.humidity_max_age_s <= 0:
            raise ValueError("BedroomHumidifier: humidity_max_age_min must be positive")
        if self.preload_from is not None and self.preload_from == self.preload_until:
            raise ValueError("BedroomHumidifier: preload_until must differ from preload_from")

        self._notifier = self.get_app("MobileNotifier")
        saved = self._load_state()
        self._asleep = saved["asleep"]
        self._wick = saved["wick"]
        self._dry_until = saved["dry_until"]
        self._run = saved["run"]
        self._water_told = saved["water_told"]
        self._pre_evening = saved["preload_evening"]
        self._pre_open = saved["preload_open"]
        self._dry_confirmed = self._wick == WICK_DRYING
        self._want_mode = self.run_mode
        self._attempts = 0
        self._attempt_key = None
        self._sent_at = 0.0
        self._not_before = 0.0
        self._off_reason = None
        self._water_next_push_at = 0.0
        self._water_waiting = False
        self._unreadable = ()
        self._reason = ""
        self._published_state = None
        self._published_key = None
        self._saved_text = None

        for entity in (
            self.sleep_entity, self.window_entity, *self.humidity_sources, self.water_entity, self.person_entity,
            self.occupancy_entity,
        ):
            self.listen_state(self._on_change, entity)
        # A mode change leaves the state "on" untouched, so only attribute="all" sees it.
        self.listen_state(self._on_change, self.humidifier, attribute="all")
        self.run_every(self._tick, "now+60", CHECK_S)
        if self.preload_from is not None:
            self.run_daily(self._tick, self.preload_from)
            self.run_daily(self._tick, self.preload_until)

        now = self._now()
        self._evaluate(now)
        if self._wick == WICK_DRYING:
            self.run_in(self._tick, max(0.0, self._dry_until - now) + 5)
        if self.preload_from is None:
            before = "before bed off"
        else:
            before = (
                f"before bed {self.preload_from:%H:%M} to {self.preload_until:%H:%M} in {self.preload_mode} "
                f"while {self.occupancy_entity} is off"
            )
        self.log(
            f"BedroomHumidifier {self.health_entity} initialized: {self.humidifier}, "
            f"humidity from {', '.join(self.humidity_sources)} (at most {self.humidity_max_age_s / 60:g} min old), "
            f"on below {self.on_below:g} %, off at {self.off_at:g} %, {before}, sleep mode {self._asleep}, wick {self._wick}",
            level="INFO",
        )

    # ---------- triggers ----------

    def _on_change(self, entity, attribute, old, new, kwargs):
        self._evaluate(self._now())

    def _tick(self, kwargs):
        self._evaluate(self._now(), force=True)

    def _evaluate(self, now, force=False):
        sleep = {"on": True, "off": False}.get(self.get_state(self.sleep_entity))
        window = {"on": "open", "off": "closed"}.get(self.get_state(self.window_entity))
        humidity, source = self._read_humidity(now)
        kind, mode = self._read_device()
        water = self.get_state(self.water_entity, attribute="all") or {}
        level = water.get("state")
        level_since = _epoch(water.get("last_changed"))

        self._note_unreadable(sleep, window, humidity, kind)
        edge = self._sleep_edge(sleep)
        self._before_bed(now)
        self._observe(now, kind, humidity, source, edge)
        phase = self._phase()
        decision = self._decide(now, kind, mode, window, humidity, phase)
        decision = self._guard_water(decision, level)
        self._act(now, decision, kind)
        self._water(now, level, level_since)
        self._publish(kind, mode, humidity, source, level, level_since, phase, force)
        self._persist()

    # ---------- readings ----------

    def _read_humidity(self, now):
        """(humidity, source): the first listed source that reads a number and reported within
        humidity_max_age_s, else (None, None). The humidity is rounded to 0.1 %."""
        for source in self.humidity_sources:
            full = self.get_state(source, attribute="all") or {}
            value = _number(full.get("state"))
            stamp = _epoch(full.get("last_reported"))
            if stamp is None:
                stamp = _epoch(full.get("last_updated"))
            if value is not None and stamp is not None and now - stamp <= self.humidity_max_age_s:
                return round(value, 1), source
        return None, None

    def _read_device(self):
        """(kind, mode): kind is off, humidify, dry, or None when the humidifier is unreadable.

        Off is decided by the state alone, whatever the mode. An on humidifier is told humidifying
        from drying by its action, and by its mode only when the action is missing."""
        full = self.get_state(self.humidifier, attribute="all") or {}
        attrs = full.get("attributes") or {}
        state, mode, action = full.get("state"), attrs.get("mode"), attrs.get("action")
        if state == "off":
            return "off", mode
        if state != "on":
            return None, mode
        if action == "drying":
            return "dry", mode
        if action == "humidifying":
            return "humidify", mode
        return ("dry" if mode == self.drying_mode else "humidify"), mode

    def _note_unreadable(self, sleep, window, humidity, kind):
        names = (("sleep mode", sleep), ("window", window), ("bedroom humidity", humidity), ("humidifier", kind))
        unreadable = tuple(name for name, value in names if value is None)
        if unreadable == self._unreadable:
            return
        self._unreadable = unreadable
        if unreadable:
            self.log(f"BedroomHumidifier: {', '.join(unreadable)} unreadable, leaving the humidifier as it is", level="WARNING")
        else:
            self.log("BedroomHumidifier: every input readable again", level="INFO")

    def _sleep_edge(self, sleep):
        """"on" or "off" when sleep mode changed since the last readable reading, else None."""
        if sleep is None:
            return None
        previous, self._asleep = self._asleep, sleep
        if previous is None or previous == sleep:
            return None
        return "on" if sleep else "off"

    def _evening(self, now):
        """The date, as text, of the evening whose window holds `now`, else None."""
        if self.preload_from is None:
            return None
        past_midnight = timedelta(days=1 if self.preload_until < self.preload_from else 0)
        today = datetime.fromtimestamp(now, HOUSE_TZ).date()
        for day in (today - timedelta(days=1), today):
            start = datetime.combine(day, self.preload_from, HOUSE_TZ).timestamp()
            end = datetime.combine(day + past_midnight, self.preload_until, HOUSE_TZ).timestamp()
            if start <= now < end:
                return day.isoformat()
        return None

    def _before_bed(self, now):
        """Opens the before-bed phase when the evening's window is first seen with sleep mode off and
        ends it when sleep mode turns on or the window is over. The evening is remembered, so the
        phase opens once and a window that is over starts nothing."""
        evening = self._evening(now)
        if self._pre_open:
            if self._asleep:
                self._end_before_bed("Sleep mode on")
                self.log("BedroomHumidifier: sleep mode on, the night takes over from before bed", level="INFO")
            elif evening != self._pre_evening:
                self._end_before_bed("Before-bed window over")
                self.log(f"BedroomHumidifier: before-bed window over without sleep mode, wick {self._wick}", level="INFO")
                if self._wick == WICK_WET:
                    self._wick = WICK_DUE
        if evening is not None and evening != self._pre_evening and self._asleep is not None:
            self._pre_evening, self._pre_open = evening, not self._asleep
            if self._pre_open:
                self.log(f"BedroomHumidifier: before bed, window until {self.preload_until:%H:%M}", level="INFO")

    def _end_before_bed(self, reason):
        self._pre_open = False
        if self._run is not None and self._run["kind"] == "humidify":
            self._off_reason = reason

    def _phase(self):
        """night while sleep mode is on, preload while the evening's window is open and no drying is
        under way, else None."""
        if self._asleep:
            return PHASE_NIGHT
        if self._pre_open and self._wick not in (WICK_DUE, WICK_DRYING):
            return PHASE_PRELOAD
        return None

    # ---------- bookkeeping ----------

    def _observe(self, now, kind, humidity, source, edge):
        if edge == "on":
            self.log("BedroomHumidifier: sleep mode on", level="INFO")
            if self._wick in (WICK_DUE, WICK_DRYING):
                self.log("BedroomHumidifier: drying cancelled, the wick stays wet", level="INFO")
                self._abandon_drying()
                if self._run is not None:
                    self._off_reason = "Sleep mode on"
        elif edge == "off":
            self.log(f"BedroomHumidifier: sleep mode off, wick {self._wick}", level="INFO")
            if self._run is not None:
                self._off_reason = "Sleep mode off"
            if self._wick == WICK_WET:
                self._wick = WICK_DUE
        if self._phase() and kind == "humidify" and self._wick == WICK_DRY:
            self._wick = WICK_WET
        self._track(now, kind, humidity, source)

    def _track(self, now, kind, humidity, source):
        """Open and close the run that goes to the run log: a humidifier that is on before bed or
        while sleep mode is on, or one that is drying during our drying. Each phase has its own run."""
        if kind is None:
            return
        phase = self._phase()
        tracked = None
        if kind == "humidify" and phase:
            tracked = ("humidify", phase)
        elif kind == "dry" and self._wick == WICK_DRYING:
            tracked = ("dry", None)
        if self._run is not None and _identity(self._run) != tracked:
            self._close_run(now, kind, humidity, source)
        if tracked is not None and self._run is None:
            self._run = {"kind": tracked[0], "phase": tracked[1], "start": now, "humidity": humidity, "source": source}

    def _close_run(self, now, kind, humidity, source):
        run, self._run = self._run, None
        reason, self._off_reason = self._off_reason, None
        if reason is None:
            reason = "Turned off" if kind == "off" else "Mode changed"
        record = {
            "night": night_of(run["start"]),
            "kind": run["kind"],
            "phase": _identity(run)[1],
            "start": _iso(run["start"]),
            "stop": _iso(now),
            "minutes": round((now - run["start"]) / 60, 1),
            "humidity_start": run.get("humidity"),
            "humidity_source_start": run.get("source"),
            "humidity_stop": humidity,
            "humidity_source_stop": source,
            "stop_reason": reason,
        }
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            self.log(f"BedroomHumidifier: run log write failed: {e}", level="WARNING")

    def _abandon_drying(self):
        self._wick = WICK_WET
        self._dry_until = None
        self._dry_confirmed = False

    def _finish_drying(self):
        self._wick = WICK_DRY
        self._dry_until = None
        self._dry_confirmed = False

    # ---------- decisions ----------

    def _decide(self, now, kind, mode, window, humidity, phase):
        """The next command as (key, why), or None. Sets self._reason, the sensor's explanation."""
        if kind is None:
            self._reason = "Humidifier unavailable"
            return None
        if self._asleep is None:
            self._reason = "Sleep mode unreadable, leaving the humidifier as it is"
            return None
        if phase:
            return self._decide_run(kind, mode, window, humidity, phase)
        return self._decide_day(now, kind, mode)

    def _decide_run(self, kind, mode, window, humidity, phase):
        """The night's rule, which the evening's window shares; only the wanted mode and a person
        who has left differ."""
        before_bed = phase == PHASE_PRELOAD
        if before_bed and self.get_state(self.person_entity) != "home":
            self._reason = "Not home"
            return None if kind == "off" else (STOP, "Not home")
        if window == "open":
            self._reason = "Window open"
            return None if kind == "off" else (STOP, "Window open")
        if window is None:
            self._reason = "Window unreadable, leaving the humidifier as it is"
            return None
        if humidity is None:
            self._reason = "Bedroom humidity unreadable, leaving the humidifier as it is"
            return None
        occupied = self.get_state(self.occupancy_entity) != "off"
        self._want_mode = self.preload_mode if before_bed and not occupied else self.run_mode
        running = kind == "humidify"
        if humidity < self.on_below:
            want_run = True
            why = f"Humidity {humidity:g} % is below {self.on_below:g} %"
        elif humidity >= self.off_at:
            want_run = False
            why = f"Humidity {humidity:g} % is at or above {self.off_at:g} %"
        else:
            want_run = running
            why = f"Humidity {humidity:g} % is between {self.on_below:g} and {self.off_at:g} %"
        if want_run:
            where = "night"
            if before_bed:
                where = f"before bed, {self._want_mode}, " + ("someone in the room" if occupied else "room empty")
            self._reason = f"{why}, runs until {self.off_at:g} % ({where})"
            if kind == "off":
                return START, why
            if kind == "dry" or mode != self._want_mode:
                return SET_MODE, why
            return None
        self._reason = f"{why}, stays off" if kind == "off" else f"{why}, stopping"
        return None if kind == "off" else (STOP, why)

    def _decide_day(self, now, kind, mode):
        if self._wick == WICK_DUE:
            self._reason = "Sleep mode is off, drying the wick"
            return DRY, "Sleep mode off"
        if self._wick == WICK_DRYING:
            if now >= self._dry_until:
                if kind == "dry":
                    self._reason = "Drying time over, turning it off"
                    return DRY_STOP, "Drying time over"
                self._finish_drying()
                self.log("BedroomHumidifier: drying over", level="INFO")
            elif kind == "dry":
                self._dry_confirmed = True
                self._reason = f"Drying the wick until {_clock(self._dry_until)}"
                return None
            elif not self._dry_confirmed:
                self._reason = "Drying has not started yet"
                return DRY, "Drying did not start"
        if kind == "off":
            self._reason = "Sleep mode is off"
        elif kind == "dry":
            self._reason = "Drying filter started by hand, leaving it alone"
        else:
            self._reason = f"On by hand ({mode}), leaving it alone"
        return None

    def _guard_water(self, decision, level):
        """PySwitchbot refuses set_mode when the tank is empty, so a command that needs it waits."""
        if decision is None or level != WATER_EMPTY or decision[0] not in (START, SET_MODE, DRY):
            return decision
        self._reason = "Water tank is empty"
        if decision[0] == DRY:
            self.log("BedroomHumidifier: drying skipped, the tank is empty and the humidifier refuses the command", level="WARNING")
            self._abandon_drying()
        return None

    # ---------- commands ----------

    def _act(self, now, decision, kind):
        if decision is None:
            self._attempts, self._attempt_key = 0, None
            return
        key, why = decision
        if now < self._not_before:
            return
        if key != self._attempt_key:
            self._attempt_key, self._attempts = key, 0
        elif now - self._sent_at < RETRY_S:
            return
        if self._attempts >= MAX_ATTEMPTS:
            self._give_up(now, key)
            return
        self._attempts += 1
        self._sent_at = now
        self._send(now, key, why, kind)
        if self._attempts == 1:
            self.run_in(self._tick, RETRY_S)

    def _give_up(self, now, key):
        self._not_before = now + COOLDOWN_S
        self._attempts, self._attempt_key = 0, None
        self.log(
            f"BedroomHumidifier: {key} did not take after {MAX_ATTEMPTS} tries, no new commands for {COOLDOWN_S // 60} min",
            level="ERROR",
        )
        if key == DRY:
            self._abandon_drying()

    def _steps(self, key, kind):
        if key == START:
            return [("humidifier/turn_on", {}), ("humidifier/set_mode", {"mode": self._want_mode})]
        if key == SET_MODE:
            return [("humidifier/set_mode", {"mode": self._want_mode})]
        if key == DRY:
            dry = ("humidifier/set_mode", {"mode": self.drying_mode})
            resend = self._wick == WICK_DRYING
            return [("humidifier/turn_on", {}), dry] if resend and kind == "off" else [dry]
        return [("humidifier/turn_off", {})]

    def _send(self, now, key, why, kind):
        self.log(f"BedroomHumidifier: {key} (try {self._attempts}/{MAX_ATTEMPTS}): {why}", level="INFO")
        if key in (STOP, DRY_STOP):
            self._off_reason = why
        for service, data in self._steps(key, kind):
            error = self._call(service, data)
            if error:
                self.log(f"BedroomHumidifier: {service} failed: {error}", level="WARNING")
                if key in (STOP, DRY_STOP):
                    self._off_reason = None
                return False
        if key == START and self._wick == WICK_DRY:
            self._wick = WICK_WET
        elif key == DRY:
            if self._wick == WICK_DUE:
                self._wick = WICK_DRYING
                self._dry_until = now + self.drying_s
                self.run_in(self._tick, self.drying_s + 5)
        elif key == DRY_STOP:
            self._finish_drying()
        return True

    def _call(self, service, data):
        """None when Home Assistant accepted the call, else why it did not."""
        try:
            reply = self.call_service(service, entity_id=self.humidifier, hass_timeout=CALL_TIMEOUT_S, **data)
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        if isinstance(reply, dict) and reply.get("success") is True:
            return None
        return _error_text(reply)

    # ---------- water ----------

    def _water(self, now, level, since):
        if level == WATER_EMPTY:
            if self._water_told or now < self._water_next_push_at:
                return
            if self.get_state(self.person_entity) != "home":
                if not self._water_waiting:
                    self._water_waiting = True
                    self.log(f"BedroomHumidifier: tank is empty and {self.notify_target} is not home, telling on arrival", level="INFO")
                return
            self._water_told = True
            self._water_waiting = False
            self._water_next_push_at = now + PUSH_RETRY_S
            self.log("BedroomHumidifier: tank is empty, pushing", level="WARNING")
            self.create_task(self._push_water(since))
        elif level in WATER_BACK:
            if self._water_told or self._water_waiting:
                self.log("BedroomHumidifier: tank refilled, water push re-armed", level="INFO")
            self._water_told = False
            self._water_waiting = False
            self._water_next_push_at = 0.0

    async def _push_water(self, since):
        message = f"The tank has read empty since {_clock(since)}." if since else "The tank reads empty."
        try:
            sent = await self._notifier.notify(
                title="Bedroom humidifier is out of water",
                message=message,
                target=self.notify_target,
                data={"tag": WATER_TAG},
            )
        except Exception as e:
            self.log(f"BedroomHumidifier: notify failed: {e}", level="WARNING")
            sent = 0
        if not sent:
            self.log("BedroomHumidifier: push reached no one, will retry", level="WARNING")
            self._water_told = False

    # ---------- semantic entity ----------

    def _publish(self, kind, mode, humidity, source, level, level_since, phase, force):
        if level == WATER_EMPTY:
            state = "no_water"
            reason = f"Water tank is empty since {_clock(level_since)}" if level_since else "Water tank is empty"
        elif kind is None:
            state, reason = "unknown", self._reason
        elif kind == "off":
            state, reason = "off", self._reason
        elif kind == "dry":
            state, reason = "drying", self._reason
        else:
            state, reason = ("humidifying" if phase else "manual"), self._reason
        attrs = {"friendly_name": "Bedroom humidifier", "icon": ICONS[state], "reason": reason}
        if phase:
            attrs["phase"] = phase
        if humidity is not None:
            attrs["humidity"] = humidity
            attrs["humidity_source"] = source
        if level is not None:
            attrs["water_level"] = level
        if kind in ("humidify", "dry") and mode:
            attrs["mode"] = mode
        key = (state, reason, humidity, source, level, attrs.get("mode"), phase)
        if key == self._published_key and not force:
            return
        try:
            self.set_state(self.health_entity, state=state, replace=True, attributes=attrs)
        except Exception as e:
            self.log(f"BedroomHumidifier: publish failed: {e}", level="WARNING")
            return
        if state != self._published_state:
            before = self._published_state or "start"
            self.log(f"BedroomHumidifier: {before} -> {state}: {reason}", level="INFO")
        self._published_state, self._published_key = state, key

    # ---------- restart survival ----------

    def _load_state(self):
        raw = {}
        try:
            with open(self.state_file) as f:
                raw = json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log(f"BedroomHumidifier: state file {self.state_file} unreadable, starting empty: {e}", level="WARNING")
        raw = raw if isinstance(raw, dict) else {}
        asleep = raw.get("asleep")
        wick = raw.get("wick") if raw.get("wick") in WICKS else WICK_DRY
        dry_until = _number(raw.get("dry_until"))
        if wick == WICK_DRYING and dry_until is None:
            wick = WICK_WET
        if wick != WICK_DRYING:
            dry_until = None
        run = raw.get("run")
        if not (isinstance(run, dict) and run.get("kind") in ("humidify", "dry") and _number(run.get("start")) is not None):
            run = None
        evening = raw.get("preload_evening")
        evening = evening if isinstance(evening, str) else None
        return {
            "asleep": asleep if isinstance(asleep, bool) else None,
            "wick": wick,
            "dry_until": dry_until,
            "run": run,
            "water_told": raw.get("water_told") is True,
            "preload_evening": evening,
            "preload_open": evening is not None and raw.get("preload_open") is True,
        }

    def _persist(self):
        text = json.dumps({
            "asleep": self._asleep,
            "wick": self._wick,
            "dry_until": self._dry_until,
            "run": self._run,
            "water_told": self._water_told,
            "preload_evening": self._pre_evening,
            "preload_open": self._pre_open,
        })
        if text == self._saved_text:
            return
        tmp = f"{self.state_file}.tmp"
        try:
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"BedroomHumidifier: state save failed: {e}", level="WARNING")
            return
        self._saved_text = text

    def _now(self):
        return time.time()
