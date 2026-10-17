"""
HeatingController - plans the setpoints for the managed floor-heating zones and records what it
would write. SHADOW ONLY: this app has no write path. It never calls a climate, switch, number
or input service, so a thermostat can only ever receive a setpoint from somebody else. Its
outputs are the sensors it publishes, the decision log and its own state file.

The planning rules live in heating_plan.py and the weather side in heating_forecast.py; this
file reads Home Assistant, runs those rules and publishes the result.

Outputs:
  - sensor.heating_zone_<zone>: the zone's comfort target as state; mode, planned setpoint,
    trim, day timeline, budget and reasons as attributes.
  - sensor.heating_controller: shadow or off_season, with forecast and outdoor status.
  - <log_dir>/decisions-YYYY-MM.jsonl, append only. Line types: forecast (every fetch),
    decision (per zone when anything changed, at least every 30 minutes), bedroom (each closed
    evening of the bedroom loop), daily (per zone and for the house, once a day).
  - state_file: trims, nudges, window and bucket state, day timeline and statistics, the
    bedroom loop. A missing or corrupt file falls back to the yaml defaults.

Reading policy: AppDaemon 4.5.13 can serve a stale get_state value while still firing the
state callback, so every read takes the fresher of the last callback value and the store.

Publish contract (AppDaemon 4.5.13's set_state drops any attribute whose value is 0, False or
None, also inside lists and dicts): booleans go out as the strings "true"/"false", values that
can be exactly zero as strings, and lists are never empty (["<none>"]).

Callbacks may run on several AppDaemon threads; one lock serialises the evaluation.
Log strings are ASCII only.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Optional

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import heating_forecast as hf
import heating_plan as hp

TICK_SECONDS = 300
DECISION_REPEAT_S = 1800.0
MAX_TICK_GAP_S = 900.0
THERMOSTAT_FRESH_MIN = 60.0
OUTDOOR_FRESH_MIN = 20.0
NO_SETPOINT_CHANGE_S = 7200.0
DAILY_KEEP = 60
NOTIFIED_KEEP_S = 3 * 86400.0
STATE_VERSION = 1


def _num(value) -> Optional[float]:
    """A state as a number; unknown, unavailable and junk are None, never 0."""
    if value in (None, "", "unknown", "unavailable", "none", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signed(value: float, places: int = 1) -> str:
    return f"{value:+.{places}f}"


def _text(value: Optional[float], places: int = 1) -> Optional[str]:
    return None if value is None else f"{value:.{places}f}"


def _publishable(value):
    """Make a value survive AppDaemon's set_state: see the module docstring."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value) if value == 0 else value
    if isinstance(value, dict):
        return {k: _publishable(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        items = [_publishable(v) for v in value if v is not None]
        return items or ["<none>"]
    return value


def _label(zone: str) -> str:
    return zone.replace("_", " ").title()


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


class ZoneRuntime:
    """Everything the controller remembers about one zone."""

    def __init__(self, zc: hp.ZoneConfig, cfg: hp.Config, saved: dict):
        saved = _dict(saved)
        self.zc = zc
        self.trim = _num(saved.get("trim"))
        if self.trim is None:
            self.trim = zc.trim.initial
        self.trim = hp.clamp(self.trim, zc.trim.minimum, zc.trim.maximum)
        nudge = _num(saved.get("nudge")) or 0.0
        self.nudge = hp.clamp(nudge, zc.nudge.minimum, zc.nudge.maximum) if zc.residents else 0.0
        self.last_planned = _num(saved.get("last_planned"))
        self.tracker = hp.WindowTracker(cfg.window, _dict(saved.get("window")))
        self.bucket = hp.TokenBucket(cfg.writes_per_hour, _dict(saved.get("bucket")))
        self.blocks = hp.BlockLog(_dict(saved.get("blocks")))
        self.days = {
            d: hp.ZoneDay(d, snap)
            for d, snap in _dict(saved.get("days")).items()
            if isinstance(snap, dict)
        }
        self.cut_day = saved.get("cut_day") if isinstance(saved.get("cut_day"), str) else None
        daily = saved.get("daily")
        self.daily = list(daily)[-DAILY_KEEP:] if isinstance(daily, list) else []
        self.yesterday = (
            saved.get("yesterday") if isinstance(saved.get("yesterday"), dict) else None
        )
        self.desired: Optional[float] = None
        self.desired_since = 0.0
        self.priority: Optional[str] = None
        self.last_base: Optional[float] = None
        self.last_mode: Optional[str] = None
        self.last_plan_mode: Optional[str] = None
        self.setpoint_seen: Optional[float] = None
        self.setpoint_changed_ts: Optional[float] = None
        self.last_tick_ts: Optional[float] = None
        self.decision_key = None
        self.decision_ts = 0.0
        self.published = None
        self.device_limits_seen = None
        self.projection = None

    def snapshot(self) -> dict:
        return {
            "trim": self.trim,
            "nudge": self.nudge,
            "last_planned": self.last_planned,
            "window": self.tracker.snapshot(),
            "bucket": self.bucket.snapshot(),
            "blocks": self.blocks.snapshot(),
            "days": {d: acc.snapshot() for d, acc in self.days.items()},
            "cut_day": self.cut_day,
            "daily": self.daily[-DAILY_KEEP:],
            "yesterday": self.yesterday,
        }


class HeatingController(hass.Hass):
    def initialize(self):
        try:
            self.cfg = hp.parse_config(self.args)
        except hp.ConfigError as e:
            self.log(f"HeatingController config error, not started: {e}", level="ERROR")
            return
        cfg = self.cfg
        self._lock = threading.RLock()
        self._notifier = self.get_app("MobileNotifier")
        self._cache: dict = {}
        self._guard = hp.ReadingGuard()
        self._forecast: Optional[hf.Forecast] = None
        self._location: Optional[tuple] = None
        self._fetch_failing = False
        self._forecast_was_usable: Optional[bool] = None
        self._eval_pending = False
        self._warned: set = set()

        saved = self._load_state()
        saved_zones = _dict(saved.get("zones"))
        self._zones = {
            name: ZoneRuntime(zc, cfg, saved_zones.get(name)) for name, zc in cfg.zones.items()
        }
        self._loop = hp.BedroomLoop(cfg.bedroom, _dict(saved.get("bedroom")))
        self._house = {
            d: dict(v) for d, v in _dict(saved.get("house")).items() if isinstance(v, dict)
        }
        self._notified = {
            k: float(v) for k, v in _dict(saved.get("notified")).items() if _num(v) is not None
        }

        self._watched = self._watched_entities()
        self._triggers = self._trigger_entities()
        for entity in sorted(self._watched):
            self.listen_state(self._on_state, entity, attribute="all")
        self.listen_event(self._on_plugin_started, "plugin_started")
        self.run_every(self._tick, "now+30", TICK_SECONDS)
        self.run_every(self._fetch_forecast, "now+5", int(cfg.forecast_refresh_minutes * 60))
        self.run_daily(self._daily_rollup, "03:00:00")

        self.log(
            f"HeatingController: shadow mode, {len(self._zones)} zones, "
            f"{len(self._watched)} entities watched, log dir {cfg.log_dir}",
            level="INFO",
        )

    # ---------------------------------------------------------------- wiring

    def _watched_entities(self) -> set:
        cfg = self.cfg
        names = {cfg.season_entity, cfg.bedroom.air_entity, cfg.bedroom.ceiling_entity}
        for zc in cfg.zones.values():
            names.update([zc.thermostat, zc.floor_sensor, zc.target_entity])
            names.update(zc.control_air)
            names.update(zc.openings)
        if cfg.party:
            names.update([cfg.party.start_entity, cfg.party.end_entity, cfg.party.switch_entity])
        names.discard("")
        return names

    def _trigger_entities(self) -> set:
        """Entities whose change is worth an evaluation right away, not at the next tick."""
        cfg = self.cfg
        names = {cfg.season_entity, cfg.bedroom.ceiling_entity}
        for zc in cfg.zones.values():
            names.add(zc.target_entity)
            names.update(zc.openings)
        if cfg.party:
            names.update([cfg.party.start_entity, cfg.party.end_entity, cfg.party.switch_entity])
        names.discard("")
        return names

    def _on_state(self, entity, attribute, old, new, kwargs):
        if not isinstance(new, dict):
            return
        ts = self._parse_ts(new.get("last_updated"))
        with self._lock:
            now_ts = self.get_now().timestamp()
            attrs = new.get("attributes") or {}
            self._cache[entity] = {
                "state": new.get("state"),
                "attributes": attrs,
                "ts": ts if ts is not None else now_ts,
                "changed": self._parse_ts(new.get("last_changed")),
            }
            for rt in self._zones.values():
                if rt.zc.thermostat == entity:
                    self._note_setpoint(rt, attrs.get("temperature"), now_ts)
            if entity == self.cfg.bedroom.air_entity:
                self._feed_loop(self.get_now())
        if entity in self._triggers and not self._eval_pending:
            self._eval_pending = True
            self.run_in(self._evaluate_pending, 2)

    def _on_plugin_started(self, event_name, data, kwargs):
        self.log("HeatingController: plugin_started - republishing", level="INFO")
        for rt in self._zones.values():
            rt.published = None
        self._evaluate("tick")

    def _tick(self, kwargs):
        self._evaluate("tick")

    def _evaluate_pending(self, kwargs):
        self._eval_pending = False
        self._evaluate("event")

    # ---------------------------------------------------------------- reading

    @staticmethod
    def _parse_ts(value) -> Optional[float]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            return None

    def _snap(self, entity: str) -> Optional[dict]:
        """The fresher of the last callback value and the store, as
        {state, attributes, ts, changed}; None when neither knows the entity."""
        if not entity:
            return None
        store = None
        try:
            raw = self.get_state(entity, attribute="all")
        except Exception:
            raw = None
        if isinstance(raw, dict):
            store = {
                "state": raw.get("state"),
                "attributes": raw.get("attributes") or {},
                "ts": self._parse_ts(raw.get("last_updated")),
                "changed": self._parse_ts(raw.get("last_changed")),
            }
        cached = self._cache.get(entity)
        if store is None:
            return cached
        if cached is None:
            return store
        return cached if (cached["ts"] or 0.0) > (store["ts"] or 0.0) else store

    def _state_of(self, entity: str) -> Optional[str]:
        snap = self._snap(entity)
        return None if snap is None else snap["state"]

    def _note_setpoint(self, rt: ZoneRuntime, value, now_ts: float) -> None:
        setpoint = _num(value)
        if setpoint is None:
            return
        if rt.setpoint_seen is None or abs(setpoint - rt.setpoint_seen) > 1e-9:
            rt.setpoint_changed_ts = now_ts
        rt.setpoint_seen = setpoint

    def _datetime_ts(self, entity: str, tz) -> Optional[float]:
        """An input_datetime (date and time, local) as epoch seconds."""
        state = self._state_of(entity)
        if state in (None, "", "unknown", "unavailable"):
            return None
        try:
            naive = datetime.strptime(str(state)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        localize = getattr(tz, "localize", None)
        aware = localize(naive) if localize else naive.replace(tzinfo=tz)
        return aware.timestamp()

    # ---------------------------------------------------------------- forecast

    def _http_get_json(self, url: str):
        import requests

        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()

    def _read_location(self) -> Optional[tuple]:
        snap = self._snap("zone.home")
        attrs = snap["attributes"] if snap else {}
        lat, lon = _num(attrs.get("latitude")), _num(attrs.get("longitude"))
        if lat is not None and lon is not None:
            self._location = (lat, lon)
        return self._location

    def _fetch_forecast(self, kwargs):
        with self._lock:
            location = self._read_location()
            now = self.get_now()
        if location is None:
            self._warn_once("location", "HeatingController: zone.home has no location, no forecast")
            return
        try:
            payload = self._http_get_json(hf.request_url(*location))
            forecast = hf.parse_response(payload, now.timestamp())
        except Exception as e:
            with self._lock:
                house = self._house_day(now)
                house["forecast_failed"] = house.get("forecast_failed", 0) + 1
                if not self._fetch_failing:
                    self.log(
                        f"HeatingController: forecast fetch failed ({type(e).__name__}: {e}), "
                        "keeping the last good one",
                        level="WARNING",
                    )
                self._fetch_failing = True
            return
        with self._lock:
            self._forecast = forecast
            house = self._house_day(now)
            house["forecast_fetched"] = house.get("forecast_fetched", 0) + 1
            if self._fetch_failing:
                self.log("HeatingController: forecast fetch works again", level="INFO")
            self._fetch_failing = False
            self._log_forecast(now, forecast)

    def _log_forecast(self, now, fc: hf.Forecast) -> None:
        now_ts = now.timestamp()
        start = next((i for i, t in enumerate(fc.times) if t >= now_ts), 0)
        window = slice(start, start + 48)

        def rounded(series, places):
            return [None if v is None else round(v, places) for v in series[window]]

        self._append_log(
            now,
            {
                "type": "forecast",
                "ts": self._iso(now_ts, now.tzinfo),
                "t0": fc.times[start] if fc.times else None,
                "step_s": hf.HOUR_S,
                "temp": rounded(fc.temperature, 1),
                "wind": rounded(fc.wind, 1),
                "ghi": rounded(fc.ghi, 0),
                "cloud": rounded(fc.cloud, 0),
            },
        )

    def _budgets(self, zc: hp.ZoneConfig, now_ts: float, tz) -> list:
        """Today's and tomorrow's budget for a zone from the current forecast."""
        fc = self._forecast
        if not hf.is_usable(fc, now_ts, self.cfg.forecast_stale_hours) or self._location is None:
            return []
        lat, lon = self._location
        today = hp.local_dt(now_ts, tz).date()
        out = []
        for day in (today, today + timedelta(days=1)):
            weather = hf.day_weather(fc, day, tz)
            if weather is None:
                continue
            sun = []
            if zc.sun_cut.enabled:
                cut = zc.sun_cut
                sun = hf.sun_hours(fc, day, tz, lat, lon, cut.az_min, cut.az_max, cut.ghi_min)
            out.append(hp.day_budget(zc, weather, sun, self.cfg.sun_term_factor))
        return out

    # ---------------------------------------------------------------- evaluation

    def _evaluate(self, reason: str) -> None:
        with self._lock:
            try:
                self._evaluate_locked(self.get_now(), reason)
            except Exception as e:
                tail = " | ".join(traceback.format_exc().strip().splitlines()[-3:])
                self.log(
                    f"HeatingController evaluation failed: {type(e).__name__}: {e} [{tail}]",
                    level="ERROR",
                )

    def _feed_loop(self, now) -> None:
        cfg = self.cfg.bedroom
        snap = self._snap(cfg.air_entity)
        attrs = snap["attributes"] if snap else {}
        air = _num(snap["state"]) if snap else None
        stale = attrs.get("stale") == "true"
        window_open = attrs.get("window_open") == "true"
        helper = _num(self._state_of(cfg.ceiling_entity))
        ceiling = helper if helper is not None else cfg.ceiling_default_c
        decision = self._loop.observe(now.timestamp(), now.tzinfo, air, stale, window_open, ceiling)
        if decision:
            self.log(
                f"HeatingController: bedroom evening {decision['day']} mean {decision['mean']} "
                f"-> {decision['action']}, bathroom hold-back {'on' if decision['hold'] else 'off'}",
                level="INFO",
            )
            self._append_log(
                now, dict(decision, type="bedroom", ts=self._iso(now.timestamp(), now.tzinfo))
            )

    def _evaluate_locked(self, now, reason: str) -> None:
        cfg = self.cfg
        now_ts, tz = now.timestamp(), now.tzinfo
        today = hp.local_dt(now_ts, tz).date()
        periodic = reason == "tick"

        season_on = self._state_of(cfg.season_entity) == "on"
        forecast_ok = hf.is_usable(self._forecast, now_ts, cfg.forecast_stale_hours)
        if forecast_ok != self._forecast_was_usable and self._forecast_was_usable is not None:
            self.log(
                (
                    "HeatingController: forecast usable again"
                    if forecast_ok
                    else "HeatingController: forecast stale, no sun cuts until it is refreshed"
                ),
                level="INFO" if forecast_ok else "WARNING",
            )
        self._forecast_was_usable = forecast_ok

        self._feed_loop(now)
        bedroom = self._bedroom_view(now_ts, tz)
        party = self._party_view(now_ts, tz)
        outdoor = self._outdoor_now(now_ts)
        house = self._house_day(now)
        if periodic:
            house["ticks"] = house.get("ticks", 0) + 1
            self._accumulate(house, "outdoor", outdoor[0])
            self._accumulate(house, "relay_w", _num(self._state_of(cfg.relay_power_entity)))

        ctx = {
            "now": now,
            "now_ts": now_ts,
            "tz": tz,
            "today": today,
            "periodic": periodic,
            "season_on": season_on,
            "forecast_ok": forecast_ok,
            "bedroom": bedroom,
            "party": party,
            "zones": {},
        }
        for zone, rt in self._zones.items():
            try:
                self._evaluate_zone(zone, rt, ctx)
            except Exception as e:
                tail = " | ".join(traceback.format_exc().strip().splitlines()[-3:])
                self.log(
                    f"HeatingController zone {zone} failed: {type(e).__name__}: {e} [{tail}]",
                    level="ERROR",
                )

        self._party_notices(ctx)
        self._publish_controller(ctx, outdoor)
        self._rollup_if_due(now)
        self._prune_notified(now_ts)
        self._save_state()

    @staticmethod
    def _accumulate(store: dict, key: str, value: Optional[float]) -> None:
        if value is None:
            return
        acc = store.setdefault(key, [0.0, 0, None, None])
        acc[0] += value
        acc[1] += 1
        acc[2] = value if acc[2] is None else min(acc[2], value)
        acc[3] = value if acc[3] is None else max(acc[3], value)

    def _house_day(self, now) -> dict:
        day = hp.local_dt(now.timestamp(), now.tzinfo).date().isoformat()
        return self._house.setdefault(day, {})

    def _bedroom_view(self, now_ts: float, tz) -> dict:
        cfg = self.cfg.bedroom
        snap = self._snap(cfg.air_entity)
        attrs = snap["attributes"] if snap else {}
        helper = _num(self._state_of(cfg.ceiling_entity))
        bedtime = helper if helper is not None else cfg.ceiling_default_c
        return {
            "air": _num(snap["state"]) if snap else None,
            "stale": attrs.get("stale") == "true",
            "window_open": attrs.get("window_open") == "true",
            "ceiling": hp.bedroom_ceiling(cfg, hp.clock_minute(now_ts, tz), bedtime),
            "bedtime_ceiling": bedtime,
            "hold": self._loop.hold,
            "last": self._loop.last,
        }

    def _party_view(self, now_ts: float, tz) -> hp.PartyState:
        pc = self.cfg.party
        if pc is None:
            return hp.PartyState()
        switch = self._snap(pc.switch_entity)
        on = switch is not None and switch["state"] == "on"
        return hp.party_state(
            now_ts,
            pc,
            self._datetime_ts(pc.start_entity, tz),
            self._datetime_ts(pc.end_entity, tz),
            on,
            switch["changed"] if on else None,
        )

    def _outdoor_now(self, now_ts: float) -> tuple:
        """Outdoor temperature for the log: the weather station while it is fresh, then the
        weather entity, then the forecast hour. Never used for a setpoint."""
        for entity in self.cfg.outdoor_entities:
            snap = self._snap(entity)
            if snap is None:
                continue
            if entity.startswith("weather."):
                value, max_age = _num(snap["attributes"].get("temperature")), 180.0
            else:
                value, max_age = _num(snap["state"]), OUTDOOR_FRESH_MIN
            age = None if snap["ts"] is None else (now_ts - snap["ts"]) / 60.0
            if value is not None and age is not None and age <= max_age:
                return value, entity
        if self._forecast is not None and hf.is_usable(
            self._forecast, now_ts, self.cfg.forecast_stale_hours
        ):
            value = hf.value_at(self._forecast, self._forecast.temperature, now_ts)
            if value is not None:
                return value, "open-meteo"
        return None, "none"

    def _device_limits(self, rt: ZoneRuntime) -> tuple:
        """Effective (lower, upper): the configured limits, tightened by what the device
        itself enforces. A mismatch is logged once per change."""
        zc = rt.zc
        stem = zc.thermostat.split(".", 1)[1]
        dev_min = _num(self._state_of(f"number.{stem}_min_heat_setpoint"))
        dev_max = _num(self._state_of(f"number.{stem}_max_heat_setpoint"))
        seen = (dev_min, dev_max)
        if seen != rt.device_limits_seen and None not in seen:
            if dev_min != zc.lower_c or dev_max != zc.upper_c:
                self.log(
                    f"HeatingController {zc.name}: thermostat limits {dev_min}-{dev_max} differ "
                    f"from the planned {zc.lower_c}-{zc.upper_c}",
                    level="WARNING",
                )
        rt.device_limits_seen = seen
        lower = max(zc.lower_c, dev_min) if dev_min is not None else zc.lower_c
        upper = min(zc.upper_c, dev_max) if dev_max is not None else zc.upper_c
        if lower >= upper:
            return zc.lower_c, zc.upper_c
        return lower, upper

    def _read_zone(self, rt: ZoneRuntime, now_ts: float, window_active: bool) -> dict:
        zc = rt.zc
        snap = self._snap(zc.thermostat)
        state = snap["state"] if snap else None
        online = state not in (None, "unavailable", "unknown")
        attrs = snap["attributes"] if (snap and online) else {}
        stale = []
        if not online:
            stale.append(f"thermostat:{zc.thermostat}")

        therm_temp = self._guard.filter(
            zc.thermostat, attrs.get("current_temperature"), now_ts, window_active
        )
        therm_age = None if not snap or snap["ts"] is None else (now_ts - snap["ts"]) / 60.0
        if online and (therm_temp is None or therm_age is None or therm_age > THERMOSTAT_FRESH_MIN):
            stale.append(f"thermostat_reading:{zc.thermostat}")

        feels, sun_hit = [], False
        for entity in zc.control_air:
            s = self._snap(entity)
            value = None
            if s is not None:
                raw = _num(s["state"])
                if raw is not None and s["attributes"].get("stale") != "true":
                    value = self._guard.filter(entity, raw, now_ts, window_active)
                sun_hit = sun_hit or s["attributes"].get("sun_hit") == "true"
            if value is None:
                stale.append(f"control_air:{entity}")
            feels.append(value)

        floor_temp, floor_age = None, None
        if zc.floor_sensor:
            fs = self._snap(zc.floor_sensor)
            if fs is not None:
                floor_temp = self._guard.filter(zc.floor_sensor, fs["state"], now_ts, window_active)
                floor_age = None if fs["ts"] is None else (now_ts - fs["ts"]) / 60.0
            if floor_temp is None:
                stale.append(f"floor:{zc.floor_sensor}")

        therm_pair = None if therm_temp is None or therm_age is None else (therm_temp, therm_age)
        floor_pair = None if floor_temp is None or floor_age is None else (floor_temp, floor_age)
        air, source = hp.resolve_control_air(feels, therm_pair, floor_pair)
        return {
            "online": online,
            "hvac_mode": state,
            "setpoint": _num(attrs.get("temperature")),
            "hvac_action": attrs.get("hvac_action"),
            "therm_temp": therm_temp,
            "therm_age": therm_age,
            "floor_temp": floor_temp,
            "air": air,
            "air_source": source,
            "sun_hit": sun_hit,
            "stale": stale,
        }

    def _evaluate_zone(self, zone: str, rt: ZoneRuntime, ctx: dict) -> None:
        zc, cfg = rt.zc, self.cfg
        now_ts, tz, now = ctx["now_ts"], ctx["tz"], ctx["now"]
        day = ctx["today"].isoformat()

        window_active = rt.tracker.state != hp.STATE_CLOSED or bool(rt.tracker.open_openings())
        reading = self._read_zone(rt, now_ts, window_active)
        if rt.setpoint_seen is None:
            self._note_setpoint(rt, reading["setpoint"], now_ts)
        openings = {e: self._state_of(e) for e in zc.openings}
        event = rt.tracker.update(now_ts, openings, reading["air"])

        helper = _num(self._state_of(zc.target_entity))
        base = helper if helper is not None else zc.base_c
        priority = rt.priority
        if event in (hp.EVENT_OPENED, hp.EVENT_RESTORED):
            priority = event
        if rt.last_base is not None and abs(base - rt.last_base) > 1e-9:
            priority = "target_change"
        rt.last_base = base

        lower, upper = self._device_limits(rt)
        budgets = self._budgets(zc, now_ts, tz)
        if ctx["forecast_ok"]:
            cut = hp.evaluate_cut(zc, budgets, now_ts, rt.cut_day)
            rt.cut_day = cut.active_day
        else:
            cut = hp.CutEval()
            rt.cut_day = None

        party = ctx["party"]
        party_windows = party.windows if (cfg.party and cfg.party.zone == zone) else ()
        inp = hp.ZoneInputs(
            season_on=ctx["season_on"],
            online=reading["online"],
            base=base,
            nudge=rt.nudge,
            trim=rt.trim,
            lower_c=lower,
            upper_c=upper,
            fallback=reading["air_source"] == "none",
            window_state=rt.tracker.state,
            party_windows=party_windows,
            hold_back=self._loop.hold and zc.guard.enabled,
            cut_windows=cut.windows,
            last_planned=rt.last_planned,
        )
        plan = hp.plan(zc, inp, now_ts, tz)

        if rt.last_plan_mode is not None and rt.last_plan_mode != plan.plan_mode:
            if hp.MODE_PARTY in (rt.last_plan_mode, plan.plan_mode):
                priority = "party"
        rt.last_plan_mode = plan.plan_mode

        acc = rt.days.setdefault(day, hp.ZoneDay(day))
        gate = self._shadow_gate(rt, plan, now_ts, priority, acc)
        if gate.write or gate.reason == "unchanged":
            priority = None
        rt.priority = priority

        today_budget = next((b for b in budgets if b.day == ctx["today"]), None)
        if today_budget is not None:
            acc.note_budget(self._budget_summary(today_budget))

        bias = self._trim_sample(rt, plan, reading, window_active, now_ts)
        if ctx["periodic"]:
            if rt.last_tick_ts is None:
                gap = float(TICK_SECONDS)
            else:
                gap = min(now_ts - rt.last_tick_ts, MAX_TICK_GAP_S)
            rt.last_tick_ts = now_ts
            acc.add_tick(
                hp.TickSample(
                    ts=now_ts,
                    hour=hp.local_dt(now_ts, tz).hour,
                    dt_s=max(gap, 0.0),
                    plan_mode=plan.plan_mode,
                    season_on=ctx["season_on"],
                    calling=reading["hvac_action"] == "heating",
                    air=reading["air"],
                    target=plan.target,
                    lower_c=lower,
                    bias=bias,
                    fallback=reading["air_source"] == "none",
                )
            )
        for stale_day in [d for d in rt.days if d < (ctx["today"] - timedelta(days=3)).isoformat()]:
            del rt.days[stale_day]

        rt.blocks.observe(now_ts, day, plan.plan_mode, plan.setpoint)
        day_end = hp.local_midnight_ts(ctx["today"] + timedelta(days=1), tz)
        horizon = hp.local_midnight_ts(ctx["today"] + timedelta(days=2), tz)
        projected = self._project(rt, inp, now_ts, horizon, tz)
        today_part = [(s, min(e, day_end), m, sp) for s, e, m, sp in projected if s < day_end]
        timeline = hp.render_blocks(rt.blocks, today_part, now_ts, day_end, tz)
        next_change = None
        if rt.tracker.state == hp.STATE_CLOSED:
            next_change = next((b for b in projected if b[3] != plan.setpoint), None)
        ctx["zones"][zone] = {"air": reading["air"], "target": plan.target}

        if rt.last_mode is not None and rt.last_mode != plan.mode:
            self.log(
                f"HeatingController {zone}: {rt.last_mode} -> {plan.mode}, planned setpoint {plan.setpoint:.1f}",
                level="INFO",
            )
        rt.last_mode = plan.mode

        self._log_decision(
            zone, rt, ctx, plan, reading, gate, budgets, lower, upper, base, party, bias
        )
        self._publish_zone(
            zone,
            rt,
            ctx,
            plan,
            reading,
            cut,
            party_windows,
            budgets,
            timeline,
            next_change,
            lower,
            upper,
            base,
        )

    @staticmethod
    def _project(rt: ZoneRuntime, inp: hp.ZoneInputs, now_ts: float, horizon: float, tz) -> list:
        """The plan from now to the horizon. It only depends on the inputs and the clock, so
        the blocks of the last projection are reused, trimmed to now, while the inputs hold."""
        key = (replace(inp, window_state=hp.STATE_CLOSED), horizon)
        if rt.projection is None or rt.projection[0] != key or rt.projection[1] > now_ts:
            blocks = hp.project_blocks(rt.zc, inp, now_ts, horizon, tz)
            rt.projection = (key, now_ts, blocks)
        blocks = rt.projection[2]
        return [(max(s, now_ts), e, m, sp) for s, e, m, sp in blocks if e > now_ts]

    def _shadow_gate(
        self, rt: ZoneRuntime, plan: hp.Plan, now_ts: float, priority, acc
    ) -> hp.GateResult:
        """What the writer would do with this plan: the same token and stability rules, with
        no write behind it. Only counts while the zone is neither off season nor offline."""
        if plan.mode != plan.plan_mode:
            return hp.GateResult(False, f"gated:{plan.mode}")
        if rt.desired is None or abs(plan.setpoint - rt.desired) > 1e-9:
            rt.desired, rt.desired_since = plan.setpoint, now_ts
        result = hp.write_gate(
            plan.setpoint,
            rt.last_planned,
            now_ts,
            rt.desired_since,
            priority,
            rt.bucket,
            self.cfg.write_stable_minutes * 60.0,
        )
        if result.write:
            rt.last_planned = plan.setpoint
            acc.add_would_write(now_ts, plan.setpoint)
        return result

    def _trim_sample(
        self, rt, plan, reading, window_active: bool, now_ts: float
    ) -> Optional[float]:
        """Thermostat minus control air, when the hour is fit to learn the offset from."""
        quiet = (
            rt.setpoint_changed_ts is not None
            and now_ts - rt.setpoint_changed_ts >= NO_SETPOINT_CHANGE_S
        )
        fresh = reading["therm_age"] is not None and reading["therm_age"] <= THERMOSTAT_FRESH_MIN
        if (
            plan.mode == hp.MODE_HOLD
            and reading["air_source"] == "feel"
            and reading["therm_temp"] is not None
            and fresh
            and quiet
            and not window_active
            and not reading["sun_hit"]
        ):
            return reading["therm_temp"] - reading["air"]
        return None

    @staticmethod
    def _budget_summary(b: hp.DayBudget) -> dict:
        def r(v, nd=2):
            return None if v is None else round(v, nd)

        return {
            "t_mean": r(b.t_mean, 1),
            "wind_mean": r(b.wind_mean, 1),
            "ghi_kwh": r(b.ghi_kwh),
            "h_dull": r(b.h_dull),
            "u_eq": r(b.u_eq),
            "s_sun": r(b.s_sun),
            "s_credit": r(b.s_credit),
            "h_pred": r(b.h_pred),
            "n_sun": b.n_sun,
            "ratio": r(b.ratio),
            "skipped_h": r(b.skipped_h),
        }

    # ---------------------------------------------------------------- party notices

    def _party_notices(self, ctx: dict) -> None:
        pc = self.cfg.party
        if pc is None or pc.zone not in self._zones:
            return
        party, now_ts, tz = ctx["party"], ctx["now_ts"], ctx["tz"]
        rt = self._zones[pc.zone]
        if (
            party.pause
            and ctx["season_on"]
            and rt.last_plan_mode == hp.MODE_PARTY
            and party.event_end
        ):
            key = f"party_pause:{int(party.event_end)}"
            if key not in self._notified:
                self._notified[key] = now_ts
                self._push(
                    "Party: heating pause (shadow)",
                    f"Heating in the living area would pause until {hp.hhmm(party.event_end, tz)}. "
                    "Nothing is written yet.",
                )
        view = ctx["zones"].get(pc.zone)
        if view is None:
            return
        band = self._snap(pc.airing_band_entity)
        message = hp.airing_suggestion(
            pc,
            now_ts,
            party,
            view["air"],
            view["target"],
            None if band is None else str(band["attributes"].get("air_band") or "").lower() or None,
            self._notified.get("airing"),
        )
        if message:
            self._notified["airing"] = now_ts
            self._push("Party: air the living area", message)

    def _push(self, title: str, message: str) -> None:
        self.create_task(self._safe_notify(title, message))

    async def _safe_notify(self, title: str, message: str) -> None:
        try:
            await self._notifier.notify(title=title, message=message, target=self.cfg.notify_target)
            self.log(f"HeatingController pushed: {title}", level="INFO")
        except Exception as e:
            self.log(f"HeatingController push failed: {e}", level="WARNING")

    def _prune_notified(self, now_ts: float) -> None:
        for key in [
            k for k, v in self._notified.items() if now_ts - v > NOTIFIED_KEEP_S and k != "airing"
        ]:
            del self._notified[key]

    # ---------------------------------------------------------------- log and state

    @staticmethod
    def _iso(ts: float, tz) -> str:
        return hp.local_dt(ts, tz).isoformat(timespec="seconds")

    def _append_log(self, now, record: dict) -> None:
        path = os.path.join(self.cfg.log_dir, f"decisions-{now.strftime('%Y-%m')}.jsonl")
        try:
            os.makedirs(self.cfg.log_dir, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        except Exception as e:
            self._warn_once("decision log", f"HeatingController: decision log not written: {e}")

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.log(message, level="WARNING")

    def _load_state(self) -> dict:
        try:
            with open(self.cfg.state_file, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.log(
                f"HeatingController: state file unreadable, using defaults: {e}", level="WARNING"
            )
            return {}

    def _save_state(self) -> None:
        data = {
            "version": STATE_VERSION,
            "zones": {name: rt.snapshot() for name, rt in self._zones.items()},
            "bedroom": self._loop.snapshot(),
            "house": self._house,
            "notified": self._notified,
        }
        tmp = self.cfg.state_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, self.cfg.state_file)
        except Exception as e:
            self._warn_once("state save", f"HeatingController: state not saved: {e}")

    def _log_decision(
        self, zone, rt, ctx, plan, reading, gate, budgets, lower, upper, base, party, bias
    ):
        now_ts, tz = ctx["now_ts"], ctx["tz"]
        key = (
            plan.mode,
            plan.plan_mode,
            plan.setpoint,
            gate.write,
            rt.tracker.state,
            rt.cut_day,
            self._loop.hold,
            party.pause,
            reading["air_source"] == "none",
        )
        if key == rt.decision_key and now_ts - rt.decision_ts < DECISION_REPEAT_S:
            return
        rt.decision_key, rt.decision_ts = key, now_ts
        bedroom = ctx["bedroom"]
        record = {
            "type": "decision",
            "ts": self._iso(now_ts, tz),
            "zone": zone,
            "mode": plan.mode,
            "plan_mode": plan.plan_mode,
            "season": ctx["season_on"],
            "target": round(plan.target, 2),
            "aim": None if plan.aim is None else round(plan.aim, 2),
            "raw": round(plan.raw, 2),
            "setpoint": plan.setpoint,
            "protected_min": plan.protected_min,
            "would_write": gate.write,
            "gate": gate.reason,
            "tokens": round(rt.bucket.available(now_ts), 2),
            "base": base,
            "nudge": rt.nudge,
            "trim": rt.trim,
            "limits": [lower, upper],
            "thermostat": {
                "online": reading["online"],
                "mode": reading["hvac_mode"],
                "temp": reading["therm_temp"],
                "setpoint": reading["setpoint"],
                "action": reading["hvac_action"],
            },
            "air": None if reading["air"] is None else round(reading["air"], 2),
            "air_source": reading["air_source"],
            "floor": reading["floor_temp"],
            "bias_sample": None if bias is None else round(bias, 3),
            "window": rt.tracker.state,
            "openings_on": rt.tracker.open_openings(),
            "party": party.pause if self.cfg.party and self.cfg.party.zone == zone else None,
            "cut_day": rt.cut_day,
            "budget": [self._budget_summary(b) | {"day": b.day.isoformat()} for b in budgets],
            "reasons": list(plan.reasons),
            "stale_inputs": reading["stale"],
        }
        if rt.zc.guard.enabled:
            record["bedroom"] = {
                "air": bedroom["air"],
                "stale": bedroom["stale"],
                "window_open": bedroom["window_open"],
                "ceiling": bedroom["ceiling"],
                "hold_back": bedroom["hold"],
            }
        self._append_log(ctx["now"], record)

    # ---------------------------------------------------------------- rollup

    def terminate(self):
        """Keep the day's statistics across a reload."""
        if getattr(self, "_zones", None):
            with self._lock:
                self._save_state()

    def _daily_rollup(self, kwargs):
        with self._lock:
            self._rollup_if_due(self.get_now())
            self._save_state()

    def _rollup_if_due(self, now) -> None:
        """Close every finished day: learn the trim, write the daily records, keep the last
        60. Idempotent, so a missed 03:00 run is made up at the next tick."""
        if now.hour < 3:
            return
        today = hp.local_dt(now.timestamp(), now.tzinfo).date().isoformat()
        for zone, rt in self._zones.items():
            for day in sorted(d for d in rt.days if d < today):
                acc = rt.days.pop(day)
                if not acc.ticks:
                    continue
                update = hp.update_trim(rt.trim, acc.bias, rt.zc.trim)
                record = acc.record(zone, rt.trim, update)
                rt.trim = update.trim
                record["ts"] = self._iso(now.timestamp(), now.tzinfo)
                rt.daily.append(record)
                rt.daily = rt.daily[-DAILY_KEEP:]
                rt.yesterday = {
                    "day": day,
                    "call_hours": record["call_hours"],
                    "in_band_pct": record["in_band_pct"],
                    "would_writes": record["would_writes"],
                    "h_pred": (record["budget"] or {}).get("h_pred"),
                    "trim": update.trim,
                }
                self._append_log(now, record)
        for day in sorted(d for d in self._house if d < today and not self._house[d].get("closed")):
            stats = self._house[day]
            stats["closed"] = True
            self._append_log(
                now,
                {
                    "type": "daily",
                    "day": day,
                    "zone": "_house",
                    "ts": self._iso(now.timestamp(), now.tzinfo),
                    "ticks": stats.get("ticks", 0),
                    "forecast_fetched": stats.get("forecast_fetched", 0),
                    "forecast_failed": stats.get("forecast_failed", 0),
                    "outdoor": self._range(stats.get("outdoor")),
                    "relay_w_mean": self._range(stats.get("relay_w"), mean_only=True),
                },
            )
        for day in [
            d
            for d in self._house
            if d < (date.fromisoformat(today) - timedelta(days=DAILY_KEEP)).isoformat()
        ]:
            del self._house[day]

    @staticmethod
    def _range(acc, mean_only: bool = False):
        if not acc or not acc[1]:
            return None
        mean = round(acc[0] / acc[1], 2)
        return mean if mean_only else {"mean": mean, "min": acc[2], "max": acc[3]}

    # ---------------------------------------------------------------- publishing

    def _publish(self, entity: str, state, attributes: dict) -> None:
        try:
            self.set_state(entity, state=state, replace=True, attributes=_publishable(attributes))
        except Exception as e:
            self.log(f"HeatingController: publish failed for {entity}: {e}", level="WARNING")

    def _publish_zone(
        self,
        zone,
        rt,
        ctx,
        plan,
        reading,
        cut,
        party_windows,
        budgets,
        timeline,
        next_change,
        lower,
        upper,
        base,
    ):
        zc, tz, now_ts = rt.zc, ctx["tz"], ctx["now_ts"]
        today_budget = next((b for b in budgets if b.day == ctx["today"]), None)
        acc = rt.days.get(ctx["today"].isoformat())
        window = next((w for w in cut.windows if w[1] > now_ts), None)
        party_window = next((w for w in party_windows if w[1] > now_ts), None)

        def iso(ts):
            return self._iso(ts, tz)

        attrs = {
            "friendly_name": f"Heating {_label(zone)}",
            "icon": "mdi:radiator",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "mode": plan.mode,
            "plan_mode": plan.plan_mode,
            "live": False,
            "setpoint_now": plan.setpoint,
            "base_target": base,
            "nudge": _signed(rt.nudge),
            "nudge_min": _signed(zc.nudge.minimum),
            "nudge_max": _signed(zc.nudge.maximum),
            "trim": _signed(rt.trim, 2),
            "profile_offset": _signed(plan.target - base - rt.nudge),
            "lower_limit": lower,
            "upper_limit": upper,
            "protected_min": plan.protected_min,
            "control_air": None if reading["air"] is None else round(reading["air"], 1),
            "control_air_source": reading["air_source"],
            "thermostat_temp": reading["therm_temp"],
            "floor_temp": reading["floor_temp"],
            "next_change_at": iso(next_change[0]) if next_change else None,
            "next_setpoint": next_change[3] if next_change else None,
            "cut_window": [iso(window[0]), iso(window[1])] if window else [],
            "party_window": [iso(party_window[0]), iso(party_window[1])] if party_window else [],
            "day_blocks": timeline,
            "budget_today_h": _text(today_budget.h_pred) if today_budget else None,
            "budget_dull_h": _text(today_budget.h_dull) if today_budget else None,
            "sun_credit_h": _text(today_budget.s_credit) if today_budget else None,
            "call_hours_today": _text(acc.call_s / 3600.0) if acc else None,
            "reasons": list(plan.reasons),
            "stale_inputs": reading["stale"],
            "yesterday": rt.yesterday,
        }
        if zc.guard.enabled:
            last = ctx["bedroom"]["last"] or {}
            attrs["bedroom_hold_back"] = ctx["bedroom"]["hold"]
            attrs["bedroom_evening_mean"] = _text(last.get("mean"))
            attrs["bedroom_ceiling"] = ctx["bedroom"]["ceiling"]
        payload = (round(plan.target, 1), attrs)
        if payload == rt.published:
            return
        rt.published = payload
        self._publish(f"sensor.heating_zone_{zone}", round(plan.target, 1), attrs)

    def _publish_controller(self, ctx: dict, outdoor: tuple) -> None:
        now_ts, tz = ctx["now_ts"], ctx["tz"]
        fc = self._forecast
        today = ctx["today"].isoformat()
        writes = sum(
            len(rt.days[today].would_writes) for rt in self._zones.values() if today in rt.days
        )
        attrs = {
            "friendly_name": "Heating controller",
            "icon": "mdi:radiator",
            "zones_live": [],
            "forecast_source": "open-meteo",
            "forecast_ok": ctx["forecast_ok"],
            "forecast_issued": None if fc is None else self._iso(fc.fetched_at, tz),
            "forecast_age_min": None if fc is None else f"{hf.age_minutes(fc, now_ts):.0f}",
            "outdoor_source": outdoor[1],
            "outdoor_now": _text(outdoor[0]),
            "would_writes_today": str(writes),
            "sun_term_factor": self.cfg.sun_term_factor,
            "last_tick": self._iso(now_ts, tz),
        }
        self._publish(
            "sensor.heating_controller", "shadow" if ctx["season_on"] else "off_season", attrs
        )
