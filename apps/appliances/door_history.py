"""
The door sensor's last real close, read from the recorder.

A delayed start can run the motor hours after the door shut on the load, and the close a monitor
keeps in memory does not survive an AppDaemon restart in between. The recorder still has it: a
close is a recorded "on" directly followed by "off". "unavailable"/"unknown" -> "off" is the sensor
coming back after an HA restart, not a hand on the door, so it never counts.

Pure; the washer and dryer monitors fetch the history and ask here. Sibling-import module
(AppDaemon puts app dirs on sys.path).
"""

from datetime import datetime, timezone

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
