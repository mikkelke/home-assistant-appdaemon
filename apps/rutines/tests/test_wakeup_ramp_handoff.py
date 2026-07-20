from __future__ import annotations

import sys
import types
import unittest
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

SESSION = "input_boolean.bedroom_bed_session"


def make_ramp_app(session, ramp_active=True, session_listener=None, current_pct=50):
    """WakeupRoutine with just the state the ramp / bed-session handoff methods need,
    without running AppDaemon's initialize()."""
    app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
    app.bed_session_entity = SESSION
    app.ramp_active = ramp_active
    app._session_listener = session_listener
    app.ramp_timer = None
    app.current_pct = current_pct
    app.ramp_max_pct = 90
    app.ramp_step_pct = 6
    app.ramp_interval_sec = 60
    app.adaptive_main_switch = "switch.adaptive_lighting_bedroom"
    app.light_entity = "light.bedroom_bed_lights"
    app.user_log = "test_log"
    app.log = lambda *a, **kw: None

    app.states = {SESSION: session}
    app.get_state = lambda entity, **kw: app.states.get(entity)

    app.run_in = MagicMock(return_value="timer-handle")
    app.cancel_timer = MagicMock()
    app.cancel_listen_state = MagicMock()
    app._schedule_next_ramp_tick = MagicMock()
    app._finish_ramp = MagicMock()
    app.call_service = MagicMock()
    return app


class BedSessionActive(unittest.TestCase):
    """_bed_session_active treats None/unknown as active (fail toward not stranding the
    ramp); only an explicit "off" is inactive."""

    def _app(self, state):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        app.bed_session_entity = SESSION
        app.get_state = lambda entity, **kw: state
        return app

    def test_on_is_active(self):
        self.assertTrue(self._app("on")._bed_session_active())

    def test_off_is_not_active(self):
        self.assertFalse(self._app("off")._bed_session_active())

    def test_none_is_active(self):
        self.assertTrue(self._app(None)._bed_session_active())

    def test_unknown_is_active(self):
        self.assertTrue(self._app("unknown")._bed_session_active())


class RampTick(unittest.TestCase):
    def test_steps_brightness_when_session_on(self):
        app = make_ramp_app(session="on", current_pct=50)
        app._ramp_tick(None)
        app.call_service.assert_called_once_with(
            "light/turn_on", entity_id=app.light_entity,
            brightness_pct=56, transition=app.ramp_interval_sec,
        )
        self.assertEqual(app.current_pct, 56)
        app._schedule_next_ramp_tick.assert_called_once()
        app._finish_ramp.assert_not_called()

    def test_finishes_without_stepping_when_session_off(self):
        app = make_ramp_app(session="off", current_pct=50)
        app._ramp_tick(None)
        app.call_service.assert_not_called()
        app._schedule_next_ramp_tick.assert_not_called()
        app._finish_ramp.assert_called_once_with("bed session ended")
        self.assertEqual(app.current_pct, 50)

    def test_inactive_ramp_is_a_noop(self):
        app = make_ramp_app(session="on", ramp_active=False, current_pct=50)
        app._ramp_tick(None)
        app.call_service.assert_not_called()
        app._finish_ramp.assert_not_called()


class OnBedSessionChange(unittest.TestCase):
    def test_session_off_during_ramp_finishes(self):
        app = make_ramp_app(session="off", ramp_active=True)
        app._on_bed_session_change(SESSION, "state", "on", "off", {})
        app._finish_ramp.assert_called_once_with(
            "bed session ended - handing bedroom to ceiling logic"
        )

    def test_session_on_does_not_finish(self):
        app = make_ramp_app(session="on", ramp_active=True)
        app._on_bed_session_change(SESSION, "state", "off", "on", {})
        app._finish_ramp.assert_not_called()

    def test_session_off_while_ramp_inactive_does_not_finish(self):
        app = make_ramp_app(session="off", ramp_active=False)
        app._on_bed_session_change(SESSION, "state", "on", "off", {})
        app._finish_ramp.assert_not_called()


class MaybeStartLightRamp(unittest.TestCase):
    """_maybe_start_light_ramp's bed-session gate. The method's other gates (manual
    override / wake window / darkness) are stubbed open so only the new session check
    is under test here."""

    def _app(self, session):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        app.bed_session_entity = SESSION
        app.ramp_active = False
        app.manual_override_entity = None
        app._within_wake_window = lambda: True
        app._room_dark_for_wake_light = lambda: True
        app.states = {SESSION: session}
        app.get_state = lambda entity, **kw: app.states.get(entity)
        app.user_log = "test_log"
        app.log = lambda *a, **kw: None
        app.turn_off = MagicMock()
        app.call_service = MagicMock()
        app._attach_cancel_listeners_light = MagicMock()
        app._schedule_next_ramp_tick = MagicMock()
        app._turn_off_sleep_modes_if_on = MagicMock()
        app.ramp_start_pct = 1
        app.ramp_step_pct = 6
        app.ramp_interval_sec = 60
        app.light_entity = "light.bedroom_bed_lights"
        app.adaptive_brightness_switch = "switch.adaptive_lighting_adapt_brightness_bedroom_bed_lights"
        return app

    def test_session_off_skips_start(self):
        app = self._app("off")
        app._maybe_start_light_ramp()
        self.assertFalse(app.ramp_active)
        app.call_service.assert_not_called()
        app._attach_cancel_listeners_light.assert_not_called()

    def test_session_on_starts_ramp(self):
        app = self._app("on")
        app._maybe_start_light_ramp()
        self.assertTrue(app.ramp_active)
        app.call_service.assert_called_once()

    def test_session_unknown_starts_ramp(self):
        app = self._app(None)
        app._maybe_start_light_ramp()
        self.assertTrue(app.ramp_active)


if __name__ == "__main__":
    unittest.main()
