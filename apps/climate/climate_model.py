"""
Pure climate model -- the single source of truth for the bedroom cooling physics.

ZERO AppDaemon imports: every function here is a plain, side-effect-free callable so
both apps (smart_cooling.py the controller, bedroom_comfort.py the advisory) and the unit
tests can import it without a running AppDaemon. The apps keep ALL I/O (sensor/forecast/
price/history reads, service calls, publishing); this module keeps only the math.

Why it exists: smart_cooling and bedroom_comfort had independently reimplemented the same
physics (equilibrium projection, coast law, comfort limit, vent feasibility) and DRIFTED --
the dashboard advised "deploy AC" projecting the sealed bedroom to ~23C from the warm
kitchen while the room actually sat at 20C with a window holding it there. Promoting the
math to one module (and driving the projection from the weather equilibrium E instead of
the warm kitchen proxy) removes the divergence and the circular ceiling<->rise_frac
dependency between the two apps.

Contents:
  - parse_forecast_envelope: shared weather.get_forecasts response-envelope digging
  - equilibrium: legacy_equilibrium (kitchen/mid/floor proxy) + model_d_apartment (weather)
  - coast_peak / calc_floor_target: the sealed-zone coast law and its inverse (ONE copy)
  - comfort limit: dew_point_c, project_morning_dp, effective_ceiling, hours_until_morning,
    classify (moved verbatim from bedroom_comfort)
  - free cooling: windows_can_cool (feasibility against a TARGET) + vent_helps (compat
    wrapper) + summarize_open_windows
  - plan_sleep: the cheapest-path planner (windows cost 0 vs AC energy*price + noise)
  - multi-night storage-advisor chain (A1 fit): DeployAdvisor's separate night-ahead
    apartment-mass projector (kitchen_chain/floor_chain/b23_aux/night_peak/project_nights)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# ------------------------------------------------------------- forecast envelope parsing

def parse_forecast_envelope(resp, entity_id) -> list:
    """Dig the raw weather.get_forecasts service-response envelope down to its forecast
    list. AppDaemon's call_service(..., return_response=True) wraps the actual list under
    a result/response/<entity_id>/forecast chain; walk that chain and, if it doesn't land
    on a list, recursively search the whole envelope for the first list whose first element
    looks like a forecast dict (has "temperature"). Shared verbatim by smart_cooling and
    deploy_advisor -- they dig the identical envelope shape out of the identical service call.

    Pure and never raises: any unrecognised shape returns [] rather than blowing up the
    caller's eval loop.
    """
    node = resp
    for key in ("result", "response", entity_id, "forecast"):
        if isinstance(node, dict) and key in node:
            node = node[key]
    if isinstance(node, list):
        return node

    def find(n):
        if isinstance(n, list) and n and isinstance(n[0], dict) and "temperature" in n[0]:
            return n
        if isinstance(n, dict):
            for v in n.values():
                r = find(v)
                if r is not None:
                    return r
        return None

    return find(resp) or []


# ------------------------------------------------------------- equilibrium math

def legacy_equilibrium(kitchen, mid, floor, person_offset, empty_fallback=24.5):
    """Legacy proxy equilibrium the sealed sleeping zone drifts toward overnight.

    The warmest sensible reading of the neighbour wall (kitchen), the mid wall and the
    floor, plus the sleeper offset (errs deep -> safe). ``empty_fallback`` (24.5) is used
    when every reading is None, so calling this with (kitchen, mid, floor, person_offset)
    is byte-identical to smart_cooling's old inline body.
    """
    vals = [v for v in (kitchen, mid, floor) if v is not None]
    return (max(vals) if vals else empty_fallback) + person_offset


@dataclass
class ModelDCoeffs:
    """Coefficient bundle for the verified Model D apartment-peak predictor."""
    b0: float
    b_solar: float
    b_vent: float
    vent_knee: float
    b_prev: float


def model_d_apartment(solar_mean, outdoor_max, prev_kitchen_max, coeffs: ModelDCoeffs):
    """Model D predicted apartment/kitchen daytime peak (e_apartment ONLY).

    b0 + b_solar*solar_mean + b_vent*max(0, outdoor_max - vent_knee) + b_prev*prev_kitchen_max.
    The caller (_weather_equilibrium) still composes e_weather = e_apartment + person_offset +
    safety_margin and the E = max(e_weather, e_legacy - relief) relief floor itself.
    """
    return (coeffs.b0
            + coeffs.b_solar * solar_mean
            + coeffs.b_vent * max(0.0, outdoor_max - coeffs.vent_knee)
            + coeffs.b_prev * prev_kitchen_max)


def grounded_equilibrium(e_weather, apartment_now, night_outdoor, comfort_limit,
                         reality_margin=1.0, warm_night_margin=1.0):
    """Reality-check the weather equilibrium before it drives the ADVISORY sleep plan.

    ``e_weather`` is the weather model's DAYTIME apartment-peak prediction. Feeding that
    straight into the coast law treats the daytime peak as the overnight equilibrium the
    sealed room drifts toward -- correct only on a HOT night, when the day's heat is still
    in the mass and the outdoor air holds it there. On a COOL/cooling night the sealed room
    drifts toward the cool NIGHT apartment (a few degrees below the daytime peak), not the
    peak, so projecting from ``e_weather`` over-predicts and false-alarms "run the AC" while
    every room already sits degrees below its own limit.

    Rule:
      - Warm night (``night_outdoor >= comfort_limit - warm_night_margin``): the night will
        hold the daytime heat, so pre-cool-ahead-of-a-hot-night is real -> return
        ``e_weather`` unchanged (the weather model's own value).
      - Cool/cooling night: the sealed room can't drift materially warmer than the apartment
        is right now -> return ``min(e_weather, apartment_now + reality_margin)``.

    Pure and None-safe: if ``apartment_now`` or ``night_outdoor`` is missing there's no
    trustworthy reality anchor, so fall back to ``e_weather`` (errs warm/safe -- keeps the
    weather value). A None ``e_weather`` is returned as-is (the coast law already treats a
    missing equilibrium as "cannot project").
    """
    if e_weather is None:
        return e_weather
    if apartment_now is None or night_outdoor is None:
        return e_weather
    if night_outdoor >= comfort_limit - warm_night_margin:
        return e_weather
    return min(e_weather, apartment_now + reality_margin)


# ------------------------------------------------------------- coast law (one copy)

def coast_peak(floor, equilibrium, rise_frac, zone_offset) -> Optional[float]:
    """Sealed sleeping-zone peak if nobody cools: the floor mass drifts toward the
    equilibrium E by fraction rise_frac over the night, and the sleeping zone rides
    zone_offset above the floor. floor + (E - floor)*rise_frac + zone_offset.

    Equilibrium is an INPUT (the weather E), no longer recomputed from max(floor,kitchen,mid).
    Returns None if floor or equilibrium is missing.
    """
    if floor is None or equilibrium is None:
        return None
    return floor + (equilibrium - floor) * rise_frac + zone_offset


def calc_floor_target(equilibrium, ceiling, rise_frac, zone_offset, min_temp) -> float:
    """Inverse of the coast law: the floor pre-cool target so the zone stays <= ceiling.

    The mid wall sits ~zone_offset above the floor, so cap the FLOOR peak at
    (ceiling - zone_offset). The floor rises by (E - F0)*r over the window, so
    F0 + (E - F0)*r <= cap -> F0 = (cap - E*r)/(1 - r). Clamp to [min_temp, ceiling].
    Byte-identical to smart_cooling's old _calc_target.
    """
    cap = ceiling - zone_offset
    r = min(0.95, max(0.05, rise_frac))
    if equilibrium <= cap:
        return ceiling            # room won't break the ceiling on its own -> no pre-cool
    f0 = (cap - equilibrium * r) / (1.0 - r)
    return max(min_temp, min(ceiling, round(f0, 2)))


# ------------------------------------------------------------- comfort limit
# Moved verbatim from bedroom_comfort.py; this is now their single home. bedroom_comfort
# re-exports them so its test surface (bc.dew_point_c etc.) is unchanged.

def dew_point_c(t_c, rh_pct) -> Optional[float]:
    """Magnus formula dew point. Returns None on invalid input."""
    try:
        t = float(t_c)
        rh = float(rh_pct)
    except (TypeError, ValueError):
        return None
    if rh <= 0 or rh > 100:
        return None
    a, b = 17.62, 243.12
    gamma = math.log(rh / 100.0) + a * t / (b + t)
    return b * gamma / (a - gamma)


def project_morning_dp(dp_now, sleepers, hours, rate_per_sleeper_c_per_h) -> Optional[float]:
    """Dew point after `hours` of sleepers adding moisture to a sealed room."""
    if dp_now is None:
        return None
    hours = max(0.0, min(10.0, float(hours)))
    return dp_now + rate_per_sleeper_c_per_h * max(0, int(sleepers)) * hours


def effective_ceiling(base, dp_morning, sleepers, knee_c=12.0,
                      penalty_per_c=0.15, second_sleeper_c=0.5,
                      max_reduction_c=1.5):
    """Night ceiling lowered for projected humidity and a second sleeper.
    Bounded: never more than max_reduction_c below the base. Returns (ceiling, reduction).
    """
    reduction = 0.0
    if dp_morning is not None:
        reduction += penalty_per_c * max(0.0, dp_morning - knee_c)
    if sleepers >= 2:
        reduction += second_sleeper_c
    reduction = min(max_reduction_c, reduction)
    return round(base - reduction, 1), round(reduction, 2)


def hours_until_morning(now, morning_hour=7) -> float:
    """Hours from `now` to the next 07:00, capped at 10 (projection horizon)."""
    target_day = now
    if now.hour >= morning_hour:
        from datetime import timedelta
        target_day = now + timedelta(days=1)
    target = target_day.replace(hour=morning_hour, minute=0, second=0, microsecond=0)
    hours = (target - now).total_seconds() / 3600.0
    return max(0.0, min(10.0, hours))


def classify(t, dp_now, ceiling_base, ceiling_eff) -> str:
    """Human-comfort label on ABSOLUTE anchors - deliberately not the planning
    knob: with the knob at 20 a perfectly nice 20.8 C room read as "hot"
    (2026-07-12). The knob steers SmartCooling; this label describes the room."""
    del ceiling_base, ceiling_eff  # planning inputs, not comfort anchors
    if t is None:
        return "unknown"
    if t >= 24.5:
        return "hot"
    if dp_now is not None and dp_now >= 13.5:
        return "sticky"
    if t >= 23.0:
        return "warm"
    return "comfortable"


# ------------------------------------------------------------- free-cooling feasibility

def windows_can_cool(target, outdoor_temp, outdoor_dew, indoor_dew,
                     temp_margin=0.5, dew_margin=0.0):
    """Would opening a window help, measured against a TARGET (the sleep limit) rather
    than just the current indoor temperature?

    True only when the outdoor air is BOTH cooler than target - temp_margin AND no more humid
    than indoor_dew - dew_margin -- opening a warmer or muggier window imports heat/water.
    dew_margin defaults to 0.0: veto only when outdoor is genuinely MORE humid than indoor.
    (2026-07-20 the bedroom cooled AND dried on a window only ~0.3C drier, so the old 1C
    drier buffer wrongly rejected a proven free-cool -- user: cool-enough-outside = window night.)
    None on any missing input (message preserves 'dew point' for the too-humid branch, so
    bedroom_comfort's vent_helps tests stay green). Returns (ok, reason).
    """
    if None in (target, outdoor_temp, outdoor_dew, indoor_dew):
        return None, "outdoor or indoor data missing"
    if outdoor_temp >= target - temp_margin:
        return False, f"outdoor {outdoor_temp:.1f}C not cooler than bedroom {target:.1f}C"
    if outdoor_dew > indoor_dew - dew_margin:
        return False, f"outdoor dew point {outdoor_dew:.1f}C too humid vs indoor {indoor_dew:.1f}C"
    return True, (f"outdoor {outdoor_temp:.1f}C / DP {outdoor_dew:.1f}C is cooler and drier "
                  f"than bedroom {target:.1f}C / DP {indoor_dew:.1f}C")


def vent_helps(t_in, dp_in, t_out, dp_out):
    """Compatibility wrapper for bedroom_comfort's published vent_helps/vent_reason.

    Venting helps only when outdoor air is BOTH cooler and drier than the CURRENT indoor
    temperature (target = t_in). Kept as the leaf comfort read; implemented on top of
    windows_can_cool so there is one feasibility rule.
    """
    return windows_can_cool(target=t_in, outdoor_temp=t_out,
                            outdoor_dew=dp_out, indoor_dew=dp_in,
                            temp_margin=0.0, dew_margin=0.0)


def summarize_open_windows(contacts: dict) -> list:
    """Given {name: contact_state}, the sorted list of names whose contact reads open.

    Explicit 'on' = open (NOT the condenser fail-open rule that lives in smart_cooling):
    a wrong 'open' here only mis-labels advice, it never actuates.
    """
    return sorted(name for name, state in (contacts or {}).items() if state == "on")


# ------------------------------------------------------------- cheapest-path planner

@dataclass
class SleepPlanInputs:
    """Plain input bundle for plan_sleep. equilibrium is the driving E the sealed room
    coasts toward (smart_cooling passes e_active -- the weather Model D when enabled --
    so the plan stops over-projecting from the warm kitchen). All plumbing stays in the
    app; the planner is pure.
    """
    floor: Optional[float]
    equilibrium: Optional[float]
    rise_frac: float
    zone_offset: float
    comfort_limit: float
    min_temp: float
    floor_cool_cph: float
    cool_power_kw: float
    cheapest_price: Optional[float]
    outdoor_temp: Optional[float]
    outdoor_dew: Optional[float]
    indoor_dew: Optional[float]
    open_windows: list = field(default_factory=list)
    noise_penalty_kr: float = 0.5
    peak_margin_c: float = 0.2
    hybrid_gap_c: float = 1.5
    temp_margin_c: float = 0.5      # outdoor must be at least this far below the limit to cool
    muggy_slack_c: float = 2.0      # outdoor dew this much above indoor before it's "too muggy"


def _windows_phrase(open_windows) -> str:
    if not open_windows:
        return "all closed"
    return " + ".join(open_windows) + " open"


def _cost_label(cost) -> str:
    if cost is None:
        return "cost unknown"
    if cost <= 0.0:
        return "free"
    return f"~{cost:.1f} kr"


def plan_sleep(inp: SleepPlanInputs) -> dict:
    """Pure cheapest-path chooser: keep the sleeping zone under the comfort limit across
    the night for the least money (windows cost 0, AC = energy_kWh*price + a fixed noise
    penalty), planning the whole night rather than the current instant.

    projected_peak = coast_peak(floor, equilibrium, rise_frac, zone_offset).
      - peak within peak_margin_c of the limit           -> 'nothing' (free)
      - else cooler outside (temp) AND not meaningfully muggier than indoors:
          gap <= hybrid_gap_c                             -> 'windows' (free)
          gap  > hybrid_gap_c                             -> 'hybrid'  (windows now + AC backup)
      - else (too WARM, or genuinely MUGGY, outside)      -> 'ac'
    A window cools whenever it's cooler outside; humidity merely level with indoors is a note,
    NOT a reason to run the compressor. Windows always beat equal-comfort AC (0 < ac_cost + pen).

    Returns a plain dict (recommendation/projected_peak/comfort_limit/est_cost_kr/
    cost_label/headline/detail/open_windows/windows_summary). ADVISORY ONLY.
    """
    limit = inp.comfort_limit
    open_windows = list(inp.open_windows or [])
    windows_summary = _windows_phrase(open_windows)
    projected_peak = coast_peak(inp.floor, inp.equilibrium, inp.rise_frac, inp.zone_offset)

    base = {
        "comfort_limit": round(limit, 1),
        "open_windows": open_windows,
        "windows_summary": windows_summary,
    }

    if projected_peak is None:
        base.update({
            "recommendation": "nothing",
            "projected_peak": None,
            "est_cost_kr": 0.0,
            "cost_label": "free",
            "headline": "Not enough to plan yet",
            "detail": "Missing floor or equilibrium reading -- cannot project tonight.",
        })
        return base

    peak_disp = round(projected_peak, 1)
    gap = projected_peak - limit

    # AC cost of pre-cooling the floor deep enough to keep the zone under the limit.
    target = calc_floor_target(inp.equilibrium, limit, inp.rise_frac,
                               inp.zone_offset, inp.min_temp)
    deficit = max(0.0, inp.floor - target)
    if inp.cheapest_price is None:
        ac_cost = None
    else:
        kwh = inp.cool_power_kw * (deficit / inp.floor_cool_cph)
        ac_cost = round(kwh * inp.cheapest_price + inp.noise_penalty_kr, 2)

    if gap <= inp.peak_margin_c:
        rec, cost = "nothing", 0.0
        headline = "Comfortable as-is"
        detail = (f"Projected peak {peak_disp:.1f}C stays at/under the {limit:.1f}C sleep "
                  f"limit -- nothing needed tonight.")
    else:
        # A window COOLS whenever it's cooler outside than the target -- humidity is a separate
        # comfort question, not a reason to burn the compressor. Only run the AC when a window
        # genuinely can't do the job: too WARM outside, OR the outdoor air is meaningfully
        # MUGGIER than indoors (opening it imports real moisture -- not a knife-edge tie).
        cool_enough = (inp.outdoor_temp is not None
                       and inp.outdoor_temp < limit - inp.temp_margin_c)
        too_muggy = (inp.outdoor_dew is not None and inp.indoor_dew is not None
                     and inp.outdoor_dew - inp.indoor_dew > inp.muggy_slack_c)
        if cool_enough and not too_muggy:
            humid_note = ("" if (inp.outdoor_dew is None or inp.indoor_dew is None
                                 or inp.outdoor_dew <= inp.indoor_dew)
                          else " (it won't lower the humidity, but a window still cools it)")
            if gap <= inp.hybrid_gap_c:
                rec, cost = "windows", 0.0
                headline = "Open a window"
                detail = (f"Projected peak {peak_disp:.1f}C is {gap:.1f}C over the {limit:.1f}C "
                          f"limit, and it's cooler outside -- a window covers it for free.{humid_note}")
            else:
                rec, cost = "hybrid", ac_cost
                headline = f"Open windows now, AC backup {_cost_label(ac_cost)}"
                detail = (f"Projected peak {peak_disp:.1f}C is {gap:.1f}C over the {limit:.1f}C "
                          f"limit -- open windows now (cooler outside); keep the AC ready "
                          f"({_cost_label(ac_cost)}) if the room won't settle.{humid_note}")
        else:
            rec, cost = "ac", ac_cost
            headline = f"Run the AC {_cost_label(ac_cost)}"
            reason = ("it's not cool enough outside to open a window" if not cool_enough
                      else "opening a window would import muggy outdoor air")
            detail = (f"Projected peak {peak_disp:.1f}C is {gap:.1f}C over the {limit:.1f}C "
                      f"limit and {reason} -- pre-cool with the AC ({_cost_label(ac_cost)}).")

    base.update({
        "recommendation": rec,
        "projected_peak": peak_disp,
        "est_cost_kr": 0.0 if cost is None else cost,
        "cost_label": _cost_label(cost),
        "headline": headline,
        "detail": detail,
    })
    return base


# ------------------------------------------------------------- multi-night storage-advisor chain (A1 fit 2026-07-09)
# Moved from deploy_advisor.py -- its only consumer, and a SEPARATE model from the
# sleeping-zone coast law above: DeployAdvisor projects the whole apartment (kitchen/floor/
# bedroom-wall) night by night with its own validated regression chain (A1 fit, 2026-07-09
# background agent; chained 7-day validation peak MAE 0.46 C, target was 0.5), so it can warn
# days ahead of a too-warm night even while the AC sits torn down. deploy_advisor keeps
# module-level aliases (DEFAULT_FIT/kitchen_chain/floor_chain/b23_aux/night_peak/
# project_nights/daily_from_hourly) so its existing test surface is unchanged.

A1_FIT = {
    "k_tmax": 0.137,
    "k_ev": 0.508,
    "k_const": 0.799,
    "comfort_floor": 22.25,
    "f_k": 0.634,
    "f_ev": 0.138,
    "f_const": 0.472,
    "b_f": 1.062,
    "b_k": 0.063,
    "b_const": -2.918,
    "zone_uplift": 1.5,
}


def kitchen_chain(k, t_max, t_ev, c):
    """Next kitchen 23:00 reading given today's daily high (t_max) and evening (t_ev)
    temperature, chained forward one night at a time. Warms toward t_max, relaxes toward
    t_ev but never below c['comfort_floor'] -- the behavioral venting floor (people vent
    down to a habit, not to whatever the evening air actually is)."""
    return (k + c["k_tmax"] * max(0.0, t_max - k)
            + c["k_ev"] * (max(c["comfort_floor"], t_ev) - k) + c["k_const"])


def floor_chain(f, k_next, t_ev, c):
    """Next bedtime floor reading: pulled toward the next kitchen_chain value, plus a
    relief term when the evening is cooler than the floor (venting), zero otherwise."""
    return f + c["f_k"] * (k_next - f) + c["f_ev"] * min(0.0, t_ev - f) + c["f_const"]


def b23_aux(f_next, k_next, c):
    """Bedroom-wall 23:00 reading: a linear blend of the next floor_chain and kitchen_chain
    values, used only as the third leg of night_peak's equilibrium average."""
    return c["b_f"] * f_next + c["b_k"] * k_next + c["b_const"]


def night_peak(f, k, b, rise_frac, c):
    """One night's projected sleeping-zone peak: E = mean(kitchen, bedroom-wall, floor) is
    this chain's own equilibrium estimate, run through the SAME coast law as the rest of the
    module (coast_peak) instead of a second inline copy of floor + (E-floor)*r + zone_uplift
    (folded 2026-07-22 -- the arithmetic is unchanged, only the implementation is shared)."""
    e = (k + b + f) / 3.0
    return coast_peak(f, e, rise_frac, c["zone_uplift"])


def project_nights(k0, f0, b0, days, rise_frac, c):
    """days = [{'date', 't_max', 't_ev'}, ...]; day 0 = tonight (anchors are
    today's measured state, so tonight uses them directly). Returns one dict
    per night with the projected sleeping-zone peak at ~07:00."""
    out = []
    k, f, b = k0, f0, b0
    for i, d in enumerate(days):
        if i > 0:  # chain today's state forward through day i's weather
            k_next = kitchen_chain(k, d["t_max"], d["t_ev"], c)
            f = floor_chain(f, k_next, d["t_ev"], c)
            k = k_next
            b = b23_aux(f, k, c)
        peak = night_peak(f, k, b, rise_frac, c)
        out.append({"date": d["date"], "t_max": round(d["t_max"], 1),
                    "t_ev": round(d["t_ev"], 1), "kitchen": round(k, 1),
                    "floor": round(f, 1), "peak": round(peak, 1)})
    return out


def daily_from_hourly(hourly, today):
    """hourly = [(local_dt, temp)]; group into calendar days with t_max
    (06-23h high) and t_ev (22:00, fallback 21/23h). Skips incomplete days."""
    days = {}
    for dt, temp in hourly:
        days.setdefault(dt.date(), []).append((dt.hour, temp))
    out = []
    for date in sorted(days):
        if date < today:
            continue
        hours = dict(days[date])
        t_ev = hours.get(22, hours.get(21, hours.get(23)))
        daytime = [t for h, t in hours.items() if 6 <= h <= 23]
        if t_ev is None or not daytime:
            continue
        out.append({"date": date.isoformat(), "t_max": max(daytime), "t_ev": t_ev})
    return out
