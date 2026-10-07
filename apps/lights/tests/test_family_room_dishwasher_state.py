"""FamilyRoomLights + dishwasher Unemptied: the island belongs to FamilyRoomLights except while
dishwasher_island_signal shows its signal (Unemptied AND kitchen occupied).

The dishwasher state changing must never switch the island off by itself (that raced the signal app's own
hand-back and darkened an occupied family room), and the standby / neighbour island logic must keep powering the
island while the dishwasher is Unemptied and the kitchen is empty.
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

import family_room_lights as fr  # noqa: E402

DISHWASHER = "sensor.dishwasher_state"
KPIR = "binary_sensor.kitchen_active"
ROOM_STATE = "sensor.room_state_family_room"
DARK = "sensor.darkness_family_room"
ISLAND = "light.island_lights"
COUNTER = "light.kitchen_counter_lights"


def make_states(dishwasher="Unemptied", darkness="dark", kitchen="off", island="off"):
    label = "Occupied (Dark)" if darkness == "dark" else "Occupied (Bright)"
    return {
        DISHWASHER: {"state": dishwasher},
        DARK: {"state": darkness},
        ROOM_STATE: {"state": label, "attributes": {}},
        KPIR: {"state": kitchen},
        ISLAND: {"state": island},
        COUNTER: {"state": "off"},
    }


def make_app(states):
    app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
    app.presence = {"kitchen": KPIR}
    app._dishwasher_state_entity = DISHWASHER
    app.room_state_text_entity = ROOM_STATE
    app._darkness_confirmed_sensor = DARK
    app.light_map = {"island": [ISLAND], "hallway": [], "all": [ISLAND, COUNTER]}
    app.manual_override_booleans = {}
    app._diag_sensor = None
    app._manual_bright_watch = set()
    app._manual_bright_echo_until = {}
    app._manual_bright_echo_seconds = 8.0
    app.log = lambda *a, **kw: None

    app.states = dict(states)

    def get_state(entity, attribute=None, **kw):
        ent = app.states.get(entity)
        if ent is None:
            return None
        if attribute is None:
            return ent.get("state")
        return ent.get("attributes", {}).get(attribute)

    app.get_state = get_state
    app.turn_on = MagicMock()
    app.turn_off = MagicMock()
    app._schedule_evaluation = MagicMock()
    return app


class DishwasherStateChangeLeavesTheIslandAlone(unittest.TestCase):
    def test_entering_unemptied_does_not_switch_the_island_off(self):
        app = make_app(make_states(dishwasher="Unemptied", island="on"))
        app._on_dishwasher_state_change(DISHWASHER, None, "Off", "Unemptied", {})
        app.turn_off.assert_not_called()
        app.turn_on.assert_not_called()
        app._schedule_evaluation.assert_called_once_with()

    def test_leaving_unemptied_does_not_switch_the_island_off(self):
        app = make_app(make_states(dishwasher="Emptied", island="on"))
        app._on_dishwasher_state_change(DISHWASHER, None, "Unemptied", "Emptied", {})
        app.turn_off.assert_not_called()
        app.turn_on.assert_not_called()
        app._schedule_evaluation.assert_called_once_with()


class IslandStaysWithFamilyRoomLightsWhileKitchenIsEmpty(unittest.TestCase):
    def test_island_only_standby_powers_the_island_in_the_dark(self):
        app = make_app(make_states(dishwasher="Unemptied", darkness="dark", kitchen="off"))
        app._turn_on_island_only()
        self.assertEqual([c.args[0] for c in app.turn_on.call_args_list], [ISLAND])

    def test_activating_standby_powers_the_island_in_the_dark(self):
        app = make_app(make_states(dishwasher="Unemptied", darkness="dark", kitchen="off"))
        app._activate_standby_mode()
        self.assertEqual([c.args[0] for c in app.turn_on.call_args_list], [ISLAND])

    def test_nothing_is_exempt_from_turn_off_while_the_kitchen_is_empty(self):
        for darkness in ("dark", "bright"):
            with self.subTest(darkness=darkness):
                app = make_app(make_states(dishwasher="Unemptied", darkness=darkness, kitchen="off"))
                self.assertEqual(app._turn_off_exempt_dishwasher_signal_lights(), set())


class KeptDishwasherRules(unittest.TestCase):
    """Rules that only apply while the signal owns the island or in bright daylight - unchanged."""

    def test_unemptied_and_bright_still_leaves_the_island_unpowered(self):
        app = make_app(make_states(dishwasher="Unemptied", darkness="bright", kitchen="off"))
        app._turn_on_island_only()
        app.turn_on.assert_not_called()

    def test_full_group_is_exempt_from_turn_off_only_for_the_bright_kitchen_signal(self):
        bright = make_app(make_states(dishwasher="Unemptied", darkness="bright", kitchen="on"))
        self.assertEqual(bright._turn_off_exempt_dishwasher_signal_lights(), {ISLAND})

        dark = make_app(make_states(dishwasher="Unemptied", darkness="dark", kitchen="on"))
        self.assertEqual(dark._turn_off_exempt_dishwasher_signal_lights(), set())

        emptied = make_app(make_states(dishwasher="Emptied", darkness="bright", kitchen="on"))
        self.assertEqual(emptied._turn_off_exempt_dishwasher_signal_lights(), set())


if __name__ == "__main__":
    unittest.main()
