"""Kristine's train to Odense for sensor.house_upcoming. Pure.

Weekdays in WINDOW, house time, while she is home and the route's data is at most MAX_AGE old:
the next two departures from KBH H, each with "+N" or "cancelled", and a chip when the S-tog leg
is not OK. `at` is the earliest future departure that is not cancelled; untimed when all are.
Departures need the absolute `ts` that TransitAlarm writes with expose_departure_ts. On-time
departures arrive without `delay_min` and `cancelled`, because AppDaemon drops 0 and False.
"""

from datetime import datetime, time, timedelta, timezone

from upcoming_core import make_row
from upcoming_maint import HOUSE_TZ, parse_iso

WINDOW = (time(6, 0), time(9, 30))
MAX_AGE = timedelta(minutes=10)
CLOCK_SKEW = timedelta(minutes=2)
SHOWN = 2
STOG_CHIPS = {"Delayed": "S-tog delayed", "Disrupted": "S-tog disrupted"}


def _truthy(value):
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def departures(raw, now):
    """Future departures [(time, scheduled "HH:MM", delay minutes, cancelled)], soonest first."""
    out = []
    for dep in raw if isinstance(raw, list) else []:
        if not isinstance(dep, dict) or isinstance(dep.get("ts"), bool):
            continue
        try:
            when = datetime.fromtimestamp(float(dep["ts"]), timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        if when <= now:
            continue
        try:
            delay = int(float(dep.get("delay_min") or 0))
        except (TypeError, ValueError):
            delay = 0
        label = str(dep.get("scheduled_time") or dep.get("time") or when.astimezone(HOUSE_TZ).strftime("%H:%M"))[:5]
        out.append((when, label, delay, _truthy(dep.get("cancelled"))))
    return sorted(out)


def _label(label, delay, cancelled):
    if cancelled:
        return f"{label} cancelled"
    return f"{label} +{delay}" if delay > 0 else label


def train_row(route, stog_status, person_state, now, users=("Kristine",), window=WINDOW):
    local = now.astimezone(HOUSE_TZ)
    if local.weekday() >= 5 or not window[0] <= local.time() < window[1] or person_state != "home":
        return None
    attrs = (route or {}).get("attributes") if isinstance(route, dict) else None
    if not isinstance(attrs, dict):
        return None
    as_of = parse_iso(attrs.get("data_as_of"))
    if as_of is None or not -CLOCK_SKEW <= now - as_of <= MAX_AGE:
        return None
    future = departures(attrs.get("departures"), now)
    if not future:
        return None
    detail = ", ".join(_label(label, delay, cancelled) for _, label, delay, cancelled in future[:SHOWN])
    at = next((when for when, _, _, cancelled in future if not cancelled), None)
    return make_row("train:kristine", "train", "Train to Odense", detail, at=at, icon="mdi:train",
                    chip=STOG_CHIPS.get(stog_status), audience_users=list(users))
