"""Maintenance rows for sensor.house_upcoming: the Rober2 parts, stick vacuum faults and the battery
inventory (the appliances' run-based rows are upcoming_runs). Pure: each step takes the persisted
state and `now` and returns new state.

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

A device with a replace-by day (upcoming_battery_age) that has not passed gets a row timed at
DUE_TIME that day, low condition or not; offline rows stay untimed. A day from the model's median
lifetime reads as approximate.

Every condition carries a push key that is unique per episode; `pushed_by` models belong to another
app's notifications and have none. A replace-by day alone is never pushed.

A Rober2 part is due once it has no cleaning time left; that row carries the part's action and is
pushed once per episode. Before that the part's row is timed at DUE_TIME on the day its cleaning time
left runs out at the robot's recent cleaning rate. It reads as an estimate (~), is never pushed, and
a part further out than ROBER2_HORIZON_DAYS has none.

Stick vacuum faults come from sensor.stick_vacuum. A clog's push waits for its session to close;
not charging is pushed unless its source flags the episode as row only. Both share their episode
keys with the daily digest.
"""

import math
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from upcoming_core import BATTERY_ID_PREFIX, TEXT_MAX, make_row, utc_iso
from upcoming_guides import GUIDES

HOUSE_TZ = ZoneInfo("Europe/Copenhagen")
HOLD = timedelta(hours=2)
OFFLINE_GRACE = timedelta(hours=24)
CHARGE_MARGIN = 10
DEFAULT_RISE = 30
LOW_DAYS_KEEP = 7
DUE_TIME = time(9, 0)
PUSHED_KEEP = timedelta(days=180)

ACTIONS = ("replace", "recharge", "charge")
ACTION_ICONS = {
    "replace": "mdi:battery-alert-variant-outline",
    "recharge": "mdi:battery-sync-outline",
    "charge": "mdi:battery-charging-low",
}
OFFLINE_ICON = "mdi:lan-disconnect"
DUE_ICON = "mdi:battery-clock-outline"

ROBER2_PARTS = (
    ("filter", "Rober2 filter", "replace"),
    ("main_brush", "Rober2 main brush", "replace"),
    ("side_brush", "Rober2 side brush", "replace"),
    ("sensor", "Rober2 sensors", "clean"),
)
ROBER2_ICON = "mdi:robot-vacuum"
ROBER2_RATE_WINDOW = timedelta(days=30)
ROBER2_RATE_MIN_SPAN = timedelta(days=7)
ROBER2_RATE_DEFAULT = 2900.0
ROBER2_HORIZON_DAYS = 366

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
    """Joins the non-empty parts, most important first, with ", ", skipping any part that would
    take the text past `limit`."""
    text = ""
    for part in parts:
        if not part:
            continue
        candidate = f"{text}, {part}" if text else part
        if len(candidate) <= limit:
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
            "empty": float(cfg.get("empty", 0)),
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


def _replace_by(facts, model, now):
    """(day, approximate) of the device's replace-by day while it has not passed, else None."""
    facts = facts or {}
    try:
        day = date.fromisoformat(facts.get("replace_by") or "")
    except (TypeError, ValueError):
        return None
    if model["action"] is None or day < house_date(now):
        return None
    return day, facts.get("basis") == "model"


def _due_text(action, day, approximate, now):
    left = (day - house_date(now)).days
    when = "today" if left == 0 else f"{left} day{'' if left == 1 else 's'}"
    return f"{action} by {'~' if approximate else ''}{ddmm(day, now)} - {when}"


def battery_title(name, cell):
    count = re.match(r"(\d+)x", cell)
    return f"{name} {'batteries' if count and int(count.group(1)) > 1 else 'battery'}"


def battery_row(entity, device, model, st, value, facts, now):
    """(row, push key) for the device, or None: an open condition, else its replace-by day. Offline
    outranks low; a low row is timed at the replace-by day when there is one."""
    st = st or {}
    key_base = None if model["pushed_by"] else f"battery:{entity}"
    if value is None and "off_ep" in st:
        since = parse_iso(st.get("off_since"))
        start = since or parse_iso(st.get("off_seen")) or parse_iso(st["off_ep"])
        if now - start >= OFFLINE_GRACE:
            offline_only = model["low"] is None and model["drop"] is None
            parts = [f"no data since {ddmm(since, now)}" if since else "offline",
                     model["action"] if offline_only else None, *_age_parts(facts, now), model["cell"]]
            row = make_row(BATTERY_ID_PREFIX + entity, "maint", device["name"], fit_parts(parts),
                           icon=OFFLINE_ICON, area=device["area"], audience="admin")
            return row, (f"{key_base}:offline:{st['off_ep']}" if key_base else None)
    due = _replace_by(facts, model, now)
    low = "low_since" in st
    if not low and due is None:
        return None
    parts = [_due_text(model["action"], *due, now) if due else model["action"], model["cell"], *_age_parts(facts, now)]
    row = make_row(BATTERY_ID_PREFIX + entity, "maint", battery_title(device["name"], model["cell"]), fit_parts(parts),
                   at=datetime.combine(due[0], DUE_TIME, HOUSE_TZ) if due else None,
                   icon=ACTION_ICONS[model["action"]] if low else DUE_ICON, area=device["area"], audience="admin")
    return row, (f"{key_base}:low:{st['low_since']}" if low and key_base else None)


# ---------------------------------------------------------------- Rober2


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


def rober2_rate(series, now):
    """Seconds of cleaning a day over the last ROBER2_RATE_WINDOW, from the robot's cumulative
    cleaning time [(aware datetime, seconds or None)], oldest first, as the recorder holds it for that
    window. A count that falls back adds nothing. ROBER2_RATE_DEFAULT when the readings cover less
    than ROBER2_RATE_MIN_SPAN or show no cleaning."""
    counts = [(when, value) for when, value in series if value is not None]
    cleaned = sum(max(0.0, after - before) for (_, before), (_, after) in zip(counts, counts[1:]))
    span = min(now - counts[0][0], ROBER2_RATE_WINDOW) if counts else timedelta(0)
    if cleaned <= 0 or span < ROBER2_RATE_MIN_SPAN:
        return ROBER2_RATE_DEFAULT
    return cleaned / (span / timedelta(days=1))


def rober2_day(seconds_left, rate, now):
    """The house day `seconds_left` of cleaning time runs out at `rate` seconds of cleaning a day, or
    None without a positive count, or when that day is further out than ROBER2_HORIZON_DAYS."""
    if seconds_left is None or seconds_left <= 0:
        return None
    days = seconds_left / rate
    return house_date(now + timedelta(days=days)) if days <= ROBER2_HORIZON_DAYS else None


def rober2_rows(states, area, left, rate, now):
    """[(row, push key)] for the Rober2 parts, at most one row per part: its due row, else one dated by
    the cleaning time left in `left` (seconds by part) at `rate` seconds a day. Both share the part's
    row id, so the due row takes over from the estimate; only the due row has a push key."""
    out = []
    for part, title, action in ROBER2_PARTS:
        since = (states.get(part) or {}).get("due_since")
        if since:
            detail, at, key = action, None, f"rober2:{part}:{since}"
        else:
            day = rober2_day(left.get(part), rate, now)
            if day is None:
                continue
            detail, at, key = _due_text(action, day, True, now), datetime.combine(day, DUE_TIME, HOUSE_TZ), None
        guide = GUIDES[f"rober2:{part}"]
        row = make_row(f"maint:rober2:{part}", "maint", title, detail, at=at, icon=ROBER2_ICON, area=area,
                       audience="admin", steps=guide.steps, warn=guide.warn)
        out.append((row, key))
    return out


# ---------------------------------------------------------------- stick vacuum


def stick_problems(full):
    attributes = (full or {}).get("attributes") or {}
    return {part.strip() for part in str(attributes.get("problems") or "").split(",") if part.strip()}


def stick_rows(full, area):
    """[(row, push key)] for the semantic stick vacuum faults. Source timestamps identify episodes."""
    attrs = (full or {}).get("attributes") or {}
    problems = stick_problems(full)
    out = []
    if "clogged" in problems:
        guide = GUIDES["stick:clogged"]
        row = make_row("maint:stick:clogged", "maint", "Stick vacuum air path clogged", "check tube, nozzle + inlet",
                       icon="mdi:vacuum", area=area, audience="admin", steps=guide.steps, warn=guide.warn)
        since = parse_iso(attrs.get("clogged_since"))
        key = f"stick:clogged:{utc_iso(since)}" if since and attrs.get("session_open") == "no" else None
        out.append((row, key))
    if "not_charging" in problems:
        since = parse_iso(attrs.get("not_charging_since"))
        clock = since.astimezone(HOUSE_TZ).strftime("%H:%M") if since else "unknown"
        row = make_row("maint:stick:not_charging", "maint", "Stick vacuum not charging",
                       f"since {clock} - check it is on the mount", icon="mdi:vacuum", area=area, audience="admin")
        key = f"stick:not_charging:{utc_iso(since)}" if since and attrs.get("not_charging_push") != "no" else None
        out.append((row, key))
    return out


def stick_push_text(row):
    return f"{row['title']} {row['detail']}" if row["id"] == "maint:stick:not_charging" else row["title"]


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
