"""Alternating bathroom floor-heating mornings for a three-week trial.

The trial runs past last_day, up to extend_until, until min_boosts boosts have run.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass

LOCAL_TIMEZONE = ZoneInfo("Europe/Copenhagen")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _clock(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError(f"expected a quoted HH:MM time, got {value!r}")
    return time.fromisoformat(value)


class BathroomBoostTrial(hass.Hass):
    def initialize(self):
        try:
            self._configure()
        except (TypeError, ValueError, KeyError) as error:
            self.log(f"BathroomBoostTrial config error, not started: {error}", level="ERROR")
            return
        self._lock = threading.RLock()
        self._cache = {}
        self._active = None
        self._state_dirty = False
        self._morning_day = None
        self._finished = False
        self._load_state()
        now = self._now()
        if self._active:
            self._check_boost(now, restarting=True)
        if (
            not self._in_trial(now)
            and self._finished
            and not self._active
            and not self._state_dirty
        ):
            return
        for entity in (self.thermostat, self.floor, self.air, self.season):
            self.listen_state(self._on_state, entity, attribute="all")
        self.run_daily(self._on_morning, self.start)
        for snapshot_time in self.snapshot_times:
            self.run_daily(
                self._on_snapshot, snapshot_time, snapshot_time=snapshot_time.isoformat()
            )
        self.run_every(self._on_tick, now + timedelta(seconds=60), 60)
        self.log(
            f"BathroomBoostTrial: {self.first_day} through {self.last_day}, "
            f"alternating mornings at {self.start:%H:%M}",
            level="INFO",
        )

    def _configure(self):
        for name, domain in (
            ("thermostat", "climate"),
            ("floor", "sensor"),
            ("air", "sensor"),
            ("season", "input_boolean"),
        ):
            entity = self.args.get(name)
            if (
                not isinstance(entity, str)
                or not entity.startswith(domain + ".")
                or not entity.split(".", 1)[1]
            ):
                raise ValueError(f"{name} must be a {domain} entity")
            setattr(self, name, entity)
        for name in (
            "boost_setpoint",
            "rest_setpoint",
            "floor_below",
            "air_below",
            "floor_rise",
            "max_heating_hours",
            "max_reading_age_hours",
        ):
            value = _number(self.args.get(name))
            if value is None:
                raise ValueError(f"{name} must be finite")
            if name in ("floor_rise", "max_heating_hours", "max_reading_age_hours") and value <= 0:
                raise ValueError(f"{name} must be positive")
            setattr(self, name, value)
        if self.boost_setpoint <= self.rest_setpoint:
            raise ValueError("boost_setpoint must exceed rest_setpoint")
        self.start = _clock(self.args.get("start"))
        self.latest_end = _clock(self.args.get("latest_end"))
        if self.start >= self.latest_end:
            raise ValueError("start must precede latest_end")
        self.first_day = date.fromisoformat(str(self.args.get("first_day")))
        self.last_day = date.fromisoformat(str(self.args.get("last_day")))
        if self.first_day > self.last_day:
            raise ValueError("first_day must not follow last_day")
        self.min_boosts = self.args.get("min_boosts", 0)
        if isinstance(self.min_boosts, bool) or not isinstance(self.min_boosts, int) or self.min_boosts < 0:
            raise ValueError("min_boosts must be a nonnegative integer")
        self.extend_until = date.fromisoformat(str(self.args.get("extend_until", self.last_day)))
        if self.extend_until < self.last_day:
            raise ValueError("extend_until must not precede last_day")
        snapshots = self.args.get("snapshot_times")
        if not isinstance(snapshots, list) or not snapshots:
            raise ValueError("snapshot_times must be a nonempty list")
        self.snapshot_times = [_clock(value) for value in snapshots]
        log_path = self.args.get("log_path")
        if not isinstance(log_path, str) or not log_path.strip():
            raise ValueError("log_path must be a nonempty path")
        self.log_path = Path(log_path)
        self.state_file = self.log_path.with_name(self.log_path.stem + "_state.json")

    def _now(self):
        return self.get_now().astimezone(LOCAL_TIMEZONE)

    def _day_type(self, day):
        return "boost" if (day - self.first_day).days % 2 == 0 else "control"

    def _boosts_done(self):
        try:
            with self.log_path.open(encoding="utf-8") as stream:
                return sum(json.loads(line).get("kind") == "stop" for line in stream if line.strip())
        except FileNotFoundError:
            return 0
        except Exception as error:
            self.log(f"BathroomBoostTrial: log unreadable: {error}", level="WARNING")
            return 0

    def _end_day(self, day):
        if day <= self.last_day or self._boosts_done() >= self.min_boosts:
            return self.last_day
        return self.extend_until

    def _in_trial(self, now):
        end_day = self._end_day(now.date())
        if now.date() > end_day and not self._finished:
            self.log("BathroomBoostTrial: finished", level="INFO")
            self._finished = True
        return self.first_day <= now.date() <= end_day

    def _snapshot(self, entity):
        try:
            store = self.get_state(entity, attribute="all")
        except Exception as error:
            self.log(f"BathroomBoostTrial: cannot read {entity}: {error}", level="WARNING")
            store = None
        store = store if isinstance(store, dict) else {}
        cached = self._cache.get(entity)
        if cached is not None and (_timestamp(cached.get("last_updated")) or 0) >= (
            _timestamp(store.get("last_updated")) or 0
        ):
            return cached
        return store

    def _reading(self, entity, now):
        state = self._snapshot(entity)
        value = _number(state.get("state"))
        updated = _timestamp(state.get("last_updated"))
        age = None if updated is None else (now.timestamp() - updated) / 3600
        return value, age

    def _values(self, now):
        floor, floor_age = self._reading(self.floor, now)
        air, air_age = self._reading(self.air, now)
        return {"floor": floor, "air": air, "floor_age_hours": floor_age, "air_age_hours": air_age}

    def _on_state(self, entity, attribute, old, new, kwargs):
        with self._lock:
            if isinstance(new, dict):
                cached = self._cache.get(entity, {})
                if (_timestamp(new.get("last_updated")) or 0) >= (
                    _timestamp(cached.get("last_updated")) or 0
                ):
                    self._cache[entity] = new
            self._check_boost(self._now())

    def _on_tick(self, kwargs):
        with self._lock:
            now = self._now()
            if self._state_dirty:
                self._state_dirty = not self._save_state()
            self._check_boost(now)
            self._in_trial(now)

    def _on_morning(self, kwargs):
        with self._lock:
            now = self._now()
            if self._active:
                self._check_boost(now)
                return
            if not self._in_trial(now) or self._morning_day == now.date():
                return
            self._morning_day = now.date()
            values = self._values(now)
            if self._snapshot(self.season).get("state") != "on":
                self._append_log(now, "skip", reason="season_off", **values)
                return
            if not self._append_log(now, "morning", **values):
                return
            if self._day_type(now.date()) == "control":
                return
            reason = None
            if now.time() < self.start or now.time() >= self.latest_end:
                reason = "outside_boost_window"
            for name, threshold in (("floor", self.floor_below), ("air", self.air_below)):
                value, age = values[name], values[name + "_age_hours"]
                if reason:
                    break
                if value is None:
                    reason = name + "_unavailable"
                elif age is None or age < 0 or age > self.max_reading_age_hours:
                    reason = name + "_stale"
                elif value >= threshold:
                    reason = name + "_warm"
            if reason:
                self._append_log(now, "skip", reason=reason, **values)
                return
            self._active = {
                "start_ts": now.timestamp(),
                "start_floor": values["floor"],
                "heating_seconds": 0.0,
                "heating_started_ts": now.timestamp() if self._heating() else None,
            }
            if not self._save_state():
                self._active = None
                self._append_log(now, "skip", reason="state_save_failed", **values)
                return
            try:
                self.call_service(
                    "climate/set_temperature",
                    entity_id=self.thermostat,
                    temperature=self.boost_setpoint,
                    hvac_mode="heat",
                )
            except Exception as error:
                self.log(f"BathroomBoostTrial: boost request failed: {error}", level="ERROR")
                self._stop(now, "boost_request_failed", values)

    def _on_snapshot(self, kwargs):
        with self._lock:
            now = self._now()
            if self._in_trial(now):
                self._append_log(
                    now, "snapshot", snapshot_time=kwargs.get("snapshot_time"), **self._values(now)
                )

    def _heating(self):
        return (self._snapshot(self.thermostat).get("attributes") or {}).get(
            "hvac_action"
        ) == "heating"

    def _account_heating(self, now):
        started = self._active["heating_started_ts"]
        if started is not None:
            self._active["heating_seconds"] += max(0.0, now.timestamp() - started)
        self._active["heating_started_ts"] = now.timestamp() if self._heating() else None

    def _check_boost(self, now, restarting=False):
        if not self._active:
            return
        self._account_heating(now)
        start_day = datetime.fromtimestamp(self._active["start_ts"], LOCAL_TIMEZONE).date()
        deadline = datetime.combine(start_day, self.latest_end, LOCAL_TIMEZONE)
        values = self._values(now)
        reason = self._active.get("stop_reason")
        if restarting and now >= deadline:
            reason = "restart_after_end"
        elif not reason and now >= deadline:
            reason = "latest_end"
        if not reason and self._snapshot(self.season).get("state") != "on":
            reason = "season_off"
        if (
            not reason
            and values["floor"] is not None
            and values["floor"] >= self._active["start_floor"] + self.floor_rise
        ):
            reason = "floor_rise"
        if not reason and values["air"] is not None and values["air"] >= self.air_below:
            reason = "air_warm"
        if not reason and self._active["heating_seconds"] >= self.max_heating_hours * 3600:
            reason = "max_heating_hours"
        if reason:
            self._stop(now, reason, values)
        else:
            self._save_state()

    def _stop(self, now, reason, values):
        self._active["stop_reason"] = reason
        self._save_state()
        try:
            self.call_service(
                "climate/set_temperature", entity_id=self.thermostat, temperature=self.rest_setpoint
            )
        except Exception as error:
            self.log(f"BathroomBoostTrial: rest request failed: {error}", level="ERROR")
            return
        completed = self._active
        self._active = None
        self._state_dirty = not self._save_state()
        day = datetime.fromtimestamp(completed["start_ts"], LOCAL_TIMEZONE).date()
        self._append_log(
            now,
            "stop",
            day=day,
            reason=reason,
            heating_hours=completed["heating_seconds"] / 3600,
            start_floor=completed["start_floor"],
            **values,
        )

    def _append_log(self, now, kind, day=None, **values):
        day = day or now.date()
        row = {
            "ts": now.isoformat(),
            "day": day.isoformat(),
            "kind": kind,
            "day_type": self._day_type(day),
            **values,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, allow_nan=False) + "\n")
            return True
        except Exception as error:
            self.log(f"BathroomBoostTrial: log write failed: {error}", level="ERROR")
            return False

    def _save_state(self):
        temporary = self.state_file.with_suffix(".json.tmp")
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump({"active": self._active}, stream, allow_nan=False)
            os.replace(temporary, self.state_file)
            return True
        except Exception as error:
            self.log(f"BathroomBoostTrial: state save failed: {error}", level="ERROR")
            return False

    def _load_state(self):
        try:
            with self.state_file.open(encoding="utf-8") as stream:
                saved = json.load(stream)
            if not isinstance(saved, dict):
                raise ValueError("state must be an object")
            active = saved.get("active")
            if active is None:
                return
            if not isinstance(active, dict):
                raise ValueError("active must be an object")
            for name in ("start_ts", "start_floor", "heating_seconds"):
                if _number(active.get(name)) is None:
                    raise ValueError(f"invalid {name}")
                active[name] = float(active[name])
            now_ts = self._now().timestamp()
            if not 0 <= active["start_ts"] <= now_ts or active["heating_seconds"] < 0:
                raise ValueError("invalid boost interval")
            day = datetime.fromtimestamp(active["start_ts"], LOCAL_TIMEZONE).date()
            if not self.first_day <= day <= self._end_day(day) or self._day_type(day) != "boost":
                raise ValueError("saved boost is outside the trial")
            started = active.get("heating_started_ts")
            if started is not None and (
                _number(started) is None or not active["start_ts"] <= float(started) <= now_ts
            ):
                raise ValueError("invalid heating_started_ts")
            active["heating_started_ts"] = None if started is None else float(started)
            if "stop_reason" in active and not isinstance(active["stop_reason"], str):
                raise ValueError("invalid stop_reason")
            self._active = active
        except FileNotFoundError:
            pass
        except Exception as error:
            self.log(f"BathroomBoostTrial: state file unreadable: {error}", level="WARNING")
