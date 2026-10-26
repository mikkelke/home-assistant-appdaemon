"""
Alerts for the floor-heating controller: the plant faults that show in the thermostats, the
relay, the floors and the forecast. State machines only, no AppDaemon import and no I/O, so every
rule can be unit-tested on its own. They are admin diagnostics for the owner; nothing here
touches a device.

Every alert is an episode: it pushes once when its condition becomes true, and pushes again only
after the condition has cleared. The cold, pump and floor alerts, which one fault often raises
together, also share a limit of one push per zone every repeat_hours.

  cold:<zone>      the control air has stayed under cold_c for cold_minutes, with the zone
                   wanting more than cold_c, its thermostat not off, and no opening, party,
                   floor cap or bedroom hold-back explaining it
  no_pump:<zone>   the zone has called for calling_hours without a break, and the relay drew no
                   more than no_pump_w for 90% of those hours
  floor:<zone>     the zone's floor rose by less than floor_rise_min_c between one hour and
                   calling_hours into an unbroken call, with no opening open and the outdoor air
                   under 12 C
  no_hot_water     a zone has called for 30 minutes and the supply clamp reads under
                   no_hot_water_c (only with a supply clamp configured)
  uneven:<zone>    the mean over the last evenness_nights nights (02-06 h) of the zone's
                   compared floor minus its reference floor is beyond evenness_c; it re-arms
                   under 1 C
  fallback:<zone>  the zone has had no usable control air for three hours and runs on its
                   constant target
  forecast_stale   the forecast is unusable
  write:<zone>     two consecutive thermostat writes were not confirmed; a confirmed write
                   clears the episode
  zone8_heat       the living-room monitor thermostat is in heat, which runs the pump for a zone
                   with no actuator

A call is unbroken while the zone's thermostat reports heating on every tick; a gap of more than
RUN_GAP_S ends it. Timestamps are epoch seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import heating_plan as hp

RUN_GAP_S = 1200.0
FALLBACK_S = hp.CONTROL_AIR_MAX_AGE_MIN * 60.0
FLOOR_FROM_S = 3600.0
FLOOR_SAMPLE_S = 1800.0
FLOOR_OUTDOOR_MAX_C = 12.0
PUMP_OFF_SHARE = 0.9
PUMP_MIN_COVERAGE = 0.5
SUPPLY_CALL_S = 1800.0
EVENNESS_REARM_C = 1.0
EVENNESS_FROM_HOUR = 2
EVENNESS_TO_HOUR = 6
EVENNESS_MIN_S = 7200.0
NIGHTS_KEPT = 14
COLD_EXEMPT_MODES = (
    hp.MODE_WINDOW, hp.MODE_SETTLE, hp.MODE_PARTY, hp.MODE_FLOOR_CAP, hp.MODE_GUARD
)

EVENT_START = "start"
EVENT_SUPPRESSED = "suppressed"
EVENT_CLEAR = "clear"


def _num(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _name(zone: str) -> str:
    return zone.replace("_", " ").capitalize()


@dataclass(frozen=True)
class ZoneObs:
    """One zone as seen on one five-minute tick."""

    zone: str
    dt_s: float
    season_on: bool
    hvac_mode: Optional[str]
    calling: bool
    air: Optional[float]
    air_fresh: bool
    target: float
    plan_mode: str
    fallback: bool
    floor: Optional[float]
    window_active: bool
    evenness: Optional[float] = None


@dataclass(frozen=True)
class HouseObs:
    now_ts: float
    day: str
    hour: int
    season_on: bool
    relay_w: Optional[float]
    outdoor: Optional[float]
    supply: Optional[float]
    supply_enabled: bool
    forecast_stale: bool
    monitor_heat: bool


@dataclass(frozen=True)
class AlertEvent:
    key: str
    zone: Optional[str]
    kind: str
    title: str
    message: str


class CallRun:
    """One zone's unbroken call for heat: when it began, how the relay behaved during the last
    calling_hours of it, and the floor temperature one hour and calling_hours in."""

    def __init__(self, saved: Optional[dict] = None):
        s = saved if isinstance(saved, dict) else {}
        self.start = _num(s.get("start"))
        self.last = _num(s.get("last"))
        self.window_seen = s.get("window_seen") is True
        self.floor_start = _num(s.get("floor_start"))
        self.floor_end = _num(s.get("floor_end"))
        self.samples: list = []
        saved_samples = s.get("samples")
        for item in saved_samples if isinstance(saved_samples, list) else []:
            if isinstance(item, list) and len(item) == 3 and None not in map(_num, item):
                self.samples.append([float(item[0]), float(item[1]), 1 if item[2] else 0])

    def snapshot(self) -> dict:
        return {
            "start": self.start,
            "last": self.last,
            "window_seen": self.window_seen,
            "floor_start": self.floor_start,
            "floor_end": self.floor_end,
            "samples": self.samples,
        }

    def reset(self) -> None:
        self.start = self.last = self.floor_start = self.floor_end = None
        self.window_seen = False
        self.samples = []

    def age(self, now_ts: float) -> float:
        return 0.0 if self.start is None else now_ts - self.start

    def feed(self, now_ts: float, z: ZoneObs, relay_w: Optional[float], cfg: hp.AlertConfig):
        if not z.calling:
            self.reset()
            return
        if self.start is None or (self.last is not None and now_ts - self.last > RUN_GAP_S):
            self.reset()
            self.start = now_ts
        self.last = now_ts
        self.window_seen = self.window_seen or z.window_active
        window_s = cfg.calling_hours * 3600.0
        if relay_w is not None:
            self.samples.append([now_ts, z.dt_s, 1 if relay_w <= cfg.no_pump_w else 0])
        self.samples = [s for s in self.samples if s[0] > now_ts - window_s]
        age = now_ts - self.start
        if z.floor is not None:
            if self.floor_start is None and FLOOR_FROM_S <= age < FLOOR_FROM_S + FLOOR_SAMPLE_S:
                self.floor_start = z.floor
            if (
                self.floor_end is None
                and self.floor_start is not None
                and window_s <= age < window_s + FLOOR_SAMPLE_S
            ):
                self.floor_end = z.floor

    def pump_off_share(self, now_ts: float, window_s: float) -> Optional[float]:
        """The share of the last window_s of this call with the relay at idle power; None
        while the call is younger than the window or the relay readings cover under half of it."""
        if self.start is None or now_ts - self.start < window_s:
            return None
        total = sum(s[1] for s in self.samples)
        if total < PUMP_MIN_COVERAGE * window_s:
            return None
        return sum(s[1] for s in self.samples if s[2]) / total

    def floor_rise(self) -> Optional[float]:
        if self.floor_start is None or self.floor_end is None:
            return None
        return self.floor_end - self.floor_start


class Alerts:
    def __init__(self, cfg: hp.AlertConfig, saved: Optional[dict] = None):
        self.cfg = cfg
        s = saved if isinstance(saved, dict) else {}
        self._episodes = _floats(s.get("episodes"))
        self._last_push = _floats(s.get("last_push"))
        self._holds = _floats(s.get("holds"))
        self._runs = {k: CallRun(v) for k, v in _dict(s.get("runs")).items()}
        self._night = {
            k: [str(v[0]), float(v[1]), int(v[2]), float(v[3])]
            for k, v in _dict(s.get("night")).items()
            if isinstance(v, list) and len(v) == 4 and None not in map(_num, v[1:])
        }
        self._nights = {
            k: [
                [str(n[0]), float(n[1])]
                for n in v
                if isinstance(n, list) and len(n) == 2 and _num(n[1]) is not None
            ]
            for k, v in _dict(s.get("nights")).items()
            if isinstance(v, list)
        }

    def snapshot(self) -> dict:
        return {
            "episodes": self._episodes,
            "last_push": self._last_push,
            "holds": self._holds,
            "runs": {zone: run.snapshot() for zone, run in self._runs.items()},
            "night": self._night,
            "nights": self._nights,
        }

    def active(self) -> list:
        return sorted(self._episodes)

    def write_result(self, zone: str, failures: int, now_ts: float) -> list:
        return self._episode(
            f"write:{zone}",
            zone,
            failures >= 2,
            now_ts,
            "Heating: thermostat write failed",
            f"{_name(zone)} has not confirmed two consecutive heating writes",
        )

    def update(self, house: HouseObs, zones: list) -> list:
        """Feed one tick. Returns the events of this tick: starts to push, suppressed starts and
        clears to log."""
        events: list = []
        longest_call = 0.0
        for z in zones:
            run = self._runs.setdefault(z.zone, CallRun())
            run.feed(house.now_ts, z, house.relay_w, self.cfg)
            longest_call = max(longest_call, run.age(house.now_ts))
            events += self._cold(z, house)
            events += self._pump(z, run, house)
            events += self._floor(z, run, house)
            events += self._fallback(z, house)
        events += self._supply(longest_call, house)
        events += self._evenness(zones, house)
        events += self._episode(
            "forecast_stale",
            None,
            house.season_on and house.forecast_stale,
            house.now_ts,
            "Heating: forecast stale",
            "The forecast is stale: no sun cuts until it is refreshed",
        )
        events += self._episode(
            "zone8_heat",
            None,
            house.monitor_heat,
            house.now_ts,
            "Heating: zone 8 in heat",
            "Zone 8 has no actuator: heat mode runs the pump",
        )
        return events

    # ------------------------------------------------------------ episodes

    def _episode(
        self,
        key: str,
        zone: Optional[str],
        cond: bool,
        now_ts: float,
        title: str,
        message: str,
        limited: bool = False,
    ) -> list:
        on = key in self._episodes
        if cond and not on:
            self._episodes[key] = now_ts
            if limited and zone is not None:
                last = self._last_push.get(zone)
                if last is not None and now_ts - last < self.cfg.repeat_hours * 3600.0:
                    return [AlertEvent(key, zone, EVENT_SUPPRESSED, title, message)]
                self._last_push[zone] = now_ts
            return [AlertEvent(key, zone, EVENT_START, title, message)]
        if on and not cond:
            del self._episodes[key]
            return [AlertEvent(key, zone, EVENT_CLEAR, title, "")]
        return []

    # ------------------------------------------------------------ rows

    def _held(self, key: str, cond: bool, now_ts: float) -> float:
        """How long `cond` has held without a break, as of now."""
        if not cond:
            self._holds.pop(key, None)
            return 0.0
        return now_ts - self._holds.setdefault(key, now_ts)

    def _cold(self, z: ZoneObs, house: HouseObs) -> list:
        cfg = self.cfg
        below = (
            house.season_on
            and z.hvac_mode != "off"
            and z.air is not None
            and z.air_fresh
            and z.plan_mode not in COLD_EXEMPT_MODES
            and z.target > cfg.cold_c
            and z.air < cfg.cold_c
        )
        held = self._held(f"cold:{z.zone}", below, house.now_ts)
        air = 0.0 if z.air is None else z.air
        return self._episode(
            f"cold:{z.zone}",
            z.zone,
            below and held >= cfg.cold_minutes * 60.0,
            house.now_ts,
            "Heating: cold room",
            f"{_name(z.zone)} {air:.1f} °C for {cfg.cold_minutes:g} min (target {z.target:.1f})",
            limited=True,
        )

    def _pump(self, z: ZoneObs, run: CallRun, house: HouseObs) -> list:
        cfg = self.cfg
        share = run.pump_off_share(house.now_ts, cfg.calling_hours * 3600.0)
        return self._episode(
            f"no_pump:{z.zone}",
            z.zone,
            share is not None and share >= PUMP_OFF_SHARE,
            house.now_ts,
            "Heating: pump off",
            f"{_name(z.zone)} has called {cfg.calling_hours:g} h, wiring-centre pump is off",
            limited=True,
        )

    def _floor(self, z: ZoneObs, run: CallRun, house: HouseObs) -> list:
        cfg = self.cfg
        rise = run.floor_rise()
        cond = (
            rise is not None
            and rise < cfg.floor_rise_min_c
            and not run.window_seen
            and house.outdoor is not None
            and house.outdoor < FLOOR_OUTDOOR_MAX_C
        )
        return self._episode(
            f"floor:{z.zone}",
            z.zone,
            cond,
            house.now_ts,
            "Heating: floor not warming",
            f"{_name(z.zone)} floor not warming after {cfg.calling_hours:g} h of heat",
            limited=True,
        )

    def _fallback(self, z: ZoneObs, house: HouseObs) -> list:
        out = house.season_on and z.fallback
        held = self._held(f"fallback:{z.zone}", out, house.now_ts)
        return self._episode(
            f"fallback:{z.zone}",
            z.zone,
            out and held >= FALLBACK_S,
            house.now_ts,
            "Heating: no room temperature",
            f"{_name(z.zone)} has no usable room temperature and runs on its constant target",
        )

    def _supply(self, longest_call: float, house: HouseObs) -> list:
        cond = (
            house.supply_enabled
            and house.supply is not None
            and longest_call >= SUPPLY_CALL_S
            and house.supply < self.cfg.no_hot_water_c
        )
        supply = 0.0 if house.supply is None else house.supply
        return self._episode(
            "no_hot_water",
            None,
            cond,
            house.now_ts,
            "Heating: no hot water",
            f"No hot water from the building (supply {supply:.0f} °C)",
        )

    def _evenness(self, zones: list, house: HouseObs) -> list:
        events: list = []
        for zone, night in list(self._night.items()):
            if night[0] != house.day or house.hour >= EVENNESS_TO_HOUR:
                del self._night[zone]
                if night[2] and night[3] >= EVENNESS_MIN_S:
                    nights = self._nights.setdefault(zone, [])
                    nights.append([night[0], round(night[1] / night[2], 3)])
                    del nights[:-NIGHTS_KEPT]
                    events += self._judge_evenness(zone, house)
        in_night = EVENNESS_FROM_HOUR <= house.hour < EVENNESS_TO_HOUR
        for z in zones:
            if z.evenness is None or not in_night or not house.season_on:
                continue
            night = self._night.setdefault(z.zone, [house.day, 0.0, 0, 0.0])
            night[1] += z.evenness
            night[2] += 1
            night[3] += z.dt_s
        return events

    def _judge_evenness(self, zone: str, house: HouseObs) -> list:
        n = self.cfg.evenness_nights
        nights = self._nights.get(zone, [])
        if len(nights) < n:
            return []
        mean = sum(v for _day, v in nights[-n:]) / n
        key = f"uneven:{zone}"
        if key in self._episodes:
            uneven = abs(mean) >= EVENNESS_REARM_C
        else:
            uneven = abs(mean) > self.cfg.evenness_c
        return self._episode(
            key,
            zone,
            uneven,
            house.now_ts,
            "Heating: uneven floors",
            f"Living/dining floors uneven: {abs(mean):.1f} °C",
        )


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _floats(value) -> dict:
    return {str(k): float(v) for k, v in _dict(value).items() if _num(v) is not None}
