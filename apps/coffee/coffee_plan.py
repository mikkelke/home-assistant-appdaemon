"""Recipes from the xBloom library, in the one shape the coffee app uses, and the plan a recipe makes.
Pure.

The integration hands a recipe over in one of two shapes: a share-link import (grinder_size, rpm, and
water_ratio as the ratio's denominator) or one built in Home Assistant (grind_size, grinder_speed_rpm,
ratio "1:N", water_ratio as total millilitres). normalize() reads both.

The plan: each pour lasts its volume over its flow rate, then its pause. plan_water is the planned
water in tenths of a millilitre for every second from the first bloom; plan_pours is
"start-end:pattern:temp_c:ml" per pour. The planned brew time is the end of the last pour plus
DRAWDOWN_GUESS_S. recipe_key is a short hash of everything that makes two brews comparable.
"""

import hashlib
import json
import math

PATTERNS = {1: "centered", 2: "spiral", 3: "circular"}
CUPS = {1: "xPod", 2: "Omni dripper", 3: "Other", 4: "Tea"}
DEFAULT_FLOW = 3.0
DRAWDOWN_GUESS_S = 75


def _num(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _ratio(recipe, dose):
    raw = recipe.get("ratio")
    if isinstance(raw, str) and ":" in raw:
        value = _num(raw.split(":", 1)[1])
        if value:
            return value
    value = _num(raw)
    if value and value <= 25:
        return value
    water = _num(recipe.get("water_ratio"))
    if water and water <= 25:
        return water
    if water and dose:
        return round(water / dose * 2) / 2
    return None


def _pour(raw):
    if not isinstance(raw, dict):
        return None
    ml = _num(raw.get("volume_ml"))
    if ml is None:
        return None
    pattern = raw.get("pattern")
    if not isinstance(pattern, str):
        pattern = PATTERNS.get(int(_num(pattern, 0)), "spiral")
    return {
        "ml": round(ml, 1),
        "temp_c": round(_num(raw.get("temperature_c"), 93.0), 1),
        "flow": round(_num(raw.get("flow_rate"), DEFAULT_FLOW) or DEFAULT_FLOW, 1),
        "pattern": pattern,
        "pause_s": int(_num(raw.get("pause_s"), 0)),
        "agitate": [int(_num(raw.get("agitate_before"), 2)) == 1, int(_num(raw.get("agitate_after"), 2)) == 1],
    }


def normalize(recipe):
    """The coffee app's recipe: id, name, dose_g, ratio (the N of 1:N), grind, rpm, cup_type, pours, total_ml.
    None for anything that is no recipe."""
    if not isinstance(recipe, dict) or not str(recipe.get("name") or "").strip():
        return None
    dose = _num(recipe.get("dose_g"))
    pours = [p for p in (_pour(raw) for raw in recipe.get("pours") or []) if p]
    if dose is None or not pours:
        return None
    grind = _num(recipe.get("grind_size"), _num(recipe.get("grinder_size")))
    rpm = _num(recipe.get("grinder_speed_rpm"), _num(recipe.get("rpm")))
    out = {
        "id": str(recipe.get("id") or ""),
        "name": str(recipe["name"]).strip(),
        "dose_g": round(dose, 1),
        "ratio": _ratio(recipe, dose),
        "grind": int(round(grind)) if grind is not None else None,
        "rpm": int(round(rpm)) if rpm is not None else None,
        "cup_type": int(_num(recipe.get("cup_type"), 0)),
        "pours": pours,
        "total_ml": round(sum(p["ml"] for p in pours), 1),
    }
    return out


def with_overrides(recipe, dose=None, ratio=None, grind=None):
    """`recipe` as a brew with one-off overrides makes it: the pours scaled to dose x ratio, as the
    integration's customizer does."""
    out = json.loads(json.dumps(recipe))
    if grind is not None:
        out["grind"] = int(grind)
    if dose is None and ratio is None:
        return out
    dose = float(dose if dose is not None else recipe["dose_g"])
    ratio = float(ratio if ratio is not None else (recipe.get("ratio") or 16))
    total = round(dose * ratio, 1)
    factor = total / recipe["total_ml"] if recipe.get("total_ml") else 1.0
    for pour in out["pours"]:
        pour["ml"] = round(pour["ml"] * factor, 1)
    out.update(dose_g=round(dose, 1), ratio=ratio, total_ml=round(sum(p["ml"] for p in out["pours"]), 1))
    return out


def recipe_key(recipe):
    fields = [recipe.get("dose_g"), recipe.get("ratio"), recipe.get("grind"), recipe.get("rpm"), recipe.get("cup_type"),
              [[p["ml"], p["temp_c"], p["flow"], p["pattern"], p["pause_s"], p["agitate"]] for p in recipe["pours"]]]
    return hashlib.sha1(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:10]


def timeline(recipe):
    """[(start s, end s, pour)] from the first bloom."""
    out, cursor = [], 0.0
    for pour in recipe["pours"]:
        length = pour["ml"] / (pour["flow"] or DEFAULT_FLOW)
        out.append((cursor, cursor + length, pour))
        cursor += length + pour["pause_s"]
    return out


def plan_end_s(recipe):
    steps = timeline(recipe)
    return steps[-1][1] if steps else 0.0


def plan_total_s(recipe):
    return int(round(plan_end_s(recipe) + DRAWDOWN_GUESS_S))


def plan_water(recipe):
    """Planned water in tenths for every second from 0 to the end of the last pour."""
    steps = timeline(recipe)
    end = int(math.ceil(round(plan_end_s(recipe), 3)))
    out = []
    for second in range(end + 1):
        water = 0.0
        for start, stop, pour in steps:
            if second >= stop:
                water += pour["ml"]
            elif second > start:
                water += pour["ml"] * (second - start) / (stop - start)
        out.append(int(round(water * 10)))
    return out


def _n(value):
    """A number as the short decimal string the entities carry."""
    if value is None:
        return ""
    value = round(float(value), 1)
    return str(int(value)) if value == int(value) else str(value)


def plan_pours(recipe):
    return ";".join(f"{int(round(start))}-{int(round(stop))}:{pour['pattern']}:{_n(pour['temp_c'])}:{_n(pour['ml'])}"
                    for start, stop, pour in timeline(recipe))


def recipe_entry(recipe):
    """One entry of sensor.coffee_recipes' `recipes`."""
    entry = {
        "id": recipe["id"],
        "name": recipe["name"],
        "dose_g": _n(recipe["dose_g"]),
        "ratio": _n(recipe.get("ratio")),
        "grind": _n(recipe.get("grind")),
        "rpm": _n(recipe.get("rpm")),
        "cup": CUPS.get(recipe.get("cup_type"), ""),
        "total_ml": _n(recipe["total_ml"]),
        "plan_s": str(plan_total_s(recipe)),
        "pours": ";".join(f"{_n(p['ml'])}/{_n(p['temp_c'])}/{_n(p['flow'])}/{p['pattern']}/{p['pause_s']}"
                          for p in recipe["pours"]),
    }
    return {key: value for key, value in entry.items() if value != ""}


def by_id(recipes, recipe_id):
    return next((r for r in recipes if r["id"] == str(recipe_id)), None) if recipe_id else None


def by_name(recipes, name):
    return next((r for r in recipes if r["name"] == name), None) if name else None
