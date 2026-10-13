"""Pure model behind RunProfiles (run_profiles.py): turns finished appliance cycles into
per-programme minute energy profiles for sensor.appliance_run_profiles. Stdlib only, no
AppDaemon, so every rule here is unit-testable.

Cycle boundary: the monitor's recorded cycle_start_time. For washer and dryer the record
implies its own start (programme end minus duration_min) and only a recorded start within
START_TOLERANCE_S of it is used; the dishwasher takes the latest recorded start.

Time origin: profile minute 0 is the power onset, the start of the activity burst that is
still running at the cycle boundary. Activity that stopped before the boundary (a previous
cycle's anti-crease tumble, a delay-start drum pulse) is never an onset.

Energy window: onset to the programme end. Washer and dryer end at cycle_start_time +
duration_min (anti-crease tumbles while the door stays shut are not the programme); the
dishwasher ends at the record ts, the machine's end including the passive-drying tail. The
window's energy must match the record's energy_kwh within ENERGY_TOLERANCE.

Units: each profile value is the energy used in that minute in dWh (0.1 Wh), so
kWh = sum(values) / 10000.

Two durations per programme:
  run_min   length of the energy profile (minutes after onset that carry energy).
  done_min  conservative physical completion, onset -> machine really done: the 90th
            percentile of trusted finishes. Washer/dryer finish = cycle_start_time +
            duration_min. Dishwasher duration_min excludes the passive-drying tail, so its
            finish is the record ts of a machine-driven end (end_reason low_power_detected,
            which the monitor defers to the end of the drying tail). A finish earlier than
            the point where the cycle had used 95 % of its energy is not trusted. Omitted
            when fewer than three trusted finishes exist.

Transport: AppDaemon 4.5.13 drops None/False/0/0.0 anywhere in an attribute tree and turns
True into "true", so profiles travel as comma-separated strings and every other published
value is a non-zero number or a non-empty string (see transport_safe()).
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

EXTRACTION_VERSION = 1
SCHEMA = 1
UNIT = "dWh/min"
TYPICAL = "typical"
APPLIANCES = ("washer", "dishwasher", "dryer")

MIN_RECORD_KWH = 0.1
ENERGY_TOLERANCE = 0.10
MAX_UNAVAILABLE_FRACTION = 0.05

ONSET_BEFORE_S = 600
ONSET_AFTER_S = 900
ONSET_QUIET_S = 120
STATE_LOOKBACK_S = 3 * 3600
START_TOLERANCE_S = 600
POWER_PAD_BEFORE_S = ONSET_BEFORE_S + 60
POWER_PAD_AFTER_S = 120
MAX_CYCLE_S = 12 * 3600

MAX_PER_KEY = 30
MIN_CYCLES = 5
IQR_K = 1.5
ACTIVE_FRACTION = 0.99
TAIL_FRACTION = 0.999
COMPLETION_TRUST_FRACTION = 0.95
COMPLETION_PERCENTILE = 0.9
MIN_COMPLETIONS = 3
USE_WINDOW_DAYS = 90
MAX_PROGRAMMES = 7
MAX_PAYLOAD_BYTES = 8192

_MISSING_STATES = ("unknown", "unavailable", "none", "")


# ---------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------

def parse_epoch(value):
    """Epoch seconds from a datetime or an ISO string ("Z" and naive mean UTC); else None."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def to_watts(state):
    """Numeric power reading, or None for unknown/unavailable/non-numeric."""
    if state is None or (isinstance(state, str) and state.strip().lower() in _MISSING_STATES):
        return None
    try:
        w = float(state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(w):
        return None
    return max(0.0, w)


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _row_time(row):
    return parse_epoch(row.get("last_updated") or row.get("last_changed"))


def flatten_history(result):
    """HA history is a list with one list of rows per entity; one entity is asked at a time."""
    if isinstance(result, list) and result and isinstance(result[0], list):
        return result[0]
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------------------
# Record-level rules (no history needed)
# ---------------------------------------------------------------------------------------

def record_ts(rec):
    return parse_epoch(rec.get("ts"))


def record_eligibility(rec):
    """None when the record may feed a profile, else the reason it may not."""
    if record_ts(rec) is None:
        return "bad_ts"
    energy = _num(rec.get("energy_kwh"))
    if energy is None:
        return "no_energy"
    if energy < MIN_RECORD_KWH:
        return "low_energy"
    duration = _num(rec.get("duration_min"))
    if duration is None or duration <= 0:
        return "no_duration"
    if rec.get("completion_class") == "interrupted":
        return "interrupted"
    options = rec.get("selected_options")
    if isinstance(options, dict) and str(options.get("soak", "")).lower() == "on":
        return "soak"
    return None


def programme_identity(appliance, rec):
    """The monitors' own learning identity for a record, or None when unknown.

    washer:     "<programme>|<temperature>" when a temperature is stored, else the
                programme (the washer monitor stores a temperature only for programmes that
                have a temperature dimension, washer_profiles.learn_key_for).
    dishwasher: eco/quick with the short option become eco_short/quick_short
                (DishwasherMonitor._learn_key).
    dryer:      the flat programme key.
    """
    prog = rec.get("confirmed")
    if not isinstance(prog, str) or not prog.strip() or prog.strip() == "unknown":
        return None
    prog = prog.strip()
    if appliance == "washer":
        temp = rec.get("confirmed_temperature")
        temp = str(temp).replace("°C", "").replace("°", "").strip() if temp is not None else ""
        return f"{prog}|{temp}" if temp else prog
    if appliance == "dishwasher" and rec.get("short") is True and prog in ("eco", "quick"):
        return f"{prog}_short"
    return prog


def human_confirmed(rec):
    return bool(rec.get("programme_confirmed_by_human") or rec.get("programme_user_confirmed"))


def public_key(identity):
    return identity.replace("|", "_")


def programme_label(appliance, identity, base_labels=None):
    """Display label from the programmes yaml labels (base_labels: programme -> label)."""
    base_labels = base_labels or {}
    if identity == TYPICAL:
        return "Typical"
    prog, _, temp = identity.partition("|")
    short = appliance == "dishwasher" and prog.endswith("_short")
    if short:
        prog = prog[: -len("_short")]
    label = base_labels.get(prog) or prog.replace("__", " - ").replace("_", " ").capitalize()
    if temp:
        label = f"{label} {temp}°C" if temp.isdigit() else f"{label} {temp}"
    if short:
        label = f"{label} short"
    return label


def extraction_fingerprint(rec, source):
    """Identity of one cached extraction: the record fields and source settings that decide
    which history is read and how it is integrated, plus the extraction version."""
    blob = json.dumps(
        [EXTRACTION_VERSION, source] + [rec.get(k) for k in ("ts", "duration_min", "idle_min", "effective_end_at")],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def state_window(rec):
    """History window for the state entity: covers the cycle start with slack."""
    ts = record_ts(rec)
    span = (_num(rec.get("duration_min")) or 0.0) + (_num(rec.get("idle_min")) or 0.0)
    return ts - span * 60.0 - STATE_LOOKBACK_S, ts


def power_window(cycle_start, ts):
    return cycle_start - POWER_PAD_BEFORE_S, ts + POWER_PAD_AFTER_S


# ---------------------------------------------------------------------------------------
# History-level extraction
# ---------------------------------------------------------------------------------------

def expected_start(appliance, rec):
    """Start implied by the record itself (epoch), or None for the dishwasher, whose
    duration_min excludes the drying tail. Washer/dryer: programme end (effective_end_at,
    else ts - idle_min) minus duration_min."""
    if appliance == "dishwasher":
        return None
    duration = _num(rec.get("duration_min"))
    if duration is None:
        return None
    end = parse_epoch(rec.get("effective_end_at"))
    if end is None:
        end = record_ts(rec) - (_num(rec.get("idle_min")) or 0.0) * 60.0
    return end - duration * 60.0


def find_cycle_start(state_rows, ts, expected=None, tolerance_s=START_TOLERANCE_S):
    """cycle_start_time (epoch) of the record's cycle from the state entity's history.

    Without an expected start: the value on the latest row at/before ts. With one: the
    recorded value nearest to it, and only within tolerance_s, so a start that a monitor
    rewrote mid-cycle is not trusted over the record's own timing."""
    latest_t, latest_cst = None, None
    candidates = set()
    for row in state_rows or []:
        t = _row_time(row)
        if t is None or t > ts + 1:
            continue
        cst = parse_epoch((row.get("attributes") or {}).get("cycle_start_time"))
        if cst is None or cst > ts:
            continue
        candidates.add(cst)
        if latest_t is None or t >= latest_t:
            latest_t, latest_cst = t, cst
    if expected is None:
        return latest_cst
    if not candidates:
        return None
    best = min(candidates, key=lambda c: abs(c - expected))
    return best if abs(best - expected) <= tolerance_s else None


def power_samples(rows):
    out = []
    for row in rows or []:
        t = parse_epoch(row.get("last_changed") or row.get("last_updated"))
        if t is not None:
            out.append((t, to_watts(row.get("state"))))
    out.sort(key=lambda s: s[0])
    return out


def detect_onset(samples, boundary, threshold, before_s=ONSET_BEFORE_S, after_s=ONSET_AFTER_S,
                 quiet_s=ONSET_QUIET_S):
    """Power onset corroborated by the cycle boundary (epoch), or None.

    Time at/above threshold inside [boundary - before_s, boundary + after_s] forms bursts;
    a gap of at least quiet_s below threshold separates bursts. The onset is the start of
    the first burst still active at the boundary (or starting after it); bursts that ended
    before the boundary are pre-start blips and are skipped. A chosen burst that was
    already running when the window opened has no observable onset and yields None."""
    lo, hi = boundary - before_s, boundary + after_s
    bursts = []
    for i, (t, w) in enumerate(samples):
        nxt = samples[i + 1][0] if i + 1 < len(samples) else hi
        a, b = max(t, lo), min(nxt, hi)
        if b <= a or w is None or w < threshold:
            continue
        if bursts and a - bursts[-1][1] < quiet_s:
            bursts[-1][1] = b
        else:
            bursts.append([a, b, t < lo])
    for start, fell, pre_window in bursts:
        if fell >= boundary:
            return None if pre_window else start
    return None


def integrate_minutes(samples, t0, t1):
    """Per-minute energy (dWh ints) over [t0, t1) holding each reading until the next one.
    Unknown/unavailable counts as 0 W; returns (dwh, unavailable_seconds)."""
    n = max(1, int(math.ceil((t1 - t0) / 60.0)))
    wh = [0.0] * n
    unavailable = 0.0
    for i, (t, w) in enumerate(samples):
        nxt = samples[i + 1][0] if i + 1 < len(samples) else t1
        a, b = max(t, t0), min(nxt, t1)
        if b <= a:
            continue
        if w is None:
            unavailable += b - a
            continue
        while a < b:
            m = int((a - t0) // 60)
            if m >= n:
                break
            edge = min(b, t0 + (m + 1) * 60.0)
            wh[m] += w * (edge - a) / 3600.0
            a = edge
    if samples and samples[0][0] > t0:
        unavailable += min(samples[0][0], t1) - t0
    return [int(round(x * 10)) for x in wh], unavailable


def cycle_end(appliance, rec, cycle_start):
    """End of the energy window (epoch), see the module docstring."""
    ts = record_ts(rec)
    if appliance == "dishwasher":
        return ts
    return min(ts, cycle_start + (_num(rec.get("duration_min")) or 0.0) * 60.0)


def extract_power(power_rows, cycle_start, end, threshold):
    """Cache entry for one record from its power history (status "ok" or a permanent reason)."""
    if not (0 < end - cycle_start <= MAX_CYCLE_S):
        return {"status": "bad_window", "cst": cycle_start}
    samples = power_samples(power_rows)
    if not samples:
        return {"status": "no_power", "cst": cycle_start}
    onset = detect_onset(samples, cycle_start, threshold)
    if onset is None or onset >= end:
        return {"status": "no_onset", "cst": cycle_start}
    dwh, unavailable = integrate_minutes(samples, onset, end)
    return {
        "status": "ok",
        "cst": round(cycle_start, 3),
        "onset": round(onset, 3),
        "end": round(end, 3),
        "unavail_s": round(unavailable, 1),
        "dwh": dwh,
    }


def entry_rejection(rec, entry):
    """None when a cached extraction validates against its record, else the reason."""
    if not entry or entry.get("status") != "ok":
        return (entry or {}).get("status") or "not_extracted"
    span = entry["end"] - entry["onset"]
    if span <= 0:
        return "bad_window"
    if entry.get("unavail_s", 0.0) > MAX_UNAVAILABLE_FRACTION * span:
        return "unavailable"
    kwh = sum(entry["dwh"]) / 10000.0
    target = _num(rec.get("energy_kwh"))
    if target is None or target <= 0 or abs(kwh - target) > ENERGY_TOLERANCE * target:
        return "energy_mismatch"
    return None


def minutes_to_fraction(dwh, fraction):
    """Minutes after onset by which the cycle had used `fraction` of its energy."""
    total = sum(dwh)
    if total <= 0:
        return 0
    acc = 0
    for i, v in enumerate(dwh):
        acc += v
        if acc >= fraction * total:
            return i + 1
    return len(dwh)


def completion_minutes(appliance, rec, entry):
    """Trusted onset -> physically-done minutes for one validated record, or None."""
    onset = entry["onset"]
    if appliance == "dishwasher":
        if rec.get("end_reason") != "low_power_detected":
            return None
        finish = record_ts(rec)
    else:
        duration = _num(rec.get("duration_min"))
        if duration is None:
            return None
        finish = entry["cst"] + duration * 60.0
    minutes = (finish - onset) / 60.0
    if minutes <= 0 or minutes < minutes_to_fraction(entry["dwh"], COMPLETION_TRUST_FRACTION) - 1:
        return None
    return minutes


def make_cycle(appliance, rec, entry):
    """Building block for profiles from a record and its validated extraction."""
    dwh = entry["dwh"]
    return {
        "ts": record_ts(rec),
        "key": programme_identity(appliance, rec),
        "human": human_confirmed(rec),
        "kwh": sum(dwh) / 10000.0,
        "active_min": minutes_to_fraction(dwh, ACTIVE_FRACTION),
        "completion": completion_minutes(appliance, rec, entry),
        "dwh": dwh,
    }


# ---------------------------------------------------------------------------------------
# Profile building
# ---------------------------------------------------------------------------------------

def percentile(values, p):
    xs = sorted(values)
    if not xs:
        return None
    pos = (len(xs) - 1) * p
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def drop_outliers(cycles):
    """Cycles inside 1.5 x IQR on both energy and active minutes (all kept below 4 cycles)."""
    if len(cycles) < 4:
        return list(cycles)
    fences = []
    for field in ("kwh", "active_min"):
        values = [c[field] for c in cycles]
        q1, q3 = percentile(values, 0.25), percentile(values, 0.75)
        spread = IQR_K * (q3 - q1)
        fences.append((field, q1 - spread, q3 + spread))
    return [c for c in cycles if all(lo <= c[f] <= hi for f, lo, hi in fences)]


def latest(cycles, limit=MAX_PER_KEY):
    return sorted(cycles, key=lambda c: c["ts"], reverse=True)[:limit]


def mean_profile(cycles):
    """Per-minute mean in dWh; a cycle contributes 0 after its own end, so the mean keeps
    the cycles' mean energy exactly."""
    length = max(len(c["dwh"]) for c in cycles)
    sums = [0.0] * length
    for c in cycles:
        for i, v in enumerate(c["dwh"]):
            sums[i] += v
    return [s / len(cycles) for s in sums]


def cut_tail(profile, fraction=TAIL_FRACTION):
    total = sum(profile)
    if total <= 0:
        return []
    acc = 0.0
    for i, v in enumerate(profile):
        acc += v
        if acc >= fraction * total:
            return profile[: i + 1]
    return list(profile)


def encode_profile(values):
    return ",".join(str(int(v)) for v in values)


def decode_profile(text):
    return [int(v) for v in text.split(",")] if text else []


def label_source(cycles):
    confirmed = sum(1 for c in cycles if c["human"])
    if confirmed == len(cycles):
        return "confirmed"
    return "inferred" if confirmed == 0 else "mixed"


def profile_from_cycles(cycles):
    """Quantised profile for one pool of already-selected cycles, or None if empty."""
    if not cycles:
        return None
    p = [int(round(v)) for v in cut_tail(mean_profile(cycles))]
    while p and p[-1] == 0:
        p.pop()
    return p or None


def build_programme(identity, label, cycles, use=0, min_cycles=MIN_CYCLES):
    """Published programme dict from a selected pool, or None below min_cycles."""
    if len(cycles) < min_cycles:
        return None
    p = profile_from_cycles(cycles)
    if not p:
        return None
    programme = {
        "key": public_key(identity),
        "label": label,
        "n": len(cycles),
        "kwh": round(sum(p) / 10000.0, 3),
        "run_min": len(p),
        "label_source": label_source(cycles),
        "p": encode_profile(p),
    }
    completions = [c["completion"] for c in cycles if c["completion"] is not None]
    if len(completions) >= MIN_COMPLETIONS:
        programme["done_min"] = int(math.ceil(percentile(completions, COMPLETION_PERCENTILE)))
    programme["_use"] = use
    return programme


def select_pools(cycles):
    """Per identity: latest MAX_PER_KEY cycles, outliers dropped within that identity only
    (so a minority programme is never filtered against a majority one). Unknown-programme
    cycles form their own pool. Typical = latest MAX_PER_KEY of all kept cycles."""
    by_key = {}
    for c in cycles:
        by_key.setdefault(c["key"], []).append(c)
    pools = {k: drop_outliers(latest(v)) for k, v in by_key.items()}
    kept = [c for pool in pools.values() for c in pool]
    return pools, latest(kept)


def current_entry(appliance, rec, entries, source):
    """The cached extraction for rec when its fingerprint still matches, else None."""
    entry = entries.get(cache_key(appliance, rec))
    if entry and entry.get("fp") == extraction_fingerprint(rec, source):
        return entry
    return None


def build_appliance(appliance, records, entries, source, now, base_labels=None,
                    use_window_days=USE_WINDOW_DAYS, max_programmes=MAX_PROGRAMMES):
    """Published block for one appliance from its feedback records and the extraction cache
    (cache key -> entry). Eligibility, identity and labels always come from the current
    record, so a corrected record is re-evaluated without re-reading history."""
    cycles = []
    use = {}
    since = now - use_window_days * 86400.0
    for rec in records:
        ts = record_ts(rec)
        identity = programme_identity(appliance, rec)
        if identity is not None and ts is not None and ts >= since:
            use[identity] = use.get(identity, 0) + 1
        if record_eligibility(rec) is not None:
            continue
        entry = current_entry(appliance, rec, entries, source)
        if entry_rejection(rec, entry) is not None:
            continue
        cycles.append(make_cycle(appliance, rec, entry))

    pools, typical_pool = select_pools(cycles)
    programmes = []
    typical = build_programme(TYPICAL, programme_label(appliance, TYPICAL, base_labels), typical_pool,
                              use=sum(use.values()))
    for identity, pool in pools.items():
        if identity is None:
            continue
        prog = build_programme(identity, programme_label(appliance, identity, base_labels), pool,
                               use=use.get(identity, 0))
        if prog:
            programmes.append(prog)
    programmes.sort(key=lambda p: (-p["_use"], -p["n"], p["key"]))
    if typical:
        programmes.insert(0, typical)
    block = {"programmes": programmes[:max_programmes]}
    if records:
        block["records"] = len(records)
    if cycles:
        block["profiled"] = len(cycles)
    return block


def cache_key(appliance, rec):
    return f"{appliance}|{rec.get('ts')}"


# ---------------------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------------------

def payload_bytes(attributes):
    return len(json.dumps(attributes, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def public_block(block):
    out = {k: v for k, v in block.items() if k != "programmes"}
    out["programmes"] = [{k: v for k, v in p.items() if not k.startswith("_")} for p in block["programmes"]]
    return out


def build_attributes(blocks, max_bytes=MAX_PAYLOAD_BYTES):
    """Entity attributes for the given appliance blocks, trimmed to max_bytes by dropping
    the least-used non-typical programme across all appliances, one at a time."""
    blocks = {a: {**b, "programmes": list(b["programmes"])} for a, b in blocks.items()}

    def render():
        return {
            "schema": SCHEMA,
            "unit": UNIT,
            "appliances": {a: public_block(b) for a, b in blocks.items()},
            "friendly_name": "Appliance run profiles",
            "icon": "mdi:chart-timeline-variant",
        }

    attributes = render()
    while payload_bytes(attributes) > max_bytes:
        candidates = [
            (p["_use"], p["n"], a, i)
            for a, b in blocks.items()
            for i, p in enumerate(b["programmes"])
            if p["key"] != TYPICAL
        ]
        if not candidates:
            break
        _, _, a, i = min(candidates)
        del blocks[a]["programmes"][i]
        attributes = render()
    return attributes


def transport_safe(value):
    """True when AppDaemon's attribute cleaning would leave value unchanged: no None, no
    booleans and no numeric zero anywhere."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return True
    if isinstance(value, dict):
        return all(transport_safe(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(transport_safe(v) for v in value)
    return False
