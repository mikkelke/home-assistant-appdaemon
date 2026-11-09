"""Pure start advice for the Energy Plan's default appliance programme.

Schema-1 run profiles retain their published order: the first programme is the default.
Costs integrate minute energy against all-in hourly prices, with fractional-minute overlap.
A missing energy-carrying hour makes a start infeasible; free and negative prices are valid
within -5..15 kr/kWh. Published prices override forecast hours, and only published hours
choose a start. Done-by is Any and the washer's optional dryer chain is off.

The planner considers pressing now and every remaining whole minute in the same hour,
using 30-minute knob positions through 3 hours and hourly positions through 24 hours.
Savings must pass both 0.10 kr and 10 percent (the latter only for a positive baseline).
Every press in a window stays within 0.05 kr of its cheapest cost and passes the same rule.
A window closing more than 10 minutes before the next hour is spoken as "press by".

build_advice returns a best-start state (UTC ISO or unknown), its entity attributes and a
spoken English sentence. Running and Paused suppress the next-start plan. Malformed
profile payloads reject every appliance, rather than pricing corrupted energy.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

HOME_TIME_ZONE = "Europe/Copenhagen"
RUN_PROFILES_ENTITY_ID = "sensor.appliance_run_profiles"
# The dashboard resolves this sensor from the first grid flow's entity_energy_price.
PRICE_ENTITY_ID = "sensor.energi_data_service"
KNOB_STEPS_MIN = tuple(range(0, 181, 30)) + tuple(range(240, 1441, 60))
RUN_PROFILE_MAX_MINUTES = 720
PLAN_PRICE_RANGE_KR = {"min": -5, "max": 15}
PLAN_SAVING_RULE = {"minKr": 0.1, "minPct": 10}
PRESS_WINDOW_TOLERANCE_KR = 0.05
PRESS_BY_MIN_SLACK_MIN = 10
MINUTE_MS = 60_000
HOUR_MS = 3_600_000
EPSILON_KR = 1e-9
MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MISSING = object()


class PayloadError(ValueError):
    """Invalid schema-1 profile data."""


def _pick(obj, *names):
    for name in names:
        if name in obj:
            return obj[name]
    return _MISSING


def _number(value):
    return type(value) in (int, float)


def _to_int(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        # Safe integer range is checked before constructing a potentially huge integer.
        digits = value.strip().lstrip("0") or "0"
        if len(digits) > 16:
            return None
        value = int(digits)
    if not _number(value) or abs(value) > MAX_SAFE_INTEGER or not math.isfinite(value) or value != math.trunc(value):
        return None
    return int(value)


def _parse_profile(value, where):
    if not isinstance(value, str):
        raise PayloadError(f"{where}: profile must be a string")
    text = re.sub(r"\s+", "", value)
    if not re.fullmatch(r"[0-9]+(,[0-9]+)*", text):
        raise PayloadError(f"{where}: profile must be comma-separated integers >= 0")
    parts = text.split(",")
    if len(parts) > RUN_PROFILE_MAX_MINUTES:
        raise PayloadError(f"{where}: profile longer than {RUN_PROFILE_MAX_MINUTES} minutes")
    wh = []
    for part in parts:
        dwh = _to_int(part)
        if dwh is None:
            raise PayloadError(f"{where}: profile value out of range")
        wh.append(dwh / 10)
    return wh


def _parse_programme(raw, where):
    if not isinstance(raw, dict):
        raise PayloadError(f"{where}: not an object")
    key = _pick(raw, "key")
    if not isinstance(key, str) or key == "":
        raise PayloadError(f"{where}: missing key")
    at = f"{where}.{key}"
    label = _pick(raw, "label")
    if not isinstance(label, str) or label == "":
        raise PayloadError(f"{at}: missing label")
    n = _to_int(_pick(raw, "n"))
    if n is None or n < 1:
        raise PayloadError(f"{at}: n must be an integer >= 1")
    wh = _parse_profile(_pick(raw, "p", "profile"), at)
    energy_raw = _pick(raw, "energy_min", "run_min")
    energy_min = len(wh) if energy_raw is _MISSING else _to_int(energy_raw)
    if energy_min != len(wh):
        raise PayloadError(f"{at}: energy_min does not match the profile length")
    completion_raw = _pick(raw, "completion_min", "done_min")
    completion = None if completion_raw is _MISSING else _to_int(completion_raw)
    if completion_raw is not _MISSING and (completion is None or completion < 1 or completion > 1440):
        raise PayloadError(f"{at}: completion_min must be 1..1440")
    source = _pick(raw, "label_source")
    if source is not _MISSING and source not in ("confirmed", "inferred", "mixed"):
        raise PayloadError(f"{at}: unknown label_source")
    cumulative = [0.0]
    for value in wh:
        cumulative.append(cumulative[-1] + value)
    return {"key": key, "label": label, "n": n, "kwh": cumulative[-1] / 1000,
            "energyMin": energy_min, "completionMin": None if completion is None else max(completion, energy_min),
            "labelConfirmed": source in ("confirmed", "mixed"), "whPerMin": wh, "cumWh": cumulative}


def parse_run_profiles(attributes):
    """Parse entity attributes, rejecting the entire payload on a malformed programme."""
    try:
        if not isinstance(attributes, dict):
            raise PayloadError("attributes missing")
        if _to_int(_pick(attributes, "schema")) != 1:
            raise PayloadError("unsupported schema")
        raw = _pick(attributes, "appliances")
        if not isinstance(raw, dict):
            raise PayloadError("appliances missing")
        appliances = {}
        for appliance, entry in raw.items():
            programmes = _pick(entry, "programmes") if isinstance(entry, dict) else _MISSING
            if programmes is _MISSING:
                appliances[appliance] = []
                continue
            if not isinstance(programmes, list):
                raise PayloadError(f"{appliance}: programmes must be a list")
            parsed = [_parse_programme(p, f"{appliance}[{i}]") for i, p in enumerate(programmes)]
            if len({p["key"] for p in parsed}) != len(parsed):
                raise PayloadError(f"{appliance}: duplicate programme key")
            appliances[appliance] = parsed
        return {"ok": True, "profiles": {"appliances": appliances}}
    except PayloadError as error:
        return {"ok": False, "error": str(error)}


def _cum_at(programme, minute):
    length = len(programme["whPerMin"])
    if minute <= 0:
        return 0
    if minute >= length:
        return programme["cumWh"][length]
    whole = math.floor(minute)
    return programme["cumWh"][whole] + (minute - whole) * programme["whPerMin"][whole]


def energy_between_wh(programme, from_min, to_min):
    return _cum_at(programme, to_min) - _cum_at(programme, from_min)


def parse_raw_points(value):
    """Defensive shape parse of raw_today, raw_tomorrow or forecast."""
    if not isinstance(value, list):
        return None
    points = [p for p in value if isinstance(p, dict) and isinstance(p.get("hour"), str) and _number(p.get("price"))]
    return points or None


def is_plan_price(price):
    return _number(price) and PLAN_PRICE_RANGE_KR["min"] <= price <= PLAN_PRICE_RANGE_KR["max"] and math.isfinite(price)


def _date_ms(text):
    """Parse the ISO timestamps supplied by HA, with Date.parse's millisecond precision."""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(HOME_TIME_ZONE))
        return math.trunc(dt.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return None


def build_hourly_prices(raw_today, raw_tomorrow, tomorrow_valid, forecast):
    """Published today, valid tomorrow, then forecast: first valid point per hour wins."""
    by_ms = {}
    for points, estimated in ((raw_today, False), (raw_tomorrow if tomorrow_valid else None, False), (forecast, True)):
        for point in points or []:
            ms = _date_ms(point["hour"])
            if ms is None or ms % HOUR_MS != 0 or not is_plan_price(point["price"]) or ms in by_ms:
                continue
            by_ms[ms] = {"ms": ms, "price": point["price"]}
            if estimated:
                by_ms[ms]["estimated"] = True
    return [by_ms[ms] for ms in sorted(by_ms)]


def index_prices(timeline):
    return {point["ms"]: point for point in timeline}


def _floor_hour(ms):
    return math.floor(ms / HOUR_MS) * HOUR_MS


def run_cost(programme, prices, start_ms):
    end_ms = start_ms + programme["energyMin"] * MINUTE_MS
    kr, estimated = 0.0, False
    hour_ms = _floor_hour(start_ms)
    while hour_ms < end_ms:
        wh = energy_between_wh(programme, (hour_ms - start_ms) / MINUTE_MS,
                               (hour_ms + HOUR_MS - start_ms) / MINUTE_MS)
        if wh > 0:
            point = prices.get(hour_ms)
            if point is None:
                return None
            kr += (wh / 1000) * point["price"]
            if point.get("estimated"):
                estimated = True
        hour_ms += HOUR_MS
    return {"kr": kr, "estimated": estimated}


def completion_ms(programme, start_ms):
    completion = programme["completionMin"]
    return None if completion is None else start_ms + completion * MINUTE_MS


def _misses_done_by(programme, start_ms, done_by_ms):
    done_ms = completion_ms(programme, start_ms)
    return done_by_ms is not None and done_ms is not None and done_ms > done_by_ms


def start_cost(programme, prices, start_ms, done_by_ms=None):
    if _misses_done_by(programme, start_ms, done_by_ms):
        return None
    return run_cost(programme, prices, start_ms)


def passes_saving_rule(baseline_kr, plan_kr):
    saving = baseline_kr - plan_kr
    if saving + EPSILON_KR < PLAN_SAVING_RULE["minKr"]:
        return False
    return baseline_kr <= 0 or saving + EPSILON_KR >= baseline_kr * PLAN_SAVING_RULE["minPct"] / 100


def _normalise_steps(steps):
    return sorted({0, *(s for s in steps if _number(s) and math.isfinite(s) and s >= 0)})


def _press_grid(press_from_ms):
    hour_end_ms = _floor_hour(press_from_ms) + HOUR_MS
    grid = [press_from_ms]
    t = math.floor(press_from_ms / MINUTE_MS) * MINUTE_MS + MINUTE_MS
    while t < hour_end_ms:
        grid.append(t)
        t += MINUTE_MS
    return grid


def _priced_until(prices, from_ms):
    hour_ms = _floor_hour(from_ms)
    if hour_ms not in prices:
        return None
    while hour_ms + HOUR_MS in prices:
        hour_ms += HOUR_MS
    return hour_ms + HOUR_MS


def plan_start(programme, timeline, inputs):
    """Line-for-line decision port of planStart; input and result keys follow its contract."""
    prices = index_prices(timeline)
    earliest = inputs.get("earliestPressMs")
    press_from_ms = max(inputs["nowMs"], inputs["nowMs"] if earliest is None else earliest)
    done_by_ms = inputs.get("doneByMs")
    steps = _normalise_steps(inputs["knobStepsMin"])
    grid = _press_grid(press_from_ms)
    costs = [[start_cost(programme, prices, press + knob * MINUTE_MS, done_by_ms) for press in grid] for knob in steps]
    no_delay = costs[0][0]
    result = {"pressFromMs": press_from_ms, "noDelay": no_delay, "plan": None, "savingKr": None,
              "doneByMissed": _misses_done_by(programme, press_from_ms, done_by_ms),
              "pricedUntilMs": _priced_until(prices, press_from_ms)}

    now_best = None
    for k in range(len(steps)):
        cost = costs[k][0]
        if cost is not None and (now_best is None or cost["kr"] < now_best["cost"]["kr"] - EPSILON_KR):
            now_best = {"k": k, "cost": cost}
    now_choice = now_best
    if now_best is not None and now_best["k"] > 0 and no_delay is not None and not passes_saving_rule(no_delay["kr"], now_best["cost"]["kr"]):
        now_choice = {"k": 0, "cost": no_delay}

    best = None
    for p in range(len(grid)):
        for k in range(len(steps)):
            cost = costs[k][p]
            if cost is not None and (best is None or cost["kr"] < best["cost"]["kr"] - EPSILON_KR):
                best = {"k": k, "p": p, "cost": cost}
    wait_wins = (best is not None and best["p"] > 0 and
                 (now_choice is None or passes_saving_rule(now_choice["cost"]["kr"], best["cost"]["kr"])))
    chosen = best if wait_wins else {**now_choice, "p": 0} if now_choice is not None else None
    if chosen is None:
        return result

    row = costs[chosen["k"]]
    ref = chosen["cost"]
    chosen_p = chosen["p"]
    baseline_kr = ((now_choice["cost"]["kr"] if now_choice is not None else None) if chosen_p > 0
                   else (no_delay["kr"] if no_delay is not None else None) if steps[chosen["k"]] > 0 else None)

    def within(cost):
        return (cost is not None and cost["kr"] <= ref["kr"] + PRESS_WINDOW_TOLERANCE_KR + EPSILON_KR and
                (baseline_kr is None or passes_saving_rule(baseline_kr, cost["kr"])))

    first = last = chosen_p
    while first > 0 and within(row[first - 1]):
        first -= 1
    while last + 1 < len(grid) and within(row[last + 1]):
        last += 1
    knob_min = steps[chosen["k"]]
    kind = "wait" if first > 0 else "knob" if knob_min > 0 else "now"
    start_ms = grid[first] + knob_min * MINUTE_MS
    cost_kr = row[first]["kr"]
    hour_end_ms = _floor_hour(press_from_ms) + HOUR_MS
    result["plan"] = {"kind": kind, "knobMin": knob_min, "pressFromMs": grid[first], "pressUntilMs": grid[last],
                      "showPressBy": grid[last] < hour_end_ms - PRESS_BY_MIN_SLACK_MIN * MINUTE_MS,
                      "startMs": start_ms, "doneMs": completion_ms(programme, start_ms), "costKr": cost_kr,
                      "estimated": any(c is not None and c["estimated"] for c in row[first:last + 1])}
    if no_delay is not None and passes_saving_rule(no_delay["kr"], cost_kr):
        result["savingKr"] = no_delay["kr"] - cost_kr
    return result


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clock(ms):
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(HOME_TIME_ZONE)).strftime("%H:%M")


def _delay_words(minutes):
    if minutes < 60:
        return f"{minutes:g} minutes"
    hours = minutes / 60
    return f"{hours:g} {'hour' if hours == 1 else 'hours'}"


def _money(value):
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def advice_sentence(start_plan):
    """Speak the same start, knob and press-window information as the Plan chips."""
    choice = start_plan.get("plan")
    if choice is None:
        return "No prices ahead."
    kind = choice["kind"]
    knob = choice["knobMin"]
    if kind == "now":
        sentence = "Start now"
    elif kind == "knob":
        sentence = f"Press now with the delay set to {_delay_words(knob)}"
    else:
        sentence = f"Wait until {_clock(choice['pressFromMs'])}, then press start"
        if knob > 0:
            sentence += f" with the delay set to {_delay_words(knob)}"
    if choice["showPressBy"]:
        # A one-instant wait window already names its only press time.
        if kind != "wait" or _clock(choice["pressUntilMs"]) != _clock(choice["pressFromMs"]):
            sentence += f"; press by {_clock(choice['pressUntilMs'])}"
    if start_plan.get("savingKr") is not None:
        sentence += f"; saves {_money(start_plan['savingKr'])} kr."
    elif kind == "now":
        sentence += "; waiting saves nothing."
    else:
        sentence += "."
    return sentence


def build_advice(key, profiles_result, timeline, now_ms, state=None):
    """Default programme, Any finish time, published prices and no chained dryer."""
    attributes = {"device_class": "timestamp"}
    output = {"state": "unknown", "attributes": attributes, "advice": "No profile yet."}
    if state in ("Running", "Paused"):
        output["advice"] = "Already running."
        return output
    programmes = profiles_result.get("profiles", {}).get("appliances", {}).get(key, []) if profiles_result.get("ok") else []
    if not programmes:
        return output
    programme = programmes[0]
    attributes["programme"] = programme["key"]
    published = [point for point in timeline if not point.get("estimated")]
    planned = plan_start(programme, published, {"nowMs": now_ms, "knobStepsMin": KNOB_STEPS_MIN})
    output["advice"] = advice_sentence(planned)
    choice = planned["plan"]
    if choice is None:
        return output
    output["state"] = _iso(choice["startMs"])
    attributes.update({"kind": choice["kind"], "knob_min": choice["knobMin"], "press_from": _iso(choice["pressFromMs"]),
                       "cost_kr": choice["costKr"], "saving_kr": planned["savingKr"],
                       "baseline_kr": None if planned["noDelay"] is None else planned["noDelay"]["kr"],
                       "estimated": choice["estimated"]})
    if choice["showPressBy"]:
        attributes["press_by"] = _iso(choice["pressUntilMs"])
    return output
