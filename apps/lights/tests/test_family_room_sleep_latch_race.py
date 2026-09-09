"""FamilyRoomLights sleep-mode latch vs. the PIR-triggered evaluation race.

Defect (confirmed from box logs 2026-09-09 21:53:48): _sleep_activated_during_presence was
only ever set inside the listen_state callback for the sleep-mode booleans, so whether it
latched in time depended on callback ORDER relative to the PIR-triggered evaluation -
AppDaemon does not guarantee that order. Measured incident: a dining-room PIR ON triggered
an immediate evaluation at 21:53:48.280; another app flipped input_boolean.kristine_sleep_mode
on at 21:53:48.478; the evaluation's _gather_lighting_context() read the boolean as already
"on" via get_state at 21:53:48.656 and dimmed the room to island-only; the sleep-mode
callback (which would have set the latch) only ran at 21:53:49.248 - 0.968s too late.

Fix under test: _sleep_started_during_presence_session() derives the latch from the sleeping
boolean's last_changed timestamp instead of callback order. If it flipped on at/after the
start of the current presence session (1s tolerance), the latch is set inline in
_gather_lighting_context() before the decision tree runs, regardless of which callback fires
first. A boolean that predates the session (someone already asleep when the room was walked
into) still routes to island-only - unchanged, "walk-in-while-sleeping" behavior.
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

DPIR = "binary_sensor.dining_room_pir_presence"
KRISTINE_SLEEP = "input_boolean.kristine_sleep_mode"
MIKKEL_SLEEP = "input_boolean.mikkel_sleep_mode"
ROOM_STATE = "sensor.room_state_family_room"
DARK = "sensor.darkness_family_room"
ISLAND = "light.island_lights"
COUNTER = "light.kitchen_counter_lights"

# Anchors the incident: the family-room presence session (PIR on) starts here.
SESSION_START = datetime(2026, 9, 9, 21, 53, 48, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


def base_states(kristine_sleep_last_changed, other_lights_on=False):
    """Dark family room, dining-room PIR on, Kristine sleeping, Mikkel home and awake -
    the "some sleeping" branch that racily fell through to island-only in the incident."""
    light_state = "on" if other_lights_on else "off"
    return {
        "zone.home": {
            "state": "2",
            "attributes": {"persons": ["person.kristine", "person.mikkel"]},
        },
        KRISTINE_SLEEP: {"state": "on", "last_changed": iso(kristine_sleep_last_changed)},
        MIKKEL_SLEEP: {"state": "off"},
        DARK: {"state": "dark"},
        ROOM_STATE: {"state": "Occupied (Dark)", "attributes": {}},
        DPIR: {"state": "on"},
        ISLAND: {"state": light_state},
        COUNTER: {"state": light_state},
    }


def make_app(states, presence_session_started_at):
    app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
    app.presence = {"dining_room": DPIR}
    app.raw_pir_sensors = app.presence
    app._family_presence_sensors = dict(app.presence)
    app.doors = {}
    app.adjacent_presence = {}
    app.adjacent_rooms = []
    app.rooftop_door_sensor = None
    app.apartment_entry_door_sensor = None
    app.manual_override_entity = None
    app.manual_override_booleans = {}
    app.sleep_modes = [KRISTINE_SLEEP, MIKKEL_SLEEP]
    app.room_state_text_entity = ROOM_STATE
    app._darkness_confirmed_sensor = DARK
    app._dishwasher_state_entity = None
    app.light_map = {"island": [ISLAND], "hallway": [], "all": [ISLAND, COUNTER]}
    app._door_arrival_latch = False
    app._latch_zone_persons_snapshot = None
    app._presence_suspect_after_minutes = None
    app._sleep_activated_during_presence = False
    app._presence_session_started_at = presence_session_started_at

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
    app.log_calls = []
    app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
    return app


def decide(app):
    context = app._gather_lighting_context()
    action = app._determine_lighting_action(context)
    return context, action


def assert_no_errors(test, app):
    errors = [(lvl, msg) for lvl, msg in app.log_calls if lvl == "ERROR"]
    test.assertEqual(errors, [])


class SleepFlippedDuringPresenceSessionPreservesLighting(unittest.TestCase):
    """(a) Sleep boolean flips on AFTER the presence session started - the incident race.
    The latch must be derived from the timestamp before the decision tree runs, not from
    whichever listen_state callback happens to fire first."""

    def test_sleep_after_session_start_preserves_not_island_only(self):
        session_start = SESSION_START.timestamp()
        states = base_states(kristine_sleep_last_changed=SESSION_START + timedelta(seconds=3))
        app = make_app(states, presence_session_started_at=session_start)

        context, action = decide(app)

        self.assertTrue(context["family_presence"])
        self.assertTrue(context["sleep_status"]["anyone_sleeping"])
        self.assertFalse(context["sleep_status"]["everyone_sleeping"])
        self.assertEqual(action["action"], "preserve_current_state")
        self.assertNotEqual(action["reason"], "family_presence_dark_some_sleeping")
        self.assertTrue(app._sleep_activated_during_presence)
        assert_no_errors(self, app)

    def test_latching_logs_an_info_message(self):
        session_start = SESSION_START.timestamp()
        states = base_states(kristine_sleep_last_changed=SESSION_START + timedelta(seconds=3))
        app = make_app(states, presence_session_started_at=session_start)

        decide(app)

        self.assertTrue(
            any(
                lvl == "INFO" and "Sleep mode turned on during this presence session" in msg
                for lvl, msg in app.log_calls
            )
        )


class SleepPredatingSessionStillGoesIslandOnly(unittest.TestCase):
    """(b) Sleep boolean predates the presence session (already asleep when the room was
    walked into) - walk-in-while-sleeping behavior is unchanged: island-only, latch stays off."""

    def test_sleep_before_session_start_still_island_only(self):
        session_start = SESSION_START.timestamp()
        states = base_states(kristine_sleep_last_changed=SESSION_START - timedelta(seconds=600))
        app = make_app(states, presence_session_started_at=session_start)

        context, action = decide(app)

        self.assertTrue(context["sleep_status"]["anyone_sleeping"])
        self.assertEqual(action["action"], "turn_on_island_only")
        self.assertEqual(action["reason"], "family_presence_dark_some_sleeping")
        self.assertFalse(app._sleep_activated_during_presence)
        assert_no_errors(self, app)


class PresenceLostResetsSessionAndLatch(unittest.TestCase):
    """(c) Losing presence must clear both the latch and the session-start timestamp, so
    the next presence session starts from a clean slate."""

    def _make_minimal_app(self):
        app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
        app._door_arrival_latch = False
        app._latch_zone_persons_snapshot = None
        app._sleep_activated_during_presence = True
        app._presence_session_started_at = SESSION_START.timestamp()
        app.log_calls = []
        app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
        app.evaluations = []
        app._schedule_evaluation = lambda **kw: app.evaluations.append(kw)
        return app

    def test_handle_presence_lost_clears_latch_and_session_start(self):
        app = self._make_minimal_app()

        app._handle_presence_lost()

        self.assertFalse(app._sleep_activated_during_presence)
        self.assertIsNone(app._presence_session_started_at)
        self.assertEqual(len(app.evaluations), 1)
        assert_no_errors(self, app)


class PirOffResetsSessionWhenPresenceFullyLost(unittest.TestCase):
    """The other presence-lost call site (_on_raw_pir_off, all family PIRs off) must clear
    the same two fields."""

    def test_pir_off_with_no_remaining_presence_clears_session(self):
        app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
        app._family_presence_sensors = {"dining_room": DPIR}
        app._door_arrival_latch = False
        app._latch_zone_persons_snapshot = None
        app._sleep_activated_during_presence = True
        app._presence_session_started_at = SESSION_START.timestamp()
        app.states = {DPIR: {"state": "off"}}
        app.get_state = lambda entity, attribute=None, **kw: app.states.get(entity, {}).get("state")
        app.log_calls = []
        app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
        app.evaluations = []
        app._schedule_evaluation = lambda **kw: app.evaluations.append(kw)

        app._on_raw_pir_off(DPIR, "state", "on", "off", {"room": "dining_room"})

        self.assertFalse(app._sleep_activated_during_presence)
        self.assertIsNone(app._presence_session_started_at)
        assert_no_errors(self, app)


if __name__ == "__main__":
    unittest.main()
