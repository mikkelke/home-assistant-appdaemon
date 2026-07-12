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


if __name__ == "__main__":
    unittest.main()
