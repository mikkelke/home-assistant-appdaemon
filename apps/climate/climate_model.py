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
  - compose_briefing / nice_cost: the ONE-VOICE verdict copy (title + bare instruction)
    shared by the morning push, the evening rescue framing and the Tonight card
  - resolve_wake: the next wake moment (enabled alarm, else 07:00 workdays /
    09:00 weekends -- user 2026-07-29)
  - multi-night storage-advisor chain (A1 fit): DeployAdvisor's separate night-ahead
    apartment-mass projector (kitchen_chain/floor_chain/b23_aux/night_peak/project_nights)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
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
                         reality_margin=1.0, warm_night_margin=1.0,
                         cold_front_out_max=None, cold_front_anchor_offset=6.5):
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
      - COLD FRONT (``night_outdoor < cold_front_out_max``, disabled when None): "right
        now" is itself a stale anchor -- the whole flat drains overnight. Night of
        2026-08-15: kitchen 28.4 at noon -> 20.4 at 01:00 while outdoor fell to 13.6; the
        anchor was 2C+ colder by 03:00 than at the 21:00 tick, so the plan pre-cooled
        against warmth that never survived the night (and the post-midnight tier ran on
        top). The flat bottoms near the night's outdoor minimum plus a building offset
        (measured 6.8C that night), so additionally cap at
        ``night_outdoor + cold_front_anchor_offset``.

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
    e = min(e_weather, apartment_now + reality_margin)
    if cold_front_out_max is not None and night_outdoor < cold_front_out_max:
        e = min(e, night_outdoor + cold_front_anchor_offset)
    return e


def vented_zone_at(zone_now, vent_temp, hours, tau_h=7.0, min_gap_c=1.5):
    """Where the bedroom zone lands after ``hours`` of FREE VENTING toward ``vent_temp``.

    The advisory anchors "reality" on the zone temperature, but the room is not sealed until
    bedtime -- so anchoring on the reading taken NOW treats a morning still holding yesterday's
    heat as if it were tonight's starting point. On 2026-08-01 that produced "Set up the AC" at
    08:34 with the zone at 23.4C, 6 windows open and 16C outside: by bedtime the room could not
    possibly still be at 23.4C. Same class of mistake as the 2026-07-29 streak (judging window
    feasibility on the dawn minimum) -- an advisory about TONIGHT must be evaluated on tonight's
    conditions, not on the instant it happens to run.

    Newton cooling toward the venting air: tau ~7 h for this bedroom with a window open,
    from the measured 2026-07-20 free-cool (22->20C in 3 h against ~16C outdoor => gap x0.67,
    tau = 3/ln(1.5) ~ 7.4 h). Deliberately conservative in the warm direction:
      - no benefit claimed unless the air is at least ``min_gap_c`` cooler than the zone;
      - the result never drops below ``vent_temp + 1`` (a vented room approaches outdoor, it
        does not beat it);
      - caller must only pass venting hours it believes will actually happen (windows open).
    None-safe and monotonic: bad/missing inputs return ``zone_now`` unchanged (today's
    behaviour), so a failure here can only make the plan warmer/safer, never cooler.
    """
    if zone_now is None or vent_temp is None or hours is None:
        return zone_now
    if hours <= 0 or zone_now - vent_temp < min_gap_c:
        return zone_now
    settled = vent_temp + (zone_now - vent_temp) * math.exp(-hours / max(0.5, tau_h))
    return max(settled, vent_temp + 1.0)


def next_bedtime(now, wake_dt, sleep_hours):
    """TONIGHT's bedtime. Before the morning wake, wake - sleep_hours is LAST night's
    bedtime (in the past): venting-window math anchored on it collapses to zero hours, the
    warm sealed room anchors the plan, and the briefing over-calls (2026-08-07: "Set up the
    AC" at 06:35, plan said "nothing" at 06:48 once the wake rolled over). Roll a past
    bedtime one day forward during the morning; an evening past bedtime stays put - the
    night underway is sealed, and zero venting is then the truth."""
    bedtime = wake_dt - timedelta(hours=sleep_hours)
    if bedtime <= now and now.hour < 12:
        bedtime += timedelta(hours=24)
    return bedtime


def vented_zone_hours(zone_now, hourly_temps, tau_h=7.0, min_gap_c=1.5):
    """Hour-by-hour version of vented_zone_at: walk the venting window one hour at a time,
    cooling toward THAT hour's forecast outdoor temp, and crediting only hours genuinely
    cooler than the zone (min_gap_c). Warm hours change nothing - no cooling credit, and no
    heating either (the household closes windows when it's hotter outside, and day-heat is
    already the weather model's job).

    Why not the scalar version: it vented toward max(outdoor-now, night-low) for the WHOLE
    window, and a plan computed at 08:30 reads the day's minimum as "outdoor-now" - so a
    15 h projection cooled toward dawn air that only exists for two of those hours. That
    made the mornings of 2026-08-03/05 say windows/nothing while the flat heat-soaked into
    24.0C and 25.0C nights. Feeding the actual forecast hours kills the dawn optimism: the
    22-26C midday hours credit nothing, and only the genuinely cool evening hours pull the
    anchor down.

    None-safe and conservative: no/empty forecast returns zone_now unchanged (the caller
    falls back to the scalar heuristic or the raw reading); the result never drops below
    the coolest credited hour + 1 (venting approaches outdoor, it does not beat it)."""
    if zone_now is None or not hourly_temps:
        return zone_now
    zone = zone_now
    floor_seen = None
    for temp in hourly_temps:
        if temp is None:
            continue
        if zone - temp < min_gap_c:
            continue
        zone = temp + (zone - temp) * math.exp(-1.0 / max(0.5, tau_h))
        floor_seen = temp if floor_seen is None else min(floor_seen, temp)
    if floor_seen is not None:
        zone = max(zone, floor_seen + 1.0)
    return zone


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


# ------------------------------------------------------------- cooling time (two regimes)

def cooling_minutes(floor, target, fast_rate, wall=None,
                    crawl_headroom=0.7, crawl_rate=0.4):
    """Engaged minutes to take the floor from ``floor`` down to ``target``, honoring the
    unit's two cooling regimes (2026-07-30): above ``wall + crawl_headroom`` the machine
    runs at its measured fast rate (~1.7 C/h); below that knee the floor crawls toward the
    feasible wall at ~0.2-0.5 C/h regardless of the machine. Pricing a whole descent at the
    fast rate booked a 4.4C job as 165 min when the meter said the real shape of such a day
    is ~6 engaged hours -- so the scheduler bought too few cheap slots and topped up at
    worse prices later.

    With no learned wall the whole descent prices at the fast rate (old behavior). Returns
    0.0 when there is nothing to cool. Same knee constant as the rate learner's headroom
    gate, so "where we stop learning speed" and "where we start pricing crawl" agree."""
    if floor is None or target is None or floor <= target:
        return 0.0
    fast_rate = max(0.05, fast_rate)
    crawl_rate = max(0.05, crawl_rate)
    if wall is None:
        return (floor - target) / fast_rate * 60.0
    knee = wall + crawl_headroom
    fast_part = max(0.0, floor - max(target, knee))
    crawl_part = max(0.0, min(floor, knee) - target)
    return (fast_part / fast_rate + crawl_part / crawl_rate) * 60.0


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
    # DEPRECATED/UNUSED (2026-07-29 cost-model rebuild -- see kwh_per_deg below): kept only
    # so old callers/tests that still pass them don't break the constructor. plan_sleep no
    # longer reads either.
    session_factor: float = 2.5
    learned_night_cost: Optional[float] = None
    peak_margin_c: float = 0.2
    hybrid_gap_c: float = 1.5
    # Hard bound on the windows/hybrid bucket (2026-07-29): a gap this large is a heat-soaked
    # MASS, not merely warm air, and venting cannot rescue that no matter how cool the outside
    # air is -- see plan_sleep's docstring for the incident this fixes.
    windows_max_gap_c: float = 2.5
    temp_margin_c: float = 0.5      # outdoor must be at least this far below the limit to cool
    muggy_slack_c: float = 2.0      # outdoor dew this much above indoor before it's "too muggy"
    # The temperature judged for window feasibility WHEN the cooling would actually happen
    # (overnight), not right now -- see plan_sleep's docstring for why this matters. None
    # falls back to outdoor_temp (the old behaviour).
    night_outdoor: Optional[float] = None
    # kWh spent per degree (C) of pre-cool deficit closed -- learned from real metered
    # sessions (SmartCooling._finalize_session); see plan_sleep's docstring for provenance.
    kwh_per_deg: float = 1.6


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
    the night for the least money (windows cost 0, AC = a real night's cost), planning the
    whole night rather than the current instant.

    AC cost is a SCALING model, kWh spent per degree (C) of pre-cool deficit closed
    (inp.kwh_per_deg): kwh = deficit * kwh_per_deg; ac_cost = kwh * cheapest_price. This
    replaced a flat learned-kr-per-night EMA (inp.learned_night_cost) on 2026-07-29: that
    EMA was poisoned by 3 finalized 0.00 kWh sessions (armed+deployed nights the compressor
    never actually ran) dragging it down to 0.36 kr while 15 metered days (2026-07-10..29)
    actually averaged 4.50 kr -- see SmartCooling.session_min_kwh, which now excludes those
    trivial sessions from every learned value. kwh_per_deg is itself learned the same way
    (SmartCooling._finalize_session: session_kwh / the deficit captured at session start),
    seeded at kwh_per_deg_default (1.6) from that same 15-day window -- genuine cooling
    nights ran 2.7-7.4 kWh for ~3-4C deficits (e.g. 2026-07-17: 6.27 kWh / 10.61 kr).
    inp.session_factor/inp.learned_night_cost are DEPRECATED and no longer read (kept only
    so old callers/tests don't break the constructor); noise_penalty_kr likewise no longer
    inflates the displayed cost -- both kept only for backward-compat.

    projected_peak = coast_peak(floor, equilibrium, rise_frac, zone_offset). cool_enough
    (whether a window could do the job) is judged against inp.night_outdoor -- the
    temperature expected WHEN the cooling would actually happen, overnight -- falling back
    to inp.outdoor_temp only if that's unavailable. This fixes the 2026-07-29 case: at
    05:30 the CURRENT outdoor reading is the day's daily minimum, so judging feasibility
    against it made a window look sufficient every single morning, even projecting a
    25.9C peak against a 22.5C limit ("AC not needed" at 05:55; the plan itself flipped to
    'ac' by 10:00 once the day's real numbers were in).
      - peak within peak_margin_c of the limit           -> 'nothing' (free)
      - else cool_enough AND not meaningfully muggier than indoors:
          gap <= hybrid_gap_c                             -> 'windows'  (free)
          gap <= windows_max_gap_c                        -> 'hybrid'  (windows now + AC backup)
          gap  > windows_max_gap_c                        -> 'ac'      (too big for venting alone)
      - else (too WARM, or genuinely MUGGY, outside)      -> 'ac'
    The windows_max_gap_c bound (2026-07-29) exists because the hybrid bucket used to be
    unbounded: any gap above hybrid_gap_c with cool-enough air read as 'hybrid', which
    compose_briefing then reported as "AC not needed" -- reported live with a 3.4C gap
    (projected peak 25.9C vs a 22.5C limit) that should have been an unambiguous 'ac'. A
    heat-soaked thermal mass cannot be rescued by venting no matter how cool the air
    outside is; past windows_max_gap_c a window is no longer credible.
    A window cools whenever it's cooler outside; humidity merely level with indoors is a note,
    NOT a reason to run the compressor. Windows always beat equal-comfort AC (0 < ac_cost).

    Returns a plain dict (recommendation/projected_peak/comfort_limit/est_cost_kr/
    cost_label/headline/detail/open_windows/windows_summary/deficit). ADVISORY ONLY.
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

    # AC cost of pre-cooling the floor deep enough to keep the zone under the limit: the
    # scaling model (see the docstring), kWh = deficit(C) * kwh_per_deg.
    target = calc_floor_target(inp.equilibrium, limit, inp.rise_frac,
                               inp.zone_offset, inp.min_temp)
    deficit = max(0.0, inp.floor - target)
    if inp.cheapest_price is None:
        ac_cost = None
    else:
        kwh = deficit * inp.kwh_per_deg
        ac_cost = round(kwh * inp.cheapest_price, 2)

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
        #
        # cool_enough is judged against night_outdoor (the temperature overnight, when the
        # cooling would actually happen) when known, falling back to the current outdoor_temp
        # reading only if it's missing -- see the docstring for why the current reading alone
        # is unsafe (it's the daily minimum at 05:30, every single morning).
        night_temp = inp.night_outdoor if inp.night_outdoor is not None else inp.outdoor_temp
        cool_enough = (night_temp is not None
                       and night_temp < limit - inp.temp_margin_c)
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
            elif gap <= inp.windows_max_gap_c:
                rec, cost = "hybrid", ac_cost
                headline = f"Open windows now, AC backup {_cost_label(ac_cost)}"
                detail = (f"Projected peak {peak_disp:.1f}C is {gap:.1f}C over the {limit:.1f}C "
                          f"limit -- open windows now (cooler outside); keep the AC ready "
                          f"({_cost_label(ac_cost)}) if the room won't settle.{humid_note}")
            else:
                # Gap too large for venting alone even though the air outside is cool -- a
                # heat-soaked mass, not a hot-air problem (2026-07-29: a 3.4C gap here used
                # to read as 'hybrid'/"AC not needed" before this bound existed).
                rec, cost = "ac", ac_cost
                headline = f"Run the AC {_cost_label(ac_cost)}"
                detail = (f"Projected peak {peak_disp:.1f}C is {gap:.1f}C over the {limit:.1f}C "
                          f"limit -- too big a gap for a window alone, even with cool air "
                          f"outside -- pre-cool with the AC ({_cost_label(ac_cost)}).")
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
        "deficit": round(deficit, 2),
    })
    return base


# ------------------------------------------------------------- one-voice verdict copy

def nice_cost(cost_label):
    """plan_sleep's cost_label ('~1.3 kr') -> prose ('about 1.3 kr'); None for the labels
    that shouldn't produce a cost clause at all ('free', 'cost unknown', empty)."""
    if not cost_label or cost_label in ("free", "cost unknown"):
        return None
    if cost_label.startswith("~"):
        return "about " + cost_label[1:].strip()
    return cost_label


def compose_briefing(plan_state, plan_attrs, status_attrs, ac_deployed, armed):
    """The ONE voice: title = the verdict, body = the bare instruction (user 2026-07-22,
    three copy rounds: "like Apple made it" -> "more decided" -> "still too chatty").
    Moved here from morning_briefing so the push, the card and any future surface render
    the SAME words from the SAME function -- copy can no longer drift between channels.

    plan_state is sleep_plan's recommendation ("windows"|"ac"|"hybrid"|"nothing"); an
    unrecognised value falls back to the plan's own headline. status_attrs is accepted
    for call-site stability but unused (the day-outlook line was cut). hybrid and ac render
    IDENTICAL copy (2026-08-28, user: "there is not control on window?" -- windows are a
    read-only contact sensor, never an actuator, so the AC is the only lever anyone can act
    on; hybrid vs ac only says how much of the gap the AC needs to cover, not what to do
    about it). plan_sleep still tracks the three-way split for its own reasoning/detail
    text and the dashboard's day strip -- only this ONE-voice verdict collapses it.
    TODAY only, by design (user 2026-07-29: "I just need to know what to do today...
    Nothing about tomorrow") -- the multi-day outlook lives on the card's day strip,
    never in the sentence."""
    plan_attrs = dict(plan_attrs or {})
    title = "Morning climate"
    body = ""

    cost = nice_cost(plan_attrs.get("cost_label"))

    if plan_state == "windows":
        title = "AC not needed"
        body = "Keep windows open."
        if ac_deployed:
            body += " You can stow the AC."
    elif plan_state == "nothing":
        title = "Nothing to do"
        body = "The bedroom stays cool on its own."
    elif plan_state in ("hybrid", "ac"):
        # Windows have no control lever (contact-sensor status only, never an actuator) --
        # the AC is the only decision anyone can actually act on, so hybrid (windows likely
        # won't fully close the gap) and ac (windows definitely won't) must read as the
        # identical instruction: get the AC ready. hybrid used to hedge with "windows may
        # not be enough tonight" wording, which just asked the user to weigh a lever they
        # don't have (user 2026-08-28: "I need to know what to do. You guide me.").
        if ac_deployed and armed:
            title = "AC handles tonight"
            body = "Already armed." + (f" {cost[0].upper()}{cost[1:]}." if cost else "")
        elif ac_deployed:
            title = "Arm the AC"
            body = "Just arm Cool night."
        else:
            title = "Deploy the AC"
            body = "Before you leave." + (f" {cost[0].upper()}{cost[1:]}." if cost else "")
    else:
        headline = (plan_attrs.get("headline") or "").strip()
        body = f"{headline}." if headline else ""

    return title, body


# ------------------------------------------------------------- wake time

def resolve_wake(now, alarm_hms, alarm_enabled,
                 workday_hms="07:00:00", weekend_hms="09:00:00"):
    """The next wake moment after ``now`` (naive local datetime in, naive out).

    The enabled alarm wins; with no alarm the fallback is day-typed: 07:00 on workdays,
    09:00 on weekends (user 2026-07-29), resolved against the day the wake actually lands
    on -- Friday evening resolves to Saturday 09:00, Sunday evening to Monday 07:00.
    Returns None when nothing parses (callers omit the wake display rather than guess)."""
    def _hms(v):
        try:
            parts = [int(x) for x in str(v).strip().split(":")]
        except (TypeError, ValueError):
            return None
        if not 1 <= len(parts) <= 3:
            return None
        parts += [0] * (3 - len(parts))
        try:
            return dtime(parts[0], parts[1], parts[2])
        except ValueError:
            return None

    for offset in (0, 1):
        day = now.date() + timedelta(days=offset)
        if alarm_enabled:
            t = _hms(alarm_hms)
        else:
            t = _hms(weekend_hms if day.weekday() >= 5 else workday_hms)
        if t is None:
            return None
        cand = datetime.combine(day, t)
        if cand > now:
            return cand
    return None


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
