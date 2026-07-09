# tests/test_washer_completion.py - Acceptance tests for washer completion and validation refactor.
# Run from repo root: python3 -m unittest appdaemon.apps.appliances.tests.test_washer_completion
# These tests duplicate the validation/tail-window logic so they run without the AppDaemon runtime.

import unittest

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

    valid_for_learning = (
        completion_class == "completed"
        and "runtime_too_short" not in flags
        and "energy_too_low" not in flags
        and "unknown_programme" not in flags
    )
    if transition_path == "door_opened_first" and completion_class != "completed":
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


if __name__ == "__main__":
    unittest.main()
