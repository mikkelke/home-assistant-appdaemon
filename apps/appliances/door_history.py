"""
The door sensor's last real close, read from the recorder.

A delayed start can run the motor hours after the door shut on the load, and the close a monitor
keeps in memory does not survive an AppDaemon restart in between. The recorder still has it: a
close is a recorded "on" directly followed by "off". "unavailable"/"unknown" -> "off" is the sensor
coming back after an HA restart, not a hand on the door, so it never counts.

Also the close that set a delay timer, told apart from later closes by the machine's standby power.

Pure; the monitors fetch the history and ask here. Sibling-import module
(AppDaemon puts app dirs on sys.path).
"""

from datetime import datetime, timedelta, timezone

from cycle_store import parse_utc


def _utc(value):
    """A tz-aware UTC datetime from a datetime or an ISO string (no offset reads as UTC), else None."""
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    return parse_utc(value)


def last_close(states, now, max_age_s):
    """The UTC time of the newest on -> off edge in `states` that is at most `max_age_s` old at
    `now`, else None. `states` is the door sensor's history, flattened and in time order: dicts
    with "state" and "last_changed"."""
    now = _utc(now)
    if now is None:
        return None
    found = None
    previous = None
    for entry in states or ():
        state = entry.get("state")
        if previous == "on" and state == "off":
            t = _utc(entry.get("last_changed") or entry.get("last_updated"))
            if t is not None and 0 <= (now - t).total_seconds() <= max_age_s and (found is None or t > found):
                found = t
        previous = state
    return found


def _mean_w(points, start, end):
    """Time-weighted mean of the step series `points` [(datetime_utc, watts)], sorted, over
    [start, end); None when no point is at or before `start`."""
    level = None
    total = 0.0
    cursor = start
    for t, w in points:
        if t <= start:
            level = w
            continue
        if t >= end:
            break
        if level is None:
            return None
        total += level * (t - cursor).total_seconds()
        cursor, level = t, w
    if level is None:
        return None
    total += level * (end - cursor).total_seconds()
    return total / (end - start).total_seconds()


def timer_set_close(states, power_points, now, max_age_s, idle_below_w, settle_s=300, min_stretch_s=600):
    """The UTC time of the close that most likely set a delay timer, else None.

    With the door shut, a machine with no timer set idles below `idle_below_w`; a running countdown
    draws more. Each close at most `max_age_s` old at `now` is judged by the mean power from
    `settle_s` after it until the door next opens (or `now`), when that stretch lasts at least
    `min_stretch_s`. The answer is the first close after the last one that still idled: the timer
    was set in the door session that followed. None without such an idle close (no baseline) or
    without a close after it. `states` is the door's flattened history in time order;
    `power_points` is [(datetime_utc, watts)] sorted by time."""
    now = _utc(now)
    if now is None:
        return None
    edges = []
    previous = None
    for entry in states or ():
        state = entry.get("state")
        t = _utc(entry.get("last_changed") or entry.get("last_updated"))
        if t is not None and t <= now:
            if previous == "on" and state == "off" and (now - t).total_seconds() <= max_age_s:
                edges.append(["close", t])
            elif state == "on" and previous != "on":
                edges.append(["open", t])
        previous = state
    closes = []
    for i, (kind, t) in enumerate(edges):
        if kind != "close":
            continue
        next_open = next((u for k, u in edges[i + 1:] if k == "open"), now)
        closes.append((t, next_open))
    last_idle = None
    for i, (t, next_open) in enumerate(closes):
        start = t + timedelta(seconds=settle_s)
        if (next_open - t).total_seconds() < min_stretch_s or next_open <= start:
            continue
        mean = _mean_w(power_points, start, next_open)
        if mean is not None and mean < idle_below_w:
            last_idle = i
    if last_idle is None or last_idle + 1 >= len(closes):
        return None
    return closes[last_idle + 1][0]
