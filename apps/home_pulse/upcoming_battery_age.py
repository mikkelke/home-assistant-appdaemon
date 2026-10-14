"""Battery age facts from recorder history, for the maintenance rows: when the current cells went
in, how long the previous set lasted, and, for cells that drain in a straight line, about how many
weeks are left until ETA_LEVEL. Pure.

One value per house day: the time-weighted median of the readings in effect that day. Day d is a
battery change when the median of the next days (d included, at most 7) is CHANGE_RISE above the
lower of the last day before d and the median of up to 5 days before d within 14 days, and the
lowest of those next days is CHANGE_MIN_RISE above it. Across a gap of at least one whole day
without a reading (cells that died and came back) GAP_RISE is enough for both. A change whose day
before already reached the rise at its highest is dated that day. The first SKIP_DAYS of a series
are never a change, and changes closer than MERGE_DAYS keep the first.

A device's `history_from` marks a known change in how its cells behave: a set that went in before
it is never a lifetime and never gets a trend.
"""

from datetime import date, datetime, time, timedelta
from statistics import StatisticsError, correlation, linear_regression, median

from upcoming_maint import ETA_LEVEL, HOUSE_TZ, house_date, reading

SKIP_DAYS = 7
CHANGE_RISE = 30
CHANGE_MIN_RISE = 20
GAP_RISE = 20
MERGE_DAYS = 21
TREND_MIN_DAYS = 21
TREND_MIN_VALUES = 10
TREND_MAX_SLOPE = -0.1
TREND_MIN_R2 = 0.8
LOW_DATES_DAYS = 7


def series_from_history(entries):
    """[(aware datetime, reading or None)], oldest first, from AppDaemon get_history rows; a row
    without a time is skipped."""
    rows = entries
    if isinstance(rows, list) and rows and isinstance(rows[0], list):
        rows = [row for group in rows for row in group]
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        when = row.get("last_changed")
        if isinstance(when, str):
            try:
                when = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                continue
        if not isinstance(when, datetime) or when.tzinfo is None:
            continue
        out.append((when, reading(row.get("state"))))
    out.sort(key=lambda item: item[0])
    return out


def daily_stats(series, now):
    """[(house date, median, min, max)] of the readings in effect each house day, weighted by how
    long each held; a day without a reading is absent."""
    weights = {}
    for index, (start, value) in enumerate(series):
        end = series[index + 1][0] if index + 1 < len(series) else now
        if value is None or end <= start:
            continue
        moment = start
        while moment < end:
            day = house_date(moment)
            midnight = datetime.combine(day + timedelta(days=1), time(0), HOUSE_TZ)
            stop = min(end, midnight)
            bucket = weights.setdefault(day, {})
            bucket[value] = bucket.get(value, 0.0) + (stop - moment).total_seconds()
            moment = stop
    out = []
    for day in sorted(weights):
        bucket = weights[day]
        half, total, middle = sum(bucket.values()) / 2, 0.0, None
        for value in sorted(bucket):
            total += bucket[value]
            if total >= half:
                middle = value
                break
        out.append((day, middle, min(bucket), max(bucket)))
    return out


def detect_changes(days):
    changes = []
    for index in range(SKIP_DAYS, len(days)):
        day = days[index][0]
        prev_day, prev_value = days[index - 1][0], days[index - 1][1]
        recent = [d[1] for d in days[max(0, index - 5):index] if (day - d[0]).days <= 14]
        base = min([prev_value] + ([median(recent)] if recent else []))
        ahead = [d[1] for d in days[index:index + 7] if (d[0] - day).days < 7]
        gap = (day - prev_day).days > 1
        rise, min_rise = (GAP_RISE, GAP_RISE) if gap else (CHANGE_RISE, CHANGE_MIN_RISE)
        if median(ahead) - base < rise or min(ahead) - base < min_rise:
            continue
        if not gap and days[index - 1][3] - base >= rise:
            day = prev_day
        if changes and (day - changes[-1]).days < MERGE_DAYS:
            continue
        changes.append(day)
    return changes


def _set_end(days, change):
    """When the set before `change` ended: the last day with a reading when the cells died before
    it (a gap), else the change itself."""
    before = [d[0] for d in days if d[0] < change]
    return before[-1] if before and (change - before[-1]).days > 1 else change


def _trend_weeks(days, start, today):
    points = [(float((d[0] - start).days), d[1]) for d in days if d[0] > start]
    if (today - start).days < TREND_MIN_DAYS or len({value for _, value in points}) < TREND_MIN_VALUES:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    try:
        slope, intercept = linear_regression(xs, ys)
        r2 = correlation(xs, ys) ** 2
    except StatisticsError:
        return None
    if slope > TREND_MAX_SLOPE or r2 < TREND_MIN_R2:
        return None
    current = intercept + slope * (today - start).days
    if current <= ETA_LEVEL:
        return None
    return max(1, round((current - ETA_LEVEL) / -slope / 7))


def battery_facts(days, now, *, trend=True, history_from=None):
    """{"in_since", "last_set_days", "eta_weeks", "changes"} for one device; absent facts are left
    out. Dates are ISO strings."""
    today = house_date(now)
    changes = detect_changes(days)
    facts = {"changes": [day.isoformat() for day in changes]}
    if not changes:
        return facts
    current = changes[-1]
    facts["in_since"] = current.isoformat()

    def in_regime(day):
        return history_from is None or day >= history_from

    if len(changes) >= 2 and in_regime(changes[-2]):
        facts["last_set_days"] = (_set_end(days, current) - changes[-2]).days
    if trend and in_regime(current):
        weeks = _trend_weeks(days, current, today)
        if weeks:
            facts["eta_weeks"] = weeks
    return facts


def outage_onset(series):
    """(onset, had_reading) of the outage a series ends in: the start of the trailing run without a
    reading, when a reading precedes it. (None, True) when the series ends on a reading,
    (None, False) when it holds no reading at all."""
    if series and series[-1][1] is not None:
        return None, True
    onset = None
    for moment, value in reversed(series):
        if value is not None:
            return onset, True
        onset = moment
    return None, False


def low_dates(days, low, now):
    """House days of the last LOW_DATES_DAYS with a reading at or below `low`, as ISO strings."""
    today = house_date(now)
    return [d[0].isoformat() for d in days if d[2] <= low and (today - d[0]).days < LOW_DATES_DAYS]


def peak_since(days, since):
    values = [d[3] for d in days if since is None or d[0] >= since]
    return max(values) if values else None


def as_date(value):
    return date.fromisoformat(value) if isinstance(value, str) and value else None
