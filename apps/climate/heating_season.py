"""
Season advice for the floor-heating controller: the push that asks the owner whether it is time to
turn the heat on in the autumn and whether it can go off in the spring. State and rules only, no
AppDaemon import and no I/O, so every rule can be unit-tested on its own. Nothing here touches a
device or the season switch: the season is the owner's call.

The advisor judges once a day, from evaluate_at on, and a judged day is used up. A day without a
forecast is not used up: it is judged on the first tick that has one.

Start, while the season is off. A zone cannot keep warm when the mean of its rooms' air between
SEASON_MORNING_FROM and SEASON_MORNING_TO was under its base target minus its margin on each of
the last `mornings` mornings, this one counted up to the moment of judging. Only readings with
the heat off and every opening of the zone closed count, so an aired room is not a cold room, and
a morning with fewer than MIN_MORNING_SAMPLES of them is unknown, never cold. The advice is to
start when at least one zone cannot keep warm and the mean forecast outdoor temperature of the
next OUTLOOK_HOURS is under start_below_c. The indoor rule is the signal; the forecast only
keeps one cold snap after a warm spell from firing it.

Stop, while the season is on and the date lies between stop_from and stop_until. The season has
been on for at least season_min_days, all the zones together called for heat for under idle_hours
on at least idle_days of the last look_back_days closed days (the other days are blips), and the
mean forecast outdoor temperature of the next OUTLOOK_HOURS is at least stop_min_c. A closed day
the controller's statistics cover with fewer than MIN_DAY_TICKS ticks is unknown, and an unknown
day is not an idle one.

Each push carries the facts the owner decides on: for the start the rooms that are cold this
morning, the coldest against its target, and the outlook; for the stop the heat that ran on a
typical day (the median of the days, so a blip or the last heated days do not outvote the rule),
the temperature of the rooms now and the outlook.

An advice is pushed at most once per reask_days calendar days. A change of the season starts
afresh: what was asked and judged under the other season no longer applies. Days are ISO dates
in local time.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Optional

import heating_plan as hp

OUTLOOK_HOURS = 72
MIN_MORNING_SAMPLES = 6
MIN_DAY_TICKS = 144

ADVICE_START = "start"
ADVICE_STOP = "stop"
ADVICE_NONE = "none"

REASON_NO_FORECAST = "no_forecast"
REASON_NO_DATA = "no_data"
REASON_WARM = "warm_enough"
REASON_MILD = "forecast_mild"
REASON_DATES = "outside_dates"
REASON_YOUNG = "season_young"
REASON_CALLS = "heat_wanted"
REASON_COOL = "forecast_cool"
REASON_ASKED = "asked_recently"


def _num(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _day(value) -> Optional[str]:
    """A saved ISO date as it is, anything else None."""
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) else None
    except ValueError:
        return None


def _clean_last(value) -> Optional[dict]:
    """A saved verdict is kept only when it names a day and a known advice."""
    v = _dict(value)
    known = (ADVICE_START, ADVICE_STOP, ADVICE_NONE)
    return v if _day(v.get("day")) and v.get("advice") in known else None


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def zone_label(zc: hp.ZoneConfig) -> str:
    """How the push names a zone: a room with one resident is that resident's, any other zone
    goes by its own name."""
    if len(zc.residents) == 1:
        return f"{zc.residents[0].capitalize()}'s room"
    name = zc.name.replace("_", " ")
    return name if name.endswith("room") else f"{name} room"


def day_call_hours(daily: dict, day: str) -> Optional[float]:
    """The hours all the zones together called for heat on a closed day, from the controller's
    daily records (zone -> list of records). None when a zone has no record of the day or its
    record covers fewer than MIN_DAY_TICKS ticks."""
    total = 0.0
    for records in daily.values():
        record = next((r for r in records if isinstance(r, dict) and r.get("day") == day), None)
        if record is None:
            return None
        ticks, hours = _num(record.get("ticks")), _num(record.get("call_hours"))
        if ticks is None or hours is None or ticks < MIN_DAY_TICKS:
            return None
        total += hours
    return total if daily else None


def _outlook(mean: float) -> str:
    return f"Next {OUTLOOK_HOURS // 24} days {round(mean)} °C."


def _period(days: int) -> str:
    if days == 1:
        return "yesterday"
    return "last week" if days == 7 else f"over the last {days} days"


@dataclass(frozen=True)
class Inputs:
    """What the advisor needs at the moment of judging: today's date, the season, the mean
    forecast temperature of the next OUTLOOK_HOURS, each advised zone's base target and current
    air (None where unreadable), and every zone's closed-day records."""

    day: str
    season_on: bool
    forecast_mean: Optional[float]
    bases: dict = field(default_factory=dict)
    airs: dict = field(default_factory=dict)
    daily: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """What the rule says (advice) and whether the owner is told now (push): an advice inside
    the re-ask period is shown and logged but not pushed."""

    day: str
    advice: str = ADVICE_NONE
    reason: str = ""
    push: bool = False
    title: str = ""
    message: str = ""
    forecast_mean: Optional[float] = None
    mornings: dict = field(default_factory=dict)  # zone -> mean per morning, newest first
    limits: dict = field(default_factory=dict)  # zone -> the air it must stay under to fail
    failed: tuple = ()
    call_hours: tuple = ()  # all zones together per closed day, newest first

    def to_dict(self) -> dict:
        out = {"day": self.day, "advice": self.advice, "pushed": self.push}
        if self.reason:
            out["reason"] = self.reason
        if self.forecast_mean is not None:
            out["forecast_mean"] = round(self.forecast_mean, 2)
        if self.mornings:
            out["mornings"] = {
                zone: [None if m is None else round(m, 2) for m in means]
                for zone, means in self.mornings.items()
            }
        if self.limits:
            out["limits"] = {zone: round(limit, 2) for zone, limit in self.limits.items()}
        if self.failed:
            out["failed"] = list(self.failed)
        if self.call_hours:
            out["call_hours"] = [None if h is None else round(h, 2) for h in self.call_hours]
        if self.message:
            out["message"] = self.message
        return out


class SeasonAdvisor:
    def __init__(self, cfg: hp.SeasonAdvisorConfig, zones: dict, saved: Optional[dict] = None):
        self.cfg = cfg
        self._labels = {zone: zone_label(zones[zone]) for zone in cfg.zones}
        s = _dict(saved)
        self._season_on = s["season_on"] if isinstance(s.get("season_on"), bool) else None
        self._on_since = _day(s.get("on_since"))
        self._evaluated = _day(s.get("evaluated")) or ""
        asked = _dict(s.get("asked"))
        self._asked = {k: d for k in (ADVICE_START, ADVICE_STOP) if (d := _day(asked.get(k)))}
        self._mornings: dict = {}
        for zone, days in _dict(s.get("mornings")).items():
            if zone not in cfg.zones:
                continue
            self._mornings[zone] = {
                day: [float(e[0]), int(e[1])]
                for day, e in _dict(days).items()
                if _day(day) and isinstance(e, list) and len(e) == 2 and None not in map(_num, e)
            }
        self.last: Optional[dict] = _clean_last(s.get("last"))

    def snapshot(self) -> dict:
        return {
            "season_on": self._season_on,
            "on_since": self._on_since,
            "evaluated": self._evaluated,
            "asked": self._asked,
            "mornings": {
                zone: {day: [round(e[0], 3), e[1]] for day, e in days.items()}
                for zone, days in self._mornings.items()
            },
            "last": self.last,
        }

    def note_season(self, on: bool, day: str) -> None:
        """Tell the advisor what the season switch reads. The day a season is first seen on is
        the day it started, so the stop advice waits for the days after it."""
        if on != self._season_on:
            self._season_on = on
            self._on_since = day if on else None
            self._asked = {}
            self.last = None
        elif on and self._on_since is None:
            self._on_since = day

    def observe(self, zone: str, day: str, minute: int, air: Optional[float]) -> None:
        """One reading of a zone's air, which the caller has already limited to closed openings.
        Only the morning of a season that is off is kept."""
        if self._season_on is not False or zone not in self.cfg.zones or air is None:
            return
        if not hp.SEASON_MORNING_FROM <= minute < hp.SEASON_MORNING_TO:
            return
        days = self._mornings.setdefault(zone, {})
        entry = days.setdefault(day, [0.0, 0])
        entry[0] += air
        entry[1] += 1
        oldest = _shift(day, 1 - self.cfg.mornings)
        for old in [d for d in days if d < oldest]:
            del days[old]

    def due(self, day: str, minute: int) -> bool:
        return (
            self._season_on is not None
            and self._evaluated != day
            and minute >= self.cfg.evaluate_at
        )

    def evaluate(self, inp: Inputs) -> Verdict:
        """Judge the day. Without a forecast nothing is decided and the day stays open."""
        if inp.forecast_mean is None:
            return Verdict(inp.day, reason=REASON_NO_FORECAST)
        verdict = self._stop(inp) if inp.season_on else self._start(inp)
        if verdict.advice != ADVICE_NONE:
            if self._quiet(verdict.advice, inp.day):
                verdict = replace(verdict, reason=REASON_ASKED)
            else:
                self._asked[verdict.advice] = inp.day
                verdict = replace(verdict, push=True)
        self._evaluated = inp.day
        self.last = verdict.to_dict()
        return verdict

    def _quiet(self, kind: str, day: str) -> bool:
        asked = self._asked.get(kind)
        if asked is None:
            return False
        return (date.fromisoformat(day) - date.fromisoformat(asked)).days < self.cfg.reask_days

    def _mean(self, zone: str, day: str) -> Optional[float]:
        total, count = self._mornings.get(zone, {}).get(day, (0.0, 0))
        return total / count if count >= MIN_MORNING_SAMPLES else None

    def _start(self, inp: Inputs) -> Verdict:
        cfg = self.cfg
        days = [_shift(inp.day, -k) for k in range(cfg.mornings)]
        means = {zone: [self._mean(zone, d) for d in days] for zone in cfg.zones}
        judged = [z for z in cfg.zones if None not in means[z] and inp.bases.get(z) is not None]
        limits = {z: inp.bases[z] - cfg.margin(z) for z in judged}
        failed = tuple(z for z in judged if all(m < limits[z] for m in means[z]))
        seen = dict(
            day=inp.day,
            forecast_mean=inp.forecast_mean,
            mornings=means,
            limits=limits,
            failed=failed,
        )
        if not judged:
            return Verdict(reason=REASON_NO_DATA, **seen)
        if not failed:
            return Verdict(reason=REASON_WARM, **seen)
        if inp.forecast_mean >= cfg.start_below_c:
            return Verdict(reason=REASON_MILD, **seen)
        first, *others = sorted(failed, key=lambda z: means[z][0] - inp.bases[z])
        rooms = [
            f"{_cap(self._labels[first])} {means[first][0]:.1f} °C this morning "
            f"(target {inp.bases[first]:.1f})"
        ]
        rooms += [f"{self._labels[z]} {means[z][0]:.1f}" for z in others]
        return Verdict(
            advice=ADVICE_START,
            title="Heating on?",
            message=f"{', '.join(rooms)}. {_outlook(inp.forecast_mean)} Time to turn the heat on?",
            **seen,
        )

    def _stop(self, inp: Inputs) -> Verdict:
        cfg = self.cfg
        seen = dict(day=inp.day, forecast_mean=inp.forecast_mean)
        today = date.fromisoformat(inp.day)
        if not cfg.stop_from <= (today.month, today.day) <= cfg.stop_until:
            return Verdict(reason=REASON_DATES, **seen)
        if self._on_since is None or self._on_since > _shift(inp.day, -cfg.season_min_days):
            return Verdict(reason=REASON_YOUNG, **seen)
        days = [_shift(inp.day, -k) for k in range(1, cfg.look_back_days + 1)]
        totals = tuple(day_call_hours(inp.daily, d) for d in days)
        seen["call_hours"] = totals
        known = [t for t in totals if t is not None]
        idle = sum(1 for t in known if t < cfg.idle_hours)
        if idle < cfg.idle_days:
            unknown = len(totals) - len(known)
            reason = REASON_NO_DATA if idle + unknown >= cfg.idle_days else REASON_CALLS
            return Verdict(reason=reason, **seen)
        if inp.forecast_mean < cfg.stop_min_c:
            return Verdict(reason=REASON_COOL, **seen)
        facts = [f"Heat ran {statistics.median(known):.1f} h a day {_period(cfg.look_back_days)}"]
        temps = [round(a) for a in inp.airs.values() if a is not None]
        if temps:
            low, high = min(temps), max(temps)
            facts.append(f"rooms {low}-{high} °C" if low != high else f"rooms {low} °C")
        return Verdict(
            advice=ADVICE_STOP,
            title="Heating off?",
            message=f"{', '.join(facts)}. {_outlook(inp.forecast_mean)} Turn the heat off?",
            **seen,
        )
