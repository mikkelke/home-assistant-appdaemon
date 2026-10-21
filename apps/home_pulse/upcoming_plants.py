"""Watering reminders for sensor.house_upcoming: one row for each houseplant that needs water. Pure:
each step takes the persisted state and returns new state.

A plant (the `plants` list in house_upcoming.yaml) has either a soil moisture sensor, `moisture`, or a
calendar interval, `every_days`, never both, and optionally a `water` amount in litres. The row's title
is "Water the <name>: <litres> L", and gives up name characters before it gives up the amount. A plant
may also say what it looks like, `aka`, and where it stands, `where`: plain text of one line, which
lead the info lines of the row's sheet.

A sensor plant is dry once its soil moisture reading, in percent, is at or under its `dry`, and stays dry
until a reading is CLOSE_MARGIN above it: watering makes the reading jump, while a reading wandering
around `dry` never flaps the row. A reading that is unknown, unavailable or not a number changes nothing
and has no row; a sensor that stays offline is the battery inventory's to report. A dry plant's row is
admin only and untimed, like an open battery condition. It has no Done button, the moisture rising
closes it, and its push key is unique per episode: the time the latch opened.

A calendar plant is a tapped task counted in days, like the dishwasher's filters (upcoming_runs, whose
anchors, Done and undo it uses): the anchor says when it was last watered, seeded at the moment the plant
is first seen. It has no row until LEAD_DAYS house days before the house day that is `every_days` after
the anchor's, so plants weeks away do not crowd the timeline or its row cap. From then on the row is
timed at DUE_TIME on the due day, and once that day has come it is untimed and due, with a push key for
the episode, which is the anchor's time. A Done tap starts the interval over.
"""

import re
import unicodedata
from datetime import datetime, timedelta

from upcoming_core import TEXT_MAX, clean_text, make_row, utc_iso
from upcoming_maint import DUE_TIME, HOUSE_TZ, ddmm, fit_parts, house_date, parse_iso, reading

ID_PREFIX = "maint:plant:"
ANCHOR_PREFIX = "plant:"
ICON = "mdi:watering-can-outline"
CLOSE_MARGIN = 5
LEAD_DAYS = 1


def slug(name):
    """The lowercase ASCII letters and digits of `name`, each run of anything else becoming one "_"."""
    ascii_name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")


def _note(value, who, field):
    """[value] for a note that is plain one-line ASCII text of at most TEXT_MAX characters, [] for none."""
    if value is None:
        return []
    if not isinstance(value, str) or not value or clean_text(value) != value:
        raise ValueError(f"{who}: {field} must be plain text on one line of at most {TEXT_MAX} characters "
                         f"without accents, not {value!r}")
    return [value]


def _parse(raw):
    sensor, calendar, slugs = {}, {}, set()
    for cfg in raw or []:
        cfg = cfg if isinstance(cfg, dict) else {}
        entity, days, water = cfg.get("moisture"), cfg.get("every_days"), cfg.get("water")
        name = str(cfg.get("name") or "").strip()
        who, key, litres = f"plant {name or cfg!r}", slug(name), reading(water)
        if not key:
            raise ValueError(f"{who}: the name needs letters or digits")
        if (entity is None) == (days is None):
            raise ValueError(f"{who}: give exactly one of moisture and every_days")
        if water is not None and (litres is None or litres <= 0):
            raise ValueError(f"{who}: water must be a positive number of litres, not {water!r}")
        if key in slugs:
            raise ValueError(f"{who}: name listed twice")
        slugs.add(key)
        info = _note(cfg.get("aka"), who, "aka") + _note(cfg.get("where"), who, "where")
        plant = {"name": name, "slug": key, "area": cfg.get("area") or None, "water": litres, "info": info}
        if entity is None:
            interval = reading(days)
            if interval is None or interval < 1 or interval != int(interval):
                raise ValueError(f"{who}: every_days must be a whole number of days above zero, not {days!r}")
            if cfg.get("dry") is not None:
                raise ValueError(f"{who}: dry goes with moisture, not every_days")
            calendar[key] = {**plant, "every_days": int(interval)}
            continue
        dry = reading(cfg.get("dry"))
        if not isinstance(entity, str) or not entity or entity in sensor:
            raise ValueError(f"{who}: moisture must be a sensor entity id, listed once")
        if dry is None:
            raise ValueError(f"{who}: dry must be a number, not {cfg.get('dry')!r}")
        sensor[entity] = {**plant, "dry": dry}
    return sensor, calendar


def parse_plants(raw):
    """The sensor plants of the `plants` list of house_upcoming.yaml, validated, as {moisture entity:
    plant}. Raises ValueError on a plant that has no name with letters or digits, does not have exactly
    one of `moisture` and `every_days`, has a `dry` that is not a number, a `water` that is given but not
    a positive number or an `aka` or `where` that is not plain text, or lists an entity or name twice, so a
    typo fails at start instead of silently dropping a plant."""
    return _parse(raw)[0]


def parse_timed(raw):
    """The calendar plants of the same list, as {slug: plant}, under the same checks, and `every_days`
    has to be a whole number of days above zero and `dry` left out."""
    return _parse(raw)[1]


def valid_latches(raw, plants):
    """The saved latches that are for a configured plant and carry the time they opened."""
    raw = raw if isinstance(raw, dict) else {}
    return {entity: {"since": latch["since"]} for entity, latch in raw.items()
            if entity in plants and isinstance(latch, dict) and parse_iso(latch.get("since")) is not None}


def step_plant(st, plant, value, now):
    """One observation of a sensor plant: `value` is its soil moisture reading or None. The latch is
    {"since": when it opened} while the plant is dry and {} while it is not."""
    st = dict(st or {})
    if value is None:
        return st
    if "since" in st:
        return {} if value >= plant["dry"] + CLOSE_MARGIN else st
    if value <= plant["dry"]:
        st["since"] = utc_iso(now)
    return st


def _percent(value):
    return f"{round(value, 1):g}%"


def _title(plant):
    """"Water the <name>", then ": <litres> L" when the plant has an amount; the name is cut to make
    room, never the amount."""
    amount = f": {round(plant['water'], 2):g} L" if plant["water"] else ""
    return clean_text(f"Water the {plant['name']}", TEXT_MAX - len(amount)) + amount


def plant_row(entity, plant, st, value):
    """(row, push key) while the sensor plant is dry and its reading is known, else None."""
    st = st or {}
    if "since" not in st or value is None:
        return None
    row = make_row(ID_PREFIX + plant["slug"], "maint", _title(plant),
                   f"soil {_percent(value)}, water at {_percent(plant['dry'])}", icon=ICON, area=plant["area"],
                   audience="admin", info=plant["info"])
    return row, f"plant:{entity}:{st['since']}"


# ---------------------------------------------------------------- calendar plants


def anchor_key(plant):
    return ANCHOR_PREFIX + plant["slug"]


def timed_by_row(row, calendar):
    """The calendar plant whose row id is `row`, None for any other id."""
    return next((plant for plant in calendar.values() if ID_PREFIX + plant["slug"] == row), None)


def seed_timed(anchors, calendar, now):
    """`anchors` plus a seed at `now` for every calendar plant that has none."""
    out = dict(anchors)
    for plant in calendar.values():
        out.setdefault(anchor_key(plant), {"at": utc_iso(now), "seed": True})
    return out


def _days(count):
    return f"{count} day{'' if count == 1 else 's'}"


def timed_row(plant, anchor, now):
    """(row, push key) for a calendar plant, None without an anchor or while its due day is more than
    LEAD_DAYS away. Dated and without a key until the due day; untimed, due and with a key for the
    anchor's episode from that day on."""
    when = parse_iso((anchor or {}).get("at"))
    if when is None:
        return None
    interval = plant["every_days"]
    due = house_date(when) + timedelta(days=interval)
    left = (due - house_date(now)).days
    if left > LEAD_DAYS:
        return None
    since = max(0, (house_date(now) - house_date(when)).days)
    if left > 0:
        at, key = datetime.combine(due, DUE_TIME, HOUSE_TZ), None
        detail = fit_parts([f"{_days(left)} left", ddmm(due, now)])
    else:
        at, key = None, f"{anchor_key(plant)}:{utc_iso(when)}"
        detail = "due today" if left == 0 else f"due since {ddmm(due, now)}"
    last = (f"Counting from {ddmm(when, now)}" if anchor.get("seed")
            else f"Done {ddmm(when, now)}, {'today' if since == 0 else f'{_days(since)} ago'}")
    row = make_row(ID_PREFIX + plant["slug"], "maint", _title(plant), detail, at=at, icon=ICON, area=plant["area"],
                   audience="admin", tap_done=True,
                   info=[*plant["info"], "Every day" if interval == 1 else f"Every {interval} days", last])
    return row, key
