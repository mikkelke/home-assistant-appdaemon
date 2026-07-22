from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# climate_model has ZERO appdaemon imports -> import it directly, no stub needed.
import climate_model as cm  # noqa: E402


class LegacyEquilibrium(unittest.TestCase):
    def test_all_none_uses_fallback_plus_offset(self):
        self.assertEqual(cm.legacy_equilibrium(None, None, None, 0.5), 25.0)

    def test_custom_empty_fallback(self):
        self.assertEqual(cm.legacy_equilibrium(None, None, None, 0.0, empty_fallback=20.0), 20.0)

    def test_takes_warmest_reading(self):
        self.assertEqual(cm.legacy_equilibrium(23.0, 22.0, 21.0, 0.5), 23.5)
        self.assertEqual(cm.legacy_equilibrium(None, 24.3, 19.0, 0.5), 24.8)

    def test_person_offset_applied(self):
        self.assertEqual(cm.legacy_equilibrium(20.0, None, None, 1.0), 21.0)


class ModelDApartment(unittest.TestCase):
    COEFFS = cm.ModelDCoeffs(15.797, 0.0162, 0.198, 24.0, 0.287)

    def test_worked_example(self):
        # 15.797 + 0.0162*200 + 0.198*(31-24) + 0.287*27 = 28.174
        got = cm.model_d_apartment(200.0, 31.0, 27.0, self.COEFFS)
        self.assertAlmostEqual(got, 28.17, places=2)

    def test_vent_knee_clamps_at_zero(self):
        # outdoor below the knee -> the vent term contributes nothing
        below = cm.model_d_apartment(0.0, 20.0, 20.0, self.COEFFS)
        at = cm.model_d_apartment(0.0, 24.0, 20.0, self.COEFFS)
        self.assertEqual(below, at)


class GroundedEquilibrium(unittest.TestCase):
    """Reality-check for the ADVISORY sleep plan: the sealed room can't drift materially
    warmer than the apartment is right now UNLESS the night stays warm enough to hold the
    day's heat. Cool night -> cap at apartment_now + margin; warm night -> raw weather value."""

    def test_cool_night_grounds_to_apartment_plus_margin(self):
        # THE 2026-07-22 case: weather peak 24.7, whole flat ~21.7C, outdoor low ~15C,
        # limit 22.5. warm-night threshold = 22.5 - 1.0 = 21.5; 15 < 21.5 -> cool night ->
        # min(24.7, 21.7 + 1.0) = 22.7 (well below the daytime peak).
        self.assertAlmostEqual(
            cm.grounded_equilibrium(24.7, 21.7, 15.0, 22.5), 22.7, places=6)

    def test_cool_night_but_weather_already_below_anchor_keeps_weather(self):
        # weather already cooler than apartment_now + margin -> min() keeps the weather value
        self.assertAlmostEqual(
            cm.grounded_equilibrium(21.0, 21.7, 15.0, 22.5), 21.0, places=6)

    def test_warm_night_returns_weather_unchanged(self):
        # night_outdoor 22 >= 22.5 - 1.0 = 21.5 -> genuinely warm night: preserve the raw
        # weather peak so pre-cool-ahead-of-a-hot-night is NOT grounded away.
        self.assertEqual(cm.grounded_equilibrium(24.7, 21.7, 22.0, 22.5), 24.7)

    def test_warm_night_boundary_is_inclusive(self):
        # exactly at comfort_limit - warm_night_margin counts as warm (>=)
        self.assertEqual(cm.grounded_equilibrium(26.0, 20.0, 21.5, 22.5), 26.0)
        # just below the boundary is a cool night -> grounds
        self.assertAlmostEqual(
            cm.grounded_equilibrium(26.0, 20.0, 21.49, 22.5), 21.0, places=6)

    def test_none_apartment_falls_back_to_weather(self):
        self.assertEqual(cm.grounded_equilibrium(24.7, None, 15.0, 22.5), 24.7)

    def test_none_night_outdoor_falls_back_to_weather(self):
        self.assertEqual(cm.grounded_equilibrium(24.7, 21.7, None, 22.5), 24.7)

    def test_none_weather_returned_as_is(self):
        self.assertIsNone(cm.grounded_equilibrium(None, 21.7, 15.0, 22.5))

    def test_custom_margins(self):
        # reality_margin 0.5 -> min(24.7, 21.7 + 0.5) = 22.2; warm_night_margin 2.0 ->
        # threshold 22.5 - 2.0 = 20.5, and 15 < 20.5 so still a cool night
        self.assertAlmostEqual(
            cm.grounded_equilibrium(24.7, 21.7, 15.0, 22.5,
                                    reality_margin=0.5, warm_night_margin=2.0),
            22.2, places=6)


class CoastPeak(unittest.TestCase):
    def test_forward_law(self):
        self.assertAlmostEqual(cm.coast_peak(20.0, 25.0, 0.5, 1.0), 23.5)

    def test_zero_rise_is_floor_plus_offset(self):
        self.assertAlmostEqual(cm.coast_peak(20.0, 25.0, 0.0, 1.0), 21.0)

    def test_none_guards(self):
        self.assertIsNone(cm.coast_peak(None, 25.0, 0.5, 1.0))
        self.assertIsNone(cm.coast_peak(20.0, None, 0.5, 1.0))


class CalcFloorTarget(unittest.TestCase):
    def test_below_cap_returns_ceiling(self):
        # E (21) <= cap (23-1=22) -> no pre-cool needed
        self.assertEqual(cm.calc_floor_target(21.0, 23.0, 0.5, 1.0, 16.0), 23.0)

    def test_f0_formula(self):
        # cap=22, r=0.5, f0=(22-25*0.5)/0.5 = 19.0
        self.assertEqual(cm.calc_floor_target(25.0, 23.0, 0.5, 1.0, 16.0), 19.0)

    def test_clamped_to_min_temp_on_hot_night(self):
        # cap=22, r=0.7, f0=(22-40*0.7)/0.3 = -20 -> clamp to min_temp
        self.assertEqual(cm.calc_floor_target(40.0, 23.0, 0.7, 1.0, 16.0), 16.0)

    def test_rounds_to_two_decimals(self):
        # cap=22, r=0.5, f0=(22-25.333*0.5)/0.5 = 18.667 -> 18.67
        self.assertEqual(cm.calc_floor_target(25.333, 23.0, 0.5, 1.0, 16.0), 18.67)

    def test_rise_frac_clamped_to_band(self):
        # rise 0.0 clamps to 0.05, rise 0.99 clamps to 0.95 -- identical to the bare bounds
        self.assertEqual(cm.calc_floor_target(30.0, 23.0, 0.0, 1.0, 16.0),
                         cm.calc_floor_target(30.0, 23.0, 0.05, 1.0, 16.0))
        self.assertEqual(cm.calc_floor_target(30.0, 23.0, 0.99, 1.0, 16.0),
                         cm.calc_floor_target(30.0, 23.0, 0.95, 1.0, 16.0))


class WindowsCanCool(unittest.TestCase):
    def test_cool_and_dry_true(self):
        ok, _ = cm.windows_can_cool(23.0, 15.0, 9.0, 12.0)
        self.assertTrue(ok)

    def test_warm_outside_false(self):
        ok, why = cm.windows_can_cool(23.0, 25.0, 9.0, 12.0)
        self.assertFalse(ok)
        self.assertIn("not cooler", why)

    def test_muggy_outside_false_mentions_dew_point(self):
        ok, why = cm.windows_can_cool(23.0, 15.0, 14.0, 12.0)
        self.assertFalse(ok)
        self.assertIn("dew point", why)

    def test_missing_input_none(self):
        self.assertEqual(cm.windows_can_cool(None, 15.0, 9.0, 12.0)[0], None)
        self.assertEqual(cm.windows_can_cool(23.0, 15.0, None, 12.0)[0], None)

    def test_temp_margin_boundary(self):
        # target 23, temp_margin 0.5 -> outdoor must be < 22.5
        self.assertFalse(cm.windows_can_cool(23.0, 22.6, 9.0, 12.0)[0])
        self.assertTrue(cm.windows_can_cool(23.0, 22.4, 9.0, 12.0)[0])

    def test_dew_margin_boundary(self):
        # indoor_dew 12, dew_margin 0 (default) -> veto only when outdoor is MORE humid
        self.assertFalse(cm.windows_can_cool(23.0, 15.0, 12.1, 12.0)[0])
        self.assertTrue(cm.windows_can_cool(23.0, 15.0, 12.0, 12.0)[0])


class SummarizeOpenWindows(unittest.TestCase):
    def test_only_on_is_open_sorted(self):
        got = cm.summarize_open_windows({"kitchen": "on", "bedroom": "on",
                                         "bathroom": "off", "dining 1": "unavailable"})
        self.assertEqual(got, ["bedroom", "kitchen"])

    def test_empty_and_none(self):
        self.assertEqual(cm.summarize_open_windows({}), [])
        self.assertEqual(cm.summarize_open_windows(None), [])

    def test_all_closed(self):
        self.assertEqual(cm.summarize_open_windows({"a": "off", "b": "unknown"}), [])


class SharedComfortReexports(unittest.TestCase):
    """Spot-check the moved comfort fns (fully covered by test_bedroom_comfort via re-export)."""

    def test_dew_point_and_ceiling(self):
        self.assertAlmostEqual(cm.dew_point_c(23.3, 42.0), 9.7, delta=0.15)
        ceil, red = cm.effective_ceiling(23.0, 14.2, 2)
        self.assertAlmostEqual(red, 0.83, delta=0.01)
        self.assertAlmostEqual(ceil, 22.2, delta=0.05)

    def test_hours_until_morning(self):
        self.assertAlmostEqual(cm.hours_until_morning(datetime(2026, 7, 12, 23, 0)), 8.0)

    def test_classify(self):
        self.assertEqual(cm.classify(24.6, 10.0, 20.0, 19.9), "hot")

    def test_vent_helps_wrapper_matches_windows_can_cool(self):
        # vent_helps is the original 0-margin leaf rule: strictly cooler AND not more humid.
        a = cm.vent_helps(23.0, 10.2, 16.4, 9.1)
        b = cm.windows_can_cool(23.0, 16.4, 9.1, 10.2, temp_margin=0.0, dew_margin=0.0)
        self.assertEqual(a, b)


class PlanSleep(unittest.TestCase):
    def _inp(self, **ov):
        base = dict(
            floor=22.0, equilibrium=23.0, rise_frac=0.7, zone_offset=1.0,
            comfort_limit=23.0, min_temp=16.0, floor_cool_cph=1.0, cool_power_kw=0.5,
            cheapest_price=1.5, outdoor_temp=15.0, outdoor_dew=8.0, indoor_dew=11.0,
            open_windows=["bedroom"], noise_penalty_kr=0.5,
        )
        base.update(ov)
        return cm.SleepPlanInputs(**base)

    def test_drift_regression_cool_day_is_not_ac(self):
        # THE reported bug: room floor 20, cool sunless day E ~22.5, window open, cool/dry
        # outside -> the coast peak stays under the limit, so NOT 'ac' (the old dashboard
        # projected from the warm kitchen and said "deploy AC to ~23").
        plan = cm.plan_sleep(self._inp(floor=20.0, equilibrium=22.5, comfort_limit=23.0,
                                       outdoor_temp=15.0, outdoor_dew=8.0))
        self.assertIn(plan["recommendation"], ("nothing", "windows"))
        self.assertNotEqual(plan["recommendation"], "ac")

    def test_hot_muggy_night_is_ac_with_cost(self):
        plan = cm.plan_sleep(self._inp(floor=24.0, equilibrium=26.0, comfort_limit=23.0,
                                       outdoor_temp=24.0, outdoor_dew=18.0, indoor_dew=15.0,
                                       cheapest_price=2.0))
        self.assertEqual(plan["recommendation"], "ac")
        self.assertGreater(plan["est_cost_kr"], 0.0)
        self.assertTrue(plan["cost_label"].startswith("~"))
        self.assertIn("kr", plan["cost_label"])

    def test_hot_but_cool_dry_outside_small_gap_is_windows_free(self):
        plan = cm.plan_sleep(self._inp(floor=22.5, equilibrium=23.5, comfort_limit=23.0,
                                       outdoor_temp=15.0, outdoor_dew=8.0))
        self.assertEqual(plan["recommendation"], "windows")
        self.assertEqual(plan["est_cost_kr"], 0.0)
        self.assertEqual(plan["cost_label"], "free")

    def test_large_gap_cool_dry_outside_is_hybrid(self):
        plan = cm.plan_sleep(self._inp(floor=24.0, equilibrium=26.0, comfort_limit=23.0,
                                       outdoor_temp=14.0, outdoor_dew=7.0, cheapest_price=2.0))
        self.assertEqual(plan["recommendation"], "hybrid")
        self.assertGreater(plan["est_cost_kr"], 0.0)

    def test_free_case_has_cost_label_and_open_windows(self):
        # attribute-drop resilience: est_cost 0.0 vanishes under AppDaemon, but cost_label
        # and open_windows must always be present + non-bool-load-bearing.
        plan = cm.plan_sleep(self._inp(floor=22.5, equilibrium=23.5, comfort_limit=23.0,
                                       open_windows=["bedroom", "kitchen"]))
        self.assertEqual(plan["est_cost_kr"], 0.0)
        self.assertEqual(plan["cost_label"], "free")
        self.assertIsInstance(plan["open_windows"], list)
        self.assertEqual(plan["open_windows"], ["bedroom", "kitchen"])
        self.assertEqual(plan["windows_summary"], "bedroom + kitchen open")

    def test_windows_always_beat_equal_comfort_ac(self):
        # a windows recommendation costs 0, strictly less than the cheapest possible AC run
        # (which is at least the fixed noise penalty).
        plan = cm.plan_sleep(self._inp(floor=22.5, equilibrium=23.5, comfort_limit=23.0))
        self.assertEqual(plan["recommendation"], "windows")
        self.assertEqual(plan["est_cost_kr"], 0.0)
        self.assertLess(0.0, 0.5)  # 0 < noise_penalty_kr -> windows win

    def test_within_margin_is_nothing(self):
        # peak = 21 + (21.5-21)*0.7 + 1 = 22.35, well under the 23.0 limit -> nothing
        plan = cm.plan_sleep(self._inp(floor=21.0, equilibrium=21.5, comfort_limit=23.0))
        self.assertEqual(plan["recommendation"], "nothing")
        self.assertEqual(plan["cost_label"], "free")

    def test_missing_floor_yields_no_projection_but_safe_dict(self):
        plan = cm.plan_sleep(self._inp(floor=None))
        self.assertEqual(plan["recommendation"], "nothing")
        self.assertIsNone(plan["projected_peak"])
        self.assertEqual(plan["cost_label"], "free")
        self.assertIn("open_windows", plan)
        self.assertIn("windows_summary", plan)

    def test_muggy_outside_forces_ac_not_windows(self):
        # gap big, but outside is humid -> opening a window imports water -> 'ac'
        plan = cm.plan_sleep(self._inp(floor=24.0, equilibrium=26.0, comfort_limit=23.0,
                                       outdoor_temp=15.0, outdoor_dew=18.0, indoor_dew=12.0,
                                       cheapest_price=2.0))
        self.assertEqual(plan["recommendation"], "ac")

    def test_cool_but_barely_humid_is_windows_not_ac(self):
        # 2026-07-22 knife-edge: cool outside, outdoor dew a hair ABOVE indoor -> a window
        # still COOLS, so it must NOT flip to 'ac' on a humidity tie.
        plan = cm.plan_sleep(self._inp(floor=22.0, equilibrium=22.7, comfort_limit=22.5,
                                       outdoor_temp=15.0, outdoor_dew=13.7, indoor_dew=13.6,
                                       cheapest_price=1.6))
        self.assertNotEqual(plan["recommendation"], "ac")

    def test_cool_but_genuinely_muggy_is_ac(self):
        # Cool outside but the outdoor air is MEANINGFULLY muggier (dew +3.5 over indoor) ->
        # opening a window imports real moisture -> 'ac' still wins.
        plan = cm.plan_sleep(self._inp(floor=24.0, equilibrium=26.0, comfort_limit=23.0,
                                       outdoor_temp=15.0, outdoor_dew=15.5, indoor_dew=12.0,
                                       cheapest_price=2.0))
        self.assertEqual(plan["recommendation"], "ac")

    def test_all_closed_summary(self):
        plan = cm.plan_sleep(self._inp(open_windows=[]))
        self.assertEqual(plan["windows_summary"], "all closed")
        self.assertEqual(plan["open_windows"], [])


if __name__ == "__main__":
    unittest.main()
