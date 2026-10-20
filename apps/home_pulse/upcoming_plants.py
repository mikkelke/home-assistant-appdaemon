"""Watering reminders for sensor.house_upcoming: one row for each houseplant whose soil is dry. Pure:
each step takes the persisted latch and returns new state.

A plant (the `plants` list in house_upcoming.yaml) is dry once its soil moisture reading, in percent,
is at or under its `dry`, and stays dry until a reading is CLOSE_MARGIN above it: watering makes the
reading jump, while a reading wandering around `dry` never flaps the row. A reading that is unknown,
unavailable or not a number changes nothing and has no row; a sensor that stays offline is the battery
inventory's to report.

A dry plant's row is admin only and untimed, like an open battery condition. It has no Done button, the
moisture rising closes it, and its push key is unique per episode: the time the latch opened.
"""

import re
import unicodedata

from upcoming_core import make_row, utc_iso
from upcoming_maint import parse_iso, reading

ID_PREFIX = "maint:plant:"
ICON = "mdi:watering-can-outline"
CLOSE_MARGIN = 5


def slug(name):
    """The lowercase ASCII letters and digits of `name`, each run of anything else becoming one "_"."""
    ascii_name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")


def parse_plants(raw):
    """The `plants` list of house_upcoming.yaml, validated, as {moisture entity: plant}. Raises
    ValueError on a plant without a moisture entity, a name with no letters or digits, a `dry` that is
    not a number, or an entity or name listed twice, so a typo fails at start instead of silently
    dropping a plant."""
    plants = {}
    for cfg in raw or []:
        cfg = cfg if isinstance(cfg, dict) else {}
        entity, name, dry = cfg.get("moisture"), str(cfg.get("name") or "").strip(), reading(cfg.get("dry"))
        key = slug(name)
        if not isinstance(entity, str) or not entity:
            raise ValueError(f"plant {name!r}: missing moisture entity")
        if not key:
            raise ValueError(f"plant {entity}: the name needs letters or digits")
        if dry is None:
            raise ValueError(f"plant {entity}: dry must be a number, not {cfg.get('dry')!r}")
        if entity in plants or key in {plant["slug"] for plant in plants.values()}:
            raise ValueError(f"plant {entity}: entity or name listed twice")
        plants[entity] = {"name": name, "slug": key, "area": cfg.get("area") or None, "dry": dry}
    return plants


def valid_latches(raw, plants):
    """The saved latches that are for a configured plant and carry the time they opened."""
    raw = raw if isinstance(raw, dict) else {}
    return {entity: {"since": latch["since"]} for entity, latch in raw.items()
            if entity in plants and isinstance(latch, dict) and parse_iso(latch.get("since")) is not None}


def step_plant(st, plant, value, now):
    """One observation of a plant: `value` is its soil moisture reading or None. The latch is
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


def plant_row(entity, plant, st, value):
    """(row, push key) while the plant is dry and its reading is known, else None."""
    st = st or {}
    if "since" not in st or value is None:
        return None
    row = make_row(ID_PREFIX + plant["slug"], "maint", f"Water the {plant['name']}",
                   f"soil {_percent(value)}, water at {_percent(plant['dry'])}", icon=ICON, area=plant["area"],
                   audience="admin")
    return row, f"plant:{entity}:{st['since']}"
