"""Rain forecast while a terrace door stands open, for sensor.house_upcoming. Exploratory: the row
only says "forecast", never pushes, and every fetch is kept as a fixture so the rule can be
measured against what the roof station saw. Pure.

A door open for OPEN_FOR starts hourly met.no fetches, repeated every REFETCH while one stays open.
The row sits at the first forecast hour within HORIZON with at least MIN_MM, and goes when the
doors close, when the rooftop rain alert is on, or when the forecast is empty or older than
MAX_AGE.
"""

from datetime import datetime, timedelta

from upcoming_core import make_row, utc_iso

OPEN_FOR = timedelta(minutes=10)
REFETCH = timedelta(minutes=30)
MAX_AGE = timedelta(minutes=90)
HORIZON = timedelta(hours=3)
HOUR = timedelta(hours=1)
MIN_MM = 0.3
FIXTURE_HOURS = 4


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


def _doors_text(open_long, door_cfg, both_area):
    if len(open_long) == 1:
        cfg = door_cfg.get(open_long[0]) or {}
        return f"{cfg.get('label', 'terrace door')} open", cfg.get("area")
    return "terrace doors open", both_area


def rain_row(hours, fetched_at, open_long, door_cfg, alert_on, now, both_area="rooftop"):
    if not open_long or alert_on or fetched_at is None or now - fetched_at > MAX_AGE:
        return None
    for start, mm in hours:
        if start + HOUR <= now:
            continue
        if start >= now + HORIZON:
            break
        if mm >= MIN_MM:
            detail, area = _doors_text(open_long, door_cfg, both_area)
            return make_row("rain:terrace", "rain", "Rain forecast", detail, at=start, icon="mdi:weather-pouring",
                            area=area)
    return None


def fixture(fetched_at, hours, open_long, row):
    """One line for the fixture log: the fetch time, each early hour as [start, minutes ahead, mm],
    the open doors and where the row went."""
    return {
        "fetched": utc_iso(fetched_at),
        "doors": list(open_long),
        "hours": [[utc_iso(start), round((start - fetched_at).total_seconds() / 60), mm]
                  for start, mm in hours[:FIXTURE_HOURS]],
        "row_at": row.get("at") if row else None,
    }
