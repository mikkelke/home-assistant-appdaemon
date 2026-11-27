"""The bedroom humidifier's tank, for sensor.house_upcoming: one maintenance row while the water level reads low
or empty, none at medium, high or without a reading. Pure.

The row is untimed, in `humidifier_area`, and never enters the maintenance push: BedroomHumidifier pushes the
empty tank itself.
"""

from upcoming_core import make_row

ROW_ID = "maint:humidifier:water"
DETAILS = {"low": "Tank low", "empty": "Tank empty, it cannot run"}
ICONS = {"low": "mdi:cup-water", "empty": "mdi:water-off"}


def parse(args):
    """The humidifier_* keys of the app's arguments: {"entity", "area"}, or None when humidifier_water is unset."""
    cfg = args if isinstance(args, dict) else {}
    entity = cfg.get("humidifier_water")
    if not entity:
        return None
    area = cfg.get("humidifier_area")
    if not str(entity).startswith("sensor.") or not isinstance(area, str) or not area:
        raise ValueError("humidifier: humidifier_water must be a sensor and humidifier_area an area id")
    return {"entity": str(entity), "area": area}


def rows(cfg, level):
    """The refill row for a water level of low or empty, else none."""
    if cfg is None or level not in DETAILS:
        return []
    return [make_row(ROW_ID, "maint", "Refill the humidifier", DETAILS[level], icon=ICONS[level], area=cfg["area"],
                     audience="admin")]
