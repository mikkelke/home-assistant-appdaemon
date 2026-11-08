import json
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from cook_fsm import PRESETS

HOUSE_TZ = ZoneInfo("Europe/Copenhagen")
CAPS = {"cook_state": 6144, "cook_live": 6144, "cook_log": 6144}
NAMES = {"cook_state": ("Cook state", "mdi:chef-hat"),
         "cook_live": ("Cook live", "mdi:chart-areaspline"),
         "cook_log": ("Cook log", "mdi:history")}
STEP_S = 30
LIVE_S = 4 * 60 * 60
LOG_FIELDS = ("id", "kind", "start", "end", "outcome", "target", "minutes", "preset", "serve", "pull",
              "pulled_core", "peak")


def num(value, digits=0):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    value = round(value, digits)
    return str(int(value)) if digits == 0 or value == int(value) else f"{value:.{digits}f}".rstrip("0")


def size(attrs):
    return len(json.dumps(attrs, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def leaves_ok(value):
    if isinstance(value, str):
        return True
    return isinstance(value, list) and all(
        isinstance(item, str) or (isinstance(item, dict) and all(isinstance(v, str) for v in item.values()))
        for item in value)


def finish(entity_key, attrs):
    name, icon = NAMES[entity_key]
    return {"v": "1", **{k: v for k, v in attrs.items() if v is not None},
            "friendly_name": name, "icon": icon}


def epoch(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            result = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None
    return result if math.isfinite(result) else None


def iso(value):
    at = epoch(value)
    if at is None:
        return None
    try:
        return datetime.fromtimestamp(at, HOUSE_TZ).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def _clock(at):
    local = iso(at)
    return local[11:16] if local else None


def _label(prefix, at, approximate=False):
    clock = _clock(at)
    return f"{prefix} {'≈ ' if approximate else ''}{clock}" if clock else prefix


def _moment(bath, probe, now):
    if bath.get("low_water"):
        return "low_water", bath.get("_low_since"), "Low water · top up"
    if probe.get("lost"):
        since = probe.get("stale_since")
        return "lost", since, _label("No reading since", since)
    moments = []

    def add(priority, kind, at, label):
        moments.append((priority, epoch(at) if epoch(at) is not None else float("inf"), kind, at, label))

    phase = bath.get("phase", "off")
    if phase == "too_hot":
        excess = None
        if num(bath.get("temp"), 1) is not None and num(bath.get("target"), 1) is not None:
            excess = float(bath["temp"]) - float(bath["target"])
        add(2, "bath_ready", None,
            f"+{excess:.1f}° · add cold" if excess is not None else "Add cold water")
    elif phase == "ready":
        add(2, "bath_ready", bath.get("ready_at"), "Bag in · bath ready")
    elif phase == "done":
        add(2, "bath_done", bath.get("done_at"), _label("Done", bath.get("done_at")))
    elif phase == "chilled":
        add(2, "chill_done", bath.get("chill_until"), "Into the fridge")
    elif phase == "interrupted":
        add(2, "", bath.get("stopped_at"), _label("Stopped", bath.get("stopped_at")))
    elif phase == "heating":
        add(3, "bath_ready", bath.get("ready_at"), _label("Ready", bath.get("ready_at"), True))
    elif phase == "cooking":
        add(3, "bath_done", bath.get("done_at"), _label("Done", bath.get("done_at")))
    elif phase == "chilling":
        add(3, "chill_done", bath.get("chill_until"), _label("Fridge", bath.get("chill_until")))

    phase = probe.get("phase", "off")
    if phase == "pull":
        at = probe.get("_pull_at")
        add(1, "pull", at, _label("Pull", at))
    elif phase == "carve":
        at = probe.get("_carved_at", probe.get("carve_at"))
        add(1, "carve", at, _label("Carve", at))
    elif phase == "resting":
        add(3, "carve", probe.get("carve_at"), _label("Carve", probe.get("carve_at"), True))
    elif phase in ("tracking", "near"):
        add(3, "pull", probe.get("eta"), _label("Pull", probe.get("eta"), True))
    if not moments:
        return "", None, ""
    _, _, kind, at, label = min(moments, key=lambda moment: moment[:2])
    return kind, at, label


def state_entity(bath, probe, now):
    bath, probe = bath or {}, probe or {}
    bath_active = bath.get("phase", "off") != "off" or bool(bath.get("low_water"))
    probe_active = probe.get("phase", "off") != "off"
    state = "both" if bath_active and probe_active else "bath" if bath_active else "probe" if probe_active else "off"
    kind, at, label = _moment(bath, probe, now)
    attrs = {"bath_phase": bath.get("phase", "off"), "probe_phase": probe.get("phase", "off"),
             "next_kind": kind, "next_at": iso(at), "next_label": label,
             "presets": [{k: str(row[k]) if k == "key" else num(row[k], 1)
                          for k in ("key", "serve", "pull", "rest")} for row in PRESETS]}
    for lane_name, lane, times, numbers in (
        ("bath", bath, ("ready_at", "in_at", "done_at", "chill_until", "stopped_at"),
         ("temp", "target", "minutes", "last_target", "last_minutes", "rate")),
        ("probe", probe, ("eta", "pulled_at", "carve_at", "stale_since"),
         ("serve", "pull", "core", "ambient", "core_index", "slope", "pulled_core", "peak")),
    ):
        for key in times:
            attrs[f"{lane_name}_{key}"] = iso(lane.get(key))
        for key in numbers:
            digits = 3 if key in ("rate", "slope") else 0 if key == "core_index" else 1
            attrs[f"{lane_name}_{key}"] = num(lane.get(key), digits)
    attrs["bath_low_water"] = "1" if bath.get("low_water") else None
    attrs["probe_in_meat"] = "1" if probe.get("in_meat") else None
    attrs["probe_preset"] = probe.get("preset")
    if probe.get("needle"):
        values = list(probe["needle"][:4])
        values.extend([None] * (4 - len(values)))
        attrs["probe_needle"] = _csv(values)
    return state, finish("cook_state", attrs)


def _csv(values):
    return ",".join((num(float(value) * 10) or "") if num(value, 6) is not None else "" for value in values)


def live_entity(samples, pulled_at=None):
    rows = [sample for sample in samples if epoch(sample.get("at")) is not None]
    rows.sort(key=lambda sample: epoch(sample["at"]))
    if rows:
        cutoff = epoch(rows[-1]["at"]) - LIVE_S
        rows = [sample for sample in rows if epoch(sample["at"]) >= cutoff]
    while True:
        attrs = {"step": str(STEP_S), "bath": _csv([row.get("bath") for row in rows]),
                 "core": _csv([row.get("core") for row in rows]),
                 "ambient": _csv([row.get("ambient") for row in rows])}
        if rows:
            attrs["t0"] = iso(rows[0]["at"])
            pulled = epoch(pulled_at)
            first, last = epoch(rows[0]["at"]), epoch(rows[-1]["at"])
            if pulled is not None and first <= pulled < last + STEP_S:
                attrs["pull_mark"] = str(min(len(rows) - 1, int((pulled - first) // STEP_S)))
        out = finish("cook_live", attrs)
        if size(out) <= CAPS["cook_live"] or not rows:
            return str(len(rows)), out
        rows = rows[1:]


def _record_row(record):
    out = {}
    for key in LOG_FIELDS:
        value = record.get(key)
        if value is None or isinstance(value, (dict, list, tuple)):
            continue
        if key in ("start", "end") or key.endswith("_at") or key == "chill_until":
            text = iso(value)
        elif isinstance(value, bool):
            text = "1" if value else "0"
        elif isinstance(value, (float, int)):
            text = num(value, 3)
        else:
            text = str(value)
        if text is not None:
            out[key] = text
    return out


def log_entity(records):
    ordered = sorted(records, key=lambda record: epoch(record.get("start")) or 0, reverse=True)
    rows = [_record_row(record) for record in ordered[:20]]
    while True:
        attrs = finish("cook_log", {"cooks": rows})
        if size(attrs) <= CAPS["cook_log"] or not rows:
            return str(len(rows)), attrs
        rows = rows[:-1]
