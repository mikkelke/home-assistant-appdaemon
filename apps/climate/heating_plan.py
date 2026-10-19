"""
Planning logic for the floor-heating controller. Pure functions and small state machines, no
AppDaemon import and no I/O, so every rule can be unit-tested on its own.

The thermostats keep running their own ITLC floor-heating algorithm. This module decides what
setpoint the controller would hand them, in two layers:

  - HOLD: the setpoint follows the comfort profile, shifted EARLIER by the slab lag (the floor
    answers hours late), plus `trim`, the learned offset between the thermostat's own reading
    and the seated-air reading the targets are defined in.
  - COAST: the setpoint drops to a protected minimum (sun cut) or to the zone's lower limit
    (open window, party, bedroom loop).

Terms used throughout:
  - lower limit / upper limit: the setpoint bounds of a zone, in the thermostat's own scale.
  - target (T*): the comfort target in seated-air terms, base + nudge + profile offset.
  - aim: the air temperature the setpoint is meant to produce (T* shifted by the lead, or capped).
  - protected minimum: the air temperature a sun cut still defends.

Mode precedence, first match wins (see plan()): off_season / device_offline, window, settle,
party, guard, sun_cut, fallback, hold. The plan is always computed, also when the first row
gates the zone, so the log shows what would have been planned. `guard` is the bedroom loop
holding the bathroom back (BedroomLoop).

Timestamps are epoch seconds. Local clock times come from the tzinfo passed in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime
from typing import Optional

import heating_forecast as hf

MODE_OFF_SEASON = "off_season"
MODE_DEVICE_OFFLINE = "device_offline"
MODE_WINDOW = "window"
MODE_SETTLE = "settle"
MODE_PARTY = "party"
MODE_GUARD = "guard"
MODE_SUN_CUT = "sun_cut"
MODE_FALLBACK = "fallback"
MODE_HOLD = "hold"

SETPOINT_STEP_C = 0.5
QUANTIZE_HOLD_C = 0.4
TRIM_HOURS = range(7, 23)
TRIM_MIN_TICKS_PER_HOUR = 6
TRIM_MIN_HOURS = 6
TRIM_GAIN = 0.5
TRIM_STEP_C = 0.05
CONTROL_AIR_MAX_AGE_MIN = 180.0
VALID_RANGE_C = (5.0, 35.0)
JUMP_C = 2.0
JUMP_WINDOW_S = 600.0
MIN_VALID_EPOCH = 365 * 86400.0
PLANNED_PARTY_MAX_S = 24 * 3600.0
SEASON_MORNING_FROM = 5 * 60
SEASON_MORNING_TO = 9 * 60


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------- time helpers


def local_dt(ts: float, tz) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def local_midnight_ts(day: date, tz) -> float:
    """Epoch of 00:00 local time on `day`. Works for zoneinfo and pytz tzinfos."""
    naive = datetime.combine(day, dtime(0, 0))
    localize = getattr(tz, "localize", None)
    aware = localize(naive) if localize else naive.replace(tzinfo=tz)
    return aware.timestamp()


def clock_minute(ts: float, tz) -> int:
    dt = local_dt(ts, tz)
    return dt.hour * 60 + dt.minute


def hhmm(ts: float, tz) -> str:
    return local_dt(ts, tz).strftime("%H:%M")


def parse_clock(text: object) -> int:
    """'HH:MM' to minutes since midnight."""
    if not isinstance(text, str) or ":" not in text:
        raise ConfigError(f"clock time must be a quoted 'HH:MM' string, got {text!r}")
    try:
        h, m = (int(x) for x in text.split(":")[:2])
    except ValueError:
        raise ConfigError(f"bad clock time {text!r}") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ConfigError(f"bad clock time {text!r}")
    return h * 60 + m


def parse_month_day(text: object) -> tuple:
    """'MM-DD' to (month, day)."""
    if not isinstance(text, str) or text.count("-") != 1:
        raise ConfigError(f"date must be a quoted 'MM-DD' string, got {text!r}")
    try:
        month, day = (int(x) for x in text.split("-"))
        date(2000, month, day)
    except ValueError:
        raise ConfigError(f"bad date {text!r}") from None
    return month, day


def in_span(minute: int, start: int, end: int) -> bool:
    """Whether a clock minute is inside [start, end), the span wrapping past midnight when
    start > end."""
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


# ---------------------------------------------------------------- configuration


@dataclass(frozen=True)
class Period:
    start: int
    end: int
    offset: float


@dataclass(frozen=True)
class Profile:
    lead_hours: float
    periods: tuple = ()
    default_offset: float = 0.0
    outside_is_lower_limit: bool = False


@dataclass(frozen=True)
class TrimConfig:
    initial: float = 0.0
    minimum: float = -1.0
    maximum: float = 1.0


@dataclass(frozen=True)
class BudgetConfig:
    """Expected call-hours per day. Either a linear model on degree-days, sun and wind
    (a, b_om, c, d), or a change-point model (k hours per K below base_c)."""

    a: float = 0.0
    b_om: float = 0.0
    c: float = 0.0
    d: float = 0.0
    changepoint_k: Optional[float] = None
    changepoint_base_c: float = 15.5


@dataclass(frozen=True)
class SunCutConfig:
    enabled: bool = False
    az_min: float = 100.0
    az_max: float = 260.0
    ghi_min: float = 150.0
    before_hours: float = 8.0
    end_before_hours: float = 1.0
    credit_ratio: float = 0.5
    end_ratio: float = 0.35
    min_skipped_h: float = 0.5


@dataclass(frozen=True)
class GuardConfig:
    """Whether the bedroom loop may hold this zone back, and down to what."""

    enabled: bool = False
    cap: str = "lower_limit"


@dataclass(frozen=True)
class NudgeConfig:
    minimum: float = 0.0
    maximum: float = 0.0
    step: float = 0.5


@dataclass(frozen=True)
class ZoneConfig:
    name: str
    thermostat: str
    control_air: tuple
    floor_sensor: str
    openings: tuple
    target_entity: str
    base_c: float
    lower_c: float
    upper_c: float
    protected_min_c: Optional[float]
    protected_min_offset_c: Optional[float]
    profile: Profile
    trim: TrimConfig
    budget: Optional[BudgetConfig]
    sun_cut: SunCutConfig
    guard: GuardConfig
    residents: tuple
    nudge: NudgeConfig
    buttons: dict
    evenness_floors: tuple = ()  # (reference, compared): the alert watches compared minus reference


@dataclass(frozen=True)
class WindowConfig:
    open_debounce_minutes: float = 2.0
    long_open_minutes: float = 30.0
    settle_max_minutes: float = 60.0
    settle_recovered_c: float = 0.3
    unavailable_closed_minutes: float = 10.0


@dataclass(frozen=True)
class PartyConfig:
    zone: str
    lead_hours: float = 8.0
    start_entity: str = ""
    end_entity: str = ""
    switch_entity: str = ""
    switch_max_hours: float = 12.0
    default_hours: float = 6.0
    airing_over_target_c: float = 1.0
    airing_band_entity: str = ""
    airing_bands: tuple = ("stuffy", "poor")
    airing_repeat_minutes: float = 45.0


@dataclass(frozen=True)
class BedroomConfig:
    """The sleeper's ceiling on the bedroom's air, and the evening window the loop reads.

    The ceiling is the helper value from evening_from until morning_from, and
    morning_offset_c higher from then until wake."""

    air_entity: str = "sensor.bedroom_feel"
    ceiling_entity: str = ""
    ceiling_default_c: float = 21.0
    morning_offset_c: float = 0.5
    evening_from: int = 21 * 60
    evening_to: int = 23 * 60 + 30
    morning_from: int = 3 * 60
    wake: int = 6 * 60
    release_below_c: float = 20.5
    min_samples: int = 6


@dataclass(frozen=True)
class AlertConfig:
    """Thresholds of the plant alerts, see heating_alerts."""

    cold_c: float = 19.0
    cold_minutes: float = 30.0
    calling_hours: float = 4.0
    no_pump_w: float = 5.0
    floor_rise_min_c: float = 0.1
    evenness_c: float = 1.5
    evenness_nights: int = 3
    no_hot_water_c: float = 28.0
    repeat_hours: float = 6.0


@dataclass(frozen=True)
class SeasonAdvisorConfig:
    """Rules of the season advisor, see heating_season. zones are the advised zones in the order
    the config lists them, rooms the room sensors each one is judged on, and margins the zones
    whose margin differs from margin_c. evaluate_at is a clock time in minutes since midnight,
    and the morning it judges is SEASON_MORNING_FROM to SEASON_MORNING_TO. stop_from and
    stop_until are the (month, day) between which the stop advice is considered."""

    zones: tuple
    rooms: dict = field(default_factory=dict)
    margins: dict = field(default_factory=dict)
    evaluate_at: int = 8 * 60
    margin_c: float = 0.5
    mornings: int = 2
    start_below_c: float = 12.0
    stop_from: tuple = (3, 1)
    stop_until: tuple = (9, 30)
    look_back_days: int = 7
    idle_days: int = 5
    idle_hours: float = 2.0
    stop_min_c: float = 5.0
    season_min_days: int = 7
    reask_days: int = 3

    def margin(self, zone: str) -> float:
        return self.margins.get(zone, self.margin_c)


@dataclass(frozen=True)
class Config:
    season_entity: str
    state_file: str
    log_dir: str
    notify_target: str
    forecast_refresh_minutes: float
    forecast_stale_hours: float
    sun_term_factor: float
    writes_per_hour: float
    write_stable_minutes: float
    keypad_relock_minutes: float
    outdoor_entities: tuple
    relay_power_entity: str
    bedroom: BedroomConfig
    party: Optional[PartyConfig]
    window: WindowConfig
    zones: dict = field(default_factory=dict)
    owner: str = "mikkel"
    appdaemon_user_id: str = ""
    user_ids: dict = field(default_factory=dict)
    supply_temp_entity: str = ""
    return_temp_entity: str = ""
    living_monitor_entity: str = ""
    alerts: AlertConfig = field(default_factory=AlertConfig)
    season_advisor: Optional[SeasonAdvisorConfig] = None


def _req(raw: dict, key: str, where: str):
    if raw.get(key) in (None, ""):
        raise ConfigError(f"{where}: missing '{key}'")
    return raw[key]


def _as_tuple(value) -> tuple:
    if value in (None, ""):
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _parse_profile(raw, where: str) -> Profile:
    raw = raw or {}
    periods = []
    for p in raw.get("periods") or []:
        start, end = parse_clock(p.get("from")), parse_clock(p.get("to"))
        if start == end:
            raise ConfigError(f"{where}: profile period {p!r} is empty")
        periods.append(Period(start, end, float(p.get("offset", 0.0))))
    outside = raw.get("outside")
    if outside not in (None, "lower_limit"):
        raise ConfigError(f"{where}: profile outside must be 'lower_limit'")
    return Profile(
        lead_hours=float(raw.get("lead_hours", 0.0)),
        periods=tuple(periods),
        default_offset=float(raw.get("default_offset", 0.0)),
        outside_is_lower_limit=outside == "lower_limit",
    )


def _parse_budget(raw) -> Optional[BudgetConfig]:
    if not raw:
        return None
    cp = raw.get("changepoint")
    if cp:
        return BudgetConfig(
            changepoint_k=float(cp["k"]), changepoint_base_c=float(cp.get("base_c", 15.5))
        )
    return BudgetConfig(
        a=float(raw.get("a", 0.0)),
        b_om=float(raw.get("b_om", 0.0)),
        c=float(raw.get("c", 0.0)),
        d=float(raw.get("d", 0.0)),
    )


def _parse_sun_cut(raw, where: str) -> SunCutConfig:
    raw = raw or {}
    if not raw.get("enabled", False):
        return SunCutConfig(enabled=False)
    az = raw.get("azimuth") or [100, 260]
    if len(az) != 2 or not az[0] < az[1]:
        raise ConfigError(f"{where}: sun_cut azimuth must be [min, max]")
    return SunCutConfig(
        enabled=True,
        az_min=float(az[0]),
        az_max=float(az[1]),
        ghi_min=float(raw.get("ghi_min", 150)),
        before_hours=float(raw.get("before_hours", 8)),
        end_before_hours=float(raw.get("end_before_hours", 1)),
        credit_ratio=float(raw.get("credit_ratio", 0.5)),
        end_ratio=float(raw.get("end_ratio", 0.35)),
        min_skipped_h=float(raw.get("min_skipped_h", 0.5)),
    )


def _parse_guard(raw, where: str) -> GuardConfig:
    raw = raw or {}
    if not raw.get("enabled", False):
        return GuardConfig(enabled=False)
    cap = raw.get("cap", "lower_limit")
    if cap not in ("protected_min", "lower_limit"):
        raise ConfigError(f"{where}: bedroom_guard cap must be protected_min or lower_limit")
    return GuardConfig(enabled=True, cap=cap)


def _parse_bedroom(raw: dict) -> BedroomConfig:
    cfg = BedroomConfig(
        air_entity=raw.get("air_entity", "sensor.bedroom_feel"),
        ceiling_entity=raw.get("ceiling_entity", ""),
        ceiling_default_c=float(raw.get("ceiling_default_c", 21.0)),
        morning_offset_c=float(raw.get("morning_offset_c", 0.5)),
        evening_from=parse_clock(raw.get("evening_from", "21:00")),
        evening_to=parse_clock(raw.get("evening_to", "23:30")),
        morning_from=parse_clock(raw.get("morning_from", "03:00")),
        wake=parse_clock(raw.get("wake", "06:00")),
        release_below_c=float(raw.get("release_below_c", 20.5)),
        min_samples=int(raw.get("min_samples", 6)),
    )
    if not cfg.evening_from < cfg.evening_to:
        raise ConfigError("bedroom: the evening window must lie inside one day")
    return cfg


def _parse_zone(name: str, raw: dict) -> ZoneConfig:
    where = f"zone {name}"
    lower, upper = float(_req(raw, "lower_limit_c", where)), float(
        _req(raw, "upper_limit_c", where)
    )
    if not lower < upper:
        raise ConfigError(f"{where}: lower limit must be below the upper limit")
    pm_abs, pm_off = raw.get("protected_min_c"), raw.get("protected_min_offset_c")
    if pm_abs is not None and pm_off is not None:
        raise ConfigError(f"{where}: give protected_min_c or protected_min_offset_c, not both")
    control_air = _as_tuple(raw.get("control_air"))
    if not control_air:
        raise ConfigError(f"{where}: control_air needs at least one sensor")
    trim = raw.get("trim") or {}
    nudge = raw.get("nudge") or {}
    buttons = dict(raw.get("buttons") or {})
    if set(buttons) - {"warmer", "colder"}:
        raise ConfigError(f"{where}: buttons are 'warmer' and 'colder'")
    evenness = _as_tuple(raw.get("evenness_floors"))
    if evenness and len(evenness) != 2:
        raise ConfigError(f"{where}: evenness_floors needs [reference, compared]")
    return ZoneConfig(
        name=name,
        thermostat=_req(raw, "thermostat", where),
        control_air=control_air,
        floor_sensor=raw.get("floor") or "",
        openings=_as_tuple(raw.get("openings")),
        target_entity=_req(raw, "target_entity", where),
        base_c=float(_req(raw, "base_c", where)),
        lower_c=lower,
        upper_c=upper,
        protected_min_c=None if pm_abs is None else float(pm_abs),
        protected_min_offset_c=None if pm_off is None else float(pm_off),
        profile=_parse_profile(raw.get("profile"), where),
        trim=TrimConfig(
            initial=float(trim.get("initial", 0.0)),
            minimum=float(trim.get("min", -1.0)),
            maximum=float(trim.get("max", 1.0)),
        ),
        budget=_parse_budget(raw.get("budget")),
        sun_cut=_parse_sun_cut(raw.get("sun_cut"), where),
        guard=_parse_guard(raw.get("bedroom_guard"), where),
        residents=_as_tuple(raw.get("residents")),
        nudge=NudgeConfig(
            minimum=float(nudge.get("min", 0.0)),
            maximum=float(nudge.get("max", 0.0)),
            step=float(nudge.get("step", SETPOINT_STEP_C)),
        ),
        buttons=buttons,
        evenness_floors=evenness,
    )


def _parse_alerts(raw: dict) -> AlertConfig:
    d = AlertConfig()
    cfg = AlertConfig(
        cold_c=float(raw.get("cold_c", d.cold_c)),
        cold_minutes=float(raw.get("cold_minutes", d.cold_minutes)),
        calling_hours=float(raw.get("calling_hours", d.calling_hours)),
        no_pump_w=float(raw.get("no_pump_w", d.no_pump_w)),
        floor_rise_min_c=float(raw.get("floor_rise_min_c", d.floor_rise_min_c)),
        evenness_c=float(raw.get("evenness_c", d.evenness_c)),
        evenness_nights=int(raw.get("evenness_nights", d.evenness_nights)),
        no_hot_water_c=float(raw.get("no_hot_water_c", d.no_hot_water_c)),
        repeat_hours=float(raw.get("repeat_hours", d.repeat_hours)),
    )
    if cfg.calling_hours <= 0 or cfg.cold_minutes < 0 or cfg.repeat_hours < 0:
        raise ConfigError("alerts: calling_hours must be positive, the others not negative")
    if cfg.evenness_nights < 1:
        raise ConfigError("alerts: evenness_nights must be at least 1")
    return cfg


def _parse_season_advisor(raw, zones: dict) -> Optional[SeasonAdvisorConfig]:
    if not raw:
        return None
    zones_raw = raw.get("zones")
    if not isinstance(zones_raw, dict) or not zones_raw:
        raise ConfigError("season_advisor: zones maps each advised zone to its rooms")
    rooms, margins = {}, {}
    for name, z in zones_raw.items():
        if name not in zones:
            raise ConfigError(f"season_advisor: unknown zone {name}")
        if not isinstance(z, dict):
            raise ConfigError(f"season_advisor: zone {name} needs rooms")
        sensors = tuple(dict.fromkeys(_as_tuple(z.get("rooms"))))
        if not sensors or not all(isinstance(e, str) and e for e in sensors):
            raise ConfigError(f"season_advisor: zone {name} needs rooms, a list of sensors")
        rooms[name] = sensors
        if z.get("margin_c") is not None:
            margins[name] = float(z["margin_c"])
    d = SeasonAdvisorConfig(zones=())
    cfg = SeasonAdvisorConfig(
        zones=tuple(zones_raw),
        rooms=rooms,
        margins=margins,
        evaluate_at=parse_clock(raw.get("evaluate_at", "08:00")),
        margin_c=float(raw.get("margin_c", d.margin_c)),
        mornings=int(raw.get("mornings", d.mornings)),
        start_below_c=float(raw.get("start_below_c", d.start_below_c)),
        stop_from=parse_month_day(raw.get("stop_from", "03-01")),
        stop_until=parse_month_day(raw.get("stop_until", "09-30")),
        look_back_days=int(raw.get("look_back_days", d.look_back_days)),
        idle_days=int(raw.get("idle_days", d.idle_days)),
        idle_hours=float(raw.get("idle_hours", d.idle_hours)),
        stop_min_c=float(raw.get("stop_min_c", d.stop_min_c)),
        season_min_days=int(raw.get("season_min_days", d.season_min_days)),
        reask_days=int(raw.get("reask_days", d.reask_days)),
    )
    if cfg.evaluate_at < SEASON_MORNING_FROM + 60:
        raise ConfigError("season_advisor: evaluate_at must be an hour into the morning or later")
    if min(cfg.margin_c, *margins.values(), cfg.season_min_days) < 0 or cfg.idle_hours <= 0:
        raise ConfigError(
            "season_advisor: margins and season_min_days must not be negative, idle_hours positive"
        )
    if min(cfg.mornings, cfg.look_back_days, cfg.idle_days, cfg.reask_days) < 1:
        raise ConfigError(
            "season_advisor: mornings, look_back_days, idle_days and reask_days must be at least 1"
        )
    if cfg.idle_days > cfg.look_back_days:
        raise ConfigError("season_advisor: idle_days must fit in look_back_days")
    if cfg.stop_from > cfg.stop_until:
        raise ConfigError("season_advisor: stop_from must not be after stop_until")
    return cfg


def parse_config(args: dict) -> Config:
    """Controller arguments (the parsed yaml) to a validated Config. Raises ConfigError."""
    zones_raw = args.get("zones") or {}
    if not zones_raw:
        raise ConfigError("no zones configured")
    zones = {name: _parse_zone(name, raw or {}) for name, raw in zones_raw.items()}

    forecast = args.get("forecast") or {}
    window = args.get("window") or {}
    bedroom = args.get("bedroom") or {}
    party_raw = args.get("party")
    party = None
    if party_raw:
        zone = _req(party_raw, "zone", "party")
        if zone not in zones:
            raise ConfigError(f"party: unknown zone {zone}")
        party = PartyConfig(
            zone=zone,
            lead_hours=float(party_raw.get("lead_hours", 8)),
            start_entity=party_raw.get("start_entity", ""),
            end_entity=party_raw.get("end_entity", ""),
            switch_entity=party_raw.get("switch_entity", ""),
            switch_max_hours=float(party_raw.get("switch_max_hours", 12)),
            default_hours=float(party_raw.get("default_hours", 6)),
            airing_over_target_c=float(party_raw.get("airing_over_target_c", 1.0)),
            airing_band_entity=party_raw.get("airing_band_entity", ""),
            airing_bands=tuple(party_raw.get("airing_bands") or ("stuffy", "poor")),
            airing_repeat_minutes=float(party_raw.get("airing_repeat_minutes", 45)),
        )
    for zc in zones.values():
        if zc.guard.enabled and not bedroom.get("ceiling_entity"):
            raise ConfigError(f"zone {zc.name}: bedroom_guard needs bedroom.ceiling_entity")

    return Config(
        season_entity=_req(args, "season_entity", "config"),
        state_file=args.get("state_file", "/conf/apps/climate/heating_controller_state.json"),
        log_dir=args.get("log_dir", "/conf/heating_log"),
        notify_target=args.get("notify_target", "mikkel"),
        forecast_refresh_minutes=float(forecast.get("refresh_minutes", 60)),
        forecast_stale_hours=float(forecast.get("stale_hours", 12)),
        sun_term_factor=float(args.get("sun_term_factor", 0.5)),
        writes_per_hour=float(args.get("writes_per_hour", 2)),
        write_stable_minutes=float(args.get("write_stable_minutes", 10)),
        keypad_relock_minutes=float(args.get("keypad_relock_minutes", 15)),
        outdoor_entities=_as_tuple(args.get("outdoor_entities")),
        relay_power_entity=args.get("relay_power_entity", ""),
        bedroom=_parse_bedroom(bedroom),
        party=party,
        window=WindowConfig(
            open_debounce_minutes=float(window.get("open_debounce_minutes", 2)),
            long_open_minutes=float(window.get("long_open_minutes", 30)),
            settle_max_minutes=float(window.get("settle_max_minutes", 60)),
            settle_recovered_c=float(window.get("settle_recovered_c", 0.3)),
            unavailable_closed_minutes=float(window.get("unavailable_closed_minutes", 10)),
        ),
        zones=zones,
        owner=str(args.get("owner", "mikkel")).lower(),
        appdaemon_user_id=str(args.get("appdaemon_user_id") or ""),
        user_ids={str(k): str(v).lower() for k, v in (args.get("user_ids") or {}).items()},
        supply_temp_entity=args.get("supply_temp_entity") or "",
        return_temp_entity=args.get("return_temp_entity") or "",
        living_monitor_entity=(args.get("unmanaged") or {}).get("living_monitor") or "",
        alerts=_parse_alerts(args.get("alerts") or {}),
        season_advisor=_parse_season_advisor(args.get("season_advisor"), zones),
    )


# ---------------------------------------------------------------- targets


def comfort_target(
    zc: ZoneConfig, base: float, nudge: float, ts: float, tz, lower_c: float
) -> float:
    """T*(ts): the comfort target in seated-air terms at the local clock time of ts."""
    minute = clock_minute(ts, tz)
    for p in zc.profile.periods:
        if in_span(minute, p.start, p.end):
            return base + nudge + p.offset
    if zc.profile.outside_is_lower_limit:
        return lower_c
    return base + nudge + zc.profile.default_offset


def protected_min(zc: ZoneConfig, base: float, nudge: float, lower_c: float) -> float:
    if zc.protected_min_c is not None:
        value = zc.protected_min_c
    elif zc.protected_min_offset_c is not None:
        value = base + nudge + zc.protected_min_offset_c
    else:
        value = lower_c
    return max(value, lower_c)


def quantize(raw: float, last: Optional[float], step: float = SETPOINT_STEP_C) -> float:
    """Round to the device step, but keep `last` while the new value is within
    QUANTIZE_HOLD_C of it, so a value hovering between two steps does not flap."""
    if last is not None and round(abs(raw - last), 6) < QUANTIZE_HOLD_C:
        return last
    return round(math.floor(raw / step + 0.5) * step, 2)


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


# ---------------------------------------------------------------- budget and sun cut


@dataclass(frozen=True)
class DayBudget:
    day: date
    t_mean: float
    hdd: float
    wind_mean: float
    ghi_kwh: float
    h_dull: Optional[float] = None  # expected call-hours with no sun credit
    u_eq: Optional[float] = None  # expected duty, h_dull / 24 limited to [0, 1]
    s_sun: Optional[float] = None  # call-hours the forecast sun would save
    s_credit: Optional[float] = None  # the part of s_sun the cut decision trusts
    h_pred: Optional[float] = None  # expected call-hours, h_dull minus s_sun
    n_sun: int = 0
    sun_start: Optional[float] = None
    sun_end: Optional[float] = None
    window_start: Optional[float] = None
    window_end: Optional[float] = None
    skipped_h: Optional[float] = None  # heat the cut would skip, u_eq times the window length
    ratio: Optional[float] = None  # s_credit over skipped_h


def dull_hours(budget: BudgetConfig, dw: hf.DayWeather) -> float:
    if budget.changepoint_k is not None:
        return budget.changepoint_k * max(0.0, budget.changepoint_base_c - dw.t_mean)
    return max(0.0, budget.a * dw.hdd + budget.c * dw.wind_mean * dw.hdd / 100.0 + budget.d)


def day_budget(
    zc: ZoneConfig, dw: hf.DayWeather, sun_starts: list, sun_term_factor: float
) -> DayBudget:
    """Budget and cut window of one day for one zone.

    The cut window runs from before_hours ahead of the first sun hour to end_before_hours
    before the last one ends. A cut pays off when the sun credit covers at least credit_ratio
    of the heat the window would skip."""
    out = DayBudget(dw.day, dw.t_mean, dw.hdd, dw.wind_mean, dw.ghi_kwh)
    if zc.budget is None:
        return out
    h_dull = dull_hours(zc.budget, dw)
    s_sun = max(0.0, -zc.budget.b_om * dw.ghi_kwh)
    out = replace(
        out,
        h_dull=h_dull,
        u_eq=min(1.0, h_dull / 24.0),
        s_sun=s_sun,
        s_credit=s_sun * sun_term_factor,
        h_pred=max(0.0, h_dull - s_sun),
        n_sun=len(sun_starts),
    )
    cut = zc.sun_cut
    if not cut.enabled or not sun_starts:
        return out
    sun_start, sun_end = sun_starts[0], sun_starts[-1] + hf.HOUR_S
    w_start = sun_start - cut.before_hours * 3600.0
    w_end = sun_end - cut.end_before_hours * 3600.0
    out = replace(out, sun_start=sun_start, sun_end=sun_end)
    if w_end <= w_start:
        return out
    skipped = out.u_eq * (w_end - w_start) / 3600.0
    ratio = out.s_credit / skipped if skipped > 0 else None
    return replace(out, window_start=w_start, window_end=w_end, skipped_h=skipped, ratio=ratio)


@dataclass(frozen=True)
class CutEval:
    windows: tuple = ()  # qualifying (start_ts, end_ts, day iso) cut windows
    active_day: Optional[str] = None  # day whose window contains `now`


def evaluate_cut(
    zc: ZoneConfig, budgets: list, now_ts: float, active_day: Optional[str]
) -> CutEval:
    """The cut windows worth taking, and which of them is running now.

    A window qualifies at credit_ratio. A window that is already running only has to stay
    above end_ratio, so a forecast hovering around one threshold does not end and restart a
    cut."""
    cut = zc.sun_cut
    if not cut.enabled:
        return CutEval()
    windows = []
    for b in budgets:
        if b.window_start is None or b.ratio is None or b.n_sun < 2:
            continue
        if b.skipped_h < cut.min_skipped_h:
            continue
        threshold = cut.end_ratio if b.day.isoformat() == active_day else cut.credit_ratio
        if b.ratio >= threshold:
            windows.append((b.window_start, b.window_end, b.day.isoformat()))
    running = next((w[2] for w in windows if w[0] <= now_ts < w[1]), None)
    return CutEval(windows=tuple(windows), active_day=running)


# ---------------------------------------------------------------- party


@dataclass(frozen=True)
class PartyState:
    pause: bool = False  # the zone is held at its lower limit
    in_event: bool = False  # between start and end, when airing suggestions apply
    source: str = ""
    windows: tuple = ()  # ((pause_start_ts, end_ts), ...) for the plan
    event_end: Optional[float] = None


def valid_epoch(ts: Optional[float]) -> bool:
    """An unset input_datetime reads as 1970-01-01."""
    return ts is not None and ts > MIN_VALID_EPOCH


def party_state(
    now_ts: float,
    pc: PartyConfig,
    planned_start: Optional[float],
    planned_end: Optional[float],
    switch_on: bool,
    switch_since: Optional[float],
) -> PartyState:
    """The planned party [start, end) with the zone paused pc.lead_hours ahead, and the manual
    party switch (stops by itself after switch_max_hours). A missing end, one before the start,
    or one more than a day after it (a helper left at an old value) means start plus
    default_hours. Start and end at the very same moment is how two untouched helpers read, so
    it is no party."""
    windows = []
    sources = []
    if valid_epoch(planned_start) and planned_end != planned_start:
        end = planned_end
        if not valid_epoch(end) or end < planned_start or end - planned_start > PLANNED_PARTY_MAX_S:
            end = planned_start + pc.default_hours * 3600.0
        windows.append((planned_start - pc.lead_hours * 3600.0, planned_start, end, "planned"))
    if switch_on and valid_epoch(switch_since):
        windows.append(
            (switch_since, switch_since, switch_since + pc.switch_max_hours * 3600.0, "manual")
        )
    pause = in_event = False
    event_end = None
    for pause_start, start, end, source in windows:
        if pause_start <= now_ts < end:
            pause = True
            sources.append(source)
            event_end = max(event_end or 0.0, end)
            if start <= now_ts:
                in_event = True
    return PartyState(
        pause=pause,
        in_event=in_event,
        source="+".join(sources),
        windows=tuple((w[0], w[2]) for w in windows if w[2] > now_ts),
        event_end=event_end,
    )


def airing_suggestion(
    pc: PartyConfig,
    now_ts: float,
    state: PartyState,
    control_air: Optional[float],
    target: float,
    band: Optional[str],
    last_push_ts: Optional[float],
) -> Optional[str]:
    """Why to air the living area during a party, or None. At most one suggestion every
    airing_repeat_minutes."""
    if not state.in_event:
        return None
    if last_push_ts is not None and now_ts - last_push_ts < pc.airing_repeat_minutes * 60.0:
        return None
    reasons = []
    if control_air is not None and control_air >= target + pc.airing_over_target_c:
        reasons.append(f"{control_air:.1f} \u00b0C")
    if band in pc.airing_bands:
        reasons.append(f"air {band}")
    if not reasons:
        return None
    return "Living area " + ", ".join(reasons) + ": open the terrace doors for 10 min"


# ---------------------------------------------------------------- bedroom loop


def bedroom_ceiling(cfg: BedroomConfig, minute: int, bedtime_ceiling: float) -> Optional[float]:
    """The ceiling on the bedroom's air at a clock minute, or None outside the sleeping hours."""
    if in_span(minute, cfg.evening_from, cfg.morning_from):
        return bedtime_ceiling
    if in_span(minute, cfg.morning_from, cfg.wake):
        return bedtime_ceiling + cfg.morning_offset_c
    return None


class BedroomLoop:
    """Keeps the bedroom from taking comfort from the bathroom's heat.

    The bathroom shares a wall with the bedroom and its slab answers hours late, so the loop
    is a daily one: once the evening window [evening_from, evening_to) is over, the mean
    bedroom air in it is compared with the bedtime ceiling. Above it the bathroom is held back
    (its setpoint stays at the lower limit) until an evening mean below release_below_c ends
    the hold; in between the previous decision stands.

    Sleep wins: too few readings count as warm. An evening in which the bedroom window was
    open is the sleeper airing on purpose; it says nothing about the heat reaching the room,
    so the previous decision stands."""

    SAMPLE_GAP_S = 240.0

    def __init__(self, cfg: BedroomConfig, saved: Optional[dict] = None):
        self.cfg = cfg
        s = saved or {}
        self.hold = bool(s.get("hold", False))
        self.day = s.get("day", "")
        self.total = float(s.get("total", 0.0))
        self.count = int(s.get("count", 0))
        self.airing = bool(s.get("airing", False))
        self.last_sample_ts = s.get("last_sample_ts")
        self.decided = s.get("decided", "")
        self.last = s.get("last")

    def snapshot(self) -> dict:
        return {
            "hold": self.hold,
            "day": self.day,
            "total": round(self.total, 3),
            "count": self.count,
            "airing": self.airing,
            "last_sample_ts": self.last_sample_ts,
            "decided": self.decided,
            "last": self.last,
        }

    def observe(
        self,
        now_ts: float,
        tz,
        air: Optional[float],
        stale: bool,
        window_open: bool,
        ceiling: float,
    ) -> Optional[dict]:
        """Feed the bedroom's current reading. Returns the decision record when an evening
        has just been closed."""
        cfg = self.cfg
        dt = local_dt(now_ts, tz)
        minute = dt.hour * 60 + dt.minute
        today = dt.date().isoformat()

        decision = None
        if self.day and self.day != self.decided:
            if today > self.day or (today == self.day and minute >= cfg.evening_to):
                decision = self._decide(ceiling)

        if cfg.evening_from <= minute < cfg.evening_to:
            if self.day != today:
                self.day, self.total, self.count = today, 0.0, 0
                self.airing, self.last_sample_ts = False, None
            if window_open:
                self.airing = True
            elif (
                air is not None
                and not stale
                and (
                    self.last_sample_ts is None or now_ts - self.last_sample_ts >= self.SAMPLE_GAP_S
                )
            ):
                self.total += air
                self.count += 1
                self.last_sample_ts = now_ts
        return decision

    def _decide(self, ceiling: float) -> dict:
        cfg = self.cfg
        mean = self.total / self.count if self.count else None
        if self.airing:
            action = "keep_airing"
        elif self.count < cfg.min_samples:
            action, self.hold = "hold_no_data", True
        elif mean > ceiling:
            action, self.hold = "hold", True
        elif mean < cfg.release_below_c:
            action, self.hold = "release", False
        else:
            action = "keep"
        self.decided = self.day
        self.last = {
            "day": self.day,
            "mean": None if mean is None else round(mean, 2),
            "samples": self.count,
            "airing": self.airing,
            "ceiling": ceiling,
            "action": action,
            "hold": self.hold,
        }
        return self.last


# ---------------------------------------------------------------- reading guards


class ReadingGuard:
    """Rejects temperature readings that cannot be right: outside VALID_RANGE_C, or a jump of
    more than JUMP_C from the last accepted reading within JUMP_WINDOW_S (unless a window is
    open, when fast moves are real). A rejected jump holds the last accepted value."""

    def __init__(self):
        self._last = {}

    def filter(self, key: str, value, now_ts: float, window_open: bool = False) -> Optional[float]:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        if not (VALID_RANGE_C[0] <= v <= VALID_RANGE_C[1]):
            return None
        last = self._last.get(key)
        if (
            last is not None
            and not window_open
            and now_ts - last[0] < JUMP_WINDOW_S
            and abs(v - last[1]) > JUMP_C
        ):
            return last[1]
        self._last[key] = (now_ts, v)
        return v


def resolve_control_air(feels: list, thermostat: Optional[tuple], floor: Optional[tuple]) -> tuple:
    """(air, source) for a zone. The fused room readings first (mean when there are several),
    then the thermostat's own reading, then the floor sensor plus 0.5, each only while younger
    than CONTROL_AIR_MAX_AGE_MIN. `thermostat` and `floor` are (value, age_minutes) or None.
    Source 'none' puts the zone into fallback mode."""
    values = [v for v in feels if v is not None]
    if values:
        return sum(values) / len(values), "feel"
    if thermostat is not None and thermostat[1] <= CONTROL_AIR_MAX_AGE_MIN:
        return thermostat[0], "thermostat"
    if floor is not None and floor[1] <= CONTROL_AIR_MAX_AGE_MIN:
        return floor[0] + 0.5, "floor"
    return None, "none"


# ---------------------------------------------------------------- window tracker


STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_SETTLE = "settle"

EVENT_OPENED = "window_open"
EVENT_SETTLE = "window_closed"
EVENT_RESTORED = "window_restored"


class WindowTracker:
    """Open/closed state of a zone's outward openings.

    CLOSED -> OPEN when an opening has read 'on' for open_debounce_minutes. When everything is
    closed again: after an airing of at most long_open_minutes the zone SETTLEs (stays at its
    lower limit until the air is back within settle_recovered_c of what it was, or
    settle_max_minutes), because a short dip recovers from the room's own mass and heating
    against it would only overshoot hours later. After a longer opening the room has really
    cooled and the plan is restored at once. An opening unavailable for
    unavailable_closed_minutes counts as closed, so a dead contact cannot pin a room."""

    def __init__(self, cfg: WindowConfig, saved: Optional[dict] = None):
        self.cfg = cfg
        saved = saved or {}
        self.state = saved.get("state", STATE_CLOSED)
        if self.state not in (STATE_CLOSED, STATE_OPEN, STATE_SETTLE):
            self.state = STATE_CLOSED
        self.opened_at = saved.get("opened_at")
        self.pre_open_air = saved.get("pre_open_air")
        self.settle_since = saved.get("settle_since")
        self._on_since = {}
        self._unavailable_since = {}

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "opened_at": self.opened_at,
            "pre_open_air": self.pre_open_air,
            "settle_since": self.settle_since,
        }

    def open_openings(self) -> list:
        return sorted(self._on_since)

    def update(self, now_ts: float, openings: dict, air: Optional[float]) -> Optional[str]:
        """Feed the current state of every opening ('on', 'off', 'unavailable', ...). Returns
        an EVENT_* when the zone's state changed."""
        cfg = self.cfg
        for entity, st in openings.items():
            if st == "on":
                self._on_since.setdefault(entity, now_ts)
                self._unavailable_since.pop(entity, None)
            elif st == "off":
                self._on_since.pop(entity, None)
                self._unavailable_since.pop(entity, None)
            else:
                since = self._unavailable_since.setdefault(entity, now_ts)
                if now_ts - since >= cfg.unavailable_closed_minutes * 60.0:
                    self._on_since.pop(entity, None)
        for entity in list(self._on_since):
            if entity not in openings:
                del self._on_since[entity]

        debounce = cfg.open_debounce_minutes * 60.0
        confirmed = [t for t in self._on_since.values() if now_ts - t >= debounce]

        if self.state in (STATE_CLOSED, STATE_SETTLE) and confirmed:
            was_closed = self.state == STATE_CLOSED
            self.state = STATE_OPEN
            self.opened_at = min(confirmed)
            if was_closed or self.pre_open_air is None:
                self.pre_open_air = air
            self.settle_since = None
            return EVENT_OPENED

        if self.state == STATE_OPEN and not self._on_since:
            if now_ts - (self.opened_at or now_ts) <= cfg.long_open_minutes * 60.0:
                self.state = STATE_SETTLE
                self.settle_since = now_ts
                return EVENT_SETTLE
            self._to_closed()
            return EVENT_RESTORED

        if self.state == STATE_SETTLE:
            recovered = (
                air is not None
                and self.pre_open_air is not None
                and air >= self.pre_open_air - cfg.settle_recovered_c
            )
            timed_out = now_ts - (self.settle_since or now_ts) >= cfg.settle_max_minutes * 60.0
            if recovered or timed_out:
                self._to_closed()
                return EVENT_RESTORED
        return None

    def _to_closed(self) -> None:
        self.state = STATE_CLOSED
        self.opened_at = None
        self.pre_open_air = None
        self.settle_since = None


# ---------------------------------------------------------------- planning


@dataclass(frozen=True)
class ZoneInputs:
    season_on: bool
    online: bool
    base: float
    nudge: float
    trim: float
    lower_c: float
    upper_c: float
    fallback: bool = False
    window_state: str = STATE_CLOSED
    party_windows: tuple = ()
    hold_back: bool = False
    cut_windows: tuple = ()
    last_planned: Optional[float] = None


@dataclass(frozen=True)
class Plan:
    mode: str  # what the zone is doing, including the season and device gate
    plan_mode: str  # what the plan says, ignoring that gate
    target: float  # T* now
    aim: Optional[float]  # air temperature the setpoint is meant to produce; None at the limit
    raw: float
    setpoint: float
    protected_min: float
    reasons: tuple = ()


def plan(zc: ZoneConfig, inp: ZoneInputs, now_ts: float, tz) -> Plan:
    """The setpoint for one zone at `now_ts`, with the mode that decided it."""
    lower, upper = inp.lower_c, inp.upper_c
    target = comfort_target(zc, inp.base, inp.nudge, now_ts, tz, lower)
    lead_ts = now_ts + zc.profile.lead_hours * 3600.0
    fallback_aim = lower if zc.profile.outside_is_lower_limit else inp.base + inp.nudge
    if inp.fallback:
        t_lead = fallback_aim
    else:
        t_lead = comfort_target(zc, inp.base, inp.nudge, lead_ts, tz, lower)
    pmin = protected_min(zc, inp.base, inp.nudge, lower)
    in_party = any(s <= now_ts < e for s, e in inp.party_windows)
    in_cut = any(s <= now_ts < e for s, e, _day in inp.cut_windows)
    guard = zc.guard
    guard_now = guard.enabled and inp.hold_back

    aim: Optional[float]
    reasons: list = []
    if inp.window_state == STATE_OPEN:
        plan_mode, aim = MODE_WINDOW, None
        reasons.append("Opening open: held at the lower limit")
    elif inp.window_state == STATE_SETTLE:
        plan_mode, aim = MODE_SETTLE, None
        reasons.append("Settling after airing: held at the lower limit until the air recovers")
    elif in_party:
        plan_mode, aim = MODE_PARTY, None
        end = max(e for s, e in inp.party_windows if s <= now_ts < e)
        reasons.append(f"Party: held at the lower limit until {hhmm(end, tz)}")
    elif guard_now:
        cap = lower if guard.cap == "lower_limit" else pmin
        plan_mode, aim = MODE_GUARD, min(t_lead, cap)
        reasons.append(f"Bedroom too warm at bedtime: aim capped at {cap:.1f}")
    elif zc.sun_cut.enabled and in_cut and not inp.fallback:
        plan_mode, aim = MODE_SUN_CUT, min(t_lead, pmin)
        end = max(e for s, e, _d in inp.cut_windows if s <= now_ts < e)
        reasons.append(f"Sun cut until {hhmm(end, tz)}: aim capped at {pmin:.1f}")
    elif inp.fallback:
        plan_mode, aim = MODE_FALLBACK, fallback_aim
        reasons.append("No usable room temperature: constant target, no profile or cuts")
    else:
        plan_mode, aim = MODE_HOLD, t_lead
        ahead = f"{zc.profile.lead_hours:g} h ahead" if zc.profile.lead_hours else "now"
        reasons.append(f"Aim {t_lead:.1f}: the target {ahead}")

    if aim is None:
        raw = lower
        setpoint = clamp(lower, lower, upper)
    else:
        raw = aim + inp.trim
        setpoint = clamp(quantize(raw, inp.last_planned), lower, upper)
        reasons.append(f"Setpoint {setpoint:.1f} (trim {inp.trim:+.2f})")

    if not inp.season_on:
        mode = MODE_OFF_SEASON
        reasons.insert(0, "Heating season is off: nothing would be written")
    elif not inp.online:
        mode = MODE_DEVICE_OFFLINE
        reasons.insert(0, "Thermostat unavailable: nothing can be written")
    else:
        mode = plan_mode
    return Plan(mode, plan_mode, target, aim, raw, setpoint, pmin, tuple(reasons))


def project_blocks(
    zc: ZoneConfig, inp: ZoneInputs, now_ts: float, until_ts: float, tz, step_s: float = 300.0
) -> list:
    """The plan from now until `until_ts` as (start_ts, end_ts, plan_mode, setpoint) blocks,
    assuming the openings stay closed and the dynamic inputs (bedroom hold-back, fallback)
    keep their current value. The plan is sampled on the clock grid of step_s seconds, so block
    edges fall on round times."""
    blocks = []
    last = inp.last_planned
    t = now_ts
    while t < until_ts:
        end = min((math.floor(t / step_s) + 1) * step_s, until_ts)
        p = plan(zc, replace(inp, window_state=STATE_CLOSED, last_planned=last), t, tz)
        last = p.setpoint
        if blocks and blocks[-1][2] == p.plan_mode and blocks[-1][3] == p.setpoint:
            blocks[-1][1] = end
        else:
            blocks.append([t, end, p.plan_mode, p.setpoint])
        t = end
    return [tuple(b) for b in blocks]


class BlockLog:
    """The day's plan as it actually ran: closed blocks plus the one still open. Kept so the
    dashboard timeline keeps the phases that are over."""

    def __init__(self, saved: Optional[dict] = None):
        saved = saved or {}
        self.day = saved.get("day", "")
        self.closed = [list(b) for b in saved.get("closed", [])]
        self.current = list(saved["current"]) if saved.get("current") else None

    def snapshot(self) -> dict:
        return {"day": self.day, "closed": self.closed, "current": self.current}

    def observe(self, now_ts: float, day: str, mode: str, setpoint: float) -> None:
        if day != self.day:
            self.day, self.closed, self.current = day, [], None
        if self.current is None:
            self.current = [now_ts, mode, setpoint]
        elif self.current[1] != mode or self.current[2] != setpoint:
            self.closed.append([self.current[0], now_ts, self.current[1], self.current[2]])
            self.current = [now_ts, mode, setpoint]


def render_blocks(log: BlockLog, projected: list, now_ts: float, day_end_ts: float, tz) -> list:
    """The day timeline: finished blocks, the running one, and the projection after now."""

    def clock(ts: float) -> str:
        return "24:00" if ts >= day_end_ts else hhmm(ts, tz)

    rows = [[b[0], b[1], b[2], b[3]] for b in log.closed]
    if log.current is not None:
        rows.append([log.current[0], now_ts, log.current[1], log.current[2]])
    for start, end, mode, setpoint in projected:
        if rows and rows[-1][2] == mode and rows[-1][3] == setpoint and rows[-1][1] >= start:
            rows[-1][1] = end
        else:
            rows.append([start, end, mode, setpoint])
    return [
        {"start": clock(r[0]), "end": clock(r[1]), "mode": r[2], "setpoint": f"{r[3]:.1f}"}
        for r in rows
    ]


# ---------------------------------------------------------------- writes (shadow bookkeeping)


class TokenBucket:
    """writes_per_hour tokens, refilled continuously. A new bucket starts full."""

    def __init__(self, per_hour: float = 2.0, saved: Optional[dict] = None):
        self.capacity = float(per_hour)
        self.refill_s = 3600.0 / per_hour
        saved = saved or {}
        self.tokens = float(saved.get("tokens", self.capacity))
        self.ts = saved.get("ts")

    def snapshot(self) -> dict:
        return {"tokens": round(self.tokens, 3), "ts": self.ts}

    def available(self, now_ts: float) -> float:
        if self.ts is not None and now_ts > self.ts:
            self.tokens = min(self.capacity, self.tokens + (now_ts - self.ts) / self.refill_s)
        self.ts = now_ts
        return self.tokens

    def take(self, now_ts: float) -> bool:
        if self.available(now_ts) < 1.0:
            return False
        self.tokens -= 1.0
        return True


@dataclass(frozen=True)
class GateResult:
    write: bool
    reason: str


def write_gate(
    desired: float,
    last: Optional[float],
    now_ts: float,
    desired_since: float,
    priority: Optional[str],
    bucket: TokenBucket,
    stable_s: float,
) -> GateResult:
    """Whether the controller would write `desired` now. A change needs a free token and
    either a priority event or a value that has stayed the same for stable_s."""
    if last is not None and abs(desired - last) < 1e-9:
        return GateResult(False, "unchanged")
    if priority is None and now_ts - desired_since < stable_s:
        return GateResult(False, "settling")
    if not bucket.take(now_ts):
        return GateResult(False, "no_token")
    return GateResult(True, f"priority:{priority}" if priority else "stable")


# ---------------------------------------------------------------- trim


@dataclass(frozen=True)
class TrimUpdate:
    trim: float
    bias: Optional[float]
    valid_hours: int
    changed: bool


def update_trim(trim: float, hours: dict, cfg: TrimConfig) -> TrimUpdate:
    """Move the trim half-way to yesterday's mean (thermostat minus control air). `hours`
    maps a clock hour to [sum, count] of valid samples. Fewer than TRIM_MIN_HOURS hours with
    at least TRIM_MIN_TICKS_PER_HOUR samples each leave the trim as it is."""
    means = [
        s / n
        for h, (s, n) in hours.items()
        if int(h) in TRIM_HOURS and n >= TRIM_MIN_TICKS_PER_HOUR
    ]
    if len(means) < TRIM_MIN_HOURS:
        return TrimUpdate(trim, None, len(means), False)
    bias = sum(means) / len(means)
    new = trim + TRIM_GAIN * (bias - trim)
    new = round(round(clamp(new, cfg.minimum, cfg.maximum) / TRIM_STEP_C) * TRIM_STEP_C, 2)
    return TrimUpdate(new, bias, len(means), new != trim)


# ---------------------------------------------------------------- daily statistics


def _hours(seconds: float) -> float:
    return round(seconds / 3600.0, 2)


@dataclass(frozen=True)
class TickSample:
    ts: float
    hour: int
    dt_s: float
    plan_mode: str
    season_on: bool
    calling: bool
    air: Optional[float]
    target: float
    lower_c: float
    bias: Optional[float]  # thermostat minus control air when the sample is valid for the trim
    fallback: bool


class ZoneDay:
    """One zone's statistics for one local day. All fields are plain numbers and lists so the
    state survives a restart as JSON."""

    def __init__(self, day: str, saved: Optional[dict] = None):
        s = saved or {}
        self.day = day
        self.ticks = s.get("ticks", 0)
        self.mode_s = dict(s.get("mode_s", {}))
        self.call_s = s.get("call_s", 0.0)
        self.fallback_s = s.get("fallback_s", 0.0)
        self.air = list(s.get("air", [0.0, 0, None, None]))  # sum, count, min, max
        self.band = list(s.get("band", [0, 0]))  # in band, counted (07-23 h, season, no coast)
        self.below_lower = s.get("below_lower", 0)
        self.afternoon_excess_max = s.get("afternoon_excess_max")
        self.bias = {int(h): list(v) for h, v in s.get("bias", {}).items()}
        self.would_writes = [list(w) for w in s.get("would_writes", [])]
        self.reverts = s.get("reverts", 0)
        self.budget = s.get("budget")

    def snapshot(self) -> dict:
        return {
            "ticks": self.ticks,
            "mode_s": self.mode_s,
            "call_s": self.call_s,
            "fallback_s": self.fallback_s,
            "air": self.air,
            "band": self.band,
            "below_lower": self.below_lower,
            "afternoon_excess_max": self.afternoon_excess_max,
            "bias": {str(h): v for h, v in self.bias.items()},
            "would_writes": self.would_writes,
            "reverts": self.reverts,
            "budget": self.budget,
        }

    def add_tick(self, t: TickSample) -> None:
        self.ticks += 1
        self.mode_s[t.plan_mode] = self.mode_s.get(t.plan_mode, 0.0) + t.dt_s
        if t.calling:
            self.call_s += t.dt_s
        if t.fallback:
            self.fallback_s += t.dt_s
        if t.air is not None:
            self.air[0] += t.air
            self.air[1] += 1
            self.air[2] = t.air if self.air[2] is None else min(self.air[2], t.air)
            self.air[3] = t.air if self.air[3] is None else max(self.air[3], t.air)
            if t.season_on:
                coasting = t.plan_mode in (MODE_WINDOW, MODE_SETTLE, MODE_PARTY)
                if 7 <= t.hour < 23 and not coasting:
                    self.band[1] += 1
                    if abs(t.air - t.target) <= 0.5:
                        self.band[0] += 1
                if t.air < t.lower_c and not coasting:
                    self.below_lower += 1
                if 12 <= t.hour < 18:
                    excess = t.air - t.target
                    if self.afternoon_excess_max is None or excess > self.afternoon_excess_max:
                        self.afternoon_excess_max = excess
        if t.bias is not None and t.hour in TRIM_HOURS:
            entry = self.bias.setdefault(t.hour, [0.0, 0])
            entry[0] += t.bias
            entry[1] += 1

    def add_would_write(self, ts: float, value: float) -> None:
        """A change of the planned setpoint, with the revert counter the shadow acceptance
        watches: going back to the value from two writes ago within 30 minutes."""
        if (
            len(self.would_writes) >= 2
            and self.would_writes[-2][1] == value
            and ts - self.would_writes[-1][0] <= 1800.0
        ):
            self.reverts += 1
        self.would_writes.append([ts, value])

    def note_budget(self, budget: dict) -> None:
        if self.budget is None:
            self.budget = budget

    def record(self, zone: str, trim_before: float, update: TrimUpdate) -> dict:
        n_air = self.air[1]
        return {
            "type": "daily",
            "day": self.day,
            "zone": zone,
            "ticks": self.ticks,
            "mode_hours": {m: _hours(s) for m, s in sorted(self.mode_s.items())},
            "call_hours": _hours(self.call_s),
            "fallback_hours": _hours(self.fallback_s),
            "air_mean": round(self.air[0] / n_air, 2) if n_air else None,
            "air_min": self.air[2],
            "air_max": self.air[3],
            "in_band_pct": round(100.0 * self.band[0] / self.band[1], 1) if self.band[1] else None,
            "band_ticks": self.band[1],
            "below_lower_ticks": self.below_lower,
            "afternoon_excess_max": (
                None if self.afternoon_excess_max is None else round(self.afternoon_excess_max, 2)
            ),
            "would_writes": len(self.would_writes),
            "would_write_reverts": self.reverts,
            "budget": self.budget,
            "trim_before": trim_before,
            "trim_after": update.trim,
            "trim_bias": None if update.bias is None else round(update.bias, 3),
            "trim_valid_hours": update.valid_hours,
        }
