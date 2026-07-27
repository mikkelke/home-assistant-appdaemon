"""Unit tests for the DishwasherIslandSignal leaving-Unemptied cleanup.

Regression (2026-07-24 19:10): opening the dishwasher (Unemptied -> Emptied) in a
bright family room turned the island off and then straight back on white for ~6
minutes. Cause: right after turn_off(full group), get_state(bulb1) still read
"on" (stale cache), so cleanup ran adaptive_lighting/apply with
turn_on_lights=True. While bright, leaving Unemptied must end with everything
off and no turn_on_lights hand-back.
"""

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

import dishwasher_island_signal  # noqa: E402

DISHWASHER = "sensor.dishwasher_state"
PIR = "binary_sensor.kitchen_pir_presence"
BULB1 = "light.island_light_1"
FULL = "light.island_lights"
SG_LIGHT = "light.island_lights_sg"
ROOM_STATE = "sensor.room_state_family_room"
DARK_SENSOR = "sensor.darkness_family_room"
AL_MAIN = "switch.adaptive_lighting_island_lights"
AL_SG = "switch.adaptive_lighting_island_light_sg"


def make_app(states):
    app = dishwasher_island_signal.DishwasherIslandSignal.__new__(
        dishwasher_island_signal.DishwasherIslandSignal
    )
    app._dishwasher = DISHWASHER
    app._pir = PIR
    app._signal_light = BULB1
    app._full_island = FULL
    app._island_sg_light = SG_LIGHT
    app._room_state = ROOM_STATE
    app._al_main = AL_MAIN
    app._al_sg = AL_SG
    app._unemptied = "Unemptied"
    app._brightness = 100
    app._hs = [120, 100]
    app._dishwasher_full_island_active = False
    app.args = {"darkness_confirmed_sensor_entity": DARK_SENSOR}

    app.states = dict(states)
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.log = lambda *a, **kw: None
    app.call_service = MagicMock()
    app.turn_on = MagicMock()
    app.turn_off = MagicMock()
    return app


def apply_calls(app):
    return [
        c for c in app.call_service.call_args_list if c.args[0] == "adaptive_lighting/apply"
    ]


def turn_on_lights_calls(app):
    return [c for c in apply_calls(app) if c.kwargs.get("turn_on_lights")]


class LeavingUnemptiedBright(unittest.TestCase):
    def _bright_green_states(self):
        """Full-group green signal active; bulb1 STILL reads on (stale) after group off."""
        return {
            DISHWASHER: "Emptied",
            PIR: "on",
            FULL: "on",
            BULB1: "on",
            SG_LIGHT: "off",
            AL_MAIN: "off",
            AL_SG: "off",
            DARK_SENSOR: "bright",
            ROOM_STATE: "Occupied (Bright)",
        }

    def test_door_open_while_bright_green_ends_all_off(self):
        app = make_app(self._bright_green_states())
        app._dishwasher_full_island_active = True

        app._on_dishwasher_state(DISHWASHER, None, "Unemptied", "Emptied", {})

        self.assertIn(((FULL,), {}), app.turn_off.call_args_list)
        # The regression: stale bulb1 "on" must NOT trigger a turn_on_lights hand-back.
        self.assertEqual(turn_on_lights_calls(app), [])
        # AL layout restored to normal (main on); no light entity is turned on.
        self.assertEqual(app.turn_on.call_args_list, [((AL_MAIN,), {})])
        self.assertFalse(app._dishwasher_full_island_active)

    def test_bright_room_state_alone_ends_all_off(self):
        """Dark-solo layout but room meanwhile confirmed bright: still end off."""
        states = self._bright_green_states()
        states.update({FULL: "off", BULB1: "on", SG_LIGHT: "on", AL_SG: "on"})
        app = make_app(states)
        app._dishwasher_full_island_active = False

        app._on_dishwasher_state(DISHWASHER, None, "Unemptied", "Emptied", {})

        self.assertEqual(turn_on_lights_calls(app), [])
        self.assertIn(((BULB1,), {}), app.turn_off.call_args_list)
        self.assertIn(((SG_LIGHT,), {}), app.turn_off.call_args_list)
        self.assertIn(((AL_SG,), {}), app.turn_off.call_args_list)
        self.assertEqual(app.turn_on.call_args_list, [((AL_MAIN,), {})])


class LeavingUnemptiedDark(unittest.TestCase):
    def test_dark_solo_hand_back_still_applies_main_al(self):
        """Dark path unchanged: bulb1 is handed back to main AL (turn_on_lights)."""
        app = make_app(
            {
                DISHWASHER: "Emptied",
                PIR: "on",
                FULL: "off",
                BULB1: "on",
                SG_LIGHT: "on",
                AL_MAIN: "off",
                AL_SG: "on",
                DARK_SENSOR: "dark",
                ROOM_STATE: "Occupied (Dark)",
            }
        )
        app._dishwasher_full_island_active = False

        app._on_dishwasher_state(DISHWASHER, None, "Unemptied", "Emptied", {})

        main_applies = [
            c for c in turn_on_lights_calls(app) if c.kwargs.get("entity_id") == AL_MAIN
        ]
        self.assertEqual(len(main_applies), 1)
        self.assertEqual(main_applies[0].kwargs.get("lights"), [BULB1])
        self.assertIn(((SG_LIGHT,), {}), app.turn_off.call_args_list)
        self.assertIn(((AL_MAIN,), {}), app.turn_on.call_args_list)


if __name__ == "__main__":
    unittest.main()
