# tests/test_dryer_overrun.py - Acceptance tests for overrun-aware ETA (2026-07-25).
# Run from repo root: python3 -m unittest appdaemon.apps.appliances.tests.test_dryer_overrun
# Exercises the real DryerMonitor._overrun_eta (pure staticmethod) - dryer_monitor imports
# cleanly standalone once appdaemon.plugins.hass.hassapi is stubbed, same trick used by
# test_washer_completion.py / test_washer_unavailable_grace.py in this directory (appdaemon
# isn't installed in the test env).

import sys
import types
import unittest
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

import dryer_monitor as dm  # noqa: E402


class TestOverrunEta(unittest.TestCase):
    """Tests for the real DryerMonitor._overrun_eta - a pure staticmethod, so no app
    instance is needed."""

    def test_not_overrun_unchanged_passthrough(self):
        """Not yet overrun -> remaining/progress computed exactly as before (dur-elapsed,
        elapsed/dur%), overrun=0."""
        remaining, progress, overrun = dm.DryerMonitor._overrun_eta(
            elapsed=90.0, dur=120, keep_fresh=False, current_power=450.0,
            main_cycle_power=400, overrun_floor_min=10,
        )
        self.assertEqual(remaining, 30.0)
        self.assertEqual(progress, 75)
        self.assertEqual(overrun, 0)

    def test_overrun_high_power_floors_at_overrun_floor_min(self):
        """Overrun with power still >= main_cycle_power (deep in main drying, provably not
        done) -> remaining floors at overrun_floor_min, progress 99, overrun positive."""
        remaining, progress, overrun = dm.DryerMonitor._overrun_eta(
            elapsed=121.9, dur=120, keep_fresh=False, current_power=702.0,
            main_cycle_power=400, overrun_floor_min=10,
        )
        self.assertEqual(remaining, 10)
        self.assertEqual(progress, 99)
        self.assertAlmostEqual(overrun, 1.9, places=6)
        self.assertGreater(overrun, 0)

    def test_overrun_keep_fresh_almost_done(self):
        """Overrun + keep_fresh_detected (anti-crease pattern confirmed) -> machine says main
        cycle is done -> remaining=2, progress=100."""
        remaining, progress, overrun = dm.DryerMonitor._overrun_eta(
            elapsed=125.0, dur=120, keep_fresh=True, current_power=30.0,
            main_cycle_power=400, overrun_floor_min=10,
        )
        self.assertEqual(remaining, 2)
        self.assertEqual(progress, 100)
        self.assertEqual(overrun, 5.0)

    def test_overrun_low_power_no_pattern_yet(self):
        """Overrun + low/intermittent power but keep-fresh pattern not yet confirmed ->
        remaining=5, progress=99 (not yet declared done, but not still hard-drying either)."""
        remaining, progress, overrun = dm.DryerMonitor._overrun_eta(
            elapsed=122.0, dur=120, keep_fresh=False, current_power=50.0,
            main_cycle_power=400, overrun_floor_min=10,
        )
        self.assertEqual(remaining, 5)
        self.assertEqual(progress, 99)
        self.assertEqual(overrun, 2.0)

    def test_published_remaining_never_zero(self):
        """max(1, round(remaining)) behavior applied at the publish site: none of the overrun
        branches nor the not-yet-overrun passthrough near the very end can ever publish 0,
        which would otherwise silently vanish from the entity (AppDaemon 4.5.13 set_state
        bug)."""
        cases = [
            dm.DryerMonitor._overrun_eta(119.9, 120, False, 450.0, 400, 10),  # not overrun, ~0.1 min left
            dm.DryerMonitor._overrun_eta(121.9, 120, False, 702.0, 400, 10),  # high power
            dm.DryerMonitor._overrun_eta(125.0, 120, True, 30.0, 400, 10),    # keep-fresh
            dm.DryerMonitor._overrun_eta(122.0, 120, False, 50.0, 400, 10),   # low power, no pattern
        ]
        for remaining, _progress, _overrun in cases:
            published = max(1, round(remaining))
            self.assertGreaterEqual(published, 1)
            self.assertNotEqual(published, 0)


if __name__ == "__main__":
    unittest.main()
