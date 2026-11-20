"""Ice in the terrace ice bath, for sensor.house_upcoming: a row while an ice skin is likely, and the text and
timing of the push that tells the person who uses the bath. Pure: each step takes the persisted state and
returns new state.

A cold spell starts with a reading at or below `likely_at_or_below_c` and ends with one above
`clear_above_c`. A reading in between ends a spell that has not yet lasted `likely_for_hours` and leaves one
that has, so once ice is likely it stays so until the water is clearly warmer. Unavailable and unknown readings
are no readings: they neither start nor end a spell. Ice is likely once a spell has lasted `likely_for_hours`,
which the clock tells without a new reading, because a sensor whose value has not changed sends no new state.

The state is {"since": start of the spell, UTC ISO, "value": the last reading, "pushed_on": house day of the
last push}; `since` and `value` exist only during a spell.

The push goes at most once a house day, from `push_from` until `push_until` house time, while ice is likely and
`person` is home. The row is untimed, in `area`, and has no audience. Its kind is "rain" because the dashboard
knows two kinds and shows a maintenance row to admins only.
"""

from datetime import date, time, timedelta

from upcoming_core import make_row, utc_iso
from upcoming_maint import HOUSE_TZ, ddmm, house_date, parse_iso, reading

CATEGORY = "ice_bath"
ROW_ID = "ice:bath"
KIND = "rain"
ICON = "mdi:snowflake"
PUSH_TITLE = "Ice bath"


def parse(raw):
    """The `ice` section of house_upcoming.yaml, validated. Raises ValueError on a missing or bad value, so a
    typo fails at start instead of silently turning the rule off."""
    cfg = raw if isinstance(raw, dict) else {}
    try:
        out = {
            "entity": str(cfg["temperature"]), "person": str(cfg["person"]), "area": str(cfg["area"]),
            "likely_at": float(cfg["likely_at_or_below_c"]), "clear_above": float(cfg["clear_above_c"]),
            "hold": timedelta(hours=float(cfg["likely_for_hours"])),
            "push_from": time.fromisoformat(str(cfg["push_from"])),
            "push_until": time.fromisoformat(str(cfg["push_until"])),
        }
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"ice: missing or invalid value {e}") from None
    if not (out["entity"].startswith("sensor.") and out["person"].startswith("person.") and out["area"]):
        raise ValueError("ice: temperature must be a sensor, person a person entity and area an area id")
    if not out["likely_at"] <= out["clear_above"]:
        raise ValueError("ice: likely_at_or_below_c must not be above clear_above_c")
    if out["hold"] <= timedelta(0) or not out["push_from"] < out["push_until"]:
        raise ValueError("ice: likely_for_hours must be positive and push_from before push_until")
    return out


def restore(raw):
    """The saved state without anything invalid."""
    raw = raw if isinstance(raw, dict) else {}
    st = {}
    since, value = parse_iso(raw.get("since")), reading(raw.get("value"))
    if since is not None and value is not None:
        st.update(since=utc_iso(since), value=value)
    try:
        st["pushed_on"] = date.fromisoformat(raw["pushed_on"]).isoformat()
    except (KeyError, TypeError, ValueError):
        pass
    return st


def step(st, cfg, value, at):
    """The state after a reading: `value` is the number or None for no reading, `at` the time it was taken."""
    st = dict(st or {})
    if value is None:
        return st
    since = parse_iso(st.get("since"))
    if value <= cfg["likely_at"]:
        since = since or at
    elif value > cfg["clear_above"] or since is None or at - since < cfg["hold"]:
        since = None
    if since is None:
        st.pop("since", None)
        st.pop("value", None)
    else:
        st["since"], st["value"] = utc_iso(since), value
    return st


def likely(st, cfg, now):
    since = parse_iso((st or {}).get("since"))
    return since is not None and now - since >= cfg["hold"]


def _since(st, now):
    """The spell's start as a clock time, with its day when that is not today."""
    since = parse_iso(st["since"]).astimezone(HOUSE_TZ)
    clock = f"{since:%H:%M}"
    return clock if house_date(since) == house_date(now) else f"{ddmm(since, now)} {clock}"


def rows(st, cfg, now):
    """The ice bath's row while ice is likely, else none."""
    if not likely(st, cfg, now):
        return []
    title = f"Ice bath at {st['value']:.1f} C since {_since(st, now)}"
    return [make_row(ROW_ID, KIND, title, icon=ICON, area=cfg["area"])]


def push_due(st, cfg, home, now):
    """Whether the day's push is due: ice is likely, `home` says its person is home, `now` is inside the push
    hours and nothing has been pushed today."""
    local = now.astimezone(HOUSE_TZ)
    return (home and likely(st, cfg, now) and cfg["push_from"] <= local.time() < cfg["push_until"]
            and st.get("pushed_on") != local.date().isoformat())


def push(st, now):
    """The push's title and message, and the category MobileNotifier scopes its audience by."""
    return {"title": PUSH_TITLE, "message": f"At {st['value']:.1f} °C since {_since(st, now)}",
            "category": CATEGORY}


def pushed(st, now):
    return {**st, "pushed_on": house_date(now).isoformat()}
