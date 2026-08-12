"""DishwasherIslandSignal + presence_trust: ghost kitchen presence never lights
the green signal, but an already-applied signal is held (asymmetric rule).

Ghost fixture = the measured 2026-08-09 16:01Z episode: kitchen composite on
via mmWave only (PIR silent since before the span) while the kitchen speaker
plays. Unemptied + that ghost previously drove the island signal for nobody.
The real clear paths (composite off / leaving Unemptied) stay untouched.
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
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
PIR_COMPOSITE = "binary_sensor.kitchen_pir_presence"
MMWAVE = "binary_sensor.kitchen_presence_presence"
PIR = "binary_sensor.kitchen_presence_pir_detection"
SPEAKER = "media_player.kitchen_2"
BULB1 = "light.island_light_1"
FULL = "light.island_lights"
SG_LIGHT = "light.island_lights_sg"
ROOM_STATE = "sensor.room_state_family_room"
DARK_SENSOR = "sensor.darkness_family_room"
AL_MAIN = "switch.adaptive_lighting_island_lights"
AL_SG = "switch.adaptive_lighting_island_light_sg"


def utc(hour, minute=0, second=0):
    return datetime(2026, 8, 9, hour, minute, second, tzinfo=timezone.utc).isoformat()


def make_app(states):
    app = dishwasher_island_signal.DishwasherIslandSignal.__new__(
        dishwasher_island_signal.DishwasherIslandSignal
    )
    app._dishwasher = DISHWASHER
    app._pir = PIR_COMPOSITE
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
    app._suspect_after_minutes = None
    app.args = {"darkness_confirmed_sensor_entity": DARK_SENSOR}

    app.states = dict(states)

    def get_state(entity, attribute=None, **kw):
        ent = app.states.get(entity)
        if ent is None:
            return None
        if not isinstance(ent, dict):
            return ent if attribute is None else None
        if attribute is None:
            return ent.get("state")
        if attribute == "last_changed":
            return ent.get("last_changed")
        if attribute == "all":
            return {
                "attributes": ent.get("attributes", {}),
                "last_changed": ent.get("last_changed"),
            }
        return ent.get("attributes", {}).get(attribute)

    app.get_state = get_state
    app.log = lambda *a, **kw: None
    app.call_service = MagicMock()
    app.turn_on = MagicMock()
    app.turn_off = MagicMock()
    return app


def ghost_states(pir_last_changed=utc(15, 35), signal_applied=False, dark=True):
    """Unemptied + kitchen composite held by mmWave only + speaker playing."""
    return {
        DISHWASHER: {"state": "Unemptied"},
        PIR_COMPOSITE: {
            "state": "on",
            "attributes": {"entity_id": [MMWAVE, PIR]},
            "last_changed": utc(16, 1),
        },
        MMWAVE: {"state": "on", "last_changed": utc(16, 1)},
        PIR: {"state": "off", "last_changed": pir_last_changed},
        SPEAKER: {"state": "playing"},
        DARK_SENSOR: {"state": "dark" if dark else "bright"},
        ROOM_STATE: {"state": "Occupied (Dark)" if dark else "Occupied (Bright)"},
        FULL: {"state": "off"},
        BULB1: {"state": "on" if signal_applied else "off"},
        SG_LIGHT: {"state": "on" if signal_applied else "off"},
        AL_MAIN: {"state": "off"},
        AL_SG: {"state": "on" if signal_applied else "off"},
    }


class GhostPresenceNeverAppliesSignal(unittest.TestCase):
    def test_dark_ghost_no_lights_no_al_changes(self):
        app = make_app(ghost_states(dark=True))
        app._sync_signal()
        app.turn_on.assert_not_called()
        app.turn_off.assert_not_called()
        app.call_service.assert_not_called()

    def test_bright_ghost_no_full_green(self):
        app = make_app(ghost_states(dark=False))
        app._sync_signal()
        app.turn_on.assert_not_called()
        self.assertFalse(app._dishwasher_full_island_active)


class GhostPresenceHoldsAppliedSignal(unittest.TestCase):
    def test_already_applied_dark_solo_is_left_untouched(self):
        """Presence went suspect after a real person latched the signal: the
        suspect presence still counts for the off-hold, so nothing is cleared
        until the composite actually drops."""
        app = make_app(ghost_states(signal_applied=True))
        app._sync_signal()
        app.turn_off.assert_not_called()
        app.call_service.assert_not_called()


class RealPresenceStillDrivesSignal(unittest.TestCase):
    def test_pir_fired_in_span_applies_dark_solo(self):
        app = make_app(ghost_states(pir_last_changed=utc(16, 30)))
        app._sync_signal()
        turned_on = [c.args[0] for c in app.turn_on.call_args_list]
        self.assertIn(AL_SG, turned_on)
        self.assertIn(SG_LIGHT, turned_on)
        self.assertIn(BULB1, turned_on)


class CompositeOffClearsAsBefore(unittest.TestCase):
    def test_pir_composite_off_clear_path_unchanged(self):
        states = ghost_states(signal_applied=True)
        states[PIR_COMPOSITE]["state"] = "off"
        app = make_app(states)
        app._sync_signal()
        turned_off = [c.args[0] for c in app.turn_off.call_args_list]
        self.assertIn(BULB1, turned_off)
        self.assertIn(SG_LIGHT, turned_off)
        self.assertIn(AL_SG, turned_off)


if __name__ == "__main__":
    unittest.main()
