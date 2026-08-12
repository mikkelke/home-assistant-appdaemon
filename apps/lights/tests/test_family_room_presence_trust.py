"""FamilyRoomLights + presence_trust: ghost kitchen presence is asymmetric.

Encodes the measured 2026-08-09 16:01->18:40Z episode: kitchen mmWave-only
presence (PIR never fired in the span) while the kitchen speaker played, room
dark, nobody sleeping. The app must NEVER turn lights on for that ghost, but
must keep holding lights that are already on (a real person standing still is
never plunged into darkness). Real presence - PIR fired in the span, or any
other family-zone room on - restores normal behavior, and confirmed-bright
turn-off is unaffected by the hold.
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

import family_room_lights as fr  # noqa: E402

KPIR = "binary_sensor.kitchen_pir_presence"
DPIR = "binary_sensor.dining_room_pir_presence"
LPIR = "binary_sensor.living_room_pir_presence"
HPIR = "binary_sensor.hallway_pir_presence"
MMWAVE = "binary_sensor.kitchen_presence_presence"
PIR = "binary_sensor.kitchen_presence_pir_detection"
SPEAKER = "media_player.kitchen_2"
ROOM_STATE = "sensor.room_state_family_room"
DARK = "sensor.darkness_family_room"
ISLAND = "light.island_lights"
COUNTER = "light.kitchen_counter_lights"


def utc(hour, minute=0, second=0):
    return datetime(2026, 8, 9, hour, minute, second, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


def ghost_states(
    lights_on=False,
    pir_last_changed=utc(15, 35),
    speaker="playing",
    darkness="dark",
):
    """2026-08-09 episode: mmWave on since 16:01Z, PIR silent since 15:35Z.
    Timestamps are in the past relative to any real test clock, so the >=10 min
    duration guard is deterministically satisfied; PIR-in-span is span-relative
    and independent of 'now'."""
    label = "Occupied (Dark)" if darkness == "dark" else "Occupied (Bright)"
    light_state = "on" if lights_on else "off"
    return {
        "zone.home": {
            "state": "2",
            "attributes": {"persons": ["person.mikkel", "person.kristine"]},
        },
        "input_boolean.kristine_sleep_mode": {"state": "off"},
        "input_boolean.mikkel_sleep_mode": {"state": "off"},
        DARK: {"state": darkness},
        ROOM_STATE: {"state": label, "attributes": {}},
        KPIR: {
            "state": "on",
            "attributes": {"entity_id": [MMWAVE, PIR]},
            "last_changed": iso(utc(16, 1)),
        },
        MMWAVE: {"state": "on", "last_changed": iso(utc(16, 1))},
        PIR: {"state": "off", "last_changed": iso(pir_last_changed)},
        SPEAKER: {"state": speaker},
        DPIR: {"state": "off"},
        LPIR: {"state": "off"},
        HPIR: {"state": "off"},
        ISLAND: {"state": light_state},
        COUNTER: {"state": light_state},
    }


def make_app(states):
    app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
    app.presence = {
        "kitchen": KPIR,
        "dining_room": DPIR,
        "living_room": LPIR,
        "hallway": HPIR,
    }
    app.raw_pir_sensors = app.presence
    app._family_presence_sensors = dict(app.presence)
    app.doors = {}
    app.adjacent_presence = {}
    app.adjacent_rooms = []
    app.rooftop_door_sensor = None
    app.apartment_entry_door_sensor = None
    app.manual_override_entity = None
    app.manual_override_booleans = {}
    app.sleep_modes = [
        "input_boolean.kristine_sleep_mode",
        "input_boolean.mikkel_sleep_mode",
    ]
    app.room_state_text_entity = ROOM_STATE
    app._darkness_confirmed_sensor = DARK
    app._dishwasher_state_entity = None
    app.light_map = {
        "island": [ISLAND],
        "hallway": [],
        "all": [ISLAND, COUNTER],
    }
    app._door_arrival_latch = False
    app._latch_zone_persons_snapshot = None
    app._sleep_activated_during_presence = False
    app._presence_suspect_after_minutes = None
    app._diag_sensor = None
    app._action_log_count = 0
    app._manual_bright_watch = set()
    app._manual_bright_echo_until = {}
    app._manual_bright_echo_seconds = 8.0
    app.log_level = "normal"
    app.log = lambda *a, **kw: None

    app.states = dict(states)

    def get_state(entity, attribute=None, **kw):
        ent = app.states.get(entity)
        if ent is None:
            return None
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
    app.turn_on = MagicMock()
    app.turn_off = MagicMock()
    return app


def decide(app):
    context = app._gather_lighting_context()
    action = app._determine_lighting_action(context)
    return context, action


class GhostEpisodeNeverTurnsLightsOn(unittest.TestCase):
    def test_ghost_presence_is_detected_as_suspect_only(self):
        app = make_app(ghost_states())
        context, _action = decide(app)
        self.assertTrue(context["family_presence"])
        self.assertTrue(context["presence_suspect_only"])

    def test_lights_off_stay_off_for_the_whole_episode(self):
        app = make_app(ghost_states(lights_on=False))
        context, action = decide(app)
        self.assertEqual(action["action"], "preserve_current_state")
        self.assertEqual(action["reason"], "presence_suspect_hold")
        app._execute_lighting_action(action, context)
        app.turn_on.assert_not_called()
        app.turn_off.assert_not_called()

    def test_lights_already_on_are_held_on(self):
        """The auto-off hold half of the asymmetry: a possibly-real still
        person keeps their light."""
        app = make_app(ghost_states(lights_on=True))
        context, action = decide(app)
        self.assertEqual(action["action"], "preserve_current_state")
        self.assertEqual(action["reason"], "presence_suspect_hold")
        app._execute_lighting_action(action, context)
        app.turn_off.assert_not_called()


class RealPresenceRestoresNormalBehavior(unittest.TestCase):
    def test_pir_fired_in_span_turns_lights_on_as_before(self):
        """PIR off-edge 16:30 > mmWave on-edge 16:01 -> someone really walked
        in during the span; dark + presence must turn everything on."""
        app = make_app(ghost_states(pir_last_changed=utc(16, 30)))
        context, action = decide(app)
        self.assertFalse(context["presence_suspect_only"])
        self.assertEqual(action["action"], "turn_on_all")
        self.assertEqual(action["reason"], "family_presence_dark_no_sleeping")
        app._execute_lighting_action(action, context)
        app.turn_on.assert_called()

    def test_real_presence_in_another_room_overrides_ghost(self):
        states = ghost_states()
        states[DPIR] = {"state": "on"}
        app = make_app(states)
        context, action = decide(app)
        self.assertFalse(context["presence_suspect_only"])
        self.assertEqual(action["action"], "turn_on_all")

    def test_speaker_not_playing_means_presence_is_trusted(self):
        app = make_app(ghost_states(speaker="idle"))
        context, action = decide(app)
        self.assertFalse(context["presence_suspect_only"])
        self.assertEqual(action["action"], "turn_on_all")


class BrightOffUnaffectedByHold(unittest.TestCase):
    def test_confirmed_bright_still_turns_everything_off(self):
        app = make_app(ghost_states(lights_on=True, darkness="bright"))
        context, action = decide(app)
        self.assertEqual(action["action"], "turn_off_all")
        self.assertEqual(action["reason"], "family_presence_bright")


if __name__ == "__main__":
    unittest.main()
