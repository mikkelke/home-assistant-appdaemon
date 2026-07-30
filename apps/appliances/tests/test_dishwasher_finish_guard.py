# tests/test_dishwasher_finish_guard.py - Acceptance tests for the learned-duration finish guard
# (2026-07-30 latency review fix).
# Run from repo root: python3 -m unittest appdaemon.apps.appliances.tests.test_dishwasher_finish_guard
# Exercises the real DishwasherMonitor._get_guard_duration - dishwasher_monitor imports cleanly
# standalone once appdaemon.plugins.hass.hassapi is stubbed, same trick used by
# test_washer_completion.py / test_washer_unavailable_grace.py in this directory (appdaemon isn't
# installed in the test env).

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

import dishwasher_monitor as dm  # noqa: E402

# Test profile set (mirrors dishwasher_monitor.py's _load_programme_profiles defaults for the
# programmes exercised here). DishwasherMonitor.PROGRAMME_PROFILES is a class attribute normally
# populated by _load_programme_profiles() during initialize(); tests set/restore it directly so
# the real _get_profile()/_get_guard_duration() can run without an AppDaemon app instance.
_TEST_PROFILES = {
    "eco": {"label": "ECO", "duration_min": 234, "duration_short_min": 74, "max_energy_kwh": 0.94},
    "quick": {"label": "QuickPowerWash", "duration_min": 58, "duration_short_min": 14, "max_energy_kwh": 1.55},
}


class TestGuardDurationLearned(unittest.TestCase):
    """Tests for the real DishwasherMonitor._get_guard_duration guard-base selection: once a
    programme has enough confirmed cycles (n >= finish_guard_min_learned_n), anchor the guard to
    the learned average instead of always trusting the profile's nominal figure (nominal ECO 234
    vs learned avg ~218-224 meant genuinely-finished cycles sat blocked for minutes)."""

    def setUp(self):
        self.app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)
        # short_entity falsy -> the eco/quick "explicit short=Yes?" check short-circuits without
        # ever calling self.get_state (which __new__ does not provide, same as the other
        # appdaemon-stub tests in this directory).
        self.app.short_entity = None
        self.app.expected_dur_at_start = None
        self._orig_profiles = dm.DishwasherMonitor.PROGRAMME_PROFILES
        dm.DishwasherMonitor.PROGRAMME_PROFILES = dict(_TEST_PROFILES)

    def tearDown(self):
        dm.DishwasherMonitor.PROGRAMME_PROFILES = self._orig_profiles

    def test_no_learned_entry_uses_nominal(self):
        """No learned entry at all for the key -> nominal, same as before this fix."""
        self.app.finish_guard_use_learned = True
        self.app.finish_guard_min_learned_n = 5
        self.app._learned_durations = {}
        self.assertEqual(self.app._get_guard_duration(tick_prog="eco"), 234)

    def test_below_threshold_n_uses_nominal(self):
        """n < finish_guard_min_learned_n -> nominal, unaffected by the (too-thin) learned data."""
        self.app.finish_guard_use_learned = True
        self.app.finish_guard_min_learned_n = 5
        self.app._learned_durations = {"eco": {"n": 4, "avg": 150.0}}
        self.assertEqual(self.app._get_guard_duration(tick_prog="eco"), 234)

    def test_at_threshold_n_uses_learned_avg(self):
        """n >= finish_guard_min_learned_n -> guard anchors to the learned average, not nominal
        (n=48, avg~220 matches the observed ECO learning history that motivated this fix)."""
        self.app.finish_guard_use_learned = True
        self.app.finish_guard_min_learned_n = 5
        self.app._learned_durations = {"eco": {"n": 48, "avg": 220.0}}
        self.assertEqual(self.app._get_guard_duration(tick_prog="eco"), 220)

    def test_learned_avg_floored_at_75_percent_nominal(self):
        """Learned average far below nominal (e.g. a polluted/short learning history) is floored
        at 75% of nominal rather than trusted outright."""
        self.app.finish_guard_use_learned = True
        self.app.finish_guard_min_learned_n = 5
        self.app._learned_durations = {"eco": {"n": 48, "avg": 100.0}}  # well under 0.75 * 234 = 175.5
        self.assertEqual(self.app._get_guard_duration(tick_prog="eco"), round(0.75 * 234))

    def test_flag_off_uses_nominal_regardless_of_learning(self):
        """finish_guard_use_learned False -> always nominal, regardless of learned data."""
        self.app.finish_guard_use_learned = False
        self.app.finish_guard_min_learned_n = 5
        self.app._learned_durations = {"eco": {"n": 48, "avg": 220.0}}
        self.assertEqual(self.app._get_guard_duration(tick_prog="eco"), 234)

    def test_eco_short_learn_key_used_when_user_confirmed_short(self):
        """User explicitly set short=Yes -> guard uses the eco_short learn key/nominal (74 min),
        never eco's - same explicit-Yes-only derivation the nominal branch itself uses (never the
        classifier's detected_short)."""
        self.app.short_entity = "input_select.dishwasher_short"
        self.app.get_state = lambda entity: "Yes"
        self.app.finish_guard_use_learned = True
        self.app.finish_guard_min_learned_n = 5
        self.app._learned_durations = {
            "eco": {"n": 48, "avg": 220.0},
            "eco_short": {"n": 6, "avg": 70.0},
        }
        self.assertEqual(self.app._get_guard_duration(tick_prog="eco"), 70)

    def test_quick_short_learn_key_used_when_user_confirmed_short(self):
        """Same eco_short derivation, mirrored for QuickPowerWash's quick_short key."""
        self.app.short_entity = "input_select.dishwasher_short"
        self.app.get_state = lambda entity: "Yes"
        self.app.finish_guard_use_learned = True
        self.app.finish_guard_min_learned_n = 5
        self.app._learned_durations = {
            "quick": {"n": 20, "avg": 55.0},
            "quick_short": {"n": 8, "avg": 13.0},
        }
        self.assertEqual(self.app._get_guard_duration(tick_prog="quick"), 13)


if __name__ == "__main__":
    unittest.main()
