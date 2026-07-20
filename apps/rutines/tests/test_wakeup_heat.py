from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lights"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds"))

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

import wakeup_bedroom as wb  # noqa: E402

NOW = datetime(2026, 7, 20, 6, 15, 0)


def make_app(states, *, heat_wave_on=False, heat_auto_enable=True, auto_clear=True,
             forecast_high=None, forecast_at=None):
    """WakeupRoutine with just the state _decide_bedroom_wake_target needs, without
    running AppDaemon's initialize()."""
    app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
    app.bedroom_cover_target = 38
    app.bedroom_cover_target_heat_wave = 72
    app.heat_wave_entity = "input_boolean.heat_wave_mode"
    app.heat_auto_enable = heat_auto_enable
    app.heat_wave_manual_auto_clear = auto_clear
    app.sun_entity = "sun.sun"
    app.solar_radiation_entity = "sensor.gw2000a_solar_radiation"
    app.window_azimuth = 70.0
    app.az_tolerance = 55.0
    app.min_elevation = 3.0
    app.radiation_threshold = 250.0
    app.outdoor_temp_entity = "sensor.gw2000a_outdoor_temperature"
    app.live_outdoor_hot_c = 22.0
    app.forecast_high_threshold_c = 25.0
    app.forecast_max_age_min = 180
    app._forecast_high_c = forecast_high
    app._forecast_high_at = forecast_at
    app.datetime = lambda: NOW

    all_states = dict(states)
    all_states[(app.heat_wave_entity, None)] = "on" if heat_wave_on else "off"
    app.get_state = lambda entity, **kw: all_states.get((entity, kw.get("attribute")))
    app.turn_off = MagicMock()
    return app


# Common "cool, no sun on window" background readings, reused by the forecast/live-outdoor
# cases below so each test only has to vary the one signal it's checking.
COOL_NO_SUN = {
    ("sun.sun", "azimuth"): 200.0,   # off window
    ("sun.sun", "elevation"): 10.0,
    ("sensor.gw2000a_solar_radiation", None): 0.0,
    ("sensor.gw2000a_outdoor_temperature", None): 18.0,
}


class ManualForce(unittest.TestCase):
    def test_manual_on_returns_heat_target_and_clears_toggle(self):
        app = make_app({}, heat_wave_on=True)
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 72)
        app.turn_off.assert_called_once_with(app.heat_wave_entity)

    def test_manual_on_without_auto_clear_leaves_toggle_alone(self):
        app = make_app({}, heat_wave_on=True, auto_clear=False)
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 72)
        app.turn_off.assert_not_called()


class AutoDecision(unittest.TestCase):
    def test_sun_on_window_and_hot_forces_heat_target(self):
        states = {
            ("sun.sun", "azimuth"): 70.0,
            ("sun.sun", "elevation"): 10.0,
            ("sensor.gw2000a_solar_radiation", None): 300.0,
        }
        app = make_app(states)
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 72)
        self.assertIn("sun on window", reason)

    def test_fresh_forecast_high_over_threshold_forces_heat_target(self):
        app = make_app(COOL_NO_SUN, forecast_high=26.0, forecast_at=NOW)
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 72)
        self.assertIn("forecast high", reason)

    def test_forecast_high_under_threshold_keeps_normal_target(self):
        app = make_app(COOL_NO_SUN, forecast_high=24.0, forecast_at=NOW)
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 38)

    def test_stale_forecast_high_is_ignored(self):
        stale_at = NOW - timedelta(minutes=181)  # just past forecast_max_age_min (180)
        app = make_app(COOL_NO_SUN, forecast_high=26.0, forecast_at=stale_at)
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 38)

    def test_live_outdoor_hot_forces_heat_target_when_forecast_unavailable(self):
        states = dict(COOL_NO_SUN)
        states[("sensor.gw2000a_outdoor_temperature", None)] = 23.0
        app = make_app(states)  # no forecast cached
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 72)
        self.assertIn("warm now", reason)

    def test_live_outdoor_cool_keeps_normal_target(self):
        states = dict(COOL_NO_SUN)
        states[("sensor.gw2000a_outdoor_temperature", None)] = 20.0
        app = make_app(states)
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 38)

    def test_everything_unavailable_keeps_normal_target(self):
        app = make_app({})
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 38)

    def test_auto_disabled_keeps_normal_target_even_when_hot(self):
        states = {
            ("sun.sun", "azimuth"): 70.0,
            ("sun.sun", "elevation"): 10.0,
            ("sensor.gw2000a_solar_radiation", None): 300.0,
        }
        app = make_app(states, heat_auto_enable=False)
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 38)


class NudgeCoverIfClosed(unittest.TestCase):
    """A closed blind can report 98-99 instead of 100; the morning open must still fire."""

    def _app(self, closed_is_100=True, closed_threshold=95):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        app.closed_is_100 = closed_is_100
        app.closed_threshold = closed_threshold
        app._set_cover_position = MagicMock()
        return app

    def test_exact_closed_100_triggers_open(self):
        app = self._app()
        app._cover_position = lambda e: 100
        app._nudge_cover_if_closed("cover.bedroom_blind", 38)
        app._set_cover_position.assert_called_once_with("cover.bedroom_blind", 38)

    def test_near_closed_99_triggers_open(self):
        app = self._app()
        app._cover_position = lambda e: 99
        app._nudge_cover_if_closed("cover.bedroom_blind", 72)
        app._set_cover_position.assert_called_once_with("cover.bedroom_blind", 72)

    def test_at_threshold_95_triggers_open(self):
        app = self._app()
        app._cover_position = lambda e: 95
        app._nudge_cover_if_closed("cover.bedroom_blind", 38)
        app._set_cover_position.assert_called_once()

    def test_just_below_threshold_94_does_not_trigger(self):
        app = self._app()
        app._cover_position = lambda e: 94
        app._nudge_cover_if_closed("cover.bedroom_blind", 38)
        app._set_cover_position.assert_not_called()

    def test_open_position_does_not_trigger(self):
        app = self._app()
        app._cover_position = lambda e: 72
        app._nudge_cover_if_closed("cover.bedroom_blind", 38)
        app._set_cover_position.assert_not_called()

    def test_none_position_falls_back_to_closed_state_string(self):
        app = self._app()
        app._cover_position = lambda e: None
        app.get_state = lambda e, **kw: "closed"
        app._nudge_cover_if_closed("cover.bedroom_blind", 38)
        app._set_cover_position.assert_called_once_with("cover.bedroom_blind", 38)

    def test_inverted_scale_near_open_zero_triggers(self):
        app = self._app(closed_is_100=False)
        app._cover_position = lambda e: 2  # <= 100-95 -> counts as closed on a 0=closed scale
        app._nudge_cover_if_closed("cover.bedroom_blind", 38)
        app._set_cover_position.assert_called_once()


if __name__ == "__main__":
    unittest.main()
