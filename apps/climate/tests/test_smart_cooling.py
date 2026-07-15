from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime
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

import smart_cooling as sc  # noqa: E402


def make_app(**overrides):
    """SmartCooling instance without running AppDaemon's initialize() -
    the pure helpers only read a handful of instance attributes."""
    app = sc.SmartCooling.__new__(sc.SmartCooling)
    app.min_temp = overrides.get("min_temp", 16.0)
    app._rise_frac = overrides.get("rise_frac", 0.5)
    app._rise_samples = overrides.get("rise_samples", 10)
    app.dry_run = overrides.get("dry_run", False)
    app.stall_deficit_min = overrides.get("stall_deficit_min", 0.3)
    app.stall_burp_cooldown_min = overrides.get("stall_burp_cooldown_min", 15)
    app._burp_until = overrides.get("burp_until", None)
    app._last_burp = overrides.get("last_burp", None)
    app.sat_engaged_min = overrides.get("sat_engaged_min", 90)
    app.sat_reset_rise = overrides.get("sat_reset_rise", 0.5)
    app.feasible_min_samples = overrides.get("feasible_min_samples", 2)
    app._sat_min = overrides.get("sat_min", None)
    app._sat_noprog_min = 0.0
    app._saturated = False
    app._feasible_floor = overrides.get("feasible_floor", None)
    app._feasible_samples = overrides.get("feasible_samples", 0)
    app._save_state = lambda: None
    app.log = lambda *a, **k: None
    return app


class AttrsBuild(unittest.TestCase):
    """Regression test for the 2026-07-14 incident: `_attrs()` referenced
    `ceiling_base` as a free variable instead of a parameter, so EVERY armed
    evaluation crashed with NameError and SmartCooling silently never
    published anything but the disarmed status - "not ready to be used" with
    no visible error to the user. Calling `_attrs()` at all is the trip wire:
    the bug fired on the very first reference, independent of arguments."""

    def _call(self, ceiling, ceiling_base):
        app = make_app()
        return app._attrs(
            floor=22.0, mid=22.5, zone=22.2, ceil_s=21.0, ac_s=17.0,
            bath=24.0, kitchen=23.0, E=24.0, target=21.5, deficit=0.5,
            ceiling=ceiling, price_now=1.2, window_open=True,
            run_min=45, next_start=datetime(2026, 7, 14, 22, 15), est_cost=1.8,
            floor_limited=False, ceiling_base=ceiling_base,
        )

    def test_no_nameerror_and_keys_present(self):
        attrs = self._call(ceiling=22.0, ceiling_base=23.0)
        for key in ("ceiling_base", "ceiling_source", "night_ceiling", "floor_target"):
            self.assertIn(key, attrs)

    def test_ceiling_source_comfort_layer_when_lowered(self):
        attrs = self._call(ceiling=22.0, ceiling_base=23.0)
        self.assertEqual(attrs["ceiling_source"], "comfort layer")
        self.assertEqual(attrs["ceiling_base"], 23.0)

    def test_ceiling_source_knob_when_unadjusted(self):
        attrs = self._call(ceiling=23.0, ceiling_base=23.0)
        self.assertEqual(attrs["ceiling_source"], "knob")


class NextMidnight(unittest.TestCase):
    """Regression coverage for the 2026-07-15 bedtime-knob removal: the price-optimizer's
    search deadline is now a fixed midnight hard cap (user: "we should not count that we can
    cool past midnight"), replacing the old per-night bedtime_entity lookup."""

    def _midnight(self, now):
        app = make_app()
        return app._next_midnight(now)

    def test_midday_rolls_to_tonight(self):
        now = datetime(2026, 7, 15, 14, 0)
        self.assertEqual(self._midnight(now), datetime(2026, 7, 16, 0, 0))

    def test_just_before_midnight_still_tonight(self):
        now = datetime(2026, 7, 15, 23, 59)
        self.assertEqual(self._midnight(now), datetime(2026, 7, 16, 0, 0))

    def test_already_past_midnight_rolls_to_tomorrow_not_negative(self):
        # if _evaluate somehow runs at 02:00, the deadline must still be STRICTLY future -
        # using "start of today" here would put it 2 hours in the past.
        now = datetime(2026, 7, 15, 2, 0)
        deadline = self._midnight(now)
        self.assertEqual(deadline, datetime(2026, 7, 16, 0, 0))
        self.assertGreater(deadline, now)


class StashLightout(unittest.TestCase):
    """The AC-removed toggle is now the lights-out trigger (was: a bedtime time-window guess
    that could be hours off from when anyone actually went to bed, corrupting rise_frac)."""

    def test_stash_records_baseline_and_end_time(self):
        app = make_app()
        app.sleep_hours = 8.0
        app._save_state = lambda: None
        app.log = lambda *a, **k: None
        now = datetime(2026, 7, 15, 23, 30)

        app._stash_lightout(floor=22.3, E=25.1, now=now)

        self.assertEqual(app._lightout["date"], "2026-07-15")
        self.assertEqual(app._lightout["F0"], 22.3)
        self.assertEqual(app._lightout["E"], 25.1)
        self.assertEqual(app._lightout["end"], datetime(2026, 7, 16, 7, 30).isoformat())

    def test_stash_overwrites_stale_inflight_record(self):
        # a second press the same night (changed their mind, went back to bed later) should
        # replace the first baseline, not merge with or defer to it.
        app = make_app()
        app.sleep_hours = 8.0
        app._save_state = lambda: None
        app.log = lambda *a, **k: None
        app._lightout = {"date": "2026-07-15", "F0": 23.0, "E": 24.0, "end": "stale"}

        app._stash_lightout(floor=21.0, E=25.0, now=datetime(2026, 7, 16, 0, 15))

        self.assertEqual(app._lightout["F0"], 21.0)
        self.assertEqual(app._lightout["E"], 25.0)


class ShouldBurp(unittest.TestCase):
    """Stall-breaker decision (2026-07-15): the unit parks at ~300 W 'idle' with real floor
    deficit left because its intake sensor sits in its own cold outflow. Measured: no fan
    mode wakes it; a fan_only burp does (44 W spent, 550-800 W of real cooling resumes)."""

    NOW = datetime(2026, 7, 15, 15, 30)

    def test_burps_when_idle_with_deficit(self):
        app = make_app()
        self.assertTrue(app._should_burp("idle", "cool", 4.0, self.NOW))

    def test_no_burp_while_actively_cooling(self):
        app = make_app()
        self.assertFalse(app._should_burp("cooling", "cool", 4.0, self.NOW))

    def test_no_burp_when_mode_not_cool(self):
        # fan_only during a burp also reports a non-cooling action; must not re-trigger
        app = make_app()
        self.assertFalse(app._should_burp("fan", "fan_only", 4.0, self.NOW))
        self.assertFalse(app._should_burp("idle", "off", 4.0, self.NOW))

    def test_no_burp_when_target_basically_reached(self):
        app = make_app()
        self.assertFalse(app._should_burp("idle", "cool", 0.2, self.NOW))

    def test_cooldown_blocks_backtoback_burps(self):
        app = make_app(last_burp=datetime(2026, 7, 15, 15, 20))  # 10 min ago < 15 cooldown
        self.assertFalse(app._should_burp("idle", "cool", 4.0, self.NOW))
        app2 = make_app(last_burp=datetime(2026, 7, 15, 15, 10))  # 20 min ago -> allowed
        self.assertTrue(app2._should_burp("idle", "cool", 4.0, self.NOW))

    def test_no_burp_while_one_is_in_flight(self):
        app = make_app(burp_until=datetime(2026, 7, 15, 15, 32))
        self.assertFalse(app._should_burp("idle", "cool", 4.0, self.NOW))


class TrackProgress(unittest.TestCase):
    """Feasibility detector (user, 2026-07-15): sustained engaged minutes with zero floor
    progress = the floor won't take more cold tonight. Without this, an unreachable ideal
    target inflates minutes_needed until the scheduler goes time-constrained and pays
    evening-peak prices chasing it."""

    def test_progress_resets_the_clock(self):
        app = make_app()
        app._track_progress(20.0, 0)
        app._track_progress(20.0, 60)             # close to threshold...
        self.assertFalse(app._track_progress(19.8, 45))  # ...but the floor moved: reset
        self.assertEqual(app._sat_noprog_min, 0.0)
        self.assertEqual(app._sat_min, 19.8)

    def test_saturates_after_engaged_minutes_without_progress(self):
        app = make_app()
        app._track_progress(20.0, 0)
        self.assertFalse(app._track_progress(20.0, 45))
        self.assertTrue(app._track_progress(20.1, 45))   # 90 engaged min, no new low
        self.assertEqual(app._feasible_floor, 20.0)
        self.assertEqual(app._feasible_samples, 1)

    def test_coast_time_is_not_evidence(self):
        # AC held off between cheap slots for hours: engaged=0 must never saturate
        app = make_app()
        app._track_progress(20.0, 0)
        for _ in range(20):
            self.assertFalse(app._track_progress(20.2, 0))

    def test_warming_well_past_the_low_resets(self):
        app = make_app()
        app._track_progress(20.0, 0)
        app._track_progress(20.0, 80)
        self.assertFalse(app._track_progress(20.6, 15))  # rose > sat_reset_rise: new situation
        self.assertEqual(app._sat_min, 20.6)
        self.assertEqual(app._sat_noprog_min, 0.0)

    def test_learned_feasible_blends_across_nights(self):
        app = make_app(feasible_floor=19.0, feasible_samples=1)
        app._sat_noprog_min = 90.0
        app._learn_feasible(20.0)
        self.assertEqual(app._feasible_samples, 2)
        self.assertAlmostEqual(app._feasible_floor, 19.5, places=2)  # w=1/2 EMA


if __name__ == "__main__":
    unittest.main()
