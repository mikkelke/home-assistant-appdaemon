"""ClaudiasRoomLights: the wall switch toggles the room's lights; turning them off after Claudia
leaves belongs to resident_room_lights_off, so this app listens to the switch only."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_LIGHTS_DIR = Path(__file__).resolve().parents[1]
if str(_LIGHTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTS_DIR))

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

import claudias_room_lights as crl  # noqa: E402

CEILING = "light.claudias_room_ceiling_lights"
STRING = "light.claudias_room_string_lights"


def make_app():
    app = crl.ClaudiasRoomLights.__new__(crl.ClaudiasRoomLights)
    app.args = {"ceiling_light": CEILING, "string_light": STRING, "verbosity_level": "normal"}
    app.listen_state = MagicMock()
    app.toggle = MagicMock()
    app.turn_off = MagicMock()
    app.log = MagicMock()
    app.initialize()
    return app


class SwitchOnly(unittest.TestCase):
    def test_listens_to_the_wall_switch_only(self):
        app = make_app()
        self.assertEqual(app.listen_state.call_count, 1)
        args, kwargs = app.listen_state.call_args
        self.assertEqual(args[1], "event.claudias_room_switch_action")
        self.assertEqual(kwargs, {"attribute": "event_type"})

    def test_top_buttons_toggle_their_lights(self):
        for event, light in (("press_1", CEILING), ("press_3", STRING)):
            with self.subTest(event):
                app = make_app()
                app._on_switch_state("event.claudias_room_switch_action", "event_type", None, event, {})
                app.toggle.assert_called_once_with(light)

    def test_bottom_buttons_do_nothing(self):
        for event in ("press_2", "press_4"):
            with self.subTest(event):
                app = make_app()
                app._on_switch_state("event.claudias_room_switch_action", "event_type", None, event, {})
                app.toggle.assert_not_called()
                app.turn_off.assert_not_called()


if __name__ == "__main__":
    unittest.main()
