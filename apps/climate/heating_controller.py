"""
HeatingController - plans the setpoints for the managed floor-heating zones and records what it
would write. SHADOW ONLY: this app has no write path. It never calls a climate, switch, number
or input service, so a thermostat can only ever receive a setpoint from somebody else. Its
outputs are the sensors it publishes, the decision log and its own state file.

The planning rules live in heating_plan.py, the weather side in heating_forecast.py, the rules
for who changed what in heating_signals.py, the plant alerts in heating_alerts.py and the season
advice in heating_season.py; this file reads Home Assistant, runs those rules and publishes the
result.

Signals: a thermostat setpoint, mode or preset change is attributed from the service calls the
app sees on the thermostats; the four nudge buttons and the helpers the owner owns are read from
raw state-change events, which carry the context that names the human. A resident's nudge moves
the plan. Everything the live controller would do about a change (restore a setpoint, revert a
helper, move a target) is only recorded.

Alerts go to the owner only, through MobileNotifier. They cover the physical plant and run in
shadow too. So does the season advice: a push that asks the owner to start the season in the
autumn or stop it in the spring, never a change of the season switch.

Outputs:
  - sensor.heating_zone_<zone>: the zone's comfort target as state; mode, planned setpoint,
    trim, nudge, last signal, day timeline, budget and reasons as attributes.
  - sensor.heating_controller: shadow or off_season, with forecast and outdoor status, the
    active alerts and the latest season advice.
  - <log_dir>/decisions-YYYY-MM.jsonl, append only. Line types: forecast (every fetch),
    decision (per zone when anything changed, at least every 30 minutes), bedroom (each closed
    evening of the bedroom loop), signal (each classified change), alert (each start,
    suppressed start and clear), season_advice (each judgement that advises something, pushed
    or not), daily (per zone and for the house, once a day).
  - state_file: trims, nudges, window and bucket state, day timeline and statistics, the
    bedroom loop, the last signals, the alert episodes and the season advisor's morning means
    and asks. A missing or corrupt file falls back to the yaml defaults.

Reading policy: AppDaemon 4.5.13 can serve a stale get_state value while still firing the
state callback, so every read takes the fresher of the last callback value and the store. An
entity created while AppDaemon runs reads as None until it restarts; a missing nudge button is
logged once and the app carries on.

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

import heating_alerts as al
import heating_forecast as hf
import heating_plan as hp
import heating_season as ss
import heating_signals as sig

TICK_SECONDS = 300
DECISION_REPEAT_S = 1800.0
MAX_TICK_GAP_S = 900.0
FLOOR_FRESH_MIN = 60.0
OUTDOOR_FRESH_MIN = 20.0
NO_SETPOINT_CHANGE_S = 7200.0
DAILY_KEEP = 60
NOTIFIED_KEEP_S = 3 * 86400.0
SIGNALS_KEEP = 50
IDENTITY_REFRESH_S = 600.0
STATE_VERSION = 1
UNREADABLE = (None, "", "unknown", "unavailable")


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
        self.nudge_at = _num(saved.get("nudge_at"))
        direction = _num(saved.get("nudge_dir")) or 0
        self.nudge_dir = (direction > 0) - (direction < 0)
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
        self.mode_seen: Optional[str] = None
        self.preset_seen: Optional[str] = None
        self.gap = False
        self.last_signal: Optional[dict] = None
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
            "nudge_at": self.nudge_at,
            "nudge_dir": self.nudge_dir,
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
        self._started_ts = self.get_now().timestamp()
        self._thermostats = {zc.thermostat: zone for zone, zc in cfg.zones.items()}
        self._buttons = self._button_map()
        self._owned = self._owned_helpers()
        self._calls = sig.CallBuffer()
        self._identity = sig.Identity(cfg.owner, cfg.appdaemon_user_id)
        self._identity_at = -IDENTITY_REFRESH_S
        saved_signals = saved.get("signals")
        if not isinstance(saved_signals, list):
            saved_signals = []
        self._signals = [s for s in saved_signals if isinstance(s, dict)][-SIGNALS_KEEP:]
        for zone, rt in self._zones.items():
            rt.last_signal = next(
                (s for s in reversed(self._signals) if s.get("zone") == zone), None
            )
        self._alerts = al.Alerts(cfg.alerts, saved.get("alerts"))
        self._advisor = (
            ss.SeasonAdvisor(cfg.season_advisor, cfg.zones, saved.get("season_advisor"))
            if cfg.season_advisor
            else None
        )
        season_saved = saved.get("season_on")
        self._season_prev: Optional[bool] = season_saved if isinstance(season_saved, bool) else None
        if not cfg.appdaemon_user_id:
            self._warn_once(
                "appdaemon id",
                "HeatingController: appdaemon_user_id not configured, "
                "other apps' changes will read as unknown accounts",
            )

        self._watched = self._watched_entities()
        self._triggers = self._trigger_entities()
        for entity in sorted(self._watched):
            self.listen_state(self._on_state, entity, attribute="all")
        self._wire_events()
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

    def _button_map(self) -> dict:
        """Nudge button entity -> (zone, direction)."""
        out = {}
        for zone, zc in self.cfg.zones.items():
            for name, direction in (("warmer", 1), ("colder", -1)):
                if zc.buttons.get(name):
                    out[zc.buttons[name]] = (zone, direction)
        return out

    def _owned_helpers(self) -> dict:
        """The helpers only the owner may change, each with the zone whose target it is."""
        cfg = self.cfg
        owned = {zc.target_entity: zone for zone, zc in cfg.zones.items()}
        others = [cfg.season_entity, cfg.bedroom.ceiling_entity]
        if cfg.party:
            others += [cfg.party.start_entity, cfg.party.end_entity, cfg.party.switch_entity]
        owned.update({entity: None for entity in others if entity})
        return owned

    def _wire_events(self) -> None:
        self.listen_event(self._on_plugin_started, "plugin_started")
        for domain in sig.CALL_DOMAINS:
            self.listen_event(self._on_service_call, sig.CALL_EVENT, domain=domain)
        for entity in sorted(self._buttons):
            if self._state_of(entity) is None:
                self._warn_once(
                    f"button {entity}",
                    f"HeatingController: {entity} is not known to AppDaemon yet (restart "
                    "AppDaemon after creating a helper); listening for it anyway",
                )
            self.listen_event(self._on_button, "state_changed", entity_id=entity)
        for entity in sorted(self._owned):
            self.listen_event(self._on_helper, "state_changed", entity_id=entity)

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
            rt = self._zones.get(self._thermostats.get(entity))
            if rt is not None:
                self._safely(
                    "thermostat change", self._on_thermostat, rt, new.get("state"), attrs, now_ts
                )
            if entity == self.cfg.bedroom.air_entity:
                self._feed_loop(self.get_now())
        if entity in self._triggers:
            self._request_eval()

    def _request_eval(self) -> None:
        if not self._eval_pending:
            self._eval_pending = True
            self.run_in(self._evaluate_pending, 2)

    def _safely(self, what: str, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            tail = " | ".join(traceback.format_exc().strip().splitlines()[-3:])
            self.log(
                f"HeatingController {what} failed: {type(e).__name__}: {e} [{tail}]", level="ERROR"
            )

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

    # ---------------------------------------------------------------- signals

    def _identity_for(self, now_ts: float) -> sig.Identity:
        """Which account is which person: the person entities' user_id, and the yaml map for a
        resident whose person is not linked to their account. Re-read every ten minutes."""
        if now_ts - self._identity_at >= IDENTITY_REFRESH_S:
            cfg = self.cfg
            users = {sig.normalize_user_id(k): v for k, v in cfg.user_ids.items()}
            for name in {cfg.owner}.union(*(zc.residents for zc in cfg.zones.values())):
                try:
                    account = self.get_state(f"person.{name}", attribute="user_id")
                except Exception:
                    account = None
                if account:
                    users[sig.normalize_user_id(account)] = name
            self._identity = self._identity.with_users(users)
            self._identity_at = now_ts
        return self._identity

    def _on_service_call(self, event_name, data, kwargs):
        self._safely("service call", self._service_call_event, data)

    def _service_call_event(self, data) -> None:
        records = sig.call_records(data, self.get_now().timestamp(), self._thermostats)
        with self._lock:
            for record in records:
                self._calls.add(record)

    @staticmethod
    def _state_event(data, first_press_ok: bool = False):
        """(entity, old, new, user_id, parent_id) of a state_changed event that is a real
        change, else None: an unreadable state on either side is Home Assistant restoring the
        entity after a restart, not a press or an edit. A button nobody has pressed yet reads
        unknown, so for a button that is a fine state to be pressed from."""
        data = data if isinstance(data, dict) else {}
        old_raw, new_raw = data.get("old_state"), data.get("new_state")
        if not isinstance(old_raw, dict) or not isinstance(new_raw, dict):
            return None
        old, new = old_raw.get("state"), new_raw.get("state")
        unreadable_old = ("", "unavailable", None) if first_press_ok else UNREADABLE
        if old == new or old in unreadable_old or new in UNREADABLE:
            return None
        context = new_raw.get("context") if isinstance(new_raw.get("context"), dict) else {}
        return data.get("entity_id"), old, new, context.get("user_id"), context.get("parent_id")

    def _on_button(self, event_name, data, kwargs):
        self._safely("nudge button", self._button_event, data)

    def _button_event(self, data) -> None:
        fields = self._state_event(data, first_press_ok=True)
        if fields is None or fields[0] not in self._buttons:
            return
        entity, _old, _new, user_id, parent_id = fields
        zone, direction = self._buttons[entity]
        with self._lock:
            rt = self._zones[zone]
            now_ts = self.get_now().timestamp()
            actor = self._identity_for(now_ts).actor(user_id, parent_id)
            before = rt.nudge
            if sig.button_allowed(rt.zc, self.cfg.owner, actor):
                effect = self._step_nudge(rt, direction, now_ts)
            else:
                effect = sig.EFFECT_IGNORED
            source, who = sig.describe(actor)
            self._record_signal(
                now_ts,
                zone,
                kind=sig.KIND_BUTTON,
                source=sig.SRC_BUTTON,
                who=sig.who_label(source, who),
                direction=direction,
                effect=effect,
                nudge=[before, rt.nudge],
            )
            if effect == sig.EFFECT_NUDGE:
                self._save_state()

    def _on_helper(self, event_name, data, kwargs):
        self._safely("helper change", self._helper_event, data)

    def _helper_event(self, data) -> None:
        fields = self._state_event(data)
        if fields is None or fields[0] not in self._owned:
            return
        entity, old, new, user_id, parent_id = fields
        zone = self._owned[entity]
        with self._lock:
            rt = self._zones.get(zone)
            now_ts = self.get_now().timestamp()
            actor = self._identity_for(now_ts).actor(user_id, parent_id)
            effect = sig.helper_effect(
                self.cfg.owner, actor, rt.zc if rt else None, rt.nudge if rt else 0.0
            )
            if effect == sig.EFFECT_NONE:
                return
            kind = sig.KIND_TARGET if rt else sig.KIND_HELPER
            source, who = sig.describe(actor)
            extra = {"entity": entity}
            if rt is not None:
                extra["nudge"] = [rt.nudge, 0.0 if effect == sig.EFFECT_NUDGE_RESET else rt.nudge]
            if effect == sig.EFFECT_NUDGE_RESET:
                rt.nudge, rt.nudge_at, rt.nudge_dir = 0.0, None, 0
                rt.priority = "nudge"
            if effect == sig.EFFECT_WOULD_REVERT:
                extra["revert_to"] = old
            self._record_signal(
                now_ts,
                zone,
                kind=kind,
                source=source,
                who=sig.who_label(source, who),
                direction=sig.Change(kind, old, new, now_ts).direction,
                effect=effect,
                old=old,
                new=new,
                **extra,
            )
            if effect == sig.EFFECT_NUDGE_RESET:
                self._save_state()

    def _on_thermostat(self, rt: ZoneRuntime, state, attrs: dict, now_ts: float) -> None:
        """Notice a setpoint, mode or preset change of a managed thermostat. The first report
        after the thermostat was unavailable only re-seeds what is remembered: whatever changed
        while it was gone is not a signal."""
        if state in UNREADABLE:
            rt.gap = True
            return
        previous = rt.setpoint_seen
        self._note_setpoint(rt, attrs.get("temperature"), now_ts)
        setpoint = _num(attrs.get("temperature"))
        preset = attrs.get("preset_mode") if isinstance(attrs.get("preset_mode"), str) else None
        changes = []
        if not rt.gap:
            if setpoint is not None and previous is not None and abs(setpoint - previous) > 1e-9:
                changes.append(sig.Change(sig.KIND_SETPOINT, previous, setpoint, now_ts))
            if rt.mode_seen is not None and state != rt.mode_seen:
                changes.append(sig.Change(sig.KIND_MODE, rt.mode_seen, state, now_ts))
            if rt.preset_seen is not None and preset is not None and preset != rt.preset_seen:
                changes.append(sig.Change(sig.KIND_PRESET, rt.preset_seen, preset, now_ts))
        rt.gap = False
        rt.mode_seen = state
        if preset is not None:
            rt.preset_seen = preset
        for change in changes:
            self._handle_change(rt, change)

    def _handle_change(self, rt: ZoneRuntime, change: sig.Change) -> None:
        zc = rt.zc
        attr = sig.attribute_change(zc, change, self._calls, self._identity_for(change.ts))
        effect = sig.thermostat_effect(zc, self.cfg.owner, change, attr)
        extra: dict = {}
        before = rt.nudge
        if effect == sig.EFFECT_NUDGE:
            effect = self._step_nudge(rt, change.direction, change.ts)
        elif effect == sig.EFFECT_WOULD_SET_TARGET:
            extra.update(self._target_step(rt, change.direction))
        if zc.residents:
            extra["nudge"] = [before, rt.nudge]
        if attr.call is not None:
            extra["call"] = attr.call.service
        self._record_signal(
            change.ts,
            zc.name,
            kind=change.kind,
            source=attr.source,
            who=sig.who_label(attr.source, attr.who),
            direction=change.direction,
            effect=effect,
            old=change.old,
            new=change.new,
            **extra,
        )
        if effect == sig.EFFECT_NUDGE:
            self._save_state()

    def _step_nudge(self, rt: ZoneRuntime, direction: int, ts: float) -> str:
        nudge, effect = sig.step_nudge(
            rt.zc.nudge, rt.nudge, direction, ts, rt.nudge_at, rt.nudge_dir
        )
        if effect in (sig.EFFECT_NUDGE, sig.EFFECT_NUDGE_LIMIT):
            rt.nudge_at, rt.nudge_dir = ts, direction
        if effect == sig.EFFECT_NUDGE:
            rt.nudge = nudge
            rt.priority = "nudge"
        return effect

    def _target_step(self, rt: ZoneRuntime, direction: int) -> dict:
        """What the live controller would write to the zone's target helper when the owner
        moves the thermostat: one step from the current target, inside the helper's range."""
        snap = self._snap(rt.zc.target_entity)
        attrs = snap["attributes"] if snap else {}
        current = _num(snap["state"]) if snap else None
        if current is None:
            current = rt.zc.base_c
        target = current + direction * hp.SETPOINT_STEP_C
        low, high = _num(attrs.get("min")), _num(attrs.get("max"))
        if low is not None:
            target = max(low, target)
        if high is not None:
            target = min(high, target)
        return {"target_from": current, "target_to": round(target, 2)}

    def _reset_nudges(self, now_ts: float) -> None:
        for zone, rt in self._zones.items():
            if rt.nudge == 0:
                continue
            before = rt.nudge
            rt.nudge, rt.nudge_at, rt.nudge_dir = 0.0, None, 0
            rt.priority = "nudge"
            self._record_signal(
                now_ts,
                zone,
                kind="season",
                source="season",
                who="season",
                direction=0,
                effect=sig.EFFECT_NUDGE_RESET,
                nudge=[before, 0.0],
            )

    def _record_signal(self, now_ts: float, zone: Optional[str], **fields) -> None:
        """Log a classified signal, remember the last ones and ask for an evaluation so the
        zone sensor shows it."""
        compact = {"zone": zone, "at": now_ts}
        compact.update(
            {
                k: fields.get(k)
                for k in ("kind", "source", "who", "direction", "effect", "old", "new")
            }
        )
        self._signals.append(compact)
        del self._signals[:-SIGNALS_KEEP]
        if zone in self._zones:
            self._zones[zone].last_signal = compact
        now = self.get_now()
        self._append_log(
            now,
            {"type": "signal", "ts": self._iso(now_ts, now.tzinfo), "zone": zone, **fields},
        )
        self.log(
            f"HeatingController signal {zone or 'house'}: {fields.get('kind')} by "
            f"{fields.get('who')} -> {fields.get('effect')}",
            level="INFO",
        )
        self._request_eval()

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
        """The budget for a zone from the current forecast, today and the next two days: the
        plan is projected to the end of tomorrow, and its lead time reaches into the day after."""
        fc = self._forecast
        if not hf.is_usable(fc, now_ts, self.cfg.forecast_stale_hours) or self._location is None:
            return []
        lat, lon = self._location
        today = hp.local_dt(now_ts, tz).date()
        out = []
        for day in (today + timedelta(days=n) for n in range(3)):
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

        season_state = self._state_of(cfg.season_entity)
        season_on = season_state == "on"
        if season_state in ("on", "off"):
            if season_on and self._season_prev is False:
                self._reset_nudges(now_ts)
            self._season_prev = season_on
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
        relay_w = _num(self._state_of(cfg.relay_power_entity))
        supply = self._clamp_temp(cfg.supply_temp_entity)
        if periodic:
            house["ticks"] = house.get("ticks", 0) + 1
            self._accumulate(house, "outdoor", outdoor[0])
            self._accumulate(house, "relay_w", relay_w)
            self._accumulate(house, "supply", supply)
            self._accumulate(house, "return", self._clamp_temp(cfg.return_temp_entity))

        ctx = {
            "now": now,
            "now_ts": now_ts,
            "tz": tz,
            "today": today,
            "periodic": periodic,
            "season_on": season_on,
            "season_known": season_state in ("on", "off"),
            "forecast_ok": forecast_ok,
            "bedroom": bedroom,
            "party": party,
            "zones": {},
            "obs": [],
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
        if periodic:
            self._alerts_tick(ctx, outdoor[0], relay_w, supply)
        self._rollup_if_due(now)
        self._safely("season advice", self._season_tick, ctx)
        self._publish_controller(ctx, outdoor)
        self._prune_notified(now_ts)
        self._save_state()

    def _clamp_temp(self, entity: str) -> Optional[float]:
        """A pipe clamp reading. It runs far above the 5-35 C a room can read, so it does not
        go through the reading guard. Empty while no clamp is configured."""
        if not entity:
            return None
        value = _num(self._state_of(entity))
        return value if value is not None and -20.0 <= value <= 120.0 else None

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
        # Salus reports about every 65.6 min; unchanged reports need not generate an HA
        # state_changed event. AppDaemon's cached last_updated (or last_reported) therefore
        # cannot time device reports. Use z2m's availability (150 min timeout) and a valid
        # reading; retain the age of the *state change* only as a diagnostic. Do not invent
        # a report timestamp on each evaluation or let unrelated attribute changes renew it.
        therm_state_age = None if not snap or snap["ts"] is None else (now_ts - snap["ts"]) / 60.0
        therm_usable = online and therm_temp is not None
        if online and not therm_usable:
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

        floor_pair = None if floor_temp is None or floor_age is None else (floor_temp, floor_age)
        # The thermostat's availability is its freshness signal. The floor fallback still
        # has the planner's existing age limit; do not widen it to fit the Salus heartbeat.
        if not any(value is not None for value in feels) and therm_usable:
            air, source = therm_temp, "thermostat"
        else:
            air, source = hp.resolve_control_air(feels, None, floor_pair)
        return {
            "online": online,
            "hvac_mode": state,
            "preset": attrs.get("preset_mode"),
            "setpoint": _num(attrs.get("temperature")),
            "hvac_action": attrs.get("hvac_action"),
            "therm_temp": therm_temp,
            "therm_state_age": therm_state_age,
            "therm_state_updated_ts": snap["ts"] if snap else None,
            "therm_usable": therm_usable,
            "floor_temp": floor_temp,
            "air": air,
            "air_source": source,
            "air_fresh": source in ("feel", "thermostat")
            or (source == "floor" and floor_age is not None and floor_age <= FLOOR_FRESH_MIN),
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
        if rt.mode_seen is None and reading["online"]:
            rt.mode_seen = reading["hvac_mode"]
        if rt.preset_seen is None and isinstance(reading["preset"], str):
            rt.preset_seen = reading["preset"]
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
            day_duty=(
                tuple((b.day.isoformat(), b.u_eq) for b in budgets if b.u_eq is not None)
                if (zc.weather_offset.enabled and ctx["forecast_ok"])
                else ()
            ),
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
            ctx["obs"].append(
                al.ZoneObs(
                    zone=zone,
                    dt_s=max(gap, 0.0),
                    season_on=ctx["season_on"],
                    hvac_mode=reading["hvac_mode"],
                    calling=reading["hvac_action"] == "heating",
                    air=reading["air"],
                    air_fresh=reading["air_fresh"],
                    target=plan.target,
                    plan_mode=plan.plan_mode,
                    fallback=reading["air_source"] == "none",
                    floor=reading["floor_temp"],
                    window_active=rt.tracker.state != hp.STATE_CLOSED
                    or bool(rt.tracker.open_openings()),
                    evenness=self._evenness(zc, now_ts, window_active),
                )
            )
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
        aired = rt.tracker.state != hp.STATE_CLOSED or bool(rt.tracker.open_openings())
        ctx["zones"][zone] = {"air": reading["air"], "target": plan.target, "window_active": aired}

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
        if (
            plan.mode == hp.MODE_HOLD
            and reading["air_source"] == "feel"
            and reading["therm_temp"] is not None
            and reading["therm_usable"]
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

    # ---------------------------------------------------------------- alerts

    def _evenness(self, zc: hp.ZoneConfig, now_ts: float, window_active: bool) -> Optional[float]:
        """The compared floor's temperature minus the reference floor's, for a zone that
        watches the evenness of its floors. A floor sensor reports on change only, so the age
        of its state says nothing about whether it is alive."""
        if len(zc.evenness_floors) != 2:
            return None
        values = []
        for entity in zc.evenness_floors:
            snap = self._snap(entity)
            if snap is None:
                return None
            value = self._guard.filter(entity, snap["state"], now_ts, window_active)
            if value is None:
                return None
            values.append(value)
        return values[1] - values[0]

    def _alerts_tick(self, ctx: dict, outdoor, relay_w, supply) -> None:
        cfg = self.cfg
        now_ts, tz = ctx["now_ts"], ctx["tz"]
        local = hp.local_dt(now_ts, tz)
        if self._forecast is None:
            forecast_stale = now_ts - self._started_ts >= cfg.forecast_stale_hours * 3600.0
        else:
            forecast_stale = not ctx["forecast_ok"]
        monitor = cfg.living_monitor_entity
        house = al.HouseObs(
            now_ts=now_ts,
            day=local.date().isoformat(),
            hour=local.hour,
            season_on=ctx["season_on"],
            relay_w=relay_w,
            outdoor=outdoor,
            supply=supply,
            supply_enabled=bool(cfg.supply_temp_entity),
            forecast_stale=forecast_stale,
            monitor_heat=bool(monitor) and self._state_of(monitor) == "heat",
        )
        for event in self._alerts.update(house, ctx["obs"]):
            self._append_log(
                ctx["now"],
                {
                    "type": "alert",
                    "ts": self._iso(now_ts, tz),
                    "key": event.key,
                    "zone": event.zone,
                    "event": event.kind,
                    "message": event.message,
                },
            )
            self.log(f"HeatingController alert {event.key}: {event.kind}", level="INFO")
            if event.kind == al.EVENT_START:
                self._push(event.title, event.message)

    # ---------------------------------------------------------------- season advice

    def _season_tick(self, ctx: dict) -> None:
        """Hand the advisor the air of the zones it watches, and judge the day once the time has
        come. It only has the periodic ticks, so a reading is never weighed twice, and it runs
        after the rollup, which closes the days it reads."""
        advisor, cfg = self._advisor, self.cfg.season_advisor
        if advisor is None or not ctx["periodic"] or not ctx["season_known"]:
            return
        now_ts, tz = ctx["now_ts"], ctx["tz"]
        local = hp.local_dt(now_ts, tz)
        day, minute = local.date().isoformat(), local.hour * 60 + local.minute
        advisor.note_season(ctx["season_on"], day)
        airs = {zone: self._season_air(cfg.rooms[zone], now_ts) for zone in cfg.zones}
        for zone, air in airs.items():
            view = ctx["zones"].get(zone)
            if view and not view["window_active"]:
                advisor.observe(zone, day, minute, air)
        if not advisor.due(day, minute):
            return
        verdict = advisor.evaluate(self._season_inputs(ctx, day, airs))
        if verdict.advice == ss.ADVICE_NONE:
            return
        self._append_log(
            ctx["now"], {"type": "season_advice", "ts": self._iso(now_ts, tz), **verdict.to_dict()}
        )
        self.log(
            f"HeatingController season advice: {verdict.advice}, pushed {verdict.push}",
            level="INFO",
        )
        if verdict.push:
            self._push(verdict.title, verdict.message)

    def _season_air(self, entities: tuple, now_ts: float) -> Optional[float]:
        """The mean air of the room sensors that read. One flagged stale or unreadable is left
        out, and when none reads there is no air."""
        values = []
        for entity in entities:
            snap = self._snap(entity)
            if snap is None or snap["attributes"].get("stale") == "true":
                continue
            value = self._guard.filter(entity, snap["state"], now_ts)
            if value is not None:
                values.append(value)
        return sum(values) / len(values) if values else None

    def _season_inputs(self, ctx: dict, day: str, airs: dict) -> ss.Inputs:
        fc, mean = self._forecast, None
        if ctx["forecast_ok"]:
            mean = hf.mean_ahead(fc, fc.temperature, ctx["now_ts"], ss.OUTLOOK_HOURS)
        bases = {}
        for zone in self.cfg.season_advisor.zones:
            rt = self._zones[zone]
            bases[zone] = rt.zc.base_c if rt.last_base is None else rt.last_base
        return ss.Inputs(
            day=day,
            season_on=ctx["season_on"],
            forecast_mean=mean,
            bases=bases,
            airs=airs,
            daily={zone: rt.daily for zone, rt in self._zones.items()},
        )

    def _season_attrs(self) -> dict:
        """The advisor's latest judgement, for sensor.heating_controller."""
        last = self._advisor.last or {}
        means = {
            zone: [_text(_num(m)) or "n/a" for m in values]
            for zone, values in _dict(last.get("mornings")).items()
            if isinstance(values, list) and values
        }
        return {
            "season_advice": last.get("advice") or ss.ADVICE_NONE,
            "season_advice_day": last.get("day"),
            "season_advice_reason": last.get("reason"),
            "season_forecast_mean": _text(_num(last.get("forecast_mean"))),
            "season_morning_means": means or None,
        }

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
            "signals": self._signals,
            "alerts": self._alerts.snapshot(),
            "season_on": self._season_prev,
        }
        if self._advisor is not None:
            data["season_advisor"] = self._advisor.snapshot()
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
            "weather_offset": plan.weather_offset,
            "limits": [lower, upper],
            "thermostat": {
                "online": reading["online"],
                "mode": reading["hvac_mode"],
                "temp": reading["therm_temp"],
                "state_age_min": reading["therm_state_age"],
                "freshness_source": "availability",
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
            record = {
                "type": "daily",
                "day": day,
                "zone": "_house",
                "ts": self._iso(now.timestamp(), now.tzinfo),
                "ticks": stats.get("ticks", 0),
                "forecast_fetched": stats.get("forecast_fetched", 0),
                "forecast_failed": stats.get("forecast_failed", 0),
                "outdoor": self._range(stats.get("outdoor")),
                "relay_w_mean": self._range(stats.get("relay_w"), mean_only=True),
            }
            for clamp in ("supply", "return"):
                if stats.get(clamp):
                    record[clamp] = self._range(stats[clamp])
            self._append_log(now, record)
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
            "weather_offset": _signed(plan.weather_offset, 2),
            "profile_offset": _signed(plan.target - base - rt.nudge),
            "lower_limit": lower,
            "upper_limit": upper,
            "protected_min": plan.protected_min,
            "control_air": None if reading["air"] is None else round(reading["air"], 1),
            "control_air_source": reading["air_source"],
            "thermostat_temp": reading["therm_temp"],
            "thermostat_state_updated_at": (
                iso(reading["therm_state_updated_ts"])
                if reading["therm_state_updated_ts"] is not None else None
            ),
            "thermostat_freshness_source": "availability",
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
        if rt.last_signal:
            sg = rt.last_signal
            attrs["last_signal"] = {
                "at": iso(sg["at"]),
                "who": sg.get("who") or "unknown",
                "source": sg.get("source"),
                "direction": sig.DIRECTION_WORDS.get(sg.get("direction")),
                "effect": sg.get("effect"),
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
            "alerts_active": self._alerts.active(),
            "outdoor_source": outdoor[1],
            "outdoor_now": _text(outdoor[0]),
            "would_writes_today": str(writes),
            "sun_term_factor": self.cfg.sun_term_factor,
            "last_tick": self._iso(now_ts, tz),
        }
        if self._advisor is not None:
            attrs.update(self._season_attrs())
        self._publish(
            "sensor.heating_controller", "shadow" if ctx["season_on"] else "off_season", attrs
        )
