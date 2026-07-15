from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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

import bedroom_door_watch as bdw  # noqa: E402


def make_app(states):
    """BedroomDoorWatch with a fake get_state reading from `states` dict, without
    running AppDaemon's initialize()."""
    app = bdw.BedroomDoorWatch.__new__(bdw.BedroomDoorWatch)
    app.smart_cooling_entity = "input_boolean.smart_cooling"
    app.ac_climate_entity = "climate.ac"
    app.door_sensor = "binary_sensor.bedroom_door_contact"
    app.open_alert_minutes = 5
    app.get_state = lambda entity, **kw: states.get(entity)
    return app


class CoolingActive(unittest.TestCase):
    def test_true_when_armed_and_deployed(self):
        app = make_app({"input_boolean.smart_cooling": "on", "climate.ac": "off"})
        self.assertTrue(app._cooling_active())

    def test_false_when_disarmed(self):
        app = make_app({"input_boolean.smart_cooling": "off", "climate.ac": "cool"})
        self.assertFalse(app._cooling_active())

    def test_false_when_ac_not_deployed(self):
        app = make_app({"input_boolean.smart_cooling": "on", "climate.ac": "unavailable"})
        self.assertFalse(app._cooling_active())


class AlertBehavior(unittest.TestCase):
    def _app(self, door_state, armed=True, deployed=True):
        app = make_app({
            "binary_sensor.bedroom_door_contact": door_state,
            "input_boolean.smart_cooling": "on" if armed else "off",
            "climate.ac": "cool" if deployed else "unavailable",
        })
        app._notifier = MagicMock()
        app._alerted_this_episode = False
        app.create_task = MagicMock()
        app.log = MagicMock()
        return app

    def test_alerts_when_still_open_and_cooling(self):
        app = self._app("on")
        app._maybe_alert({})
        app.create_task.assert_called_once()
        self.assertTrue(app._alerted_this_episode)

    def test_no_alert_if_door_closed_before_timer_fires(self):
        app = self._app("off")
        app._maybe_alert({})
        app.create_task.assert_not_called()

    def test_no_alert_if_cooling_not_active(self):
        app = self._app("on", armed=False)
        app._maybe_alert({})
        app.create_task.assert_not_called()

    def test_no_duplicate_alert_same_episode(self):
        app = self._app("on")
        app._alerted_this_episode = True
        app._maybe_alert({})
        app.create_task.assert_not_called()

    def test_reopen_after_close_rearms(self):
        app = self._app("on")
        app._alerted_this_episode = True
        # door closed then reopened -> _on_door resets the flag
        app._arm = MagicMock()
        app._disarm = MagicMock()
        app._on_door("binary_sensor.bedroom_door_contact", "state", "on", "off", {})
        app._disarm.assert_called_once()
        app._on_door("binary_sensor.bedroom_door_contact", "state", "off", "on", {})
        self.assertFalse(app._alerted_this_episode)
        app._arm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
