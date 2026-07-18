from __future__ import annotations

import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path

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

import deploy_advisor as da  # noqa: E402

C = da.DEFAULT_FIT


class KitchenChain(unittest.TestCase):
    def test_cool_evening_clamps_at_comfort_floor(self):
        # evening 15C but the flat only vents to the 22.25 behavioral floor
        k = da.kitchen_chain(24.0, 22.0, 15.0, C)
        self.assertAlmostEqual(k, 24.0 + 0.508 * (22.25 - 24.0) + 0.799, places=3)

    def test_hot_day_warms_the_mass(self):
        cool = da.kitchen_chain(24.0, 24.0, 22.25, C)
        hot = da.kitchen_chain(24.0, 30.0, 24.0, C)
        self.assertGreater(hot, cool + 0.5)


class FloorChain(unittest.TestCase):
    def test_floor_pulled_toward_kitchen(self):
        f = da.floor_chain(23.0, 25.0, 23.0, C)
        self.assertAlmostEqual(f, 23.0 + 0.634 * 2.0 + 0.472, places=3)

    def test_cool_evening_relief_term(self):
        warm_ev = da.floor_chain(23.0, 24.0, 25.0, C)   # min() term zero
        cool_ev = da.floor_chain(23.0, 24.0, 18.0, C)   # venting relief
        self.assertGreater(warm_ev, cool_ev)


class NightPeak(unittest.TestCase):
    def test_peak_between_floor_and_equilibrium_plus_uplift(self):
        peak = da.night_peak(23.0, 24.5, 24.0, 0.502, C)
        e = (24.5 + 24.0 + 23.0) / 3
        self.assertAlmostEqual(peak, 23.0 + (e - 23.0) * 0.502 + 1.5, places=3)


class ProjectNights(unittest.TestCase):
    DAYS = [
        {"date": "2026-07-13", "t_max": 24.0, "t_ev": 19.0},
        {"date": "2026-07-14", "t_max": 27.9, "t_ev": 22.0},
        {"date": "2026-07-15", "t_max": 29.0, "t_ev": 23.5},
    ]

    def test_tonight_uses_anchors_directly(self):
        nights = da.project_nights(24.0, 22.5, 23.5, self.DAYS, 0.502, C)
        self.assertEqual(len(nights), 3)
        self.assertAlmostEqual(nights[0]["peak"],
                               round(da.night_peak(22.5, 24.0, 23.5, 0.502, C), 1))

    def test_hot_spell_ratchets_upward(self):
        nights = da.project_nights(24.0, 22.5, 23.5, self.DAYS, 0.502, C)
        self.assertGreater(nights[2]["peak"], nights[0]["peak"])

    def test_realistic_hot_night_breaks_23(self):
        # anchored on a warm flat before the Sunday-style 27.9C day
        nights = da.project_nights(24.5, 23.0, 24.0, self.DAYS[1:], 0.502, C)
        self.assertGreater(nights[1]["peak"], 23.0)


class DailyFromHourly(unittest.TestCase):
    def test_extracts_tmax_and_evening(self):
        hourly = []
        for h in range(0, 24):
            hourly.append((datetime(2026, 7, 14, h), 18 + (10 if h == 15 else 0) + (4 if h == 22 else 0)))
        days = da.daily_from_hourly(hourly, date(2026, 7, 14))
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["t_max"], 28)
        self.assertEqual(days[0]["t_ev"], 22)

    def test_skips_past_and_incomplete_days(self):
        hourly = [(datetime(2026, 7, 13, 22), 20.0),
                  (datetime(2026, 7, 15, 3), 17.0)]  # day 15 has no evening/daytime
        days = da.daily_from_hourly(hourly, date(2026, 7, 14))
        self.assertEqual(days, [])


class StoredGate(unittest.TestCase):
    """Regression for 2026-07-18: 'not running right now' advised deploying an AC that
    was plugged in but price-holding (20:30), and nagged during the daily overnight
    storage (12:15). 'Stored' = continuously un-deployed >= stored_hours."""

    NOW = datetime(2026, 7, 18, 12, 15)

    def test_nightly_ritual_gap_is_not_stored(self):
        # unplugged ~22:30 last night, evaluated 12:15 next day: ~14 h < 30 h
        last = datetime(2026, 7, 17, 22, 30).isoformat()
        self.assertFalse(da.DeployAdvisor._stored(last, self.NOW, 30.0))

    def test_real_teardown_counts_as_stored(self):
        last = datetime(2026, 7, 15, 16, 0).isoformat()  # ~68 h ago
        self.assertTrue(da.DeployAdvisor._stored(last, self.NOW, 30.0))

    def test_never_seen_deployed_is_stored(self):
        self.assertTrue(da.DeployAdvisor._stored(None, self.NOW, 30.0))

    def test_garbage_stamp_fails_open_to_stored(self):
        self.assertTrue(da.DeployAdvisor._stored("not-a-date", self.NOW, 30.0))


if __name__ == "__main__":
    unittest.main()
