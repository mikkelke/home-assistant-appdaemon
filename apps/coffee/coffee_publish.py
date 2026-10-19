"""The coffee app's published shapes: the seven sensor.coffee_* entities (contract v1) and the files under
/media/coffee. Pure.

AppDaemon 4.5.13 drops None, False and 0 from set_state attributes at any depth, so every attribute
value is a string, a list of strings, or a list of flat dicts of strings: numbers are decimal strings,
series are CSV strings of tenths, a missing attribute means unknown. Each entity is kept under its size
cap (serialized attributes), the oldest list entries going first.
"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import coffee_baseline as cb
import coffee_curve as cc
from coffee_fsm import epoch, iso

V = "1"
HOUSE_TZ = ZoneInfo("Europe/Copenhagen")
CAPS = {"coffee_state": 2048, "coffee_live": 6144, "coffee_baseline": 5120, "coffee_brews": 14336,
        "coffee_recipes": 4096, "coffee_advice": 4096, "coffee_beans": 2048}
RECENT_CAP = 100 * 1024
BREWS_DAYS = 60
BREWS_MAX = 40
WEEKS = 9
LIVE_STEPS = (1, 2, 4, 8)
LAST_CURVE_STEP = 4
RECENT_STEP = 2
NAMES = {"coffee_state": ("Coffee state", "mdi:coffee-maker"), "coffee_live": ("Coffee live", "mdi:chart-areaspline"),
         "coffee_baseline": ("Coffee baseline", "mdi:chart-bell-curve"), "coffee_beans": ("Coffee beans", "mdi:seed"),
         "coffee_brews": ("Coffee brews", "mdi:history"), "coffee_recipes": ("Coffee recipes", "mdi:book-open-variant"),
         "coffee_advice": ("Coffee advice", "mdi:lightbulb-on-outline")}


def num(value, digits=0):
    """A number as a decimal string, None for no number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        value = round(float(value), digits)
    except (TypeError, ValueError):
        return None
    if digits == 0 or value == int(value):
        return str(int(value))
    return f"{value:.{digits}f}"


def power_text(watts):
    return num(watts, 1) if watts is not None and watts < 10 else num(watts)


def size(attrs):
    return len(json.dumps(attrs, separators=(",", ":"), ensure_ascii=False))


def leaves_ok(value):
    """Whether `value` keeps to the string encoding."""
    if isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(isinstance(v, str) or (isinstance(v, dict) and all(isinstance(x, str) for x in v.values()))
                   for v in value)
    return False


def finish(entity_key, attrs):
    """The attributes with v, friendly name and icon, and no empty value left in."""
    name, icon = NAMES[entity_key]
    out = {"v": V}
    for key, value in attrs.items():
        if value is None or value == "" or value == []:
            continue
        out[key] = value
    out.update(friendly_name=name, icon=icon)
    return out


def _put(out, key, value):
    if value is not None and value != "":
        out[key] = value


# -- sensor.coffee_state ------------------------------------------------------------------------


def state_entity(brew, last, now, machine, power_w, link, link_seen_at, fault, notify_guess, pending_notify,
                 live_verdict, eta_ts, eta_basis):
    """(state, attributes) of sensor.coffee_state."""
    attrs = {"machine": machine}
    _put(attrs, "power_w", power_text(power_w))
    attrs["link"] = link
    _put(attrs, "link_seen_at", iso(link_seen_at))
    _put(attrs, "fault", fault if fault and fault != "ok" else None)
    if brew is None:
        state = "asleep" if machine in ("asleep", "off") else "idle"
        if last:
            attrs["brew_id"] = last["id"]
        _put(attrs, "notify", pending_notify)
        _put(attrs, "notify_guess", notify_guess)
        return state, finish("coffee_state", attrs)
    phase = brew["phase"]
    recipe = brew.get("recipe") or {}
    bean = brew.get("bean") or {}
    final = brew.get("final") or {}
    for key, value in (("brew_id", brew["id"]), ("source", brew.get("source")), ("recipe_id", recipe.get("id")),
                       ("recipe_name", recipe.get("name") or brew.get("recipe_name")),
                       ("variant", brew.get("variant")), ("bean_id", bean.get("id")), ("bean_name", bean.get("name")),
                       ("started_at", iso(brew.get("started_ts") or brew.get("requested_ts"))),
                       ("first_bloom_at", iso(brew.get("bloom_ts"))), ("ready_at", iso(brew.get("ready_ts"))),
                       ("picked_up_at", iso(brew.get("picked_up_ts"))), ("stopped_at", iso(brew.get("stopped_ts"))),
                       ("who", brew.get("who")), ("who_method", brew.get("who_method")),
                       ("notify", brew.get("notify")), ("push", brew.get("push"))):
        _put(attrs, key, value)
    attrs["pour"] = str(brew.get("pour") or 0)
    _put(attrs, "pours_total", num(brew.get("pours_total")))
    if phase == "paused":
        _put(attrs, "paused_since", iso(brew.get("paused_since_ts")))
    elif phase in ("grinding", "brewing") and eta_ts is not None:
        attrs["eta"], attrs["eta_basis"] = iso(eta_ts), eta_basis
    if phase == "stopped":
        _put(attrs, "stop_reason", brew.get("stop_reason"))
    if phase == "start_failed":
        _put(attrs, "start_error", brew.get("start_error"))
    if phase in ("brewing", "paused"):
        _put(attrs, "verdict", live_verdict)
    if phase in ("ready", "picked_up", "stopped"):
        _put(attrs, "in_band", brew.get("in_band"))
        for key, digits in (("cup_g", 0), ("water_ml", 0), ("total_s", 0), ("drawdown_s", 0)):
            _put(attrs, key, num(final.get(key), digits))
    return phase, finish("coffee_state", attrs)


# -- sensor.coffee_live -------------------------------------------------------------------------


def live_entity(brew, curve, now, fresh, last_fresh_at, plan_water, plan_pours, ended=None):
    """(state, attributes) of sensor.coffee_live. `ended` is the last brew's curve file once it is over."""
    if brew is None and ended is None:
        return "idle", finish("coffee_live", {})
    if ended is not None:
        state = "ended"
        attrs = {"brew_id": ended["id"], "t0": iso(ended.get("t0")), "end_s": num(ended.get("end_s"))}
        cup, water, pours, pauses = ended.get("cup") or [], ended.get("water") or [], ended.get("pours") or [], \
            ended.get("pauses") or []
    else:
        state = "live" if fresh or curve is None else "stale"
        attrs = {"brew_id": brew["id"]}
        cup = curve.trimmed(curve.cup) if curve else []
        water = curve.trimmed(curve.water) if curve else []
        pours, pauses = (curve.pours, curve.pauses) if curve else ([], [])
        if curve is not None:
            attrs["t0"] = iso(curve.t0)
            _put(attrs, "end_s", num(curve.end_s))
        _put(attrs, "last_fresh_at", iso(last_fresh_at))
    attrs["pours"] = cc.pairs_csv(pours, ":")
    attrs["pauses"] = ",".join(f"{a}-{b if b is not None else a}" for a, b in pauses)
    _put(attrs, "plan_pours", plan_pours)
    for step in LIVE_STEPS:
        trial = dict(attrs, step_s=str(step), cup=cc.csv(cc.every(cup, step)), water=cc.csv(cc.every(water, step)),
                     plan_water=cc.csv(cc.every(plan_water or [], step)))
        out = finish("coffee_live", trial)
        if size(out) <= CAPS["coffee_live"]:
            return state, out
    return state, finish("coffee_live", dict(attrs, step_s=str(LIVE_STEPS[-1])))


# -- sensor.coffee_baseline ---------------------------------------------------------------------


def baseline_entity(base):
    if not base:
        return "none", finish("coffee_baseline", {})
    attrs = {"baseline_id": base.get("id"), "recipe_id": base.get("recipe_id"), "recipe_key": base.get("recipe_key"),
             "bean_id": base.get("bean_id"), "n": str(base.get("n") or 0),
             "brew_ids": ",".join(base.get("brew_ids") or []),
             "step_s": str(base.get("step_s") or cb.STEP_S), "computed_at": base.get("computed_at"),
             "plan_pours": base.get("plan_pours"), "plan_total_s": num(base.get("plan_total_s"))}
    for field in ("total_s", "cup_g", "drawdown_s"):
        _put(attrs, f"{field}_median", num(base.get(f"{field}_median")))
        _put(attrs, f"{field}_pm", num(base.get(f"{field}_pm")))
    for key in ("cup_lo", "cup_hi", "water_lo", "water_hi"):
        if base.get(key):
            attrs[key] = cc.csv(base[key])
    attrs["plan_water"] = cc.csv(base.get("plan_water") or [])
    out = finish("coffee_baseline", attrs)
    if size(out) > CAPS["coffee_baseline"]:
        out = finish("coffee_baseline", dict(attrs, plan_water=cc.csv(cc.every(base.get("plan_water") or [], 2))))
    return base.get("state") or "building", out


# -- sensor.coffee_beans ------------------------------------------------------------------------


def beans_entity(view):
    remaining = view.get("remaining")
    attrs = {"bean_id": view.get("bean_id"), "bean_name": view.get("bean_name"), "added_at": view.get("added_at"),
             "dose_g": num(view.get("dose_g"), 1), "brews_left": num(view.get("brews_left")),
             "low": "yes" if view.get("low") else None, "last_op_id": view.get("last_op_id"),
             "undo_until": view.get("undo_until"),
             "known_beans": [{"id": b["id"], "name": b["name"]} for b in view.get("known_beans") or []]}
    out = finish("coffee_beans", attrs)
    while size(out) > CAPS["coffee_beans"] and attrs["known_beans"]:
        attrs["known_beans"] = attrs["known_beans"][1:]
        out = finish("coffee_beans", attrs)
    return ("unknown" if remaining is None else str(remaining)), out


# -- sensor.coffee_brews ------------------------------------------------------------------------


def _local(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(HOUSE_TZ)


def _record_ts(record):
    return epoch(record.get("started_at")) or epoch(record.get("ts"))


def brew_row(record):
    recipe = record.get("recipe") or {}
    row = {"id": record.get("id"), "at": record.get("started_at") or record.get("ts"), "outcome": record.get("outcome"),
           "source": record.get("source"), "recipe_name": recipe.get("name"), "variant": record.get("variant"),
           "bean_name": (record.get("bean") or {}).get("name"), "grind": num(recipe.get("grind")),
           "dose_g": num(record.get("dose_g"), 1), "who": record.get("who"), "rating": record.get("rating"),
           "total_s": num(record.get("total_s")), "cup_g": num(record.get("cup_g")),
           "water_ml": num(record.get("water_ml")), "drawdown_s": num(record.get("drawdown_s")),
           "in_band": record.get("in_band"), "trial": record.get("trial"),
           "faults": ",".join(record.get("faults") or []), "coverage": num(record.get("coverage"), 2)}
    row.update({k: v for k, v in (record.get("deltas") or {}).items() if isinstance(v, str)})
    return {k: v for k, v in row.items() if isinstance(v, str) and v != ""}


def brews_entity(records, now, bean_id, last_curve, changed_at):
    since = now - BREWS_DAYS * 86400
    recent = [r for r in records if (_record_ts(r) or 0) >= since]
    recent.sort(key=lambda r: _record_ts(r) or 0, reverse=True)
    weeks = []
    today = _local(now).date()
    monday = today - timedelta(days=today.weekday())
    for back in range(WEEKS - 1, -1, -1):
        start = monday - timedelta(weeks=back)
        weeks.append(sum(1 for r in records if _record_ts(r) and start <= _local(_record_ts(r)).date()
                         < start + timedelta(weeks=1)))
    hours = [0] * 24
    for r in recent:
        hours[_local(_record_ts(r)).hour] += 1
    tastes = {"sour": 0, "balanced": 0, "bitter": 0}
    for r in records:
        if (r.get("bean") or {}).get("id") == bean_id and r.get("rating") in tastes:
            tastes[r["rating"]] += 1
    planned = sum(float(r.get("debited_g") or 0) for r in recent)
    attrs = {"brews": [brew_row(r) for r in recent[:BREWS_MAX]], "weeks": ",".join(str(n) for n in weeks),
             "brews_60d": str(len(recent)), "beans_planned_g_60d": num(planned),
             "hours": ",".join(str(n) for n in hours), "taste": ",".join(f"{k}:{v}" for k, v in tastes.items()),
             "rated_current_beans": str(sum(tastes.values())),
             "last_curve": cc.csv(cc.every(last_curve, LAST_CURVE_STEP)) if last_curve else None}
    out = finish("coffee_brews", attrs)
    while size(out) > CAPS["coffee_brews"] and attrs["brews"]:
        attrs["brews"] = attrs["brews"][:-1]
        out = finish("coffee_brews", attrs)
    return iso(changed_at) or iso(now), out


# -- sensor.coffee_recipes ----------------------------------------------------------------------


def recipes_entity(entries, usual):
    """`entries` are coffee_plan.recipe_entry dicts, `usual` the usual's normalized recipe or None."""
    ordered = sorted(entries, key=lambda e: (e["id"] != (usual or {}).get("id"), e["name"]))
    attrs = {"usual_id": (usual or {}).get("id"), "recipes": ordered}
    out = finish("coffee_recipes", attrs)
    while size(out) > CAPS["coffee_recipes"] and len(attrs["recipes"]) > 1:
        attrs["recipes"] = attrs["recipes"][:-1]
        out = finish("coffee_recipes", attrs)
    return ((usual or {}).get("name") or "none"), out


# -- sensor.coffee_advice -----------------------------------------------------------------------


def advice_entity(view):
    attrs = {}
    for key, value in view.items():
        if key == "state":
            continue
        if key == "thread":
            attrs["thread"] = [{k: str(v) for k, v in entry.items() if v not in (None, "")} for entry in value]
        elif key == "brew_ids":
            attrs["brew_ids"] = ",".join(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            attrs[key] = num(value)
        elif isinstance(value, str):
            attrs[key] = value
    out = finish("coffee_advice", attrs)
    while size(out) > CAPS["coffee_advice"] and attrs.get("thread"):
        attrs["thread"] = attrs["thread"][1:]
        out = finish("coffee_advice", attrs)
    return view.get("state", "none"), out


# -- /media/coffee/recent.json ------------------------------------------------------------------


def recent_doc(records, curves, bands, now):
    """{v, generated_at, step_s, brews, bands} of the last BREWS_DAYS days, under RECENT_CAP."""
    since = now - BREWS_DAYS * 86400
    with_curve = [r for r in records if r.get("id") in curves and (_record_ts(r) or 0) >= since]
    with_curve.sort(key=lambda r: _record_ts(r) or 0, reverse=True)
    brews = []
    for r in with_curve:
        curve = curves[r["id"]]
        brews.append({"id": r["id"], "t0": iso(curve.get("t0")), "cup": cc.every(curve.get("cup") or [], RECENT_STEP),
                      "water": cc.every(curve.get("water") or [], RECENT_STEP), "pours": curve.get("pours") or [],
                      "end_s": curve.get("end_s"), "baseline_id": r.get("baseline_id")})
    while True:
        used = {b["baseline_id"] for b in brews if b.get("baseline_id")}
        doc = {"v": 1, "generated_at": iso(now), "step_s": RECENT_STEP, "brews": brews,
               "bands": {k: v for k, v in bands.items() if k in used}}
        if len(json.dumps(doc, separators=(",", ":"))) <= RECENT_CAP or not brews:
            return doc
        brews = brews[:-1]


def band_doc(base):
    """What recent.json keeps of a baseline a brew was compared with."""
    return {"n": base.get("n"), "cup_lo": base.get("cup_lo") or [], "cup_hi": base.get("cup_hi") or [],
            "water_lo": base.get("water_lo") or [], "water_hi": base.get("water_hi") or [],
            "plan_water": cc.every(base.get("plan_water") or [], RECENT_STEP)}
