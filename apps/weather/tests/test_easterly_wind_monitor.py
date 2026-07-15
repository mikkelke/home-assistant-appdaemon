from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

import easterly_wind_monitor as ewm  # noqa: E402


def _states_getter(states):
    async def get_state(entity, **kw):
        return states.get(entity)
    return get_state


def make_app(states=None, **overrides):
    """EasterlyWindMonitor without running AppDaemon's initialize() - thresholds and
    counters are set directly so _check_conditions can be exercised in isolation, with
    get_state faked from a plain dict and call_service captured via AsyncMock."""
    app = ewm.EasterlyWindMonitor.__new__(ewm.EasterlyWindMonitor)
    app.wind_dir = "sensor.gw2000a_wind_direction"
    app.wind_gust = "sensor.gw2000a_wind_gust"
    app.wind_speed = "sensor.gw2000a_wind_speed"
    app.episode_entity = "input_boolean.easterly_wind_episode_active"
    app.dir_min = overrides.get("dir_min", 60.0)
    app.dir_max = overrides.get("dir_max", 120.0)
    app.wind_speed_windy = overrides.get("wind_speed_windy", 28.8)
    app.gust_windy = overrides.get("gust_windy", 54.0)
    app.wind_unit_label = overrides.get("wind_unit_label", "km/h")
    app.sustained_min = overrides.get("sustained_min", 5)
    app.end_after_min = overrides.get("end_after_min", 10)
    app.notify_target = "mikkel"
    app.notify_on_end = False
    app._in_episode = overrides.get("in_episode", False)
    app._condition_met_count = overrides.get("condition_met_count", 0)
    app._condition_not_met_count = overrides.get("condition_not_met_count", 0)
    app._last_gust_in_episode = overrides.get("last_gust_in_episode", 0.0)
    app.mobile_notifier = overrides.get("mobile_notifier", None)
    app.get_state = _states_getter(states or {})
    app.call_service = AsyncMock()
    app.log = MagicMock()
    return app


# direction in-band (60-120), gust over the default 54 km/h windy threshold
WINDY_STATES = {
    "sensor.gw2000a_wind_direction": "90",
    "sensor.gw2000a_wind_gust": "60",
    "sensor.gw2000a_wind_speed": "10",
}

# direction in-band but neither gust nor mean speed clears the windy threshold
CALM_STATES = {
    "sensor.gw2000a_wind_direction": "90",
    "sensor.gw2000a_wind_gust": "10",
    "sensor.gw2000a_wind_speed": "5",
}


class RunEveryRegistration(unittest.TestCase):
    """Regression test for the 2026-07-15 'now' vs 'immediate' scheduler bug (commit
    8666460): run_every(cb, "now", interval) fires the first call at now+interval, not
    immediately - only the literal string "immediate" does. Pins the fix so it can't
    silently regress."""

    def test_run_every_uses_immediate_with_configured_interval(self):
        app = ewm.EasterlyWindMonitor.__new__(ewm.EasterlyWindMonitor)
        app.args = {}
        app.get_app = MagicMock(return_value=None)
        app.run_every = MagicMock()
        app.run_in = MagicMock()
        app.log = MagicMock()
        app.initialize()
        args, kwargs = app.run_every.call_args
        self.assertEqual(args[1], "immediate")
        self.assertNotEqual(args[1], "now")
        self.assertEqual(args[2], 60)  # default check_interval_seconds


class ConditionThresholds(unittest.IsolatedAsyncioTestCase):
    """Direction-band and windy (gust-or-mean) threshold evaluation, independent of the
    sustained-count state machine (each test starts from a fresh, non-episode state)."""

    async def test_windy_and_in_band_counts_toward_sustain(self):
        app = make_app(WINDY_STATES)
        await app._check_conditions({})
        self.assertEqual(app._condition_met_count, 1)
        self.assertFalse(app._in_episode)

    async def test_calm_does_not_count(self):
        app = make_app(CALM_STATES)
        await app._check_conditions({})
        self.assertEqual(app._condition_met_count, 0)

    async def test_direction_outside_band_does_not_count_even_if_windy(self):
        states = dict(WINDY_STATES, **{"sensor.gw2000a_wind_direction": "200"})
        app = make_app(states)
        await app._check_conditions({})
        self.assertEqual(app._condition_met_count, 0)

    async def test_gust_alone_qualifies_without_mean_speed(self):
        states = {
            "sensor.gw2000a_wind_direction": "90",
            "sensor.gw2000a_wind_gust": "60",
            "sensor.gw2000a_wind_speed": "unknown",
        }
        app = make_app(states)
        await app._check_conditions({})
        self.assertEqual(app._condition_met_count, 1)

    async def test_unavailable_direction_is_treated_as_not_met(self):
        states = dict(WINDY_STATES, **{"sensor.gw2000a_wind_direction": "unavailable"})
        app = make_app(states)
        await app._check_conditions({})
        self.assertEqual(app._condition_met_count, 0)
        self.assertEqual(app._condition_not_met_count, 1)


class EpisodeLifecycle(unittest.IsolatedAsyncioTestCase):
    """The sustained-count state machine: an episode starts only after sustained_minutes
    consecutive windy+in-band checks, and ends only after end_after_minutes_not_met
    consecutive checks below threshold - single-tick blips must not flip either state."""

    async def test_episode_starts_at_sustained_threshold(self):
        app = make_app(WINDY_STATES, sustained_min=3, condition_met_count=2)
        await app._check_conditions({})
        self.assertTrue(app._in_episode)
        app.call_service.assert_awaited_once_with(
            "input_boolean/turn_on", entity_id=app.episode_entity,
        )

    async def test_episode_does_not_start_before_threshold(self):
        app = make_app(WINDY_STATES, sustained_min=3, condition_met_count=1)
        await app._check_conditions({})
        self.assertFalse(app._in_episode)
        app.call_service.assert_not_awaited()

    async def test_single_calm_tick_during_episode_does_not_end_it(self):
        app = make_app(
            CALM_STATES, in_episode=True, end_after_min=10, condition_not_met_count=0,
        )
        await app._check_conditions({})
        self.assertTrue(app._in_episode)
        app.call_service.assert_not_awaited()

    async def test_episode_ends_after_sustained_calm(self):
        app = make_app(
            CALM_STATES, in_episode=True, end_after_min=3, condition_not_met_count=2,
        )
        await app._check_conditions({})
        self.assertFalse(app._in_episode)
        app.call_service.assert_awaited_once_with(
            "input_boolean/turn_off", entity_id=app.episode_entity,
        )

    async def test_windy_tick_during_episode_tracks_peak_gust_not_restart(self):
        app = make_app(WINDY_STATES, in_episode=True, last_gust_in_episode=40.0)
        await app._check_conditions({})
        self.assertEqual(app._last_gust_in_episode, 60.0)
        app.call_service.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
