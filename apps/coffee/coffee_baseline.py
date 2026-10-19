"""The usual's baseline: what a brew of the same recipe and beans normally does. Pure.

The cohort is the complete brews with coverage of at least MIN_COVERAGE, variant usual or base, the
same recipe_key and the same beans, the newest MAX_N of them. With MIN_N or more the baseline is ready:
medians of total_s, cup_g and drawdown_s, each with pm (half the min-max range), and a band, the
pointwise min-max envelope of the cohort's curves at STEP_S (a brew holds its last value after its
end). With fewer it is building and carries the plan only. Each brew freezes the baseline it started
under, so its verdict and deltas never move afterwards.
"""

import hashlib
import statistics

MIN_N = 5
MAX_N = 20
MIN_COVERAGE = 0.9
STEP_S = 2
INSIDE_SHARE = 0.8
COHORT_VARIANTS = ("usual", "base")


def cohort(records, recipe_key, bean_id):
    """The records the baseline is made of, oldest first."""
    found = [r for r in records or []
             if r.get("outcome") == "complete" and r.get("variant") in COHORT_VARIANTS
             and (r.get("coverage") or 0) >= MIN_COVERAGE
             and (r.get("recipe") or {}).get("key") == recipe_key
             and (r.get("bean") or {}).get("id") == bean_id]
    found.sort(key=lambda r: r.get("started_at") or "")
    return found[-MAX_N:]


def baseline_id(ids, recipe_key, bean_id):
    text = "|".join([recipe_key or "", bean_id or ""] + list(ids))
    return "bl" + hashlib.sha1(text.encode()).hexdigest()[:8]


def _median_pm(values):
    values = [v for v in values if isinstance(v, (int, float))]
    if not values:
        return None, None
    return statistics.median(values), (max(values) - min(values)) / 2


def _held(series, length):
    """`series` at STEP_S, extended to `length` with its last valid value."""
    stepped = list(series[::STEP_S])
    last = next((v for v in reversed(series) if v is not None), None)
    return stepped + [last] * (length - len(stepped))


def envelope(curves):
    """(lo, hi): pointwise min and max at STEP_S of the 1 s series `curves`; None where none has a value."""
    curves = [c for c in curves if c]
    if not curves:
        return [], []
    length = max(len(c[::STEP_S]) for c in curves)
    held = [_held(c, length) for c in curves]
    lo, hi = [], []
    for i in range(length):
        values = [h[i] for h in held if h[i] is not None]
        lo.append(min(values) if values else None)
        hi.append(max(values) if values else None)
    return lo, hi


def compute(records, curves, recipe, recipe_key, bean_id, plan, now_iso):
    """The baseline (numbers, lists in tenths) for the usual `recipe` with key `recipe_key` and the beans
    `bean_id`. `curves` is {brew id: curve file data}; `plan` is {water, pours, total_s}."""
    members = cohort(records, recipe_key, bean_id)
    ids = [r["id"] for r in members]
    out = {"state": "building", "id": baseline_id(ids, recipe_key, bean_id), "recipe_id": recipe.get("id"),
           "recipe_key": recipe_key, "bean_id": bean_id, "n": len(members), "brew_ids": ids, "step_s": STEP_S,
           "plan_water": plan["water"], "plan_pours": plan["pours"], "plan_total_s": plan["total_s"],
           "computed_at": now_iso}
    if len(members) < MIN_N:
        return out
    for field in ("total_s", "cup_g", "drawdown_s"):
        median, pm = _median_pm([r.get(field) for r in members])
        if median is not None:
            out[f"{field}_median"], out[f"{field}_pm"] = median, pm
    with_curves = [curves.get(i) for i in ids if curves.get(i)]
    out["cup_lo"], out["cup_hi"] = envelope([c.get("cup") or [] for c in with_curves])
    out["water_lo"], out["water_hi"] = envelope([c.get("water") or [] for c in with_curves])
    out["state"] = "ready" if "total_s_median" in out else "building"
    return out


def none_baseline(now_iso):
    return {"state": "none", "n": 0, "brew_ids": [], "step_s": STEP_S, "computed_at": now_iso}


def frozen(baseline):
    """The copy a brew keeps from its start: what its ETA, verdict and deltas need."""
    if not baseline or baseline.get("state") == "none":
        return None
    keys = ("state", "id", "n", "recipe_key", "bean_id", "total_s_median", "total_s_pm", "cup_g_median",
            "cup_g_pm", "drawdown_s_median", "drawdown_s_pm", "cup_lo", "cup_hi", "water_lo", "water_hi",
            "plan_total_s")
    return {k: baseline[k] for k in keys if k in baseline}


def ready(base):
    return bool(base) and base.get("state") == "ready"


def eta(first_bloom_ts, base, plan_total, paused_s):
    """(epoch s, basis) of the end of the brew: the first bloom plus the baseline's median total when it is
    ready, else plus the plan; shifted by the time spent paused."""
    if first_bloom_ts is None:
        return None, None
    if ready(base) and base.get("total_s_median"):
        return first_bloom_ts + base["total_s_median"] + paused_s, "baseline"
    if plan_total:
        return first_bloom_ts + plan_total + paused_s, "plan"
    return None, None


def _band_at(base, second):
    index = int(second) // STEP_S
    lo, hi = base.get("cup_lo") or [], base.get("cup_hi") or []
    if index >= len(lo) or lo[index] is None or hi[index] is None:
        return None
    return lo[index], hi[index]


def verdict(base, second, cup_tenths):
    """on_track, slower, ahead, or building; None when it cannot be told this second."""
    if not ready(base):
        return "building"
    if cup_tenths is None:
        return None
    band = _band_at(base, second)
    if band is None:
        return None
    lo, hi = band
    return "slower" if cup_tenths < lo else "ahead" if cup_tenths > hi else "on_track"


def in_band(base, cup_series):
    """inside, above, below or building, for a finished brew's 1 s cup series."""
    if not ready(base):
        return "building"
    inside = above = below = 0
    for second in range(0, len(cup_series), STEP_S):
        value, band = cup_series[second], _band_at(base, second)
        if value is None or band is None:
            continue
        if value < band[0]:
            below += 1
        elif value > band[1]:
            above += 1
        else:
            inside += 1
    counted = inside + above + below
    if not counted:
        return "building"
    if inside / counted >= INSIDE_SHARE:
        return "inside"
    return "above" if above >= below else "below"


def signed(value):
    value = int(round(value))
    return f"+{value}" if value > 0 else str(value)


def deltas(totals, base):
    """{d_total_s, d_cup_g, d_drawdown_s} of a finished brew against its frozen baseline, as "+4"/"-3"."""
    if not ready(base):
        return {}
    out = {}
    for field in ("total_s", "cup_g", "drawdown_s"):
        if totals.get(field) is not None and base.get(f"{field}_median") is not None:
            out[f"d_{field}"] = signed(totals[field] - base[f"{field}_median"])
    return out
