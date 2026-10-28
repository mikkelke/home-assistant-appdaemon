"""SonosBathroomControl: the volume buttons only change the volume while the speaker plays."""

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

import sonos_bathroom_control as sbc  # noqa: E402

BATHROOM = "media_player.bathroom"
UP, DOWN = "004", "003"


def make_app(state, volume=0.18):
    app = sbc.SonosBathroomControl.__new__(sbc.SonosBathroomControl)
    app.media_player = BATHROOM
    app.volume_step = 0.05
    app.zwave_property_key_volume_up = UP
    app.zwave_property_key_volume_down = DOWN
    states = {None: state, "volume_level": volume}
    app.get_state = lambda entity, attribute=None: states[attribute]
    app.log = MagicMock()
    app.error = MagicMock()
    app.call_service = MagicMock()
    return app


class VolumeButtons(unittest.TestCase):
    def test_buttons_adjust_a_playing_speaker(self):
        for scene, expected in ((UP, 0.23), (DOWN, 0.13)):
            app = make_app("playing")
            app.handle_volume_event("zwave_js_value_notification", {"property_key": scene}, {})
            app.call_service.assert_called_once_with(
                "media_player/volume_set", entity_id=BATHROOM, volume_level=expected
            )

    def test_buttons_leave_a_silent_speaker_alone(self):
        for state in ("idle", "paused", "off", "unavailable", None):
            for scene in (UP, DOWN):
                app = make_app(state)
                app.handle_volume_event("zwave_js_value_notification", {"property_key": scene}, {})
                app.call_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
