"""Battery age facts from recorder history, for the maintenance rows: when the current cells went
in, how long earlier sets lasted, and the day the current cells are expected to be flat. Pure.

One value per house day: the time-weighted median of the readings in effect that day. Day d is a
battery change when the median of the next days (d included, at most 7) is CHANGE_RISE above the
lower of the last day before d and the median of up to 5 days before d within 14 days, and the
lowest of those next days is CHANGE_MIN_RISE above it. Across a gap of at least one whole day
without a reading (cells that died and came back) GAP_RISE is enough for both. A change whose day
before already reached the rise at its highest is dated that day. The first SKIP_DAYS of a series
are never a change, and changes closer than MERGE_DAYS keep the first.

A device's `history_from` marks a known change in how its cells behave: a set that went in before
it is never a lifetime, and its trend starts at `history_from`.

The trend is a straight line through the whole days after the current set's change day, or after
`history_from` when that is later, extrapolated to the model's `empty` reading. Cells that report
fine-grained values are fitted on their daily medians: TREND_MIN_VALUES distinct values over
TREND_MIN_DAYS, a slope of TREND_MAX_SLOPE or steeper and an R2 of TREND_MIN_R2. Cells that report
in coarse steps are fitted on the days the steps happened: the first day whose lowest reading
reached a new level that the next day's lowest reading also reached, every step at least STEP_MIN,
at least two steps TREND_MIN_DAYS apart, the same slope rule, and the R2 rule from three steps on.
A fit that fails any rule gives no date.

The replace-by day is the trend's empty day; without one, the current in-regime set's start plus
the device's own median lifetime once it has OWN_MIN_CYCLES of them, else plus its model's median
lifetime; a day already past is no estimate.
"""

import math
from datetime import date, datetime, time, timedelta
from statistics import StatisticsError, correlation, linear_regression, median

from upcoming_maint import HOUSE_TZ, house_date, reading

SKIP_DAYS = 7
CHANGE_RISE = 30
CHANGE_MIN_RISE = 20
GAP_RISE = 20
MERGE_DAYS = 21
TREND_MIN_DAYS = 21
TREND_MIN_VALUES = 10
TREND_MAX_SLOPE = -0.1
TREND_MIN_R2 = 0.8
STEP_MIN = 10
OWN_MIN_CYCLES = 2
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


def _line_empty_day(points, start, today, empty, *, check_r2=True):
    """The day a straight line through (days after `start`, reading) points reaches `empty`, when the
    fit passes the slope rule (and the R2 rule with `check_r2`) and the day is after `today`."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    try:
        slope, intercept = linear_regression(xs, ys)
        r2 = correlation(xs, ys) ** 2
    except StatisticsError:
        return None
    if slope > TREND_MAX_SLOPE or (check_r2 and r2 < TREND_MIN_R2):
        return None
    day = start + timedelta(days=math.floor((empty - intercept) / slope + 1e-9))
    return day if day > today else None


def step_points(days, start, today):
    """(days after `start`, level) for each step down of a coarse reporter: the first whole day after
    `start` whose lowest reading reached a new level that the next day's lowest reading also reached.
    None once a step is smaller than STEP_MIN, which is how fine-grained readings show."""
    lows = [(d[0], d[2]) for d in days if start < d[0] < today]
    if not lows:
        return []
    level, points = lows[0][1], []
    for (day, low), (_, next_low) in zip(lows, lows[1:]):
        reached = max(low, next_low)
        if reached < level:
            if level - reached < STEP_MIN:
                return None
            points.append((float((day - start).days), reached))
            level = reached
    return points


def empty_day(days, start, today, empty=0.0):
    """The day the cells are expected to read `empty`, fitted on the whole days after `start`, or
    None without a fit that passes every rule."""
    medians = [(float((d[0] - start).days), d[1]) for d in days if start < d[0] < today]
    if len({value for _, value in medians}) >= TREND_MIN_VALUES:
        if (today - start).days < TREND_MIN_DAYS:
            return None
        return _line_empty_day(medians, start, today, empty)
    steps = step_points(days, start, today)
    if not steps or len(steps) < 2 or steps[-1][0] - steps[0][0] < TREND_MIN_DAYS:
        return None
    return _line_empty_day(steps, start, today, empty, check_r2=len(steps) >= 3)


def battery_facts(days, now, *, trend=True, history_from=None, empty=0.0):
    """{"changes", "in_since", "lifetimes", "last_set_days", "empty_on"} for one device; absent facts
    are left out. `lifetimes` are the in-regime sets that ended, oldest first, in days. Dates are ISO
    strings."""
    today = house_date(now)
    changes = detect_changes(days)
    facts = {"changes": [day.isoformat() for day in changes]}
    current = changes[-1] if changes else None
    if current:
        facts["in_since"] = current.isoformat()
    lifetimes = [(_set_end(days, end) - begin).days for begin, end in zip(changes, changes[1:])
                 if history_from is None or begin >= history_from]
    if lifetimes:
        facts["lifetimes"] = lifetimes
        facts["last_set_days"] = lifetimes[-1]
    starts = [day for day in (current, history_from) if day is not None]
    if trend and starts:
        day = empty_day(days, max(starts), today, empty)
        if day:
            facts["empty_on"] = day.isoformat()
    return facts


def replace_by(facts, model_lifetimes, today, history_from=None):
    """(day, basis) the current cells are expected to need replacing by, or None. `basis` is
    "trend", "own" or "model"; `model_lifetimes` are the in-regime lifetimes of every device of the
    model."""
    facts = facts or {}
    day = as_date(facts.get("empty_on"))
    basis = "trend"
    if day is None:
        since = as_date(facts.get("in_since"))
        if since is None or (history_from is not None and since < history_from):
            return None
        own = facts.get("lifetimes") or []
        if len(own) >= OWN_MIN_CYCLES:
            lifetimes, basis = own, "own"
        elif model_lifetimes:
            lifetimes, basis = model_lifetimes, "model"
        else:
            return None
        day = since + timedelta(days=int(median(lifetimes)))
    return (day, basis) if day >= today else None


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
