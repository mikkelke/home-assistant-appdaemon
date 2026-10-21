"""Rain while a terrace door stands open, for sensor.house_upcoming: one row for each open door, in the
door's own room. Exploratory: the forecast row only says "forecast", never pushes, and every fetch is
kept as a fixture so the rule can be measured against what the roof station saw. Pure.

A door open for OPEN_FOR starts hourly met.no fetches, repeated every REFETCH while one stays open.
That door's forecast row sits at the first forecast hour within the horizon with at least MIN_MM, and
goes when the door closes, when the rooftop rain alert is on, or when the forecast is empty or older than
MAX_AGE. The horizon is short while somebody is home, who can feel the rain and needs only a warning, and
longer once nobody is, as zone.home counts them. While the alert is on every open door has a row instead,
telling to close it, at the current hour so it does not change with every tick, and gone when the door
closes or the alert goes off.
"""

from datetime import datetime, timedelta

from upcoming_core import make_row, utc_iso

OPEN_FOR = timedelta(minutes=10)
REFETCH = timedelta(minutes=30)
MAX_AGE = timedelta(minutes=90)
HOUR = timedelta(hours=1)
MIN_MM = 0.3
FIXTURE_HOURS = 4
ICON = "mdi:weather-pouring"


def doors_open_long(doors, now):
    """Doors open for OPEN_FOR or longer; `doors` maps entity -> (is_open, open since or None)."""
    return sorted(entity for entity, (is_open, since) in doors.items()
                  if is_open and since is not None and now - since >= OPEN_FOR)


def fetch_due(open_long, doors, last_fetch, now):
    """Whether to fetch now: a door is open long enough and the last fetch is from before this
    spell of open doors, or REFETCH old."""
    if not open_long:
        return False
    if last_fetch is None or now - last_fetch >= REFETCH:
        return True
    return last_fetch < min(doors[entity][1] for entity in open_long)


def forecast_hours(entries):
    """[(hour start, mm)] from weather.get_forecasts hourly entries, soonest first."""
    out = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            start = datetime.fromisoformat(str(entry["datetime"]).replace("Z", "+00:00"))
            mm = float(entry.get("precipitation") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is not None:
            out.append((start, mm))
    return sorted(out)


def horizon(zone_state, at_home, away):
    """`away` once zone.home counts nobody, else `at_home`: an unknown or unavailable count is somebody."""
    return away if str(zone_state).strip() == "0" else at_home


def _label(entity, door_cfg):
    return (door_cfg.get(entity) or {}).get("label", "terrace door")


def _row(entity, door_cfg, title, detail, at):
    """The door's row: its id is `rain:` and its room, so each door has one and the dashboard puts it in
    that room."""
    area = (door_cfg.get(entity) or {}).get("area")
    return make_row(f"rain:{area or entity.split('.', 1)[-1]}", "rain", title, detail, at=at, icon=ICON, area=area)


def forecast_rows(hours, fetched_at, open_long, door_cfg, now, ahead):
    """A "Rain forecast" row for each door in `open_long`, at the first hour with rain that starts within
    `ahead` of now, else none."""
    if not open_long or fetched_at is None or now - fetched_at > MAX_AGE:
        return []
    for start, mm in hours:
        if start + HOUR <= now:
            continue
        if start >= now + ahead:
            break
        if mm >= MIN_MM:
            return [_row(entity, door_cfg, "Rain forecast", f"{_label(entity, door_cfg)} open", start)
                    for entity in open_long]
    return []


def raining_rows(doors, door_cfg, now):
    """A row for every door that is open, telling to close it, at the current hour."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    return [_row(entity, door_cfg, f"Raining, close the {_label(entity, door_cfg)}", "rain on the open door", hour)
            for entity, (is_open, _) in sorted(doors.items()) if is_open]


def rain_rows(hours, fetched_at, doors, door_cfg, alert_on, now, ahead):
    """The rain rows: `doors` maps entity -> (is_open, open since or None)."""
    if alert_on:
        return raining_rows(doors, door_cfg, now)
    return forecast_rows(hours, fetched_at, doors_open_long(doors, now), door_cfg, now, ahead)


def fixture(fetched_at, hours, open_long, rows, ahead):
    """One line for the fixture log: the fetch time, each early hour as [start, minutes ahead, mm],
    the open doors, how far ahead the rule looked and where the forecast rows went."""
    return {
        "fetched": utc_iso(fetched_at),
        "doors": list(open_long),
        "ahead_min": round(ahead.total_seconds() / 60),
        "hours": [[utc_iso(start), round((start - fetched_at).total_seconds() / 60), mm]
                  for start, mm in hours[:FIXTURE_HOURS]],
        "row_at": rows[0].get("at") if rows else None,
    }
