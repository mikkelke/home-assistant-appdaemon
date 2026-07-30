from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# climate_model has ZERO appdaemon imports -> import it directly, no stub needed.
import climate_model as cm  # noqa: E402


class ParseForecastEnvelope(unittest.TestCase):
    """smart_cooling and deploy_advisor's shared weather.get_forecasts envelope digger."""

    ENTITY = "weather.forecast_home"
    ITEMS = [{"datetime": "2026-07-22T00:00:00+00:00", "temperature": 20.0},
             {"datetime": "2026-07-22T01:00:00+00:00", "temperature": 19.5}]

    def test_standard_envelope(self):
        resp = {"result": {"response": {self.ENTITY: {"forecast": self.ITEMS}}}}
        self.assertEqual(cm.parse_forecast_envelope(resp, self.ENTITY), self.ITEMS)

    def test_recursive_find_fallback(self):
        # the digging chain doesn't land on a list (shape drifted / wrong entity key) ->
        # fall back to searching the whole envelope for the first forecast-shaped list.
        resp = {"result": {"response": {"weather.some_other_entity": {"forecast": self.ITEMS}}}}
        self.assertEqual(cm.parse_forecast_envelope(resp, self.ENTITY), self.ITEMS)

    def test_garbage_returns_empty_list(self):
        self.assertEqual(cm.parse_forecast_envelope(None, self.ENTITY), [])
        self.assertEqual(cm.parse_forecast_envelope("not a dict", self.ENTITY), [])
        self.assertEqual(cm.parse_forecast_envelope({"nothing": "useful"}, self.ENTITY), [])


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

    def test_very_large_gap_cool_dry_outside_is_ac_not_hybrid(self):
        # THE reported 2026-07-29 bug: this is the exact 3.4C gap (projected peak 25.9C-class
        # vs a 22.5C-class limit) that used to fall into the unbounded 'hybrid' bucket and
        # get reported by the morning briefing as "AC not needed" at 05:55, while the plan
        # itself flipped to 'ac' by 10:00 once the day's real numbers were in. Past
        # windows_max_gap_c (2.5 default) a heat-soaked mass can't be rescued by venting no
        # matter how cool the air outside is -- must be 'ac', not 'hybrid'.
        plan = cm.plan_sleep(self._inp(floor=24.0, equilibrium=26.0, comfort_limit=23.0,
                                       outdoor_temp=14.0, outdoor_dew=7.0, cheapest_price=2.0))
        self.assertEqual(plan["recommendation"], "ac")
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
        # a windows recommendation costs 0, strictly less than a real AC run's cost. (2026-
        # 07-21: est_cost_kr is no longer floored by a fixed noise penalty -- see plan_sleep's
        # docstring -- so this now compares against an actual 'ac'-branch cost instead of the
        # old hardcoded "0 < noise_penalty_kr" constant.)
        plan = cm.plan_sleep(self._inp(floor=22.5, equilibrium=23.5, comfort_limit=23.0))
        self.assertEqual(plan["recommendation"], "windows")
        self.assertEqual(plan["est_cost_kr"], 0.0)
        ac_plan = cm.plan_sleep(self._inp(
            floor=24.0, equilibrium=26.0, comfort_limit=23.0, outdoor_temp=24.0,
            outdoor_dew=18.0, indoor_dew=15.0, cheapest_price=2.0))
        self.assertEqual(ac_plan["recommendation"], "ac")
        self.assertLess(plan["est_cost_kr"], ac_plan["est_cost_kr"])

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

    def test_deprecated_learned_night_cost_is_ignored(self):
        # 2026-07-29 rebuild: the flat night_cost_ema EMA that learned_night_cost used to
        # feed was poisoned by 3 finalized 0.00 kWh sessions (dragged it to 0.36 kr against
        # a real ~4.50 kr metered average) -- it no longer wins over (or influences at all)
        # the kwh_per_deg-scaled estimate. The field is kept only so an old caller/test
        # passing it doesn't break the constructor.
        kwargs = dict(floor=24.0, equilibrium=26.0, comfort_limit=23.0, outdoor_temp=24.0,
                     outdoor_dew=18.0, indoor_dew=15.0, cheapest_price=2.0)
        without = cm.plan_sleep(self._inp(**kwargs))
        with_learned = cm.plan_sleep(self._inp(learned_night_cost=5.2, **kwargs))
        self.assertEqual(without["recommendation"], "ac")
        self.assertEqual(with_learned["recommendation"], "ac")
        self.assertEqual(without["est_cost_kr"], with_learned["est_cost_kr"])
        self.assertNotEqual(with_learned["est_cost_kr"], 5.2)

    def test_deprecated_session_factor_is_ignored(self):
        # session_factor no longer scales (or influences at all) the estimate -- kwh_per_deg
        # replaces its role (2026-07-29 rebuild). Kept only for constructor back-compat.
        kwargs = dict(floor=24.0, equilibrium=26.0, comfort_limit=23.0, outdoor_temp=24.0,
                     outdoor_dew=18.0, indoor_dew=15.0, cheapest_price=2.0)
        low_factor = cm.plan_sleep(self._inp(session_factor=1.0, **kwargs))
        high_factor = cm.plan_sleep(self._inp(session_factor=99.0, **kwargs))
        self.assertEqual(low_factor["est_cost_kr"], high_factor["est_cost_kr"])

    def test_kwh_per_deg_scales_the_cost_estimate(self):
        # kwh_per_deg (kWh spent per degree C of pre-cool deficit closed) is what scales the
        # theoretical estimate now, learned from real metered sessions (see
        # SmartCooling._finalize_session) and seeded at 1.6 -- see plan_sleep's docstring
        # for the 2026-07-10..29 calibration provenance.
        kwargs = dict(floor=24.0, equilibrium=26.0, comfort_limit=23.0, outdoor_temp=24.0,
                     outdoor_dew=18.0, indoor_dew=15.0, cheapest_price=2.0)
        low = cm.plan_sleep(self._inp(kwh_per_deg=1.0, **kwargs))
        high = cm.plan_sleep(self._inp(kwh_per_deg=2.0, **kwargs))
        self.assertEqual(low["recommendation"], "ac")
        self.assertEqual(high["recommendation"], "ac")
        # deficit 8.0C (target floor-limited to min_temp 16.0): 8.0*1.0 kWh/C=8.0 kWh @
        # 2.0 kr/kWh = 16.0 kr; 8.0*2.0 kWh/C=16.0 kWh @ 2.0 kr/kWh = 32.0 kr.
        self.assertEqual(low["est_cost_kr"], 16.0)
        self.assertEqual(high["est_cost_kr"], 32.0)
        self.assertAlmostEqual(high["est_cost_kr"], low["est_cost_kr"] * 2.0, places=6)

    def test_noise_penalty_no_longer_added_to_displayed_cost(self):
        # 2026-07-21 fix (still true post-2026-07-29 rebuild): noise_penalty_kr is kept only
        # for backward-compat (default 0.5, still accepted as a field) but no longer
        # inflates est_cost_kr -- the formula is purely deficit * kwh_per_deg * price.
        kwargs = dict(floor=24.0, equilibrium=26.0, comfort_limit=23.0, outdoor_temp=24.0,
                     outdoor_dew=18.0, indoor_dew=15.0, cheapest_price=2.0, kwh_per_deg=1.6)
        no_penalty = cm.plan_sleep(self._inp(noise_penalty_kr=0.0, **kwargs))
        with_penalty = cm.plan_sleep(self._inp(noise_penalty_kr=0.5, **kwargs))
        self.assertEqual(no_penalty["est_cost_kr"], with_penalty["est_cost_kr"])
        self.assertEqual(with_penalty["est_cost_kr"], 25.6)  # 8.0C * 1.6 kWh/C * 2.0 kr/kWh


class HybridBucketBounded(unittest.TestCase):
    """2026-07-29 fix: windows_max_gap_c bounds the hybrid bucket so a large gap with
    cool-enough air can no longer read as 'hybrid' -- the incident this fixes was reported
    live as "AC not needed" at 05:55 on a 3.4C gap (projected peak ~25.9C vs a ~22.5C
    limit) that the plan itself flipped to 'ac' by 10:00. All four cases below share the
    same cool/dry outdoor reading (temp_margin/muggy_slack both pass) so only the gap
    itself drives the bucket -- equilibrium is the only thing that varies."""

    def _inp(self, **ov):
        base = dict(
            floor=20.0, rise_frac=0.5, zone_offset=1.0, comfort_limit=20.5, min_temp=16.0,
            floor_cool_cph=1.0, cool_power_kw=0.5, cheapest_price=1.5,
            outdoor_temp=15.0, outdoor_dew=8.0, indoor_dew=11.0, open_windows=["bedroom"],
        )
        base.update(ov)
        return cm.SleepPlanInputs(**base)

    def test_gap_within_peak_margin_is_nothing(self):
        # peak = 21 + (19.2-20)*0.5 = 20.6; gap = 0.1 <= peak_margin_c (0.2)
        plan = cm.plan_sleep(self._inp(equilibrium=19.2))
        self.assertEqual(plan["recommendation"], "nothing")

    def test_moderate_gap_is_windows(self):
        # peak = 21 + (21.0-20)*0.5 = 21.5; gap = 1.0, within hybrid_gap_c (1.5)
        plan = cm.plan_sleep(self._inp(equilibrium=21.0))
        self.assertEqual(plan["recommendation"], "windows")
        self.assertEqual(plan["est_cost_kr"], 0.0)

    def test_windows_hybrid_boundary_is_still_windows(self):
        # peak = 21 + (22.0-20)*0.5 = 22.0; gap = 1.5, exactly at hybrid_gap_c (inclusive)
        plan = cm.plan_sleep(self._inp(equilibrium=22.0))
        self.assertEqual(plan["recommendation"], "windows")

    def test_large_gap_is_hybrid(self):
        # peak = 21 + (23.0-20)*0.5 = 22.5; gap = 2.0, between hybrid_gap_c and
        # windows_max_gap_c (2.5)
        plan = cm.plan_sleep(self._inp(equilibrium=23.0))
        self.assertEqual(plan["recommendation"], "hybrid")
        self.assertGreater(plan["est_cost_kr"], 0.0)

    def test_hybrid_ac_boundary_is_still_hybrid(self):
        # peak = 21 + (24.0-20)*0.5 = 23.0; gap = 2.5, exactly at windows_max_gap_c (inclusive)
        plan = cm.plan_sleep(self._inp(equilibrium=24.0))
        self.assertEqual(plan["recommendation"], "hybrid")

    def test_gap_over_windows_max_is_ac_not_hybrid(self):
        # peak = 21 + (25.8-20)*0.5 = 23.9; gap = 3.4 (THE reported incident's own number),
        # over windows_max_gap_c -> 'ac', not 'hybrid': a heat-soaked mass can't be rescued
        # by venting no matter how cool the air outside is.
        plan = cm.plan_sleep(self._inp(equilibrium=25.8))
        self.assertEqual(plan["recommendation"], "ac")
        self.assertGreater(plan["est_cost_kr"], 0.0)

    def test_custom_windows_max_gap_c_is_respected(self):
        # a tighter bound (1.5) turns the same 2.0C-gap case that reads 'hybrid' by default
        # into 'ac'.
        plan = cm.plan_sleep(self._inp(equilibrium=23.0, windows_max_gap_c=1.5))
        self.assertEqual(plan["recommendation"], "ac")


class NightOutdoorDrivesCoolEnough(unittest.TestCase):
    """2026-07-29 fix: window feasibility (cool_enough) is judged against night_outdoor (the
    temperature when the cooling would actually happen) instead of the live outdoor_temp
    reading, which at 05:30 is the day's daily minimum and made a window look sufficient
    every single morning regardless of what the day would go on to do."""

    def _inp(self, **ov):
        base = dict(
            floor=20.0, rise_frac=0.5, zone_offset=1.0, comfort_limit=20.5, min_temp=16.0,
            floor_cool_cph=1.0, cool_power_kw=0.5, cheapest_price=1.5,
            equilibrium=21.0, outdoor_dew=8.0, indoor_dew=11.0, open_windows=["bedroom"],
        )
        base.update(ov)
        return cm.SleepPlanInputs(**base)

    def test_cool_now_but_warm_at_night_is_ac(self):
        # gap = 1.0 (see HybridBucketBounded.test_moderate_gap_is_windows) -- cool_enough by
        # the CURRENT reading (15.0C) alone would say 'windows', but the night stays warm
        # (21.0C, not below limit(20.5) - temp_margin(0.5) = 20.0), so it must be 'ac'.
        without_night = cm.plan_sleep(self._inp(outdoor_temp=15.0))
        with_night = cm.plan_sleep(self._inp(outdoor_temp=15.0, night_outdoor=21.0))
        self.assertEqual(without_night["recommendation"], "windows")
        self.assertEqual(with_night["recommendation"], "ac")

    def test_missing_night_outdoor_falls_back_to_current_reading(self):
        # None (not passed / sensor missing) -> same result as the old outdoor_temp-only
        # behaviour, so a caller upgrading to pass night_outdoor never regresses when it's
        # unavailable.
        plan = cm.plan_sleep(self._inp(outdoor_temp=15.0, night_outdoor=None))
        self.assertEqual(plan["recommendation"], "windows")

    def test_warm_now_but_cool_at_night_is_still_windows(self):
        # the reverse direction: outdoor_temp(21.0) alone reads "not cool enough" (limit
        # 20.5 - temp_margin 0.5 = 20.0), but the night genuinely cools to 15.0C -- night_
        # outdoor is used INSTEAD OF outdoor_temp when known, not merely as an extra veto,
        # so this must be 'windows', not 'ac'.
        plan = cm.plan_sleep(self._inp(outdoor_temp=21.0, night_outdoor=15.0))
        self.assertEqual(plan["recommendation"], "windows")


class NightPeakCoastLawEquivalence(unittest.TestCase):
    """deploy_advisor's night_peak used to inline f + (e-f)*r + zone_uplift; it is now
    expressed through the shared coast_peak. Locks the re-expression: same numbers."""

    C = {"zone_uplift": 1.5}

    @staticmethod
    def _old_inline_formula(f, k, b, r, c):
        e = (k + b + f) / 3.0
        return f + (e - f) * r + c["zone_uplift"]

    def test_matches_old_inline_formula_on_value_grids(self):
        grid = [
            (23.0, 24.5, 24.0, 0.502),   # typical mild night
            (20.0, 25.0, 22.0, 0.7),     # deep learned rise_frac
            (18.5, 18.5, 18.5, 0.3),     # everything equal -> no equilibrium pull
            (16.0, 30.0, 20.0, 0.05),    # clamped-low rise_frac, hot kitchen
        ]
        for f, k, b, r in grid:
            with self.subTest(f=f, k=k, b=b, r=r):
                self.assertAlmostEqual(
                    cm.night_peak(f, k, b, r, self.C),
                    self._old_inline_formula(f, k, b, r, self.C),
                    places=9)


class ResolveWake(unittest.TestCase):
    """Next wake moment: enabled alarm wins; else 07:00 workdays / 09:00 weekends
    (user 2026-07-29), resolved against the day the wake actually lands on."""

    def test_enabled_alarm_later_today(self):
        got = cm.resolve_wake(datetime(2026, 7, 29, 5, 0), "08:00:00", True)
        self.assertEqual(got, datetime(2026, 7, 29, 8, 0))

    def test_enabled_alarm_already_rang_rolls_to_tomorrow(self):
        got = cm.resolve_wake(datetime(2026, 7, 29, 21, 0), "08:00:00", True)
        self.assertEqual(got, datetime(2026, 7, 30, 8, 0))

    def test_friday_evening_no_alarm_lands_saturday_0900(self):
        # 2026-07-31 is a Friday: the wake lands on SATURDAY -> weekend fallback.
        got = cm.resolve_wake(datetime(2026, 7, 31, 22, 0), None, False)
        self.assertEqual(got, datetime(2026, 8, 1, 9, 0))

    def test_sunday_evening_no_alarm_lands_monday_0700(self):
        # 2026-08-02 is a Sunday: the wake lands on MONDAY -> workday fallback.
        got = cm.resolve_wake(datetime(2026, 8, 2, 22, 0), None, False)
        self.assertEqual(got, datetime(2026, 8, 3, 7, 0))

    def test_saturday_early_morning_no_alarm_is_same_day_0900(self):
        got = cm.resolve_wake(datetime(2026, 8, 1, 5, 30), None, False)
        self.assertEqual(got, datetime(2026, 8, 1, 9, 0))

    def test_garbage_alarm_returns_none(self):
        self.assertIsNone(cm.resolve_wake(datetime(2026, 7, 29, 5, 0), "not-a-time", True))

    def test_custom_fallbacks(self):
        got = cm.resolve_wake(datetime(2026, 7, 29, 22, 0), None, False,
                              workday_hms="06:30:00", weekend_hms="10:00:00")
        self.assertEqual(got, datetime(2026, 7, 30, 6, 30))   # Thursday = workday


class ComposeBriefingSharedHome(unittest.TestCase):
    def test_moved_verbatim_and_reexported(self):
        # one voice, one function: morning_briefing's compose is THIS function.
        import morning_briefing as mb
        self.assertIs(mb.compose_briefing, cm.compose_briefing)
        title, body = cm.compose_briefing("windows", {"cost_label": "~1.7 kr"}, {}, False, False)
        self.assertEqual((title, body), ("AC not needed", "Keep windows open."))


class CoolingMinutes(unittest.TestCase):
    """Two-regime descent pricing (2026-07-30): fast rate above the knee (wall+headroom),
    crawl below -- a 4.4C job priced 165 min linear vs ~6 engaged hours metered."""

    def test_todays_job_prices_like_the_meter_says(self):
        # floor 23.7 -> 19.3, wall 20.33, fast 1.68, crawl 0.4:
        # fast 23.7-21.03 = 2.67C @1.68 ~ 95min; crawl 21.03-19.3 = 1.73C @0.4 ~ 259min
        got = cm.cooling_minutes(23.7, 19.3, 1.68, wall=20.33)
        self.assertAlmostEqual(got, 95.4 + 259.5, delta=2.0)

    def test_no_wall_is_linear(self):
        self.assertAlmostEqual(cm.cooling_minutes(23.0, 20.0, 1.5), 120.0, places=3)

    def test_all_above_knee_is_pure_fast(self):
        got = cm.cooling_minutes(24.0, 22.0, 2.0, wall=20.0)   # knee 20.7, target above it
        self.assertAlmostEqual(got, 60.0, places=3)

    def test_all_below_knee_is_pure_crawl(self):
        got = cm.cooling_minutes(20.5, 20.0, 2.0, wall=20.0)   # knee 20.7, floor below it
        self.assertAlmostEqual(got, 0.5 / 0.4 * 60.0, places=3)

    def test_nothing_to_cool_is_zero(self):
        self.assertEqual(cm.cooling_minutes(20.0, 20.0, 1.7, wall=20.0), 0.0)
        self.assertEqual(cm.cooling_minutes(19.0, 20.0, 1.7, wall=20.0), 0.0)
        self.assertEqual(cm.cooling_minutes(None, 20.0, 1.7), 0.0)


if __name__ == "__main__":
    unittest.main()
