"""Ice in the terrace ice bath, for sensor.house_upcoming: a row while ice is likely, thin or thick, and the text and
timing of the push that tells the person who uses the bath once the ice is thick. Pure: each step takes the persisted
state and returns new state.

A cold spell starts with a reading of the water at or below `ice_likely_at_or_below_c` and ends with one above
`ice_clear_above_c`. A reading in between ends a spell that has not yet lasted `ice_likely_for_hours` and leaves
one that has, so once ice is likely it stays so until the water is clearly warmer. Unavailable and unknown
readings are no readings: they neither start nor end a spell. Thin ice is likely once a spell has lasted
`ice_likely_for_hours`, which the clock tells without a new reading, because a sensor whose value has not changed
sends no new state.

The ice is thick once the spell's freezing degree-hours reach `ice_thick_degree_hours`. They start at 0 with the
spell and follow the air (`ice_air`): every hour at T degrees adds -T, so frost adds and thaw takes away, never
below 0. The air counts as its last reading until the next one, and not at all while it is unavailable or unknown.

The state is {"since": start of the spell, UTC ISO, "value": the last reading of the water, "fdh": the freezing
degree-hours, "told": True once the push of this freeze-up has gone, "air": the last reading of the air, "air_at":
when it was taken, UTC ISO}. It is empty without a spell, `told` goes when the degree-hours are back at 0 and `air`
and `air_at` do not survive a restart, so the time the app was away adds nothing.

The push goes once per freeze-up, from `ice_push_from` until `ice_push_until` house time, while the ice is thick and
`ice_person` is home. The row is untimed, in `ice_area`, and has no audience; its `level` says thin or thick. Its kind
is "rain" because the dashboard knows two kinds and shows a maintenance row to admins only.
"""

from datetime import time, timedelta

from upcoming_core import make_row, utc_iso
from upcoming_maint import HOUSE_TZ, ddmm, house_date, parse_iso, reading

CATEGORY = "ice_bath"
ROW_ID = "ice:bath"
KIND = "rain"
ICON = "mdi:snowflake"
PUSH_TITLE = "Ice bath: thick ice"
SLACK = 1e-6  # degree-hours: a sum of readings that meets a threshold or cancels out exactly may miss it by a hair


def parse(args):
    """The ice_* keys of the app's arguments (house_upcoming.yaml), validated. Raises ValueError on a missing or
    bad value, so a typo fails at start instead of silently turning the rule off."""
    cfg = args if isinstance(args, dict) else {}
    try:
        out = {
            "entity": str(cfg["ice_temperature"]), "air": str(cfg["ice_air"]), "person": str(cfg["ice_person"]),
            "area": str(cfg["ice_area"]),
            "likely_at": float(cfg["ice_likely_at_or_below_c"]), "clear_above": float(cfg["ice_clear_above_c"]),
            "hold": timedelta(hours=float(cfg["ice_likely_for_hours"])),
            "thick_at": float(cfg["ice_thick_degree_hours"]),
            "push_from": time.fromisoformat(str(cfg["ice_push_from"])),
            "push_until": time.fromisoformat(str(cfg["ice_push_until"])),
        }
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"ice: missing or invalid value {e}") from None
    if not (out["entity"].startswith("sensor.") and out["air"].startswith("sensor.")
            and out["person"].startswith("person.") and out["area"]):
        raise ValueError("ice: ice_temperature and ice_air must be sensors, ice_person a person entity and ice_area "
                         "an area id")
    if not out["likely_at"] <= out["clear_above"]:
        raise ValueError("ice: ice_likely_at_or_below_c must not be above ice_clear_above_c")
    if out["hold"] <= timedelta(0) or not out["push_from"] < out["push_until"]:
        raise ValueError("ice: ice_likely_for_hours must be positive and ice_push_from before ice_push_until")
    if not out["thick_at"] > 0:
        raise ValueError("ice: ice_thick_degree_hours must be positive")
    return out


def restore(raw):
    """The saved state without anything invalid."""
    raw = raw if isinstance(raw, dict) else {}
    since, value = parse_iso(raw.get("since")), reading(raw.get("value"))
    if since is None or value is None:
        return {}
    st = {"since": utc_iso(since), "value": value, "fdh": max(0.0, reading(raw.get("fdh")) or 0.0)}
    if raw.get("told") is True:
        st["told"] = True
    return st


def step(st, cfg, value, at):
    """The state after a reading of the water: `value` is the number or None for no reading, `at` the time it was
    taken."""
    st = dict(st or {})
    if value is None:
        return st
    since = parse_iso(st.get("since"))
    if value <= cfg["likely_at"]:
        if since is None:
            st, since = {"fdh": 0.0}, at
    elif value > cfg["clear_above"] or since is None or at - since < cfg["hold"]:
        since = None
    if since is None:
        return {}
    st["since"], st["value"] = utc_iso(since), value
    return st


def accrue(st, air, now):
    """The state at `now`, when the air reads `air` (a number, None for no reading). The reading held until now
    counts from the time it was taken: its degrees below zero add to the spell's freezing degree-hours and its
    degrees above zero take away, down to 0."""
    st = dict(st or {})
    if parse_iso(st.get("since")) is None:
        return st
    now = now.replace(microsecond=0)
    held, held_at = reading(st.get("air")), parse_iso(st.get("air_at"))
    if held is not None and held_at is not None and now > held_at:
        hours = (now - held_at).total_seconds() / 3600
        st["fdh"] = max(0.0, st.get("fdh", 0.0) - held * hours)
    if st.get("fdh", 0.0) <= SLACK:
        st.pop("told", None)
    if air is None:
        st.pop("air", None)
        st.pop("air_at", None)
    else:
        st["air"], st["air_at"] = air, utc_iso(now)
    return st


def likely(st, cfg, now):
    """Whether thin ice is likely, which thick ice is as well."""
    since = parse_iso((st or {}).get("since"))
    return since is not None and now - since >= cfg["hold"]


def thick(st, cfg, now):
    return likely(st, cfg, now) and st.get("fdh", 0.0) >= cfg["thick_at"] - SLACK


def _since(st, now):
    """The spell's start as a clock time, with its day when that is not today."""
    since = parse_iso(st["since"]).astimezone(HOUSE_TZ)
    clock = f"{since:%H:%M}"
    return clock if house_date(since) == house_date(now) else f"{ddmm(since, now)} {clock}"


def rows(st, cfg, now):
    """The ice bath's row while ice is likely, else none."""
    if not likely(st, cfg, now):
        return []
    level = "thick" if thick(st, cfg, now) else "thin"
    title = f"Ice bath: {level} ice at {st['value']:.1f} C since {_since(st, now)}"
    return [make_row(ROW_ID, KIND, title, icon=ICON, area=cfg["area"], level=level)]


def push_due(st, cfg, home, now):
    """Whether the push is due: the ice is thick, `home` says its person is home, `now` is inside the push hours and
    this freeze-up has not been told."""
    local = now.astimezone(HOUSE_TZ)
    return (home and thick(st, cfg, now) and cfg["push_from"] <= local.time() < cfg["push_until"]
            and not st.get("told"))


def push(st, now):
    """The push's title and message, and the category MobileNotifier scopes its audience by."""
    return {"title": PUSH_TITLE, "message": f"At {st['value']:.1f} °C since {_since(st, now)}",
            "category": CATEGORY}


def pushed(st):
    return {**st, "told": True}
