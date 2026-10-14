"""Maintenance rows for sensor.house_upcoming: the Rober2 parts, the monthly washer drum clean and
the battery inventory. Pure: each step takes the persisted state and `now` and returns new state.

Battery rules, per model in house_upcoming.yaml:
    low: N        a reading at or below N, held for HOLD, opens the condition; with days: K it
                  opens once K house days in a row had such a reading
    drop: N       a reading N points or more below the highest reading of the current cells,
                  held for HOLD
    neither       offline only
A condition closes on a battery change held for HOLD: built-in cells ("charge") once the reading
is CHARGE_MARGIN above `low`, swappable cells once it is `rise` (default DEFAULT_RISE) above the
lowest reading of the condition. A device without a reading for OFFLINE_GRACE is offline, dated
from the transition when it was seen live or is visible in history, else undated. An offline-only
model's offline row carries its action, since going offline is how its cells announce they are flat.

Every condition carries a push key that is unique per episode; `pushed_by` models belong to another
app's notifications and have none.
"""

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from upcoming_core import BATTERY_ID_PREFIX, TEXT_MAX, make_row, utc_iso

HOUSE_TZ = ZoneInfo("Europe/Copenhagen")
HOLD = timedelta(hours=2)
OFFLINE_GRACE = timedelta(hours=24)
CHARGE_MARGIN = 10
DEFAULT_RISE = 30
LOW_DAYS_KEEP = 7
ETA_LEVEL = 20
DRUM_INTERVAL = timedelta(days=30)
PUSHED_KEEP = timedelta(days=180)

ACTIONS = ("replace", "recharge", "charge")
ACTION_ICONS = {
    "replace": "mdi:battery-alert-variant-outline",
    "recharge": "mdi:battery-sync-outline",
    "charge": "mdi:battery-charging-low",
}
OFFLINE_ICON = "mdi:lan-disconnect"

ROBER2_PARTS = (
    ("filter", "Rober2 filter", "replace"),
    ("main_brush", "Rober2 main brush", "replace"),
    ("side_brush", "Rober2 side brush", "replace"),
    ("sensor", "Rober2 sensors", "clean"),
)

DIGEST_TITLE = "Maintenance due"
DIGEST_NAMES = 3


def parse_iso(value):
    """Aware datetime from an ISO string; a naive one is house time. None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=HOUSE_TZ)


def house_date(moment):
    return moment.astimezone(HOUSE_TZ).date()


def ddmm(moment, now):
    day = moment if isinstance(moment, date) and not isinstance(moment, datetime) else house_date(moment)
    return day.strftime("%d.%m") if day.year == house_date(now).year else day.strftime("%d.%m.%y")


def reading(state):
    """A battery state as a number; None for unknown, unavailable or anything else."""
    if isinstance(state, bool):
        return None
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fit_parts(parts, limit=TEXT_MAX):
    """Joins the non-empty parts with ", ", leaving out trailing parts that would pass `limit`."""
    text = ""
    for part in parts:
        if not part:
            continue
        candidate = f"{text}, {part}" if text else part
        if len(candidate) > limit:
            break
        text = candidate
    return text


def _held(st, key, ok, now):
    """True once `ok` has held since the time kept under `key`; a miss restarts the clock."""
    if not ok:
        st.pop(key, None)
        return False
    since = parse_iso(st.get(key)) or now
    st[key] = utc_iso(since)
    return now - since >= HOLD


# ---------------------------------------------------------------- inventory


def _as_date(value):
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def parse_inventory(raw):
    """The `batteries` section of house_upcoming.yaml, validated. `devices` is one list, so adding or
    removing a device line changes a single yaml value and AppDaemon reloads the app either way.
    Raises ValueError on a device of an unknown model, a device listed twice, an unknown action, a
    rule without an action, or an entity both listed and excluded, so a typo fails at start
    instead of silently dropping a device."""
    raw = raw or {}
    models = {}
    for key, cfg in (raw.get("models") or {}).items():
        cfg = cfg or {}
        action = cfg.get("action") or None
        if action is not None and action not in ACTIONS:
            raise ValueError(f"battery model {key}: unknown action {action!r}")
        low, drop = cfg.get("low"), cfg.get("drop")
        if (low is not None or drop is not None) and action is None:
            raise ValueError(f"battery model {key}: a rule needs an action")
        models[key] = {
            "cell": str(cfg.get("cell") or ""),
            "action": action,
            "low": float(low) if low is not None else None,
            "days": max(1, int(cfg.get("days", 1))),
            "drop": float(drop) if drop is not None else None,
            "rise": float(cfg["rise"]) if cfg.get("rise") is not None else None,
            "pushed_by": cfg.get("pushed_by") or None,
            "trend": bool(cfg.get("trend", True)),
        }
    devices = {}
    for cfg in raw.get("devices") or []:
        cfg = cfg or {}
        entity = cfg.get("entity")
        if not entity or entity in devices:
            raise ValueError(f"battery device {entity!r}: missing or listed twice")
        if cfg.get("model") not in models:
            raise ValueError(f"battery device {entity}: unknown model {cfg.get('model')!r}")
        devices[entity] = {
            "model": cfg["model"],
            "name": str(cfg.get("name") or entity),
            "area": cfg.get("area") or None,
            "history_from": _as_date(cfg.get("history_from")),
        }
    excluded = set(raw.get("excluded") or [])
    both = sorted(excluded & set(devices))
    if both:
        raise ValueError(f"battery entities both listed and excluded: {', '.join(both)}")
    return {"models": models, "devices": devices, "excluded": excluded}


def unlisted_batteries(battery_entities, inventory):
    return sorted(set(battery_entities) - set(inventory["devices"]) - inventory["excluded"])


# ---------------------------------------------------------------- batteries


def _run_ending(days, today):
    present = set(days)
    run, day = 0, date.fromisoformat(today)
    while day.isoformat() in present:
        run += 1
        day -= timedelta(days=1)
    return run


def _open(st, value, now):
    st["low_since"] = utc_iso(now)
    st["low_min"] = value
    st.pop("low_from", None)
    st.pop("recover_since", None)


def _close(st):
    for key in ("low_since", "low_min", "recover_since", "low_days", "low_from"):
        st.pop(key, None)


def _step_low(st, model, value, now):
    low = model["low"]
    if "low_since" not in st:
        if model["days"] > 1:
            today = house_date(now).isoformat()
            if value <= low:
                st["low_days"] = sorted(set(st.get("low_days", [])) | {today})[-LOW_DAYS_KEEP:]
            opens = _run_ending(st.get("low_days", []), today) >= model["days"]
        else:
            opens = _held(st, "low_from", value <= low, now)
        if opens:
            _open(st, value, now)
        return
    st["low_min"] = min(st.get("low_min", value), value)
    if model["action"] == "charge":
        recovered = value >= low + CHARGE_MARGIN
    else:
        recovered = value >= st["low_min"] + (model["rise"] or DEFAULT_RISE)
    if _held(st, "recover_since", recovered, now):
        _close(st)


def _step_drop(st, model, value, now):
    if "low_since" not in st:
        st["peak"] = max(st.get("peak", value), value)
        if _held(st, "low_from", value <= st["peak"] - model["drop"], now):
            _open(st, value, now)
        return
    st["low_min"] = min(st.get("low_min", value), value)
    if _held(st, "recover_since", value >= st["low_min"] + (model["rise"] or model["drop"]), now):
        _close(st)
        st["peak"] = value


def step_battery(st, model, value, now, offline_at=None):
    """One observation of a device: `value` is its reading or None, `offline_at` the time of a
    reading-to-no-reading transition seen live. Feeding the same value again later is how a hold
    or a day rule moves on."""
    st = dict(st or {})
    if value is None:
        if "off_ep" not in st:
            st["off_ep"] = utc_iso(offline_at or now)
            if offline_at is not None:
                st["off_since"] = utc_iso(offline_at)
            else:
                st["off_seen"] = utc_iso(now)
        return st
    for key in ("off_ep", "off_since", "off_seen"):
        st.pop(key, None)
    if model["low"] is not None:
        _step_low(st, model, value, now)
    elif model["drop"] is not None:
        _step_drop(st, model, value, now)
    return st


def bootstrap_battery(st, model, *, onset=None, outage_from=None, low_dates=(), peak=None):
    """History's view of what the app did not see: the start of the current outage (`onset`, or
    `outage_from` when the window holds no reading at all), the house days with a low reading, and
    the highest reading of the current cells. A live observation is never overwritten."""
    st = dict(st or {})
    if "off_ep" in st and "off_since" not in st:
        if onset is not None:
            st["off_since"] = utc_iso(onset)
            st.pop("off_seen", None)
        elif outage_from is not None:
            seen = parse_iso(st.get("off_seen"))
            st["off_seen"] = utc_iso(min(seen, outage_from) if seen else outage_from)
    if model["low"] is not None and model["days"] > 1 and low_dates and "low_since" not in st:
        st["low_days"] = sorted(set(st.get("low_days", [])) | set(low_dates))[-LOW_DAYS_KEEP:]
    if model["drop"] is not None and peak is not None and "low_since" not in st:
        st["peak"] = max(st.get("peak", peak), peak)
    return st


def _age_parts(facts, now):
    facts = facts or {}
    parts = []
    in_since = facts.get("in_since")
    if in_since:
        parts.append(f"in since {ddmm(date.fromisoformat(in_since), now)}")
    if facts.get("last_set_days"):
        parts.append(f"last set {facts['last_set_days']} d")
    return parts


def battery_row(entity, device, model, st, value, facts, now):
    """(row, push key) for the device's open condition, or None; offline outranks low."""
    st = st or {}
    key_base = None if model["pushed_by"] else f"battery:{entity}"
    if value is None and "off_ep" in st:
        since = parse_iso(st.get("off_since"))
        start = since or parse_iso(st.get("off_seen")) or parse_iso(st["off_ep"])
        if now - start >= OFFLINE_GRACE:
            offline_only = model["low"] is None and model["drop"] is None
            parts = [f"no data since {ddmm(since, now)}" if since else "offline",
                     model["action"] if offline_only else None, model["cell"], *_age_parts(facts, now)]
            row = make_row(BATTERY_ID_PREFIX + entity, "maint", device["name"], fit_parts(parts),
                           icon=OFFLINE_ICON, area=device["area"], audience="admin")
            return row, (f"{key_base}:offline:{st['off_ep']}" if key_base else None)
    if "low_since" in st:
        parts = [model["action"]]
        eta = (facts or {}).get("eta_weeks")
        if eta and value is not None and value > ETA_LEVEL:
            parts.append(f"about {eta} week{'' if eta == 1 else 's'} to {ETA_LEVEL} %")
        parts += [model["cell"], *_age_parts(facts, now)]
        row = make_row(BATTERY_ID_PREFIX + entity, "maint", device["name"], fit_parts(parts),
                       icon=ACTION_ICONS[model["action"]], area=device["area"], audience="admin")
        return row, (f"{key_base}:low:{st['low_since']}" if key_base else None)
    return None


# ---------------------------------------------------------------- Rober2 and the washer drum


def step_rober2(st, seconds_left, now):
    """A part is due at zero seconds left; a count that jumps back up clears it only once it has
    held for HOLD, so a one-poll spike to the full count changes nothing."""
    st = dict(st or {})
    if seconds_left is None:
        return st
    if seconds_left <= 0:
        if "due_since" not in st:
            return {"due_since": utc_iso(now)}
        st.pop("clear_since", None)
        return st
    if "due_since" in st and _held(st, "clear_since", True, now):
        return {}
    return st


def rober2_rows(states, area):
    out = []
    for part, title, action in ROBER2_PARTS:
        since = (states.get(part) or {}).get("due_since")
        if since:
            row = make_row(f"maint:rober2:{part}", "maint", title, action, icon="mdi:robot-vacuum", area=area,
                           audience="admin")
            out.append((row, f"rober2:{part}:{since}"))
    return out


def drum_row(last_clean, now, area):
    """Miele's monthly Bomuld 90. Without a confirmed clean on record the row shows but is never
    pushed: missing evidence is not an overdue clean."""
    when = parse_iso(last_clean)
    if when is not None and now - when < DRUM_INTERVAL:
        return None
    detail = f"Bomuld 90 monthly, last {ddmm(when, now)}" if when else "Bomuld 90 monthly, last unknown"
    row = make_row("maint:washer:drum", "maint", "Washer drum clean", detail, icon="mdi:washing-machine",
                   area=area, audience="admin")
    return row, (f"washer:drum:{utc_iso(when)}" if when else None)


# ---------------------------------------------------------------- daily push


def digest(conditions, pushed):
    """One push for the open conditions not pushed yet in their episode: `conditions` is
    [(title, push key or None)] in row order."""
    fresh = [(title, key) for title, key in conditions if key and key not in pushed]
    if not fresh:
        return None
    names = [title for title, _ in fresh]
    message = ", ".join(names[:DIGEST_NAMES])
    if len(names) > DIGEST_NAMES:
        message += f" +{len(names) - DIGEST_NAMES}"
    return {"title": DIGEST_TITLE, "message": message, "keys": [key for _, key in fresh]}


def prune_pushed(pushed, now):
    return {key: at for key, at in (pushed or {}).items() if (parse_iso(at) or now) > now - PUSHED_KEEP}
