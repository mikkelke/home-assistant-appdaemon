from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Import only the pure functions; the module imports appdaemon which is not
# installed locally, so stub it before import.
import types

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

import bedroom_comfort as bc  # noqa: E402


class DewPoint(unittest.TestCase):
    def test_matches_measured_night(self):
        # 2026-07-09 night measurements (bedroom sensor vs computed dew point)
        self.assertAlmostEqual(bc.dew_point_c(23.3, 42.0), 9.7, delta=0.15)
        self.assertAlmostEqual(bc.dew_point_c(24.1, 54.0), 14.2, delta=0.15)

    def test_invalid(self):
        self.assertIsNone(bc.dew_point_c(None, 50))
        self.assertIsNone(bc.dew_point_c(20, 0))
        self.assertIsNone(bc.dew_point_c("x", 50))


class Projection(unittest.TestCase):
    def test_two_sleepers_full_night(self):
        # calibration: 0.25/sleeper/h * 2 sleepers * 9 h = +4.5 (9.7 -> 14.2)
        self.assertAlmostEqual(
            bc.project_morning_dp(9.7, 2, 9, 0.25), 14.2, delta=0.01)

    def test_hours_capped(self):
        self.assertAlmostEqual(
            bc.project_morning_dp(10, 2, 24, 0.25),
            bc.project_morning_dp(10, 2, 10, 0.25))


class EffectiveCeiling(unittest.TestCase):
    def test_humid_two_sleepers(self):
        ceil, red = bc.effective_ceiling(23.0, 14.2, 2)
        self.assertAlmostEqual(red, 0.83, delta=0.01)
        self.assertAlmostEqual(ceil, 22.2, delta=0.05)

    def test_dry_single(self):
        ceil, red = bc.effective_ceiling(23.0, 10.0, 1)
        self.assertEqual(red, 0.0)
        self.assertEqual(ceil, 23.0)

    def test_reduction_bounded(self):
        ceil, red = bc.effective_ceiling(23.0, 30.0, 2)
        self.assertEqual(red, 1.5)
        self.assertEqual(ceil, 21.5)


class VentHelps(unittest.TestCase):
    def test_cool_dry_outdoor(self):
        ok, _ = bc.vent_helps(23.0, 10.2, 16.4, 9.1)
        self.assertTrue(ok)

    def test_muggy_outdoor(self):
        ok, why = bc.vent_helps(24.0, 12.0, 20.0, 18.0)
        self.assertFalse(ok)
        self.assertIn("dew point", why)

    def test_warm_outdoor(self):
        ok, _ = bc.vent_helps(23.0, 12.0, 25.0, 9.0)
        self.assertFalse(ok)

    def test_missing_data(self):
        ok, _ = bc.vent_helps(None, 12.0, 20.0, 9.0)
        self.assertIsNone(ok)


class Classify(unittest.TestCase):
    def test_ladder_absolute_anchors(self):
        # anchors are human comfort, independent of the planning knob
        self.assertEqual(bc.classify(24.6, 10.0, 20.0, 19.9), "hot")
        self.assertEqual(bc.classify(23.0, 14.0, 20.0, 19.9), "sticky")
        self.assertEqual(bc.classify(23.2, 10.0, 20.0, 19.9), "warm")
        self.assertEqual(bc.classify(20.8, 10.0, 20.0, 19.9), "comfortable")


class Morning(unittest.TestCase):
    def test_before_and_after_seven(self):
        self.assertAlmostEqual(
            bc.hours_until_morning(datetime(2026, 7, 12, 23, 0)), 8.0)
        self.assertAlmostEqual(
            bc.hours_until_morning(datetime(2026, 7, 12, 3, 0)), 4.0)
        self.assertEqual(bc.hours_until_morning(datetime(2026, 7, 12, 12, 0)), 10.0)


class ReexportedNames(unittest.TestCase):
    """The refactor moved the pure fns into climate_model and re-exports them here. Guard the
    exact names other code / tests import off bedroom_comfort, and that the removed duplicates
    are gone."""

    def test_reexports_present(self):
        for name in ("dew_point_c", "project_morning_dp", "effective_ceiling",
                     "hours_until_morning", "classify", "vent_helps"):
            self.assertTrue(hasattr(bc, name), name)

    def test_duplicated_projection_and_verdict_deleted(self):
        self.assertFalse(hasattr(bc, "project_zone_peak"))
        self.assertFalse(hasattr(bc, "ac_worth"))


class WindowAwareVerdict(unittest.TestCase):
    """bedroom_comfort's verdict/projected_peak/ac_worth are now a THIN read of
    sensor.sleep_plan, and rise_frac comes from the published sensor.smart_cooling_status
    attribute -- NOT from smart_cooling's state file (that read was half the old cycle)."""

    def _app(self, plan_rec="ac", headline="Run the AC ~1.8 kr", peak=24.3,
             rise="0.63", temp="22.0", rh="50"):
        app = bc.BedroomComfort.__new__(bc.BedroomComfort)
        app.temp_entity = "sensor.bedroom_median_temperature"
        app.rh_entity = "sensor.bedroom_humidity"
        app.out_temp_entity = "sensor.gw2000a_outdoor_temperature"
        app.out_rh_entity = "sensor.gw2000a_humidity"
        app.persons = ["person.mikkel"]
        app.comfort_anchor = 23.0
        app.floor_entity = "sensor.floor"
        app.mid_entity = "sensor.mid"
        app.kitchen_entity = "sensor.kitchen"
        app.rise_frac_fallback = 0.5
        app.status_entity = "sensor.smart_cooling_status"
        app.sleep_plan_entity = "sensor.sleep_plan"
        app.dp_rate = 0.5
        app.knee = 12.0
        app.penalty = 0.15
        app.second_sleeper = 0.5
        app.max_reduction = 1.5
        app.publish_entity = "sensor.bedroom_comfort"

        plain = {
            "sensor.bedroom_median_temperature": temp,
            "sensor.bedroom_humidity": rh,
            "sensor.gw2000a_outdoor_temperature": "15.0",
            "sensor.gw2000a_humidity": "60",
            "person.mikkel": "home",
            "sensor.sleep_plan": plan_rec,
        }
        attrs = {
            ("sensor.smart_cooling_status", "rise_frac"): rise,
            ("sensor.sleep_plan", "headline"): headline,
            ("sensor.sleep_plan", "projected_peak"): peak,
        }

        def get_state(entity, attribute=None):
            if attribute is not None:
                return attrs.get((entity, attribute))
            return plain.get(entity)
        app.get_state = get_state

        app._captured = {}

        def set_state(entity, state=None, replace=None, attributes=None):
            app._captured = {"state": state, "attrs": attributes}
        app.set_state = set_state

        app.datetime = lambda: datetime(2026, 7, 20, 23, 0)
        app.log = lambda *a, **k: None
        return app

    def test_verdict_and_peak_and_ac_worth_from_plan(self):
        app = self._app(plan_rec="ac", headline="Run the AC ~1.8 kr", peak=24.3)
        app._eval()
        a = app._captured["attrs"]
        self.assertEqual(a["verdict"], "Run the AC ~1.8 kr")
        self.assertEqual(a["projected_peak"], 24.3)
        self.assertTrue(a["ac_worth"])

    def test_hybrid_counts_as_ac_worth(self):
        app = self._app(plan_rec="hybrid", headline="Open windows now, AC backup ~1.2 kr")
        app._eval()
        self.assertTrue(app._captured["attrs"]["ac_worth"])

    def test_windows_is_not_ac_worth(self):
        app = self._app(plan_rec="windows", headline="Open a window", peak=24.0)
        app._eval()
        a = app._captured["attrs"]
        self.assertFalse(a["ac_worth"])
        self.assertEqual(a["verdict"], "Open a window")

    def test_rise_frac_from_published_status_attribute(self):
        app = self._app(rise="0.63")
        app._eval()
        self.assertEqual(app._captured["attrs"]["rise_frac"], 0.63)

    def test_rise_frac_falls_back_when_status_absent(self):
        app = self._app(rise=None)
        app._eval()
        self.assertEqual(app._captured["attrs"]["rise_frac"], 0.5)

    def test_missing_plan_yields_pending_verdict(self):
        app = self._app(plan_rec=None, headline=None, peak=None)
        app._eval()
        a = app._captured["attrs"]
        self.assertEqual(a["verdict"], "sleep plan pending")
        self.assertFalse(a["ac_worth"])
        self.assertIsNone(a["projected_peak"])

    def test_no_state_file_access(self):
        # the old _rise_frac opened smart_cooling_state.json; the app must never touch a file.
        import builtins
        from unittest import mock
        app = self._app()
        with mock.patch.object(builtins, "open",
                               side_effect=AssertionError("no file access allowed")):
            app._eval()
        self.assertIn("attrs", app._captured)
        self.assertFalse(hasattr(app, "smart_cooling_state_file"))


if __name__ == "__main__":
    unittest.main()
