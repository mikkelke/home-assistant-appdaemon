# tests/test_washer_completion.py - Acceptance tests for washer completion and validation refactor.
# Run from repo root: python3 -m unittest appdaemon.apps.appliances.tests.test_washer_completion
# These tests duplicate the validation/tail-window logic so they run without the AppDaemon runtime.

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Real WasherMonitor import for TestEcoPerTemperatureLearning below. washer_monitor imports
# cleanly standalone once appdaemon.plugins.hass.hassapi is stubbed - same trick used by
# test_washer_unavailable_grace.py in this directory (appdaemon isn't installed in the test env).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hassmod = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hassmod
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import washer_monitor as wm  # noqa: E402

# Test profile set (mirrors WasherMonitor _DEFAULT_PROFILES for programmes we test)
_TEST_PROFILES = {
    "uld": {"duration_min": 39, "max_energy_kwh": 0.28, "max_dur_min": 55, "supports_anti_crease": False},
    "strygelet": {"duration_min": 119, "max_energy_kwh": 0.52, "max_dur_min": 140},
    "bomuld": {"duration_min": 159, "max_energy_kwh": 0.42, "max_dur_min": 185},
    "eco": {"duration_min": 199, "max_energy_kwh": 0.78, "max_dur_min": 235},
    "unknown": {"duration_min": 180, "max_energy_kwh": 2.50, "max_dur_min": 240},
}


def _get_profile(programme, temperature=None):
    return _TEST_PROFILES.get(programme, _TEST_PROFILES.get("unknown", {}))


def classify_cycle_completion(
    run_minutes, energy_kwh, min_cycle_minutes, min_energy_kwh,
    completion_guard_fraction, completion_guard_fraction_user_confirmed,
    user_confirmed, confirmed, confirmed_temperature, transition_path,
):
    """Mirror of WasherMonitor._classify_cycle_completion for testing."""
    flags = []
    profile = _get_profile(confirmed, confirmed_temperature)
    nominal_dur = profile.get("duration_min", 180)
    max_dur = profile.get("max_dur_min", int(nominal_dur * 1.2))
    frac = completion_guard_fraction_user_confirmed if user_confirmed else completion_guard_fraction
    min_valid_dur = max(frac * nominal_dur, min_cycle_minutes)
    min_energy = profile.get("min_valid_energy_kwh", min_energy_kwh)
    max_energy = profile.get("max_energy_kwh", 3.0)

    if run_minutes < min_valid_dur:
        flags.append("runtime_too_short")
    if run_minutes > max_dur:
        flags.append("duration_too_long")
    if energy_kwh < min_energy:
        flags.append("energy_too_low")
    if energy_kwh > max_energy:
        flags.append("energy_too_high")
    if transition_path == "door_opened_first":
        flags.append("door_opened_first")
    if confirmed in ("unknown", "") or not confirmed:
        flags.append("unknown_programme")

    if "runtime_too_short" in flags and run_minutes < min_cycle_minutes:
        completion_class = "interrupted"
    elif "runtime_too_short" in flags or "energy_too_low" in flags or "energy_too_high" in flags:
        completion_class = "suspect"
    elif transition_path == "door_opened_first" and ("runtime_too_short" in flags or "energy_too_low" in flags):
        completion_class = "suspect"
    else:
        completion_class = "completed"

    # Duration too long (e.g. a delayed-start wait that slipped past the trim guards) - never
    # trust it for learning, even if it otherwise looked "completed".
    if "duration_too_long" in flags and completion_class != "interrupted":
        completion_class = "suspect"

    valid_for_learning = (
        completion_class == "completed"
        and "runtime_too_short" not in flags
        and "energy_too_low" not in flags
        and "unknown_programme" not in flags
    )
    if transition_path == "door_opened_first" and completion_class != "completed":
        valid_for_learning = False
    if "duration_too_long" in flags:
        valid_for_learning = False

    return {"completion_class": completion_class, "valid_for_learning": valid_for_learning, "validation_flags": flags}


def is_post_end_tail_window(run_min, expected_dur, programme, anti_crease_near_end_minutes, anti_crease_min_runtime_minutes):
    """Mirror of WasherMonitor._is_post_end_tail_window."""
    if programme and programme != "unknown":
        if expected_dur and run_min >= expected_dur - anti_crease_near_end_minutes:
            return True
        if expected_dur and run_min >= expected_dur:
            return True
        return False
    return run_min >= anti_crease_min_runtime_minutes


def detect_anti_crease_pattern_from_points(watts_list, tail_max_mean_w, tail_min_std_w, max_duty_above_active, active_w):
    """Compute anti-crease pattern result from a list of watt values (mirror of _detect_anti_crease_pattern logic)."""
    if len(watts_list) < 5:
        return (False, None, None, None)
    import statistics
    mean_w = statistics.mean(watts_list)
    try:
        std_w = statistics.stdev(watts_list)
    except statistics.StatisticsError:
        std_w = 0.0
    peak_w = max(watts_list)
    duty_above = sum(1 for w in watts_list if w > active_w) / len(watts_list)
    ok = (
        mean_w <= tail_max_mean_w
        and std_w >= tail_min_std_w
        and duty_above <= max_duty_above_active
        and not any(w > 500 for w in watts_list)
    )
    return (ok, mean_w, std_w, peak_w)


def anti_crease_past_expected_finish(run_min, guard_dur, announce_past_expected, is_valid_completed_cycle):
    """Mirror of the new fast-finish decision added to WasherMonitor._check_energy_finish's
    anti-crease handling block (2026-07-30): once _detect_anti_crease_pattern() succeeds (tail_ok),
    this decides whether to skip FinishingTail/_try_finish_via_standby entirely and call
    _transition_to_unemptied() directly. guard_dur is the same expected-duration source
    _meets_finish_time_guards compares against - run_min must be STRICTLY past it, not merely
    within anti_crease_near_end_minutes (that near-but-not-past case is _is_post_end_tail_window's
    job, already satisfied by the caller before this decision runs)."""
    if not announce_past_expected:
        return False
    if not guard_dur or run_min < guard_dur:
        return False
    return bool(is_valid_completed_cycle)


def zero_power_reset_tolerated(current_power, run_min, guard_dur, announce_past_expected, anti_crease_pattern_ok):
    """Mirror of the new zero-power-backstop reset tolerance added to
    WasherMonitor._check_energy_finish's standby-backstop 'else' branch (2026-07-30): a brief
    anti-crease tumble (< 120W) must not reset _zero_power_since once run_min is past guard_dur
    and the anti-crease pattern is currently confirmed - it is post-end activity, not cycle
    activity. Returns True when the reset should be skipped (tolerated)."""
    if not announce_past_expected:
        return False
    if current_power >= 120.0:
        return False
    if not guard_dur or run_min < guard_dur:
        return False
    return bool(anti_crease_pattern_ok)


def slide_start_if_delayed(
    samples, start_ts, delay_plateau_minutes, start_w, energy_floor_kwh,
    observed_heating, cum_energy_kwh, heating_onset_ts=None,
):
    """Mirror of WasherMonitor._maybe_handle_delayed_start / _slide_start_for_delayed_start's
    plateau/slide decision.

    samples: chronological list of (timestamp, watts) as _check_energy_finish would see tick by
    tick. Gated on observed_heating/cum_energy_kwh exactly like the live gate (real wash evidence
    from the start means this was never a delayed start - never trim). While gated, tracks how
    long power has stayed below start_w; the first sample >= start_w after a plateau of at least
    delay_plateau_minutes becomes the new start. heating_onset_ts models heating/energy evidence
    appearing MID-stream: the gate closes at that tick, and (like the live gate-disarm branch) a
    qualifying open plateau slides at that moment - heating right after the wait is itself the
    resume signal. Returns the (possibly slid) start.
    """
    if observed_heating or cum_energy_kwh >= energy_floor_kwh:
        return start_ts
    plateau_start = None
    for ts, watts in samples:
        if heating_onset_ts is not None and ts >= heating_onset_ts:
            # Gate disarms here (heating/energy evidence). A qualifying open plateau slides
            # at the disarm tick; otherwise detection simply ends for the cycle.
            if plateau_start is not None and (ts - plateau_start).total_seconds() / 60 >= delay_plateau_minutes:
                return ts
            return start_ts
        if watts < start_w:
            if plateau_start is None:
                plateau_start = ts
        else:
            if plateau_start is not None:
                plateau_min = (ts - plateau_start).total_seconds() / 60
                if plateau_min >= delay_plateau_minutes:
                    return ts
            plateau_start = None
    return start_ts


def _build_power_samples(start_ts, segments, step_minutes=1):
    """Build a chronological (timestamp, watts) list from segments = [(duration_min, watts), ...],
    sampled every step_minutes within each segment (helper for delayed-start tests)."""
    samples = []
    t = start_ts
    for duration_min, watts in segments:
        steps = max(1, round(duration_min / step_minutes))
        for _ in range(steps):
            samples.append((t, watts))
            t = t + timedelta(minutes=step_minutes)
    return samples


def accumulate_session_cost(ticks, price_fallback_kr):
    """Mirror of WasherMonitor._check_energy_finish's per-tick settled per-cycle cost
    accumulation (self._session_cost_kr).

    ticks: chronological list of (energy_kwh, price_or_none) pairs - energy_kwh is the
    cumulative meter reading for that tick; price_or_none is None to model an
    unavailable/unknown price_entity reading (price_fallback_kr is used instead, same tick
    only). The first tick only establishes the energy baseline (nothing to charge yet -
    mirrors _cost_prev_energy_kwh starting at None). A negative delta (energy meter reset)
    is clamped to 0 rather than subtracting energy that was never actually used. Returns the
    total accumulated cost (kr).
    """
    cost_kr = 0.0
    prev_energy = None
    for energy_kwh, price in ticks:
        if prev_energy is None:
            prev_energy = energy_kwh
            continue
        delta_kwh = energy_kwh - prev_energy
        if delta_kwh < 0:
            delta_kwh = 0.0
        price_used = price if price is not None else price_fallback_kr
        cost_kr += delta_kwh * price_used
        prev_energy = energy_kwh
    return cost_kr


class TestClassifyCycleCompletion(unittest.TestCase):
    """Tests for classification (validation engine) logic."""

    def test_bad_historical_strygelet_suspect(self):
        """Bad historical sample: strygelet 61.8 min, 0.039 kWh -> suspect, not learnable."""
        result = classify_cycle_completion(
            run_minutes=61.8, energy_kwh=0.039,
            min_cycle_minutes=25, min_energy_kwh=0.1,
            completion_guard_fraction=0.65, completion_guard_fraction_user_confirmed=0.60,
            user_confirmed=True, confirmed="strygelet", confirmed_temperature="30°C",
            transition_path="low_power_detected",
        )
        self.assertEqual(result["completion_class"], "suspect")
        self.assertFalse(result["valid_for_learning"])
        self.assertIn("energy_too_low", result["validation_flags"])

    def test_normal_cycle_completed_learnable(self):
        """Normal cycle ends by low power -> completed, valid_for_learning=True."""
        result = classify_cycle_completion(
            run_minutes=155.0, energy_kwh=0.40,
            min_cycle_minutes=25, min_energy_kwh=0.1,
            completion_guard_fraction=0.65, completion_guard_fraction_user_confirmed=0.60,
            user_confirmed=True, confirmed="bomuld", confirmed_temperature="30°C",
            transition_path="low_power_detected",
        )
        self.assertEqual(result["completion_class"], "completed")
        self.assertTrue(result["valid_for_learning"])

    def test_door_opened_first_plausible_completed(self):
        """Door opened before low-power finish, runtime/energy plausible -> completed if guards pass."""
        result = classify_cycle_completion(
            run_minutes=120.0, energy_kwh=0.50,
            min_cycle_minutes=25, min_energy_kwh=0.1,
            completion_guard_fraction=0.65, completion_guard_fraction_user_confirmed=0.60,
            user_confirmed=True, confirmed="strygelet", confirmed_temperature="30°C",
            transition_path="door_opened_first",
        )
        self.assertEqual(result["completion_class"], "completed")
        self.assertTrue(result["valid_for_learning"])
        self.assertIn("door_opened_first", result["validation_flags"])

    def test_early_aborted_interrupted(self):
        """Wrong program stopped at 2–3 min -> interrupted."""
        result = classify_cycle_completion(
            run_minutes=2.5, energy_kwh=0.02,
            min_cycle_minutes=25, min_energy_kwh=0.1,
            completion_guard_fraction=0.65, completion_guard_fraction_user_confirmed=0.60,
            user_confirmed=False, confirmed="eco", confirmed_temperature=None,
            transition_path="door_opened_first",
        )
        self.assertEqual(result["completion_class"], "interrupted")
        self.assertFalse(result["valid_for_learning"])
        self.assertIn("runtime_too_short", result["validation_flags"])

    def test_manual_stop_eco_suspect(self):
        """Manual stop after 20–30 min on ECO -> suspect, not learnable."""
        result = classify_cycle_completion(
            run_minutes=28.0, energy_kwh=0.2,
            min_cycle_minutes=25, min_energy_kwh=0.1,
            completion_guard_fraction=0.65, completion_guard_fraction_user_confirmed=0.60,
            user_confirmed=True, confirmed="eco", confirmed_temperature=None,
            transition_path="low_power_detected",
        )
        self.assertEqual(result["completion_class"], "suspect")
        self.assertFalse(result["valid_for_learning"])
        self.assertIn("runtime_too_short", result["validation_flags"])

    def test_uld_supports_anti_crease_false(self):
        """Uld profile has supports_anti_crease False (manual: Uld is the exception)."""
        profile = _get_profile("uld", "30°C")
        self.assertFalse(profile.get("supports_anti_crease", True))

    def test_duration_too_long_eco_suspect(self):
        """366.8 min on ECO (max valid 235 min) - e.g. an unrimmed delayed-start wait leaking
        into the recorded duration -> suspect, not learnable, even though runtime/energy alone
        would otherwise pass."""
        result = classify_cycle_completion(
            run_minutes=366.8, energy_kwh=0.6,
            min_cycle_minutes=25, min_energy_kwh=0.1,
            completion_guard_fraction=0.65, completion_guard_fraction_user_confirmed=0.60,
            user_confirmed=True, confirmed="eco", confirmed_temperature=None,
            transition_path="low_power_detected",
        )
        self.assertEqual(result["completion_class"], "suspect")
        self.assertFalse(result["valid_for_learning"])
        self.assertIn("duration_too_long", result["validation_flags"])


class TestPostEndTailWindow(unittest.TestCase):
    """Tests for _is_post_end_tail_window (anti-crease eligibility)."""

    def test_near_expected_end_true(self):
        """Run time within anti_crease_near_end_minutes of expected end -> True."""
        ok = is_post_end_tail_window(140.0, 159.0, "bomuld", 25, 60)
        self.assertTrue(ok)

    def test_past_expected_end_true(self):
        """Run time past expected end -> True."""
        ok = is_post_end_tail_window(165.0, 159.0, "bomuld", 25, 60)
        self.assertTrue(ok)

    def test_mid_cycle_false(self):
        """Runtime not near expected end (mid-cycle) -> False (false anti-crease guard)."""
        ok = is_post_end_tail_window(60.0, 159.0, "bomuld", 25, 60)
        self.assertFalse(ok)

    def test_unknown_programme_min_runtime(self):
        """When programme unknown, allow after anti_crease_min_runtime_minutes."""
        ok = is_post_end_tail_window(70.0, 180.0, "unknown", 25, 60)
        self.assertTrue(ok)


class TestDetectAntiCreasePattern(unittest.TestCase):
    """Tests for anti-crease pattern (raw power stats) logic."""

    def test_tail_pattern_detected(self):
        """Low mean + sufficient std (periodic bumps) -> pattern detected. Real anti-crease is very low power."""
        watts = [12.0 + (i % 5) * 10 for i in range(20)]  # mean ~32W, peak 52W
        ok, mean_w, std_w, peak_w = detect_anti_crease_pattern_from_points(
            watts, tail_max_mean_w=40.0, tail_min_std_w=6.0,
            max_duty_above_active=0.15, active_w=100.0,
        )
        self.assertTrue(ok)
        self.assertIsNotNone(mean_w)
        self.assertIsNotNone(std_w)

    def test_flat_idle_not_anti_crease(self):
        """Flat low power (no variance) -> not anti-crease pattern (std too low)."""
        watts = [25.0] * 20
        ok, mean_w, std_w, peak_w = detect_anti_crease_pattern_from_points(
            watts, tail_max_mean_w=40.0, tail_min_std_w=6.0,
            max_duty_above_active=0.15, active_w=100.0,
        )
        self.assertFalse(ok)
        self.assertLess(std_w or 0, 6.0)


class TestAntiCreasePastExpectedFinish(unittest.TestCase):
    """Tests for the new past-expected-end fast-finish decision (2026-07-30): once a confirmed
    anti-crease pattern is seen STRICTLY past the expected duration, announce immediately instead
    of waiting through FinishingTail's tail-pulse-timeout quiesce (mirrors the dryer's keep-fresh
    transition). Near-but-not-past-end and flag-off must keep the old FinishingTail path."""

    def test_past_expected_and_valid_transitions_directly(self):
        ok = anti_crease_past_expected_finish(
            run_min=170.0, guard_dur=159.0, announce_past_expected=True, is_valid_completed_cycle=True,
        )
        self.assertTrue(ok)

    def test_near_but_not_past_keeps_old_path(self):
        """Within anti_crease_near_end_minutes of expected end but NOT past guard_dur -> old FinishingTail path."""
        ok = anti_crease_past_expected_finish(
            run_min=140.0, guard_dur=159.0, announce_past_expected=True, is_valid_completed_cycle=True,
        )
        self.assertFalse(ok)

    def test_exactly_at_expected_end_transitions_directly(self):
        """run_min == guard_dur counts as past (>=), matching _meets_finish_time_guards' own >= semantics."""
        ok = anti_crease_past_expected_finish(
            run_min=159.0, guard_dur=159.0, announce_past_expected=True, is_valid_completed_cycle=True,
        )
        self.assertTrue(ok)

    def test_flag_off_keeps_old_path_even_past_expected(self):
        ok = anti_crease_past_expected_finish(
            run_min=170.0, guard_dur=159.0, announce_past_expected=False, is_valid_completed_cycle=True,
        )
        self.assertFalse(ok)

    def test_past_expected_but_invalid_cycle_keeps_old_path(self):
        ok = anti_crease_past_expected_finish(
            run_min=170.0, guard_dur=159.0, announce_past_expected=True, is_valid_completed_cycle=False,
        )
        self.assertFalse(ok)

    def test_missing_guard_dur_keeps_old_path(self):
        """guard_dur falsy (None/0, e.g. undetermined) -> never trigger the fast path."""
        ok = anti_crease_past_expected_finish(
            run_min=170.0, guard_dur=0, announce_past_expected=True, is_valid_completed_cycle=True,
        )
        self.assertFalse(ok)


class TestZeroPowerBackstopTumbleTolerance(unittest.TestCase):
    """Tests for the zero-power-backstop reset tolerance (2026-07-30): once past expected end
    with a confirmed anti-crease pattern, a brief tumble must not reset _zero_power_since (every
    tumble would otherwise push the 3/5-minute backstop thresholds back out indefinitely)."""

    def test_past_expected_pattern_confirmed_low_tumble_tolerated(self):
        ok = zero_power_reset_tolerated(
            current_power=60.0, run_min=170.0, guard_dur=159.0,
            announce_past_expected=True, anti_crease_pattern_ok=True,
        )
        self.assertTrue(ok)

    def test_tumble_at_or_above_120w_not_tolerated(self):
        ok = zero_power_reset_tolerated(
            current_power=120.0, run_min=170.0, guard_dur=159.0,
            announce_past_expected=True, anti_crease_pattern_ok=True,
        )
        self.assertFalse(ok)

    def test_not_past_expected_end_not_tolerated(self):
        ok = zero_power_reset_tolerated(
            current_power=60.0, run_min=140.0, guard_dur=159.0,
            announce_past_expected=True, anti_crease_pattern_ok=True,
        )
        self.assertFalse(ok)

    def test_pattern_not_confirmed_not_tolerated(self):
        ok = zero_power_reset_tolerated(
            current_power=60.0, run_min=170.0, guard_dur=159.0,
            announce_past_expected=True, anti_crease_pattern_ok=False,
        )
        self.assertFalse(ok)

    def test_flag_off_not_tolerated(self):
        ok = zero_power_reset_tolerated(
            current_power=60.0, run_min=170.0, guard_dur=159.0,
            announce_past_expected=False, anti_crease_pattern_ok=True,
        )
        self.assertFalse(ok)


class TestDelayedStartSlide(unittest.TestCase):
    """Tests for the delayed-start plateau/slide decision (Miele delay timer)."""

    def test_delay_plateau_slides_start(self):
        """Selection burst, then a long flat standby well past delay_plateau_minutes, then real
        activity resumes -> start slides to the resume timestamp."""
        start = datetime(2026, 7, 17, 20, 0, 0, tzinfo=timezone.utc)
        samples = _build_power_samples(start, [
            (15, 45.0),   # selection burst (~45W for 15 min) - tripped start detection
            (350, 3.05),  # flat standby wait, well over the 30 min plateau threshold
            (5, 22.0),    # real wash resumes
        ])
        new_start = slide_start_if_delayed(
            samples, start_ts=start, delay_plateau_minutes=30, start_w=18.0,
            energy_floor_kwh=0.05, observed_heating=False, cum_energy_kwh=0.03,
        )
        expected_resume = start + timedelta(minutes=15 + 350)
        self.assertEqual(new_start, expected_resume)
        self.assertNotEqual(new_start, start)

    def test_short_soak_does_not_slide(self):
        """A normal mid-cycle soak (dip below start_w for only 12 min, well under the 30 min
        plateau threshold) must not be mistaken for a delayed-start wait."""
        start = datetime(2026, 7, 17, 20, 0, 0, tzinfo=timezone.utc)
        samples = _build_power_samples(start, [
            (20, 40.0),  # agitation
            (12, 2.0),   # short mid-cycle soak (well under 30 min)
            (20, 40.0),  # agitation resumes
        ])
        new_start = slide_start_if_delayed(
            samples, start_ts=start, delay_plateau_minutes=30, start_w=18.0,
            energy_floor_kwh=0.05, observed_heating=False, cum_energy_kwh=0.03,
        )
        self.assertEqual(new_start, start)

    def test_no_slide_after_heating(self):
        """Once real wash evidence exists - heating observed, or cumulative energy already past
        the standby floor - a long flat stretch must never be treated as a delayed-start wait."""
        start = datetime(2026, 7, 17, 20, 0, 0, tzinfo=timezone.utc)
        samples = _build_power_samples(start, [
            (15, 45.0),
            (350, 3.05),
            (5, 22.0),
        ])
        new_start = slide_start_if_delayed(
            samples, start_ts=start, delay_plateau_minutes=30, start_w=18.0,
            energy_floor_kwh=0.05, observed_heating=True, cum_energy_kwh=0.03,
        )
        self.assertEqual(new_start, start)

        new_start_energy = slide_start_if_delayed(
            samples, start_ts=start, delay_plateau_minutes=30, start_w=18.0,
            energy_floor_kwh=0.05, observed_heating=False, cum_energy_kwh=0.6,
        )
        self.assertEqual(new_start_energy, start)

    def test_heating_right_after_plateau_slides(self):
        """Heating can begin within one heartbeat of the wait ending, so no tick ever sees
        'power >= start_w but not yet heating'. The gate disarming with a qualifying plateau
        open IS the resume signal - the start must slide at that moment, not stay stuck on the
        waiting UI until the end-of-cycle history backstop."""
        start = datetime(2026, 7, 17, 20, 0, 0, tzinfo=timezone.utc)
        samples = _build_power_samples(start, [
            (15, 45.0),    # selection burst
            (350, 3.05),   # flat standby wait
            (5, 2200.0),   # heater engages immediately - observed_heating flips between ticks
        ])
        onset = start + timedelta(minutes=15 + 350)
        new_start = slide_start_if_delayed(
            samples, start_ts=start, delay_plateau_minutes=30, start_w=18.0,
            energy_floor_kwh=0.05, observed_heating=False, cum_energy_kwh=0.03,
            heating_onset_ts=onset,
        )
        self.assertEqual(new_start, onset)

    def test_heating_without_plateau_does_not_slide(self):
        """Gate disarm without a qualifying plateau (normal warm cycle heating early) must not
        slide anything."""
        start = datetime(2026, 7, 17, 20, 0, 0, tzinfo=timezone.utc)
        samples = _build_power_samples(start, [
            (10, 45.0),
            (12, 3.0),     # only 12 min quiet - under the 30 min threshold
            (5, 2200.0),
        ])
        onset = start + timedelta(minutes=10 + 12)
        new_start = slide_start_if_delayed(
            samples, start_ts=start, delay_plateau_minutes=30, start_w=18.0,
            energy_floor_kwh=0.05, observed_heating=False, cum_energy_kwh=0.03,
            heating_onset_ts=onset,
        )
        self.assertEqual(new_start, start)

    def test_normal_cycle_no_trim(self):
        """A normal ~155-min varied-power cycle with only short soaks (<= 13 min, never
        contiguous past that) must never trigger a slide."""
        start = datetime(2026, 7, 17, 20, 0, 0, tzinfo=timezone.utc)
        samples = _build_power_samples(start, [
            (10, 50.0),
            (8, 2.0),
            (15, 60.0),
            (10, 3.0),
            (40, 45.0),
            (12, 2.5),
            (60, 55.0),
        ])
        new_start = slide_start_if_delayed(
            samples, start_ts=start, delay_plateau_minutes=30, start_w=18.0,
            energy_floor_kwh=0.05, observed_heating=False, cum_energy_kwh=0.03,
        )
        self.assertEqual(new_start, start)


class TestEcoPerTemperatureLearning(unittest.TestCase):
    """ECO now learns duration separately per confirmed temperature (40-60°C auto / 40°C / 60°C),
    exactly like Bomuld already does per its 6 temperatures. Exercises the REAL
    WasherMonitor._get_profile / _programme_has_temperature (not a mirror) - washer_monitor
    imports cleanly standalone via the appdaemon stub above."""

    def setUp(self):
        self.app = wm.WasherMonitor.__new__(wm.WasherMonitor)

    def test_programme_has_temperature_true_for_eco(self):
        self.assertTrue(self.app._programme_has_temperature("eco"))

    def test_each_temperature_resolves_its_own_by_temperature_entry(self):
        for temp in ("40-60°C", "40°C", "60°C"):
            profile = self.app._get_profile("eco", temp)
            self.assertEqual(profile["duration_min"], 199)
            self.assertEqual(profile["max_energy_kwh"], 0.78)
            self.assertEqual(profile["max_dur_min"], 235)
            self.assertEqual(profile["stable_min"], 15)
            self.assertTrue(profile["heats"])
            self.assertTrue(profile["supports_anti_crease"])
            self.assertEqual(profile["label"], "ECO")

    def test_missing_temperature_falls_back_to_first_variant(self):
        """No temperature given (e.g. unconfirmed) -> falls back to the first by_temperature
        entry, same as bomuld's fallback behavior."""
        profile = self.app._get_profile("eco", None)
        self.assertEqual(profile["duration_min"], 199)
        self.assertEqual(profile["label"], "ECO")

    def test_flat_programme_unaffected(self):
        """A programme without by_temperature (e.g. 'impraegnering') still returns its flat
        top-level profile and does not report temperature-dependence."""
        self.assertFalse(self.app._programme_has_temperature("impraegnering"))
        profile = self.app._get_profile("impraegnering")
        self.assertEqual(profile["duration_min"], 25)


class TestSessionCostAccumulation(unittest.TestCase):
    """Mirror tests for the settled per-cycle cost accumulation in _check_energy_finish
    (delta vs previous tick's cumulative energy, negative-delta clamp for meter resets,
    fallback price when the price entity is unavailable)."""

    def test_normal_accumulation(self):
        """Steady energy increase at a constant price -> cost = total delta kWh * price."""
        ticks = [
            (10.000, 2.0),
            (10.100, 2.0),
            (10.250, 2.0),
            (10.400, 2.0),
        ]
        cost = accumulate_session_cost(ticks, price_fallback_kr=1.7)
        self.assertAlmostEqual(cost, 0.400 * 2.0, places=6)

    def test_meter_reset_mid_cycle_clamped_to_zero(self):
        """A negative delta (meter reset) contributes zero cost for that tick; accumulation
        then resumes normally from the new (lower) reading, without going negative."""
        ticks = [
            (10.000, 1.5),
            (10.100, 1.5),   # +0.100 kWh -> 0.15 kr
            (0.050, 1.5),    # meter reset (large negative delta) -> clamped to 0
            (0.200, 1.5),    # +0.150 kWh from the post-reset baseline -> 0.225 kr
        ]
        cost = accumulate_session_cost(ticks, price_fallback_kr=1.7)
        self.assertAlmostEqual(cost, 0.15 + 0.225, places=6)

    def test_unavailable_price_uses_fallback(self):
        """When the price reading is unavailable (None) for a tick, the fallback price is
        used for that tick's delta only; other ticks keep using their own reading."""
        ticks = [
            (5.000, 2.0),
            (5.050, 2.0),    # +0.050 kWh @ 2.0 -> 0.10 kr
            (5.120, None),   # +0.070 kWh @ fallback 1.7 -> 0.119 kr
        ]
        cost = accumulate_session_cost(ticks, price_fallback_kr=1.7)
        self.assertAlmostEqual(cost, 0.10 + 0.119, places=6)


class TestConfirmPushGating(unittest.TestCase):
    """Tests for the real WasherMonitor._should_send_confirm_push gate - a pure
    staticmethod, so no app instance is needed."""

    def test_disabled_never_sends(self):
        record = {"completion_class": "completed", "programme_user_confirmed": False}
        self.assertFalse(wm.WasherMonitor._should_send_confirm_push(record, False))

    def test_legacy_user_confirmed_blocks(self):
        record = {"completion_class": "completed", "programme_user_confirmed": True}
        self.assertFalse(wm.WasherMonitor._should_send_confirm_push(record, True))

    def test_new_confirmed_by_human_field_blocks(self):
        record = {
            "completion_class": "completed",
            "programme_user_confirmed": False,
            "programme_confirmed_by_human": True,
        }
        self.assertFalse(wm.WasherMonitor._should_send_confirm_push(record, True))

    def test_non_completed_blocks(self):
        for completion_class in ("suspect", "interrupted", None, ""):
            record = {"completion_class": completion_class, "programme_user_confirmed": False}
            self.assertFalse(wm.WasherMonitor._should_send_confirm_push(record, True))

    def test_completed_unconfirmed_enabled_sends(self):
        record = {"completion_class": "completed", "programme_user_confirmed": False}
        self.assertTrue(wm.WasherMonitor._should_send_confirm_push(record, True))


class TestConfirmActionRoundtrip(unittest.TestCase):
    """Tests for the real WasherMonitor._encode_confirm_action / _parse_confirm_action -
    pure staticmethods, so no app instance is needed."""

    def test_roundtrip_with_temperature(self):
        ts, prog, temp = "2026-07-23T18:02:11+02:00", "bomuld", "60"
        action = wm.WasherMonitor._encode_confirm_action(ts, prog, temp)
        self.assertEqual(action, "WASHER_CONFIRM|2026-07-23T18:02:11+02:00|bomuld|60")
        self.assertEqual(wm.WasherMonitor._parse_confirm_action(action), (ts, prog, temp))

    def test_roundtrip_without_temperature_uses_dash(self):
        ts, prog, temp = "2026-07-23T09:15:00+02:00", "ekspres", None
        action = wm.WasherMonitor._encode_confirm_action(ts, prog, temp)
        self.assertEqual(action, "WASHER_CONFIRM|2026-07-23T09:15:00+02:00|ekspres|-")
        self.assertEqual(wm.WasherMonitor._parse_confirm_action(action), (ts, prog, None))

    def test_non_matching_action_returns_none(self):
        self.assertIsNone(wm.WasherMonitor._parse_confirm_action("SOME_OTHER_ACTION"))
        self.assertIsNone(wm.WasherMonitor._parse_confirm_action(""))

    def test_malformed_confirm_action_returns_none(self):
        self.assertIsNone(wm.WasherMonitor._parse_confirm_action("WASHER_CONFIRM|only|two"))
        self.assertIsNone(wm.WasherMonitor._parse_confirm_action("WASHER_CONFIRM|||"))


if __name__ == "__main__":
    unittest.main()
