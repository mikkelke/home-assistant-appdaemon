"""Scenarios: the house timeline's rows and the house's own sessions narrowed into what was going on.

Pure and stdlib only, so HouseScenarios (live), scripts/scenario_replay.py (history, run inside the
Home Assistant container) and the unit tests all run the same rules.

A scenario is a dict:
    {"id", "type", "start", "end" (None while ongoing), "where", "who": [names], "members": [row ts],
     "headline", "summary", "facts": {...}}
plus "public_headline"/"public_summary" when a member only an admin sees would show in the headline.
`members` are the `ts` strings of the sensor.house_events rows the scenario stands for; the dashboard
hides those rows under the scenario, and voice leaves them out. A row belongs to at most one scenario.

Types:
- door: a ring at a street door, the street door opened (or not), the apartment door after it, and the
  delivery or resident it was for.
- night: house night mode on -> off, with the night and bedtime rows.
- machine: a washer, dryer or dishwasher run, start -> finished -> emptied; a dryer run that starts soon
  after a washer run finished joins it as one laundry scenario.
- cooking: the hob or oven drawing power, merged across short pauses, with who was in the kitchen.

Signals (all times epoch seconds, oldest first):
    {"night": [(t, "on"|"off")],
     "machines": {"washer": [(t, state)], "dryer": [...], "dish": [...]},
     "power": {"hob": [(t, watts)], "oven": [(t, watts)]},
     "areas": {"Mikkel": [(t, area)], ...}}
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

HOUSE_TZ = ZoneInfo("Europe/Copenhagen")

# Rings for the same street door this close after the previous one are the same visitor.
RING_FOLD_S = 120
# The apartment door opened this long after the last ring belongs to that visit.
APARTMENT_AFTER_S = 300
# A delivery that ended this long before or after the rings was the visit.
DELIVERY_BEFORE_S = 600
DELIVERY_AFTER_S = 900

RUN_STATES = ("Running", "Paused")
MIN_RUN_S = 300
FINISHED_AFTER_S = 600
FINISHED_BEFORE_S = 180
EMPTIED_WITHIN_S = 36 * 3600
LAUNDRY_GAP_S = 4 * 3600

POWER_THRESHOLDS = {"hob": 300.0, "oven": 200.0}
COOK_MERGE_S = 900
# A session counts once the hob and oven drew power this long in it (the hob cycles its zones on and off).
COOK_MIN_S = 300
COOK_ROW_MARGIN_S = 1800
KITCHEN_ZONE = ("Kitchen",)
RESIDENTS = ("Mikkel", "Claudia", "Kristine")
WHO_SHARE = 0.3

NIGHT_ROW_BEFORE_S = 600

MACHINES = {
    "washer": {"label": "Washer", "icon": "mdi:washing-machine"},
    "dryer": {"label": "Dryer", "icon": "mdi:tumble-dryer"},
    "dish": {"label": "Dishwasher", "icon": "mdi:dishwasher"},
}
DELIVERY_ICONS = ("mdi:moped", "mdi:cart", "mdi:package-variant-closed-check", "mdi:package-variant-closed")
COOK_ICONS = ("mdi:food-steak",)

RING = re.compile(r"^Someone rang the (.+)$")
OPENED_AFTER = re.compile(r"^Opened after (\d+) s$")
WOLT = re.compile(r"^Wolt order from (.+) delivered$")
KWH = re.compile(r"used (\d+(?:\.\d+)?) kWh")


def epoch(ts):
    """Epoch seconds of an ISO timestamp or an aware datetime (AppDaemon's get_history gives those), or None."""
    if isinstance(ts, datetime):
        return ts.timestamp()
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def iso(t):
    return None if t is None else datetime.fromtimestamp(t, HOUSE_TZ).isoformat(timespec="seconds")


def clock(t):
    return datetime.fromtimestamp(t, HOUSE_TZ).strftime("%H:%M")


def span(start, end):
    return f"since {clock(start)}" if end is None else f"{clock(start)}-{clock(end)}"


def duration(seconds):
    minutes = int(round(seconds / 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def is_public(row):
    return row.get("audience") != "admin" and not row.get("audience_users")


# ---------------------------------------------------------------------------
# Signals -> sessions
# ---------------------------------------------------------------------------


def state_sessions(points, active, min_s=0):
    """[(start, end|None)] of the spans `points` spend in `active` states; the last is open while still in one.
    Ended spans shorter than min_s are dropped."""
    out, start = [], None
    for t, state in points:
        if state in active and start is None:
            start = t
        elif state not in active and start is not None:
            if t - start >= min_s:
                out.append((start, t))
            start = None
    if start is not None:
        out.append((start, None))
    return out


def power_intervals(points, threshold):
    """[(start, end|None)] of the spans a power reading stays above threshold. A reading holds until the next."""
    out, start = [], None
    for t, value in points:
        try:
            above = float(value) > threshold
        except (TypeError, ValueError):
            above = False
        if above and start is None:
            start = t
        elif not above and start is not None:
            out.append((start, t))
            start = None
    if start is not None:
        out.append((start, None))
    return out


def cooking_sessions(power):
    """[(start, end|None, {source: seconds})]: hob and oven spans merged across pauses of COOK_MERGE_S, at least
    COOK_MIN_S long once ended."""
    spans = sorted(
        (start, end, source)
        for source, points in power.items()
        if source in POWER_THRESHOLDS
        for start, end in power_intervals(points, POWER_THRESHOLDS[source])
    )
    sessions = []
    for start, end, source in spans:
        last = sessions[-1] if sessions else None
        if last is not None and (last["end"] is None or start - last["end"] <= COOK_MERGE_S):
            last["end"] = None if last["end"] is None or end is None else max(last["end"], end)
        else:
            last = {"start": start, "end": end, "on": {}}
            sessions.append(last)
        if end is not None:
            last["on"][source] = last["on"].get(source, 0) + (end - start)
        else:
            last["on"].setdefault(source, 0)
    return [(s["start"], s["end"], s["on"]) for s in sessions if s["end"] is None or sum(s["on"].values()) >= COOK_MIN_S]


def present(areas, start, end, zone, share=WHO_SHARE):
    """Names whose area is in `zone` for at least `share` of [start, end], in config order."""
    if end <= start:
        return []
    names = []
    for name, points in areas.items():
        inside, area, last_t = 0.0, None, start
        for t, value in points:
            if t <= start:
                area = value
                continue
            if t >= end:
                break
            if area in zone:
                inside += t - last_t
            area, last_t = value, t
        if area in zone:
            inside += end - last_t
        if inside / (end - start) >= share:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# Rows + sessions -> scenarios
# ---------------------------------------------------------------------------


def _row_time(row):
    return epoch(row.get("ts"))


def _scenario(kind, start, end, members, headline, summary, who=(), where=None, facts=None):
    return {
        "id": f"{kind}:{int(start)}",
        "type": kind,
        "start": iso(start),
        "end": iso(end),
        "where": where,
        "who": list(who),
        "members": [row["ts"] for row in members],
        "headline": headline,
        "summary": summary,
        "facts": facts or {},
    }


def _ring_label(row):
    match = RING.match(row.get("cause") or "") or RING.match(row.get("text") or "")
    return match.group(1) if match else None


def _delivery_headline(row):
    text = row.get("text") or ""
    wolt = WOLT.match(text)
    if wolt:
        return f"Wolt delivery from {wolt.group(1)}"
    return text


def door_scenarios(rows, owned):
    visits = []
    for row in rows:
        label = _ring_label(row)
        if label is None or row["ts"] in owned:
            continue
        t = _row_time(row)
        open_visit = next((v for v in reversed(visits) if v["label"] == label), None)
        if open_visit and t - open_visit["last"] <= RING_FOLD_S:
            open_visit["rings"].append(row)
            open_visit["last"] = t
        else:
            visits.append({"label": label, "first": t, "last": t, "rings": [row]})
    out = []
    for visit in visits:
        members = list(visit["rings"])
        owned.update(r["ts"] for r in members)
        apartment = next(
            (
                r for r in rows
                if r["ts"] not in owned
                and visit["first"] < _row_time(r) <= visit["last"] + APARTMENT_AFTER_S
                and (
                    (r.get("icon") == "mdi:lock-open-variant" and (r.get("text") or "").startswith("Apartment door unlocked"))
                    or r.get("text") == "Came home"
                )
            ),
            None,
        )
        deliveries = [
            r for r in rows
            if r["ts"] not in owned
            and r.get("icon") in DELIVERY_ICONS
            and visit["first"] - DELIVERY_BEFORE_S <= _row_time(r) <= visit["last"] + DELIVERY_AFTER_S
        ]
        for extra in ([apartment] if apartment else []) + deliveries:
            members.append(extra)
            owned.add(extra["ts"])
        out.append(_door(visit, apartment, deliveries, members))
    return out


def _door(visit, apartment, deliveries, members):
    label = visit["label"]
    opened_after = None
    opened = failed = unanswered = False
    for ring in visit["rings"]:
        effect = ring.get("effect") or ""
        match = OPENED_AFTER.match(effect)
        if match:
            opened, opened_after = True, int(match.group(1))
        elif effect == "The door was opened for them":
            opened = True
        elif ring.get("icon") == "mdi:alert-circle":
            failed = True
        elif ring.get("icon") == "mdi:bell-off":
            unanswered = True
    if opened:
        street = f"opened after {opened_after} s" if opened_after is not None else "opened"
    elif failed:
        street = "auto-open failed"
    elif unanswered:
        street = "nobody answered"
    else:
        street = "not opened"
    parts = [f"Rang the {label} {clock(visit['first'])}", street]
    who = []
    if apartment:
        t = _row_time(apartment)
        parts.append(f"apartment door +{int(t - visit['first'])} s")
        if apartment.get("by") and is_public(apartment):
            who.append(apartment["by"])
    public_parts = list(parts)
    public_delivery = [d for d in deliveries if is_public(d)]
    let_in = None
    if who:
        let_in = f"{who[0]} came home" if who[0] in RESIDENTS else f"{who[0]} came in"
    fallback = let_in or f"Someone rang the {label}"
    headline = _delivery_headline(deliveries[0]) if deliveries else fallback
    public_headline = _delivery_headline(public_delivery[0]) if public_delivery else fallback
    scenario = _scenario(
        "door",
        visit["first"],
        max(_row_time(m) for m in members),
        members,
        headline,
        " · ".join(parts),
        who=who,
        where=label,
        facts={"door": label, "street": street, "opened_after_s": opened_after,
               "apartment_at": apartment["ts"] if apartment else None,
               "delivery": deliveries[0]["text"] if deliveries else None},
    )
    if public_headline != headline:
        scenario["public_headline"] = public_headline
        scenario["public_summary"] = " · ".join(public_parts)
    return scenario


def night_scenarios(rows, signals, owned, now):
    out = []
    for start, end in state_sessions(signals.get("night", []), ("on",)):
        stop = end if end is not None else now
        members = [
            r for r in rows
            if r["ts"] not in owned
            and start - NIGHT_ROW_BEFORE_S <= _row_time(r) <= stop
            and (
                (r.get("icon") == "mdi:weather-night" and r.get("text") == "Night mode on")
                or (r.get("icon") == "mdi:bed" and r.get("text") == "Went to bed")
            )
        ]
        owned.update(r["ts"] for r in members)
        out.append(_scenario("night", start, end, members, "Night", span(start, end), where="home"))
    return out


def machine_scenarios(rows, signals, owned, now):
    runs = []
    for kind, points in (signals.get("machines") or {}).items():
        if kind not in MACHINES:
            continue
        sessions = state_sessions(points, RUN_STATES, MIN_RUN_S)
        for index, (start, end) in enumerate(sessions):
            following = sessions[index + 1][0] if index + 1 < len(sessions) else None
            runs.append({"kind": kind, "start": start, "end": end, "next": following})
    runs.sort(key=lambda run: run["start"])
    for run in runs:
        info = MACHINES[run["kind"]]
        run["finished"] = run["emptied"] = None
        if run["end"] is None:
            continue
        run["finished"] = next(
            (
                r for r in rows
                if r["ts"] not in owned and r.get("icon") == info["icon"]
                and " finished" in (r.get("text") or "")
                and run["end"] - FINISHED_BEFORE_S <= _row_time(r) <= run["end"] + FINISHED_AFTER_S
            ),
            None,
        )
        if run["finished"]:
            owned.add(run["finished"]["ts"])
        limit = min(run["next"] or float("inf"), run["end"] + EMPTIED_WITHIN_S)
        run["emptied"] = next(
            (
                r for r in rows
                if r["ts"] not in owned and r.get("icon") == "mdi:basket-check"
                and r.get("text") == f"{info['label']} emptied"
                and run["end"] <= _row_time(r) <= limit
            ),
            None,
        )
        if run["emptied"]:
            owned.add(run["emptied"]["ts"])
    groups, used = [], set()
    for index, run in enumerate(runs):
        if index in used:
            continue
        group = [run]
        if run["kind"] == "washer" and run["end"] is not None:
            for other_index, other in enumerate(runs):
                if (
                    other_index not in used and other["kind"] == "dryer"
                    and run["end"] <= other["start"] <= run["end"] + LAUNDRY_GAP_S
                    and (run["next"] is None or other["start"] < run["next"])
                ):
                    group.append(other)
                    used.add(other_index)
                    break
        used.add(index)
        groups.append(group)
    return [_machine(group, now) for group in groups]


def _machine(group, now):
    start = group[0]["start"]
    end = None if any(run["end"] is None for run in group) else max(run["end"] for run in group)
    members = [row for run in group for row in (run["finished"], run["emptied"]) if row]
    labels = [MACHINES[run["kind"]]["label"] for run in group]
    headline = "Laundry" if len(group) > 1 else labels[0]
    kwh = 0.0
    has_kwh = False
    started_by = emptied_by = emptied_at = None
    for run in group:
        finished = run["finished"]
        if finished:
            match = KWH.search(finished.get("text") or "")
            if match:
                kwh += float(match.group(1))
                has_kwh = True
            if finished.get("by") and is_public(finished) and started_by is None:
                started_by = finished["by"]
        if run["emptied"]:
            emptied_at = run["emptied"]["ts"]
            if run["emptied"].get("by") and is_public(run["emptied"]):
                emptied_by = run["emptied"]["by"]
    if end is None:
        parts = [f"running since {clock(start)}"]
    else:
        parts = [span(start, end)]
        if len(group) > 1:
            parts.append(" then ".join(f"{label.lower()} {span(run['start'], run['end'])}" for label, run in zip(labels, group)))
        if has_kwh:
            parts.append(f"{kwh:.2f} kWh")
        if emptied_at:
            parts.append(f"emptied {clock(epoch(emptied_at))}" + (f" by {emptied_by}" if emptied_by else ""))
        else:
            parts.append("not emptied yet")
    who = [name for name in (started_by, emptied_by) if name]
    return _scenario(
        "machine",
        start,
        end,
        members,
        headline,
        " · ".join(parts),
        who=list(dict.fromkeys(who)),
        where=None,
        facts={"machines": [run["kind"] for run in group], "kwh": round(kwh, 2) if has_kwh else None,
               "started_by": started_by, "emptied_by": emptied_by, "emptied_at": emptied_at,
               "runs": [[iso(run["start"]), iso(run["end"])] for run in group]},
    )


def cooking_scenarios(rows, signals, owned, now):
    out = []
    for start, end, on in cooking_sessions(signals.get("power") or {}):
        stop = end if end is not None else now
        members = [
            r for r in rows
            if r["ts"] not in owned and r.get("icon") in COOK_ICONS
            and start - COOK_ROW_MARGIN_S <= _row_time(r) <= stop + COOK_ROW_MARGIN_S
        ]
        owned.update(r["ts"] for r in members)
        who = present(signals.get("areas") or {}, start, stop, KITCHEN_ZONE)
        used = [f"{source} {duration(seconds)}" for source, seconds in sorted(on.items()) if seconds]
        summary = " · ".join([span(start, end)] + used)
        out.append(_scenario("cooking", start, end, members, "Cooking", summary, who=who, where="Kitchen",
                             facts={"sources": sorted(on), "on_s": {k: int(v) for k, v in on.items()}}))
    return out


def build_scenarios(rows, signals, now):
    """Scenarios, oldest first. `rows` are sensor.house_events rows (any order, each with a `ts`)."""
    clean = sorted((r for r in rows if isinstance(r, dict) and _row_time(r) is not None), key=_row_time)
    owned = set()
    scenarios = door_scenarios(clean, owned)
    scenarios += machine_scenarios(clean, signals, owned, now)
    scenarios += night_scenarios(clean, signals, owned, now)
    scenarios += cooking_scenarios(clean, signals, owned, now)
    return sorted(scenarios, key=lambda s: epoch(s["start"]))


def recent(scenarios, now, keep_s, limit):
    """The scenarios still ongoing or ended within keep_s, newest `limit` of them, oldest first."""
    kept = [s for s in scenarios if s["end"] is None or epoch(s["end"]) >= now - keep_s]
    return kept[-limit:]
