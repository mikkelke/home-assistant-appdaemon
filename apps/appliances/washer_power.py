"""
Pure power-signal maths for the washer monitor.

The Miele plug is an event-driven power sensor, so deciding "has the programme
actually finished?" is a signal-processing question: time-weighted statistics
over a tail window, pulse-edge detection, cadence locking, and a few threshold
gates. All of that is arithmetic over a list of (datetime, watts) points and
belongs nowhere near AppDaemon.

washer_monitor.py keeps the thin methods that fetch history, read the tuned
thresholds off `self`, log, and drive the state machine; every function here
takes its thresholds as explicit arguments and returns a value.

Sibling-import module (AppDaemon puts app dirs on sys.path), matching the
existing `import climate_model as cm` precedent in apps/climate/.
"""

import statistics
from datetime import timedelta

# Any window containing a reading above this is treated as containing a real
# heating burst, which disqualifies it from looking like a finished cycle.
HEATING_BURST_WATTS = 500



def looks_like_cycle_end(points, max_mean_w, max_peak_w, off_max_mean_w, off_max_peak_w):
    """True only if the recent power pattern looks like a real cycle end (anti-crease
    tail or machine off), not a mid-cycle rinse.

    Mid-cycle rinse: mean ~50-80W, peaks 150-250W. Real end: mean ~18-45W, peak <120W,
    or flat idle (mean <12W). Returns (ok, mean_w, peak_w) for logging.
    """
    if len(points) < 5:
        return (False, None, None)
    watts = [p for _, p in points]
    mean_w = statistics.mean(watts)
    peak_w = max(watts)
    # Real anti-crease or gentle tail: low mean, no high spikes (we saw mean ~18-20W, peak 47W).
    if mean_w <= max_mean_w and peak_w <= max_peak_w:
        return (True, mean_w, peak_w)
    # Machine fully off: flat idle (manual: 0-2.8W; allow a bit of sensor noise).
    if mean_w <= off_max_mean_w and peak_w <= off_max_peak_w:
        return (True, mean_w, peak_w)
    return (False, mean_w, peak_w)


# =========================================================================
# Heating bursts
# =========================================================================

# The heating element draws ~2000W. A burst opens above BURST_ON_WATTS and only
# closes again below BURST_OFF_WATTS, so a wobbling element counts once, not twice.
BURST_ON_WATTS = 1000
BURST_OFF_WATTS = 500


def count_heating_bursts(points, initial_max_w=0.0):
    """Count heating bursts and track the peak wattage across a power series.

    Returns (bursts, max_w). `initial_max_w` seeds the peak so a caller can fold
    history into a running maximum it already holds.
    """
    bursts = 0
    in_burst = False
    max_w = initial_max_w
    for _t, w in points:
        if w > max_w:
            max_w = w
        if w > BURST_ON_WATTS:
            if not in_burst:
                in_burst = True
                bursts += 1
        elif w < BURST_OFF_WATTS:
            in_burst = False
    return (bursts, max_w)


# =========================================================================
# Locating the cycle start in a power series
# =========================================================================

def find_first_sustained_high(points, start_w, window_seconds, needed):
    """First timestamp where power reached start_w `needed` times within window_seconds
    (i.e. a wash is genuinely underway, not a one-off spike). None if never."""
    for i, (t0, w0) in enumerate(points):
        if w0 < start_w:
            continue
        highs = 1
        for j in range(i + 1, len(points)):
            t1, w1 = points[j]
            if (t1 - t0).total_seconds() > window_seconds:
                break
            if w1 >= start_w:
                highs += 1
                if highs >= needed:
                    return t0
    return None


def find_start_after_gap(points, from_time, to_time, start_w, gap_min_seconds,
                         sustain_window_seconds=300):
    """First sustained high-power time following a long low-power gap after from_time.

    Corrects a stale cycle_start_time on restore (e.g. machine idle 10:00-13:40, real
    start 13:40). A gap is a continuous stretch with all readings below start_w lasting
    at least gap_min_seconds. Returns the datetime, or None when no such gap-then-start
    exists - including the case where the gap simply runs to the end of the window.
    """
    gap_start = None
    for i, (t, power) in enumerate(points):
        if t < from_time:
            continue
        if power < start_w:
            if gap_start is None:
                gap_start = t
        else:
            if gap_start is not None:
                gap_len = (t - gap_start).total_seconds()
                if gap_len >= gap_min_seconds:
                    # Found a long gap; first sustained high power after this is at t (current point)
                    # Require at least one more high reading soon after to be sure it's sustained
                    sustained_count = 1
                    for j in range(i + 1, len(points)):
                        if (points[j][0] - t).total_seconds() > sustain_window_seconds:
                            break
                        if points[j][1] >= start_w:
                            sustained_count += 1
                            if sustained_count >= 2:
                                return t
                    if sustained_count >= 1:
                        return t
                gap_start = None
    # Check if gap runs to end of window (still in gap at to_time)
    if gap_start is not None and (to_time - gap_start).total_seconds() >= gap_min_seconds:
        # Gap extends to now; no "after gap" high power in window - don't infer
        return None
    return None


# =========================================================================
# Reading implied power out of the cumulative energy counter
# =========================================================================

# Consecutive energy readings closer together than this are too noisy to divide by.
MIN_ENERGY_DELTA_SECONDS = 10


def high_power_end_times(points, active_w):
    """Timestamps that close a high-power interval in a cumulative-kWh series.

    For each consecutive pair, implied watts = delta_kWh * 1000 / delta_hours; when
    that exceeds active_w, the later timestamp is recorded. The last entry is the
    moment the machine was still consuming, which is what cycle-end estimation wants.
    """
    ends = []
    for i in range(1, len(points)):
        t1, e1 = points[i - 1]
        t2, e2 = points[i]
        delta_s = (t2 - t1).total_seconds()
        if delta_s < MIN_ENERGY_DELTA_SECONDS:
            continue
        implied_w = ((e2 - e1) * 1000) / (delta_s / 3600)
        if implied_w > active_w:
            ends.append(t2)
    return ends


def end_closest_to_expected(candidates, start_time, expected_duration_min,
                            min_minutes, max_minutes):
    """Pick the high-power end whose duration from start_time is nearest to
    expected_duration_min, ignoring any outside [min_minutes, max_minutes]. None if
    nothing is in range."""
    best_end = None
    best_diff = float("inf")
    for t in candidates:
        dur = (t - start_time).total_seconds() / 60
        if dur < min_minutes or dur > max_minutes:
            continue
        diff = abs(dur - expected_duration_min)
        if diff < best_diff:
            best_diff = diff
            best_end = t
    return best_end
