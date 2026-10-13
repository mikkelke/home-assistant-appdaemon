"""FamilyRoomLights door-close vacancy hold.

Whoever closes an adjacent room's door from the hallway side reaches the family-room sensors a few
seconds later. Evaluating the instant the door closes sees a vacant room and turns the lights off,
only to turn them straight back on once the person registers. _on_door_close therefore arms a hold
(door_close_hold_seconds); while it runs, the two vacancy turn_off_all returns of the decision tree
- and only those - become preserve_current_state, provided the room is dark, the family zone is
empty and a lamp the app may switch (no zone manual override) is on.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

import yaml

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

DPIR = "binary_sensor.dining_room_active"
ROOM_STATE = "sensor.room_state_family_room"
DARK = "sensor.darkness_family_room"
LIVING = "light.living_room_corner_light"
COUNTER = "light.kitchen_counter_lights"
ISLAND = "light.island_lights"
HALLWAY = "light.hallway_lights"
KRISTINE_SLEEP = "input_boolean.kristine_sleep_mode"
MIKKEL_SLEEP = "input_boolean.mikkel_sleep_mode"
KITCHEN_MANUAL = "input_boolean.kitchen_lights_manual"
LIVING_MANUAL = "input_boolean.living_room_lights_manual"
BEDROOM_DOOR = "binary_sensor.bedroom_door_contact"
BEDROOM_PIR = "binary_sensor.bedroom_pir_presence"
KRISTINE_DOOR = "binary_sensor.kristines_room_door_contact"
KRISTINE_PIR = "binary_sensor.kristines_room_pir_presence"
ROOFTOP = "binary_sensor.rooftop_door_1_contact"
PERSONS = ("person.kristine", "person.mikkel")

T0 = 1000.0
HOLD = 10.0


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


class Scheduler:
    """run_in stand-in: callbacks fire in due-time order as the clock advances."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self._pending = []
        self._seq = 0

    def run_in(self, callback, delay, **kwargs):
        self._seq += 1
        self.calls.append((callback, delay))
        self._pending.append((self.clock.now + delay, self._seq, callback, kwargs))
        return self._seq

    def advance(self, seconds):
        end = self.clock.now + seconds
        while True:
            due = [p for p in self._pending if p[0] <= end]
            if not due:
                break
            item = min(due, key=lambda p: (p[0], p[1]))
            self._pending.remove(item)
            self.clock.now = max(self.clock.now, item[0])
            item[2](item[3])
        self.clock.now = end


def make_states(
    lamps_on=(COUNTER,),
    darkness="dark",
    pending_target=None,
    persons=PERSONS,
    sleeping=False,
    family_pir="off",
    manual_on=(),
    bedroom_door="off",
    bedroom_pir="off",
):
    label = "Occupied (Dark)" if darkness == "dark" else "Occupied (Bright)"
    sleep = "on" if sleeping else "off"
    states = {
        "zone.home": {"state": str(len(persons)), "attributes": {"persons": list(persons)}},
        KRISTINE_SLEEP: {"state": sleep},
        MIKKEL_SLEEP: {"state": sleep},
        DARK: {"state": darkness},
        ROOM_STATE: {
            "state": label,
            "attributes": {"pending_target": pending_target} if pending_target else {},
        },
        DPIR: {"state": family_pir},
        BEDROOM_DOOR: {"state": bedroom_door},
        BEDROOM_PIR: {"state": bedroom_pir},
        KRISTINE_DOOR: {"state": "off"},
        KRISTINE_PIR: {"state": "off"},
        ROOFTOP: {"state": "off"},
        KITCHEN_MANUAL: {"state": "on" if KITCHEN_MANUAL in manual_on else "off"},
        LIVING_MANUAL: {"state": "on" if LIVING_MANUAL in manual_on else "off"},
    }
    for lamp in (LIVING, COUNTER, ISLAND, HALLWAY):
        states[lamp] = {"state": "on" if lamp in lamps_on else "off"}
    return states


def make_app(states, scheduler, hold_seconds=HOLD):
    app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
    app.presence = {"dining_room": DPIR}
    app.raw_pir_sensors = app.presence
    app._family_presence_sensors = dict(app.presence)
    app.doors = {"bedroom": BEDROOM_DOOR, "kristines_room": KRISTINE_DOOR}
    app.adjacent_presence = {"bedroom": BEDROOM_PIR, "kristines_room": KRISTINE_PIR}
    app.adjacent_rooms = ["bedroom", "kristines_room", "claudias_room", "guest_bathroom"]
    app.rooftop_door_sensor = ROOFTOP
    app.apartment_entry_door_sensor = None
    app.manual_override_entity = None
    app.manual_override_booleans = {
        "kitchen": KITCHEN_MANUAL,
        "island": KITCHEN_MANUAL,
        "living": LIVING_MANUAL,
    }
    app.sleep_modes = [KRISTINE_SLEEP, MIKKEL_SLEEP]
    app.room_state_text_entity = ROOM_STATE
    app._darkness_confirmed_sensor = DARK
    app._dishwasher_state_entity = None
    app.light_map = {
        "living": [LIVING],
        "kitchen": [COUNTER],
        "island": [ISLAND],
        "hallway": [HALLWAY],
        "all": [LIVING, COUNTER, ISLAND, HALLWAY],
    }
    app._door_arrival_latch = False
    app._latch_zone_persons_snapshot = None
    app._sleep_activated_during_presence = False
    app._presence_session_started_at = None
    app._presence_suspect_after_minutes = None
    app._diag_sensor = None
    app._action_log_count = 0
    app._manual_bright_watch = set()
    app._manual_bright_echo_until = {}
    app._manual_bright_echo_seconds = 8.0
    app.log_level = "normal"
    app._eval_token = 0
    app._evaluation_timer = None
    app._evaluation_debounce_delay = 0.3
    app._last_evaluation_time = 0.0
    app._min_evaluation_interval = 0.3
    app._door_close_hold_seconds = hold_seconds
    app._door_close_hold_until = 0.0

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
    app.turn_on = MagicMock(side_effect=lambda entity, **kw: app.states[entity].update(state="on"))
    app.turn_off = MagicMock(side_effect=lambda entity, **kw: app.states[entity].update(state="off"))
    app.run_in = scheduler.run_in
    app.log_calls = []
    app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
    return app


def decide(app):
    context = app._gather_lighting_context()
    return context, app._determine_lighting_action(context)


def outcome(action):
    return action["action"], action["reason"]


class HoldTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.scheduler = Scheduler(self.clock)
        real_time = fr.time
        fr.time = types.SimpleNamespace(time=self.clock.time, monotonic=self.clock.monotonic)
        self.addCleanup(setattr, fr, "time", real_time)

    def app(self, hold_seconds=HOLD, **state_kwargs):
        return make_app(make_states(**state_kwargs), self.scheduler, hold_seconds)

    def arm(self, app, remaining=5.0):
        app._door_close_hold_until = self.clock.now + remaining

    def close(self, app, room="bedroom", door=BEDROOM_DOOR):
        app._on_door_close(door, "state", "on", "off", {"doorroom": room})

    def close_rooftop(self, app):
        app._on_door_close(ROOFTOP, "state", "on", "off", {"doorroom": "rooftop", "is_rooftop": True})


class HoldInTheDecisionTree(HoldTestCase):
    def test_default_branch_is_held_when_dark_with_a_free_lamp_on(self):
        app = self.app()
        self.arm(app)

        context, action = decide(app)

        self.assertEqual(outcome(action), ("preserve_current_state", "door_close_hold"))
        self.assertTrue(
            any(lvl == "INFO" and "turn-off held" in msg for lvl, msg in app.log_calls)
        )
        app._execute_lighting_action(action, context)
        app.turn_off.assert_not_called()

    def test_everyone_sleeping_branch_is_held(self):
        app = self.app(sleeping=True)
        self.arm(app)

        context, action = decide(app)

        self.assertTrue(context["sleep_status"]["everyone_sleeping"])
        self.assertEqual(outcome(action), ("preserve_current_state", "door_close_hold"))

    def test_vacancy_turns_off_once_the_deadline_has_passed(self):
        for sleeping, reason in ((False, "no_conditions_met"), (True, "everyone_sleeping_no_motion")):
            for remaining in (0.0, -3.0):
                with self.subTest(sleeping=sleeping, remaining=remaining):
                    app = self.app(sleeping=sleeping)
                    self.arm(app, remaining)
                    _, action = decide(app)
                    self.assertEqual(outcome(action), ("turn_off_all", reason))

    def test_a_hold_that_was_never_armed_changes_nothing(self):
        app = self.app()
        _, action = decide(app)
        self.assertEqual(outcome(action), ("turn_off_all", "no_conditions_met"))

    def test_bright_is_never_held(self):
        for sleeping, reason in ((False, "no_conditions_met"), (True, "everyone_sleeping_no_motion")):
            with self.subTest(sleeping=sleeping):
                app = self.app(darkness="bright", sleeping=sleeping)
                self.arm(app)
                _, action = decide(app)
                self.assertEqual(outcome(action), ("turn_off_all", reason))

    def test_pending_dark_counts_as_dark_like_the_rest_of_the_tree(self):
        app = self.app(darkness="bright", pending_target="dark")
        self.arm(app)

        context, action = decide(app)

        self.assertTrue(context["is_dark_for_auto_on"])
        self.assertEqual(outcome(action), ("preserve_current_state", "door_close_hold"))

    def test_only_manual_owned_lamps_on_is_not_held(self):
        app = self.app(lamps_on=(COUNTER, ISLAND), manual_on=(KITCHEN_MANUAL,))
        self.arm(app)
        _, action = decide(app)
        self.assertEqual(outcome(action), ("turn_off_all", "no_conditions_met"))

    def test_a_manual_owned_lamp_does_not_mask_a_free_lamp(self):
        app = self.app(lamps_on=(COUNTER, LIVING), manual_on=(KITCHEN_MANUAL,))
        self.arm(app)
        _, action = decide(app)
        self.assertEqual(outcome(action), ("preserve_current_state", "door_close_hold"))

    def test_no_lamp_on_is_not_held(self):
        app = self.app(lamps_on=())
        self.arm(app)
        _, action = decide(app)
        self.assertEqual(outcome(action), ("turn_off_all", "no_conditions_met"))

    def test_family_presence_is_not_a_vacancy(self):
        app = self.app(family_pir="on")
        self.arm(app)

        context, action = decide(app)

        self.assertTrue(context["family_presence"])
        self.assertEqual(outcome(action), ("turn_on_all", "family_presence_dark_no_sleeping"))

    def test_helper_refuses_while_the_family_zone_is_occupied(self):
        app = self.app()
        self.arm(app)
        context, _ = decide(app)

        self.assertTrue(app._vacancy_hold_active(context))
        self.assertFalse(app._vacancy_hold_active({**context, "family_presence": True}))

    def test_no_one_home_still_turns_off(self):
        app = self.app(persons=())
        self.arm(app)

        context, action = decide(app)

        self.assertFalse(context["anyone_home"])
        self.assertEqual(outcome(action), ("turn_off_all", "no_one_home"))

    def test_adjacent_door_standby_is_unchanged(self):
        awake = self.app(bedroom_door="on", bedroom_pir="on")
        self.arm(awake)
        _, action = decide(awake)
        self.assertEqual(outcome(action), ("turn_on_island_only", "adjacent_door_dark"))

        asleep = self.app(sleeping=True, bedroom_door="on", bedroom_pir="on")
        self.arm(asleep)
        _, action = decide(asleep)
        self.assertEqual(
            outcome(action), ("turn_on_island_only", "adjacent_activity_everyone_sleeping_dark")
        )


class DoorCloseHandler(HoldTestCase):
    def test_adjacent_close_arms_the_hold_and_schedules_the_expiry_evaluation(self):
        app = self.app()
        app._schedule_evaluation = MagicMock()

        self.close(app)

        self.assertEqual(app._door_close_hold_until, T0 + HOLD)
        self.assertEqual(self.scheduler.calls, [(app._on_door_close_hold_expired, HOLD + 0.2)])
        app._schedule_evaluation.assert_called_once_with()

    def test_rooftop_close_does_not_arm_the_hold(self):
        app = self.app()
        app._schedule_evaluation = MagicMock()

        self.close_rooftop(app)

        self.assertEqual(app._door_close_hold_until, 0.0)
        self.assertEqual(self.scheduler.calls, [])
        app._schedule_evaluation.assert_called_once_with()

    def test_a_later_close_extends_the_deadline(self):
        app = self.app()
        app._schedule_evaluation = MagicMock()
        self.close(app)

        self.clock.now = T0 + 6.0
        self.close(app, "kristines_room", KRISTINE_DOOR)

        self.assertEqual(app._door_close_hold_until, T0 + 6.0 + HOLD)
        self.assertEqual(len(self.scheduler.calls), 2)

    def test_expiry_requests_an_immediate_evaluation(self):
        app = self.app()
        app._schedule_evaluation = MagicMock()

        app._on_door_close_hold_expired({})

        app._schedule_evaluation.assert_called_once_with(immediate=True)


class DoorCloseSequence(HoldTestCase):
    """Door events, sensor edges and timers through the real scheduling and decision tree."""

    def person_registers(self, app):
        app.states[DPIR]["state"] = "on"
        app._on_raw_pir_on(DPIR, "state", "off", "on", {"room": "dining_room"})

    def test_a_person_registering_inside_the_hold_never_sees_the_lights_go_off(self):
        app = self.app()
        self.close(app)
        self.scheduler.advance(3.0)

        self.person_registers(app)
        self.scheduler.advance(HOLD + 1.0)

        app.turn_off.assert_not_called()
        self.assertEqual(app.states[COUNTER]["state"], "on")

    def test_without_a_hold_the_same_sequence_switches_the_lamp_off_and_back_on(self):
        app = self.app(hold_seconds=0.0)
        self.close(app)
        self.scheduler.advance(3.0)

        self.person_registers(app)

        self.assertEqual(app.turn_off.call_args_list, [call(COUNTER)])
        self.assertEqual(app.states[COUNTER]["state"], "on")

    def test_a_vacant_room_turns_off_when_the_hold_lapses(self):
        app = self.app()
        self.close(app)

        self.scheduler.advance(HOLD - 1.0)
        app.turn_off.assert_not_called()

        self.scheduler.advance(2.0)
        self.assertEqual(app.turn_off.call_args_list, [call(COUNTER)])

    def test_an_expiry_made_stale_by_a_later_close_preserves_and_the_last_one_turns_off(self):
        app = self.app()
        self.close(app)
        self.scheduler.advance(6.0)
        self.close(app, "kristines_room", KRISTINE_DOOR)

        self.scheduler.advance(4.5)
        self.assertAlmostEqual(app._last_evaluation_time, T0 + HOLD + 0.2)
        app.turn_off.assert_not_called()

        self.scheduler.advance(6.0)
        self.assertEqual(app.turn_off.call_args_list, [call(COUNTER)])

    def test_rooftop_close_keeps_the_immediate_turn_off(self):
        app = self.app()

        self.close_rooftop(app)
        self.scheduler.advance(1.0)

        self.assertEqual(app.turn_off.call_args_list, [call(COUNTER)])

    def test_zero_seconds_disables_the_hold(self):
        app = self.app(hold_seconds=0.0)

        self.close(app)
        self.scheduler.advance(1.0)

        self.assertEqual(app.turn_off.call_args_list, [call(COUNTER)])


class InitializeWiring(unittest.TestCase):
    @staticmethod
    def yaml_args():
        path = _LIGHTS_DIR / "family_room_lights.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))["FamilyRoomLights"]

    @staticmethod
    def initialize(args):
        app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
        app.args = args
        app.log_calls = []
        app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
        app.get_state = lambda *a, **kw: None
        app.listen_state = MagicMock()
        app.run_in = MagicMock()
        app.initialize()
        return app

    def test_yaml_ships_ten_seconds_and_no_hold_is_armed_at_startup(self):
        args = self.yaml_args()
        self.assertEqual(args["door_close_hold_seconds"], 10)

        app = self.initialize(args)

        self.assertEqual([m for lvl, m in app.log_calls if lvl == "ERROR"], [])
        self.assertEqual(app._door_close_hold_seconds, 10.0)
        self.assertEqual(app._door_close_hold_until, 0.0)

    def test_the_configured_value_is_used(self):
        args = self.yaml_args()
        args["door_close_hold_seconds"] = 7

        app = self.initialize(args)

        self.assertEqual(app._door_close_hold_seconds, 7.0)

    def test_missing_yaml_key_defaults_to_ten_seconds(self):
        args = self.yaml_args()
        del args["door_close_hold_seconds"]

        app = self.initialize(args)

        self.assertEqual(app._door_close_hold_seconds, 10.0)


if __name__ == "__main__":
    unittest.main()
