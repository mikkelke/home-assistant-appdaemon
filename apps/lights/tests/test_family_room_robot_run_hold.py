"""FamilyRoomLights robot run hold.

While nobody is home (zone.home reads exactly 0) and the robot vacuum is cleaning or returning, a
turn-on decision becomes preserve_current_state when the empty house stayed quiet from the last
entry-door close until the run started, that door has not changed since zone.home turned 0, the
untracked visitor sensor reads none, and no family light besides the island is on. Anything else,
unreadable inputs included, behaves as without the hold, and turn-off decisions are never touched.

The replay tests feed recorded state sequences of three real runs through the app's own
registered listeners; in each of them the motion the vacuum itself causes switched the whole room
on.
"""

from __future__ import annotations

import copy
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

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

APPS_DIR = Path(__file__).resolve().parents[2]

ZONE = "zone.home"
VACUUM = "vacuum.rober2"
DOOR = "binary_sensor.apartment_door_open"
KITCHEN = "binary_sensor.kitchen_active"
DINING = "binary_sensor.dining_room_active"
LIVING = "binary_sensor.living_room_active"
HALLWAY = "binary_sensor.hallway_active"
BEDROOM = "binary_sensor.bedroom_active"
BATHROOM = "binary_sensor.bathroom_active"
GUEST_BATHROOM = "binary_sensor.guest_bathroom_active"
CLAUDIAS = "binary_sensor.claudias_room_active"
KRISTINES = "binary_sensor.kristines_room_active"
QUIET = [KITCHEN, DINING, LIVING, HALLWAY, BEDROOM, BATHROOM, GUEST_BATHROOM, CLAUDIAS, KRISTINES]
ROOMS = {KITCHEN: "kitchen", DINING: "dining_room", LIVING: "living_room", HALLWAY: "hallway"}
ROOM_STATE = "sensor.room_state_family_room"
DARK = "sensor.darkness_family_room"
DISHWASHER = "sensor.dishwasher_state"
VISITOR = "sensor.untracked_visitor"
CORNER = "light.living_room_corner_light"
WINDOW = "light.living_room_window_light"
WALL = "light.living_room_wall_lights"
DINING_LIGHT = "light.dining_room_lights"
COUNTER = "light.kitchen_counter_lights"
ISLAND = "light.island_lights"
HALLWAY_LIGHT = "light.hallway_lights"
LIGHTS = [CORNER, WINDOW, WALL, DINING_LIGHT, COUNTER, ISLAND, HALLWAY_LIGHT]
SLEEP_BOOLEANS = [
    "input_boolean.house_night_mode",
    "input_boolean.kristine_sleep_mode",
    "input_boolean.claudia_sleep_mode",
    "input_boolean.mikkel_sleep_mode",
]
MANUAL_BOOLEANS = [
    "input_boolean.kitchen_lights_manual",
    "input_boolean.living_room_lights_manual",
    "input_boolean.dining_room_lights_manual",
    "input_boolean.hallway_lights_manual",
]
BEDROOM_DOOR = "binary_sensor.bedroom_door_contact"
BEDROOM_PIR = "binary_sensor.bedroom_pir_presence"
ROOFTOP = "binary_sensor.rooftop_door_1_contact"
PERSONS = ("person.kristine", "person.mikkel", "person.claudia")

# Moment zone.home turned 0; offsets below are seconds relative to it.
Z0 = datetime(2026, 10, 15, 7, 34, 17, 937000, tzinfo=timezone(timedelta(hours=2)))
Z = Z0.timestamp()
DOOR_CLOSED = -400
RUN_START = 30
TRIP = 1500


def at(seconds):
    return (Z0 + timedelta(seconds=seconds)).isoformat()


def epoch(seconds):
    return (Z0 + timedelta(seconds=seconds)).timestamp()


class FakeClock:
    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


class Scheduler:
    """run_in stand-in: callbacks fire in due-time order as the clock advances."""

    def __init__(self, clock):
        self.clock = clock
        self._pending = []
        self._seq = 0

    def run_in(self, callback, delay, **kwargs):
        self._seq += 1
        due = self.clock.now + max(delay, 0.001)
        self._pending.append((due, self._seq, callback, kwargs))
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
    zone="0",
    zone_changed=0,
    vacuum="cleaning",
    door="off",
    door_changed=DOOR_CLOSED,
    darkness="dark",
    pir="on",
    lamps_on=(),
    quiet_changed=-1200,
    sleeping=(),
    visitor="none",
):
    """Entity store for a dark morning; None drops an entity, as an unknown one reads in HA."""
    persons = list(PERSONS[: int(zone)]) if zone is not None and zone.isdigit() else []
    label = "Occupied (Dark)" if darkness == "dark" else "Occupied (Bright)"
    states = {
        ROOM_STATE: {"state": label, "attributes": {}},
        DARK: {"state": darkness},
        BEDROOM_DOOR: {"state": "off"},
        BEDROOM_PIR: {"state": "off"},
        ROOFTOP: {"state": "off"},
    }
    for sensor in QUIET:
        states[sensor] = {"state": "off", "last_changed": at(quiet_changed)}
    states[LIVING]["state"] = pir
    for boolean in SLEEP_BOOLEANS:
        states[boolean] = {"state": "on" if boolean in sleeping else "off", "last_changed": at(-3600)}
    for boolean in MANUAL_BOOLEANS:
        states[boolean] = {"state": "off"}
    for lamp in LIGHTS:
        states[lamp] = {"state": "on" if lamp in lamps_on else "off"}
    if zone is not None:
        states[ZONE] = {
            "state": zone,
            "attributes": {"persons": persons},
            "last_changed": at(zone_changed) if zone_changed is not None else None,
        }
    if vacuum is not None:
        states[VACUUM] = {"state": vacuum}
    if visitor is not None:
        states[VISITOR] = {"state": visitor}
    if door is not None:
        states[DOOR] = {
            "state": door,
            "last_changed": at(door_changed) if door_changed is not None else None,
        }
    return states


def attach_fake_ha(app, states, scheduler):
    """Give the app an entity store, a recording turn_on/turn_off, run_in and a log."""
    app.states = {entity: dict(entry) for entity, entry in states.items()}
    app.failing = set()

    def get_state(entity, attribute=None, **kw):
        if (entity, attribute) in app.failing:
            raise RuntimeError(f"get_state({entity}, {attribute}) failed")
        entry = app.states.get(entity)
        if entry is None:
            return None
        if attribute is None:
            return entry.get("state")
        if attribute == "last_changed":
            return entry.get("last_changed")
        if attribute == "all":
            return {"attributes": entry.get("attributes", {}), "last_changed": entry.get("last_changed")}
        return entry.get("attributes", {}).get(attribute)

    app.get_state = get_state
    app.turn_on = MagicMock(side_effect=lambda entity, **kw: app.states[entity].update(state="on"))
    app.turn_off = MagicMock(side_effect=lambda entity, **kw: app.states[entity].update(state="off"))
    app.set_state = MagicMock()
    app.run_in = scheduler.run_in
    app.log_calls = []
    app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))


def make_app(states, scheduler, armed=True, hold=True, quiet_sensors=None, watching_since=None):
    app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
    app.presence = {"kitchen": KITCHEN, "dining_room": DINING, "living_room": LIVING, "hallway": HALLWAY}
    app.raw_pir_sensors = app.presence
    app._family_presence_sensors = dict(app.presence)
    app.doors = {"bedroom": BEDROOM_DOOR}
    app.adjacent_presence = {"bedroom": BEDROOM_PIR}
    app.adjacent_rooms = ["bedroom", "kristines_room", "claudias_room", "guest_bathroom"]
    app.rooftop_door_sensor = ROOFTOP
    app.apartment_entry_door_sensor = DOOR
    app.guest_exit_use_adjacent_doors_only = True
    app.guest_exit_require_entry_door_closed = False
    app.manual_override_entity = None
    app.manual_override_booleans = {
        "kitchen": "input_boolean.kitchen_lights_manual",
        "island": "input_boolean.kitchen_lights_manual",
        "living": "input_boolean.living_room_lights_manual",
        "dining": "input_boolean.dining_room_lights_manual",
        "hallway": "input_boolean.hallway_lights_manual",
    }
    app.sleep_modes = list(SLEEP_BOOLEANS)
    app.room_state_text_entity = ROOM_STATE
    app._darkness_confirmed_sensor = DARK
    app._dishwasher_state_entity = None
    app.light_map = {
        "living": [CORNER, WINDOW, WALL],
        "dining": [DINING_LIGHT],
        "kitchen": [COUNTER],
        "island": [ISLAND],
        "hallway": [HALLWAY_LIGHT],
        "all": list(LIGHTS),
    }
    app._door_arrival_latch = False
    app._latch_zone_persons_snapshot = None
    app._sleep_activated_during_presence = False
    app._presence_session_started_at = None
    app._presence_suspect_after_minutes = None
    app._dark_flag = None
    app._last_darkness_state = None
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
    app._door_close_hold_seconds = 10.0
    app._door_close_hold_until = 0.0
    app._robot_vacuum_entity = VACUUM if hold else None
    app._robot_visitor_entity = VISITOR
    app._robot_quiet_sensors = list(QUIET if quiet_sensors is None else quiet_sensors)
    app._robot_quiet_for = Z if armed else None
    app._robot_hold_logged = False
    app._robot_on_edge_at = {}
    app._robot_tracking_since = epoch(DOOR_CLOSED - 3600) if watching_since is None else watching_since
    attach_fake_ha(app, states, scheduler)
    return app


def turned_on(app):
    return [c.args[0] for c in app.turn_on.call_args_list]


def turned_off(app):
    return [c.args[0] for c in app.turn_off.call_args_list]


def hold_lines(app):
    return [m for lvl, m in app.log_calls if lvl == "INFO" and m.startswith("Robot run hold:") and "held back" in m]


def no_hold_lines(app):
    return [m for lvl, m in app.log_calls if lvl == "INFO" and m.startswith("Robot run: no hold")]


class HoldTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(epoch(TRIP + 1))
        self.scheduler = Scheduler(self.clock)
        real_time = fr.time
        fr.time = types.SimpleNamespace(time=self.clock.time, monotonic=self.clock.monotonic)
        self.addCleanup(setattr, fr, "time", real_time)

    def app(self, armed=True, hold=True, **state_kwargs):
        return make_app(make_states(**state_kwargs), self.scheduler, armed=armed, hold=hold)

    def evaluate(self, app, seconds=1.0):
        self.scheduler.advance(seconds)
        app._check_lights({})

    def tick(self, seconds=1.0):
        self.scheduler.advance(seconds)

    def set_state(self, app, entity, state, changed=None):
        entry = app.states.setdefault(entity, {})
        entry["state"] = state
        if changed is not None:
            entry["last_changed"] = at(changed)

    def sensor_turns_on(self, app, sensor, seconds):
        """A quiet-house sensor turns on, as AppDaemon reports it to the registered listener."""
        self.set_state(app, sensor, "on", changed=seconds)
        app._on_quiet_sensor_on(sensor, None, "off", "on", {})

    def vacuum_event(self, app, old, new):
        self.set_state(app, VACUUM, new)
        app._on_robot_vacuum_state(VACUUM, None, old, new, {})


class HoldsTheTurnOn(HoldTestCase):
    def test_cleaning_in_a_quiet_empty_house_keeps_the_room_dark(self):
        app = self.app()

        self.evaluate(app)

        self.assertEqual(turned_on(app), [])
        self.assertEqual(turned_off(app), [])
        self.assertEqual(len(hold_lines(app)), 1)
        self.assertIn("turn on all held back", hold_lines(app)[0])
        self.assertIn(VACUUM, hold_lines(app)[0])

    def test_returning_to_the_dock_is_held_too(self):
        app = self.app(vacuum="returning")
        self.evaluate(app)
        self.assertEqual(turned_on(app), [])
        self.assertEqual(len(hold_lines(app)), 1)

    def test_the_decision_is_preserve_current_state_with_its_own_reason(self):
        app = self.app()
        context = app._gather_lighting_context()
        action = app._hold_turn_on_during_robot_run(app._determine_lighting_action(context), context)
        self.assertEqual((action["action"], action["reason"]), ("preserve_current_state", "robot_run_nobody_home"))

    def test_island_standby_is_held_like_any_other_turn_on(self):
        app = self.app()
        context = app._gather_lighting_context()
        standby = {"action": "turn_on_island_only", "reason": "adjacent_door_dark", "details": ""}

        action = app._hold_turn_on_during_robot_run(standby, context)

        self.assertEqual((action["action"], action["reason"]), ("preserve_current_state", "robot_run_nobody_home"))
        self.assertIn("turn on island only held back", hold_lines(app)[0])

    def test_decisions_that_are_not_a_turn_on_pass_through_untouched(self):
        app = self.app()
        context = app._gather_lighting_context()
        for name in ("turn_off_all", "preserve_current_state", "preserve_living_room_only"):
            decision = {"action": name, "reason": "x", "details": "y"}
            self.assertIs(app._hold_turn_on_during_robot_run(decision, context), decision)
        self.assertEqual(hold_lines(app), [])

    def test_many_evaluations_log_one_line_until_the_hold_lapses(self):
        app = self.app()
        for _ in range(4):
            self.evaluate(app)
        self.assertEqual(len(hold_lines(app)), 1)

        self.set_state(app, LIVING, "off", TRIP + 60)
        self.evaluate(app)
        self.assertEqual(len(hold_lines(app)), 1)
        self.set_state(app, LIVING, "on", TRIP + 90)
        self.evaluate(app)
        self.evaluate(app)

        self.assertEqual(len(hold_lines(app)), 2)
        self.assertEqual(turned_on(app), [])

    def test_a_run_without_a_door_edge_stays_suppressed_through_every_motion_cycle(self):
        app = self.app()
        for cycle in range(5):
            self.tick()
            self.set_state(app, LIVING, "on", TRIP + cycle * 120)
            app._on_raw_pir_on(LIVING, None, "off", "on", {"room": "living_room"})
            self.tick()
            self.set_state(app, LIVING, "off", TRIP + cycle * 120 + 60)
            app._on_raw_pir_off(LIVING, None, "on", "off", {"room": "living_room"})
            self.tick()

        self.assertEqual(turned_on(app), [])


class ActsAsBeforeWhenNotAllHold(HoldTestCase):
    def assertTurnsOnAsToday(self, app, failed_check=False):
        self.evaluate(app)
        self.assertEqual(turned_on(app), [CORNER, WINDOW, WALL, DINING_LIGHT, COUNTER, ISLAND, HALLWAY_LIGHT])
        self.assertEqual(hold_lines(app), [])
        warned = [m for lvl, m in app.log_calls if lvl == "WARNING" and "hold check failed" in m]
        self.assertEqual(bool(warned), failed_check)

    def test_hold_not_configured(self):
        self.assertTurnsOnAsToday(self.app(hold=False))

    def test_vacuum_states_that_are_not_a_run(self):
        for state in ("docked", "idle", "paused", "error", "unavailable", "unknown", "", None):
            with self.subTest(vacuum=state):
                self.assertTurnsOnAsToday(self.app(vacuum=state))

    def test_zone_home_not_exactly_zero(self):
        for zone in ("1", "2", "3", "unavailable", "unknown", None):
            with self.subTest(zone=zone):
                self.assertTurnsOnAsToday(self.app(zone=zone))

    def test_zone_home_unreadable(self):
        app = self.app()
        app.failing.add((ZONE, None))
        self.assertTurnsOnAsToday(app)

    def test_entry_door_state_not_closed_or_unknown(self):
        for door in ("on", "unavailable", "unknown", None):
            with self.subTest(door=door):
                self.assertTurnsOnAsToday(self.app(door=door))

    def test_entry_door_not_configured(self):
        app = self.app()
        app.apartment_entry_door_sensor = None
        self.assertTurnsOnAsToday(app)

    def test_untracked_visitor_sensor_reading_anything_but_none(self):
        for visitor in ("cleaner", "guest", "unavailable", "unknown", "", None):
            with self.subTest(visitor=visitor):
                self.assertTurnsOnAsToday(self.app(visitor=visitor))

    def test_untracked_visitor_sensor_not_configured(self):
        app = self.app()
        app._robot_visitor_entity = None
        self.assertTurnsOnAsToday(app)

    def test_a_visitor_arriving_during_the_run_ends_the_hold_while_the_visit_lasts(self):
        app = self.app()
        self.evaluate(app)
        self.assertEqual(turned_on(app), [])

        self.set_state(app, VISITOR, "guest")
        self.evaluate(app)
        self.assertEqual(len(turned_on(app)), 7)

        for lamp in LIGHTS:
            self.set_state(app, lamp, "off")
        app.turn_on.reset_mock()
        self.set_state(app, VISITOR, "none")
        self.evaluate(app)
        self.assertEqual(turned_on(app), [])

    def test_entry_door_changed_after_the_house_emptied(self):
        for changed in (1, RUN_START, TRIP - 300):
            with self.subTest(door_changed=changed):
                self.assertTurnsOnAsToday(self.app(door_changed=changed))

    def test_entry_door_timestamp_unreadable(self):
        for changed in (None, "not a time"):
            with self.subTest(door_changed=changed):
                app = self.app()
                app.states[DOOR]["last_changed"] = changed
                self.assertTurnsOnAsToday(app)

    def test_no_verdict_for_this_run(self):
        self.assertTurnsOnAsToday(self.app(armed=False))

    def test_verdict_belongs_to_an_earlier_empty_house_episode(self):
        self.assertTurnsOnAsToday(self.app(zone_changed=3600, door_changed=3500))

    def test_zone_home_timestamp_unreadable(self):
        self.assertTurnsOnAsToday(self.app(zone_changed=None))
        self.assertTurnsOnAsToday(self.app(zone_changed=None, armed=False))

    def test_a_check_that_raises_leaves_the_decision_alone(self):
        app = self.app()
        app.failing.add((VACUUM, None))
        self.assertTurnsOnAsToday(app)

        app = self.app()
        app.failing.add((DOOR, "last_changed"))
        self.assertTurnsOnAsToday(app, failed_check=True)

    def test_a_light_already_on_outside_the_island_keeps_todays_behaviour(self):
        app = self.app(lamps_on=(CORNER,))
        self.evaluate(app)
        self.assertEqual(turned_on(app), [WINDOW, WALL, DINING_LIGHT, COUNTER, ISLAND, HALLWAY_LIGHT])
        self.assertEqual(hold_lines(app), [])

    def test_an_island_lit_by_the_dishwasher_signal_does_not_open_the_hold(self):
        app = self.app(lamps_on=(ISLAND,))
        self.evaluate(app)
        self.assertEqual(turned_on(app), [])
        self.assertEqual(len(hold_lines(app)), 1)

    def test_turn_off_decisions_are_never_held(self):
        vacant = self.app(pir="off", lamps_on=(COUNTER, ISLAND))
        self.evaluate(vacant)
        self.assertEqual(turned_off(vacant), [COUNTER, ISLAND])

        bright = self.app(darkness="bright", lamps_on=(CORNER,))
        self.evaluate(bright)
        self.assertEqual(turned_off(bright), [CORNER])
        self.assertEqual(hold_lines(vacant) + hold_lines(bright), [])


class NormalPathIsUntouched(HoldTestCase):
    def outcome(self, **kwargs):
        app = self.app(**kwargs)
        self.evaluate(app)
        return turned_on(app), turned_off(app), dict(app.states[CORNER]), hold_lines(app)

    def test_with_someone_home_the_hold_changes_nothing(self):
        for zone in ("1", "2", "3"):
            for vacuum in ("cleaning", "returning", "docked", None):
                for darkness in ("dark", "bright"):
                    for pir in ("on", "off"):
                        for lamps in ((), (CORNER,), (ISLAND,)):
                            for sleeping in ((), ("input_boolean.kristine_sleep_mode",)):
                                kwargs = dict(
                                    zone=zone,
                                    vacuum=vacuum,
                                    darkness=darkness,
                                    pir=pir,
                                    lamps_on=lamps,
                                    sleeping=sleeping,
                                )
                                with self.subTest(**kwargs):
                                    held = self.outcome(hold=True, armed=True, **kwargs)
                                    plain = self.outcome(hold=False, armed=False, **kwargs)
                                    self.assertEqual(held, plain)
                                    self.assertEqual(held[3], [])

    def test_with_nobody_home_and_no_run_the_hold_changes_nothing(self):
        for vacuum in ("docked", "idle", "paused", "error", "unavailable", None):
            for darkness in ("dark", "bright"):
                for pir in ("on", "off"):
                    for lamps in ((), (CORNER,), (COUNTER, ISLAND)):
                        kwargs = dict(vacuum=vacuum, darkness=darkness, pir=pir, lamps_on=lamps)
                        with self.subTest(**kwargs):
                            self.assertEqual(
                                self.outcome(hold=True, armed=True, **kwargs),
                                self.outcome(hold=False, armed=False, **kwargs),
                            )

    def test_someone_home_never_reads_the_vacuum_the_visitor_or_the_door(self):
        app = self.app(zone="2")
        app.failing.update({(VACUUM, None), (VISITOR, None), (DOOR, None), (DOOR, "last_changed")})
        self.evaluate(app)
        self.assertEqual(len(turned_on(app)), 7)
        self.assertEqual([m for lvl, m in app.log_calls if lvl == "WARNING"], [])


def shipped_config():
    path = APPS_DIR / "lights" / "family_room_lights.yaml"
    return yaml.safe_load(path.read_text())["FamilyRoomLights"]


class ShippedConfig(unittest.TestCase):
    def test_yaml_names_the_vacuum_the_door_and_every_room_active_sensor(self):
        config = shipped_config()
        zones = yaml.safe_load((APPS_DIR / "presence" / "room_active.yaml").read_text())["RoomActive"]["zones"]

        visitor = yaml.safe_load((APPS_DIR / "presence" / "untracked_visitor.yaml").read_text())["UntrackedVisitor"]

        self.assertEqual(config["robot_vacuum_entity"], VACUUM)
        self.assertEqual(config["untracked_visitor_entity"], visitor["entity"])
        self.assertEqual(visitor["entity"], VISITOR)
        self.assertEqual(config["apartment_entry_door_sensor"], DOOR)
        self.assertEqual(
            sorted(config["robot_quiet_sensors"]),
            sorted(f"binary_sensor.{zone}_active" for zone in zones),
        )
        self.assertEqual(fr.ROBOT_RUN_STATES, ("cleaning", "returning"))


class ShippedConfigInitialises(unittest.TestCase):
    def initialised(self, config, listen_state=None):
        app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
        app.args = config
        app.log_calls = []
        app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
        app.get_state = lambda entity, attribute=None, **kw: "off"
        app.listen_state = listen_state or MagicMock()
        app.run_in = MagicMock()
        app.initialize()
        return app

    def test_initialize_watches_the_vacuum_and_turn_on_edges_of_every_quiet_sensor(self):
        app = self.initialised(copy.deepcopy(shipped_config()))

        self.assertEqual([m for lvl, m in app.log_calls if lvl == "ERROR"], [])
        registered = [(c.args[0].__name__, c.args[1], c.kwargs) for c in app.listen_state.call_args_list]
        self.assertIn(("_on_robot_vacuum_state", VACUUM, {}), registered)
        for sensor in QUIET:
            self.assertIn(("_on_quiet_sensor_on", sensor, {"new": "on"}), registered)
        self.assertEqual(sorted(app._robot_quiet_sensors), sorted(QUIET))
        self.assertEqual(app._robot_visitor_entity, VISITOR)
        self.assertIsNone(app._robot_quiet_for)
        self.assertEqual(app._robot_on_edge_at, {})
        self.assertIsInstance(app._robot_tracking_since, float)

    def test_without_the_vacuum_key_nothing_is_registered_and_the_hold_stays_off(self):
        config = copy.deepcopy(shipped_config())
        del config["robot_vacuum_entity"]

        app = self.initialised(config)

        registered = [c.args[0].__name__ for c in app.listen_state.call_args_list]
        self.assertNotIn("_on_robot_vacuum_state", registered)
        self.assertNotIn("_on_quiet_sensor_on", registered)
        self.assertIsNone(app._robot_vacuum_entity)

    def test_a_listener_that_cannot_be_set_up_switches_the_hold_off(self):
        def listen_state(callback, entity, **kw):
            if callback.__name__ == "_on_quiet_sensor_on":
                raise RuntimeError("cannot register")

        app = self.initialised(copy.deepcopy(shipped_config()), listen_state=listen_state)

        self.assertIsNone(app._robot_vacuum_entity)
        self.assertTrue(any(lvl == "ERROR" and "listener setup failed" in msg for lvl, msg in app.log_calls))


class QuietHouseVerdict(HoldTestCase):
    """Taken when the vacuum leaves the dock: nothing may have turned on since the entry door closed."""

    def start_run(self, **state_kwargs):
        app = self.app(armed=False, vacuum="docked", **state_kwargs)
        self.vacuum_event(app, "docked", "cleaning")
        return app

    def test_a_house_that_stayed_quiet_since_the_door_closed_arms_the_hold(self):
        app = self.start_run()
        self.assertEqual(app._robot_quiet_for, Z)
        self.assertEqual(len(no_hold_lines(app)), 0)
        self.evaluate(app)
        self.assertEqual(turned_on(app), [])

    def test_guest_active_five_minutes_after_the_door_closed_lifts_the_hold_for_the_run(self):
        for sensor in QUIET:
            with self.subTest(sensor=sensor):
                app = self.app(armed=False, vacuum="docked", pir="off")
                self.sensor_turns_on(app, sensor, DOOR_CLOSED + 300)
                self.assertLess(DOOR_CLOSED + 300, 0, "still before zone.home turned 0")
                self.vacuum_event(app, "docked", "cleaning")
                self.set_state(app, LIVING, "on", changed=TRIP)

                self.assertIsNone(app._robot_quiet_for)
                self.assertIn(sensor, no_hold_lines(app)[0])
                self.evaluate(app)
                self.assertEqual(len(turned_on(app)), 7)
                self.assertEqual(hold_lines(app), [])

    def test_activity_at_any_moment_from_the_door_close_up_to_the_run_start_counts(self):
        for seconds in (DOOR_CLOSED, DOOR_CLOSED + 1, -200, -1, 0, 1, RUN_START - 1):
            with self.subTest(seconds=seconds):
                app = self.app(armed=False, vacuum="docked")
                self.sensor_turns_on(app, BEDROOM, seconds)
                self.vacuum_event(app, "docked", "cleaning")
                self.assertIsNone(app._robot_quiet_for)

    def test_a_resident_leaving_before_the_door_closed_is_not_activity(self):
        app = self.app(armed=False, vacuum="docked")
        self.sensor_turns_on(app, HALLWAY, DOOR_CLOSED - 1)
        self.sensor_turns_on(app, KITCHEN, DOOR_CLOSED - 60)
        self.vacuum_event(app, "docked", "cleaning")
        self.assertEqual(app._robot_quiet_for, Z)

    def test_a_radar_ghost_that_stays_on_through_the_departure_is_not_activity(self):
        app = self.app(armed=False, vacuum="docked")
        self.set_state(app, DINING, "on", changed=DOOR_CLOSED - 600)
        self.vacuum_event(app, "docked", "cleaning")
        self.assertEqual(app._robot_quiet_for, Z)

    def test_sensors_fading_out_after_the_door_closed_are_not_activity(self):
        app = self.app(armed=False, vacuum="docked")
        for offset, sensor in ((12, HALLWAY), (80, LIVING), (200, DINING)):
            self.set_state(app, sensor, "off", changed=DOOR_CLOSED + offset)
        self.vacuum_event(app, "docked", "cleaning")
        self.assertEqual(app._robot_quiet_for, Z)

    def test_the_vacuum_moving_around_after_the_start_does_not_disarm(self):
        app = self.start_run()
        for sensor in QUIET:
            self.sensor_turns_on(app, sensor, RUN_START + 40)
        self.evaluate(app)
        self.assertEqual(turned_on(app), [])
        self.assertEqual(app._robot_quiet_for, Z)

    def test_sensors_not_watched_since_the_door_closed_never_arm(self):
        for watching_since in (epoch(DOOR_CLOSED + 1), epoch(-10)):
            with self.subTest(watching_since=watching_since):
                app = make_app(
                    make_states(vacuum="docked"), self.scheduler, armed=False, watching_since=watching_since
                )
                self.vacuum_event(app, "docked", "cleaning")
                self.assertIsNone(app._robot_quiet_for)
                self.assertIn("not watched", no_hold_lines(app)[0])

        app = make_app(make_states(vacuum="docked"), self.scheduler, armed=False, watching_since=epoch(DOOR_CLOSED))
        self.vacuum_event(app, "docked", "cleaning")
        self.assertEqual(app._robot_quiet_for, Z)

    def test_unknown_entry_door_history_never_arms(self):
        cases = (
            dict(door="on"),
            dict(door="unavailable"),
            dict(door="unknown"),
            dict(door=None),
            dict(door_changed=None),
            dict(door_changed=5),
            dict(zone_changed=None),
        )
        for kwargs in cases:
            with self.subTest(**kwargs):
                app = self.app(armed=False, vacuum="docked", **kwargs)
                self.vacuum_event(app, "docked", "cleaning")
                self.assertIsNone(app._robot_quiet_for)
                self.assertEqual(len(no_hold_lines(app)), 1)

        app = self.app(armed=False, vacuum="docked")
        app.states[DOOR]["last_changed"] = "not a time"
        self.vacuum_event(app, "docked", "cleaning")
        self.assertIsNone(app._robot_quiet_for)

        app = self.app(armed=False, vacuum="docked")
        app.apartment_entry_door_sensor = None
        self.vacuum_event(app, "docked", "cleaning")
        self.assertIsNone(app._robot_quiet_for)

    def test_a_visitor_in_the_flat_when_the_vacuum_starts_keeps_the_hold_off_for_the_whole_run(self):
        for visitor in ("cleaner", "guest", "unavailable", "unknown", None):
            with self.subTest(visitor=visitor):
                app = self.app(armed=False, vacuum="docked", visitor=visitor)
                self.vacuum_event(app, "docked", "cleaning")
                self.assertIsNone(app._robot_quiet_for)
                self.assertIn(VISITOR, no_hold_lines(app)[0])

                self.set_state(app, VISITOR, "none")
                self.evaluate(app)
                self.assertEqual(len(turned_on(app)), 7)
                self.assertEqual(hold_lines(app), [])

    def test_no_visitor_sensor_to_ask_never_arms(self):
        app = self.app(armed=False, vacuum="docked")
        app._robot_visitor_entity = None
        self.vacuum_event(app, "docked", "cleaning")
        self.assertIsNone(app._robot_quiet_for)

    def test_no_sensors_to_watch_never_arms(self):
        app = make_app(make_states(vacuum="docked"), self.scheduler, armed=False, quiet_sensors=[])
        self.vacuum_event(app, "docked", "cleaning")
        self.assertIsNone(app._robot_quiet_for)

    def test_a_run_that_starts_with_someone_home_never_arms(self):
        for zone in ("1", "unavailable", None):
            with self.subTest(zone=zone):
                app = self.app(armed=False, vacuum="docked", zone=zone)
                self.vacuum_event(app, "docked", "cleaning")
                self.assertIsNone(app._robot_quiet_for)
                self.assertIn("not empty", no_hold_lines(app)[0])

    def test_blips_between_cleaning_and_returning_keep_the_verdict_and_docking_ends_it(self):
        app = self.start_run()
        for old, new in (("cleaning", "returning"), ("returning", "cleaning"), ("cleaning", "returning")):
            self.vacuum_event(app, old, new)
            self.assertEqual(app._robot_quiet_for, Z)
        self.vacuum_event(app, "returning", "docked")
        self.assertIsNone(app._robot_quiet_for)

    def test_stale_verdict_from_an_earlier_empty_house_does_not_hold(self):
        app = self.start_run()
        self.set_state(app, ZONE, "0", changed=3600)
        self.set_state(app, DOOR, "off", changed=3500)
        self.evaluate(app)
        self.assertEqual(len(turned_on(app)), 7)

    def test_a_restart_forgets_the_verdict_and_the_lights_react_as_before(self):
        app = self.app(armed=False)
        self.evaluate(app)
        self.assertEqual(len(turned_on(app)), 7)

    def test_unreadable_zone_home_at_a_later_run_start_clears_the_verdict(self):
        app = self.start_run()
        app.failing.add((ZONE, None))
        self.vacuum_event(app, "docked", "cleaning")
        self.assertIsNone(app._robot_quiet_for)

    def test_an_error_while_judging_clears_the_verdict(self):
        app = self.app(armed=False, vacuum="docked")
        app.failing.add((DOOR, "last_changed"))
        self.vacuum_event(app, "docked", "cleaning")
        self.assertIsNone(app._robot_quiet_for)
        self.assertTrue(any(lvl == "ERROR" and "robot vacuum handler" in msg for lvl, msg in app.log_calls))

    def test_the_listener_keeps_the_latest_turn_on_per_sensor(self):
        app = self.app(armed=False)
        self.sensor_turns_on(app, KITCHEN, -50)
        self.sensor_turns_on(app, KITCHEN, -80)
        self.sensor_turns_on(app, DINING, -10)
        self.assertEqual(app._robot_on_edge_at, {KITCHEN: epoch(-50), DINING: epoch(-10)})

    def test_the_listener_uses_the_wall_clock_when_the_sensor_time_is_unreadable(self):
        app = self.app(armed=False)
        app.failing.add((KITCHEN, "last_changed"))
        app._on_quiet_sensor_on(KITCHEN, None, "off", "on", {})
        app.states[DINING]["last_changed"] = None
        app._on_quiet_sensor_on(DINING, None, "off", "on", {})
        self.assertEqual(app._robot_on_edge_at, {KITCHEN: self.clock.now, DINING: self.clock.now})


class CleanerAndGuests(HoldTestCase):
    """The hold ends when the entry door opens and re-arms only for a new empty-house episode."""

    def open_door(self, app, seconds):
        self.tick()
        self.set_state(app, DOOR, "on", changed=seconds)
        app._on_apartment_door_open_edge(DOOR, None, "off", "on", {})
        self.tick()

    def close_door(self, app, seconds):
        self.set_state(app, DOOR, "off", changed=seconds)

    def motion(self, app, sensor, state, seconds):
        """A family-room sensor changes, reaching every listener AppDaemon has for it."""
        self.tick()
        self.set_state(app, sensor, state, seconds)
        room = {"room": ROOMS[sensor]}
        if state == "on":
            app._on_quiet_sensor_on(sensor, None, "off", "on", {})
            app._on_raw_pir_on(sensor, None, "off", "on", room)
        else:
            app._on_raw_pir_off(sensor, None, "on", "off", room)
        self.tick()

    def test_door_opens_mid_run_and_the_next_motion_turns_the_lights_on(self):
        app = self.app(pir="off")
        self.motion(app, LIVING, "on", TRIP)
        self.assertEqual(turned_on(app), [])

        self.open_door(app, TRIP + 20)
        self.assertEqual(len(turned_on(app)), 7)

        self.close_door(app, TRIP + 40)
        self.motion(app, LIVING, "off", TRIP + 100)
        self.assertEqual(len(turned_off(app)), 7)
        app.turn_on.reset_mock()

        self.motion(app, LIVING, "on", TRIP + 200)
        self.assertEqual(len(turned_on(app)), 7)

    def test_door_opens_before_any_motion_and_the_first_motion_turns_the_lights_on(self):
        app = self.app(pir="off")
        self.open_door(app, TRIP - 60)
        self.close_door(app, TRIP - 20)
        self.assertEqual(turned_on(app), [])

        self.motion(app, LIVING, "on", TRIP)

        self.assertEqual(len(turned_on(app)), 7)
        self.assertEqual(hold_lines(app), [])

    def test_a_new_empty_house_episode_rearms_the_hold(self):
        app = self.app(pir="off")
        self.open_door(app, TRIP + 20)
        self.close_door(app, TRIP + 40)
        self.motion(app, LIVING, "on", TRIP + 60)
        self.assertEqual(len(turned_on(app)), 7)
        self.motion(app, LIVING, "off", TRIP + 600)

        # a resident arrives and leaves again; zone.home turns 0 after the last door close
        self.open_door(app, TRIP + 3100)
        self.close_door(app, TRIP + 3130)
        self.open_door(app, TRIP + 3250)
        self.close_door(app, TRIP + 3280)
        self.set_state(app, ZONE, "0", changed=TRIP + 3300)
        self.vacuum_event(app, "cleaning", "docked")
        for lamp in LIGHTS:
            self.set_state(app, lamp, "off")
        app.turn_on.reset_mock()
        self.vacuum_event(app, "docked", "cleaning")
        self.assertEqual(app._robot_quiet_for, epoch(TRIP + 3300))

        self.motion(app, LIVING, "on", TRIP + 4800)

        self.assertEqual(turned_on(app), [])
        self.assertEqual(len(hold_lines(app)), 1)

    def test_a_second_run_in_the_same_episode_after_the_door_moved_stays_off(self):
        app = self.app()
        self.open_door(app, TRIP + 20)
        self.close_door(app, TRIP + 40)
        self.vacuum_event(app, "cleaning", "docked")
        for lamp in LIGHTS:
            self.set_state(app, lamp, "off")
        app.turn_on.reset_mock()

        self.vacuum_event(app, "docked", "cleaning")
        self.assertIsNone(app._robot_quiet_for)
        self.evaluate(app)

        self.assertEqual(len(turned_on(app)), 7)


# Recorded runs: state sequences from the HA recorder, local time (+02:00). Each starts a second
# before the entry door closed behind the last resident and ends right after the vacuum tripped
# the family room. A recording ends before anything restarted: a restart gives every entity a new
# last_changed.
RECORDED_RUNS = {
    "2026-10-15 07:59": {
        "lamps_on": True,
        "start": "2026-10-15T07:30:30.000+02:00",
        "run_start": "2026-10-15T07:34:49.874+02:00",
        "recorded_turn_on": "2026-10-15T07:59",
        "initial": {
            DOOR: ("on", "2026-10-15T07:29:54.067+02:00"),
            ZONE: ("1", "2026-10-15T06:21:16.340+02:00"),
            VACUUM: ("docked", "2026-10-14T16:09:27.707+02:00"),
            KITCHEN: ("on", "2026-10-15T07:21:56.901+02:00"),
            DINING: ("on", "2026-10-15T07:25:59.236+02:00"),
            LIVING: ("on", "2026-10-15T07:26:14.678+02:00"),
            HALLWAY: ("on", "2026-10-15T07:29:52.699+02:00"),
            BEDROOM: ("off", "2026-10-14T23:53:45.655+02:00"),
            BATHROOM: ("off", "2026-10-14T17:52:16.781+02:00"),
            GUEST_BATHROOM: ("on", "2026-10-15T07:26:34.825+02:00"),
            CLAUDIAS: ("off", "2026-10-15T07:28:24.571+02:00"),
            KRISTINES: ("off", "2026-10-15T06:17:05.109+02:00"),
            DARK: ("dark", "2026-10-14T17:39:30.937+02:00"),
            ROOM_STATE: ("Occupied (Dark)", "2026-10-15T07:21:46.897+02:00"),
            DISHWASHER: ("Off", "2026-10-14T16:09:43.976+02:00"),
        },
        "events": [
            ("2026-10-15T07:30:31.139+02:00", DOOR, "off"),
            ("2026-10-15T07:30:42.836+02:00", HALLWAY, "off"),
            ("2026-10-15T07:30:43.584+02:00", GUEST_BATHROOM, "off"),
            ("2026-10-15T07:32:10.249+02:00", ROOM_STATE, "Empty (Dark)"),
            ("2026-10-15T07:32:12.436+02:00", LIVING, "off"),
            ("2026-10-15T07:32:20.307+02:00", KITCHEN, "off"),
            ("2026-10-15T07:32:27.452+02:00", DINING, "off"),
            ("2026-10-15T07:34:17.937+02:00", ZONE, "0"),
            ("2026-10-15T07:34:49.874+02:00", VACUUM, "cleaning"),
            ("2026-10-15T07:38:50.680+02:00", VACUUM, "returning"),
            ("2026-10-15T07:38:51.867+02:00", VACUUM, "cleaning"),
            ("2026-10-15T07:41:03.450+02:00", BEDROOM, "on"),
            ("2026-10-15T07:41:38.444+02:00", BEDROOM, "off"),
            ("2026-10-15T07:44:07.294+02:00", VACUUM, "returning"),
            ("2026-10-15T07:44:08.491+02:00", VACUUM, "cleaning"),
            ("2026-10-15T07:52:47.512+02:00", VACUUM, "returning"),
            ("2026-10-15T07:52:51.348+02:00", VACUUM, "cleaning"),
            ("2026-10-15T07:59:08.872+02:00", LIVING, "on"),
            ("2026-10-15T07:59:08.951+02:00", ROOM_STATE, "Occupied (Dark)"),
            ("2026-10-15T08:03:28.646+02:00", ROOM_STATE, "Empty (Dark)"),
            ("2026-10-15T08:03:43.627+02:00", LIVING, "off"),
        ],
    },
    "2026-10-16 09:16": {
        "lamps_on": True,
        "start": "2026-10-16T08:46:49.000+02:00",
        "run_start": "2026-10-16T08:51:07.032+02:00",
        "recorded_turn_on": "2026-10-16T09:16",
        "initial": {
            DOOR: ("on", "2026-10-16T08:46:00.193+02:00"),
            ZONE: ("1", "2026-10-16T07:52:09.193+02:00"),
            VACUUM: ("docked", "2026-10-15T23:32:47.439+02:00"),
            KITCHEN: ("on", "2026-10-16T08:43:15.533+02:00"),
            DINING: ("on", "2026-10-16T08:44:34.894+02:00"),
            LIVING: ("off", "2026-10-16T07:45:02.671+02:00"),
            HALLWAY: ("on", "2026-10-16T08:44:43.491+02:00"),
            BEDROOM: ("on", "2026-10-16T08:11:44.993+02:00"),
            BATHROOM: ("on", "2026-10-16T08:22:13.557+02:00"),
            GUEST_BATHROOM: ("off", "2026-10-16T07:48:14.617+02:00"),
            CLAUDIAS: ("off", "2026-10-16T06:18:17.834+02:00"),
            KRISTINES: ("off", "2026-10-16T07:46:28.251+02:00"),
            DARK: ("dark", "2026-10-15T23:33:28.560+02:00"),
            ROOM_STATE: ("Occupied (Dark)", "2026-10-16T08:43:08.150+02:00"),
            DISHWASHER: ("Unemptied", "2026-10-16T05:29:18.175+02:00"),
        },
        "events": [
            ("2026-10-16T08:46:49.310+02:00", BEDROOM, "off"),
            ("2026-10-16T08:46:50.167+02:00", DOOR, "off"),
            ("2026-10-16T08:47:04.043+02:00", HALLWAY, "off"),
            ("2026-10-16T08:48:02.990+02:00", BATHROOM, "off"),
            ("2026-10-16T08:49:06.800+02:00", KITCHEN, "off"),
            ("2026-10-16T08:50:37.896+02:00", ZONE, "0"),
            ("2026-10-16T08:51:07.032+02:00", VACUUM, "cleaning"),
            ("2026-10-16T08:54:40.902+02:00", ROOM_STATE, "Empty (Dark)"),
            ("2026-10-16T08:54:48.730+02:00", VACUUM, "returning"),
            ("2026-10-16T08:54:49.782+02:00", VACUUM, "cleaning"),
            ("2026-10-16T08:54:58.331+02:00", DINING, "off"),
            ("2026-10-16T09:00:38.632+02:00", VACUUM, "returning"),
            ("2026-10-16T09:00:39.786+02:00", VACUUM, "cleaning"),
            ("2026-10-16T09:09:44.468+02:00", VACUUM, "returning"),
            ("2026-10-16T09:09:45.647+02:00", VACUUM, "cleaning"),
            ("2026-10-16T09:16:17.403+02:00", LIVING, "on"),
            ("2026-10-16T09:16:17.536+02:00", ROOM_STATE, "Occupied (Dark)"),
            ("2026-10-16T09:20:28.150+02:00", ROOM_STATE, "Empty (Dark)"),
            ("2026-10-16T09:20:44.102+02:00", LIVING, "off"),
        ],
    },
    "2026-10-19 12:09": {
        "lamps_on": False,
        "start": "2026-10-19T11:42:13.500+02:00",
        "run_start": "2026-10-19T11:46:17.085+02:00",
        "recorded_turn_on": "2026-10-19T12:09",
        "initial": {
            DOOR: ("on", "2026-10-19T11:41:14.435+02:00"),
            ZONE: ("1", "2026-10-19T07:52:01.577+02:00"),
            VACUUM: ("docked", "2026-10-18T12:56:44.763+02:00"),
            KITCHEN: ("off", "2026-10-19T10:28:31.447+02:00"),
            DINING: ("off", "2026-10-19T09:24:36.280+02:00"),
            LIVING: ("off", "2026-10-19T06:29:07.650+02:00"),
            HALLWAY: ("off", "2026-10-19T11:42:01.748+02:00"),
            BEDROOM: ("off", "2026-10-19T11:42:06.886+02:00"),
            BATHROOM: ("off", "2026-10-19T10:41:54.854+02:00"),
            GUEST_BATHROOM: ("off", "2026-10-19T10:27:02.934+02:00"),
            CLAUDIAS: ("off", "2026-10-19T07:57:08.979+02:00"),
            KRISTINES: ("off", "2026-10-19T07:45:14.001+02:00"),
            DARK: ("dark", "2026-10-19T11:23:23.763+02:00"),
            ROOM_STATE: ("Empty (Dark)", "2026-10-19T11:41:46.785+02:00"),
            DISHWASHER: ("Off", "2026-10-18T23:00:41.635+02:00"),
        },
        "events": [
            ("2026-10-19T11:42:14.769+02:00", DOOR, "off"),
            ("2026-10-19T11:45:45.786+02:00", ZONE, "0"),
            ("2026-10-19T11:46:17.085+02:00", VACUUM, "cleaning"),
            ("2026-10-19T11:49:58.298+02:00", VACUUM, "returning"),
            ("2026-10-19T11:49:59.523+02:00", VACUUM, "cleaning"),
            ("2026-10-19T11:55:44.013+02:00", VACUUM, "returning"),
            ("2026-10-19T11:55:45.551+02:00", VACUUM, "cleaning"),
            ("2026-10-19T12:03:57.158+02:00", VACUUM, "returning"),
            ("2026-10-19T12:03:58.175+02:00", VACUUM, "cleaning"),
            ("2026-10-19T12:09:43.091+02:00", LIVING, "on"),
            ("2026-10-19T12:09:43.161+02:00", ROOM_STATE, "Occupied (Dark)"),
        ],
    },
}


def iso_epoch(value):
    return datetime.fromisoformat(value).timestamp()


def iso_local(seconds):
    return datetime.fromtimestamp(seconds, timezone(timedelta(hours=2))).isoformat(timespec="milliseconds")


def dispatch(app, registrations, entity, state, stamp):
    """Change an entity and call the listeners AppDaemon would call, as the app registered them."""
    entry = app.states.setdefault(entity, {})
    old = entry.get("state")
    assert old != state, f"{entity} did not change at {stamp}"
    entry.update(state=state, last_changed=stamp)
    for callback, watched, kwargs in registrations:
        if watched != entity or "attribute" in kwargs:
            continue
        if kwargs.get("old", old) != old or kwargs.get("new", state) != state:
            continue
        extra = {key: value for key, value in kwargs.items() if key not in ("old", "new")}
        callback(entity, None, old, state, extra)


class ReplayOfRecordedRuns(unittest.TestCase):
    """Runs the real initialize() with the shipped yaml, then feeds each recorded sequence through
    the listeners it registered, evaluation after evaluation."""

    def replay(self, run, hold=True, extra_events=()):
        config = copy.deepcopy(shipped_config())
        if not hold:
            del config["robot_vacuum_entity"]
        clock = FakeClock(iso_epoch(run["start"]))
        scheduler = Scheduler(clock)
        real_time = fr.time
        fr.time = types.SimpleNamespace(time=clock.time, monotonic=clock.monotonic)
        self.addCleanup(setattr, fr, "time", real_time)

        states = make_states(zone="1", zone_changed=None, vacuum="docked", pir="off", lamps_on=())
        for entity, (state, changed) in run["initial"].items():
            states.setdefault(entity, {}).update(state=state, last_changed=changed)
        for lamp in LIGHTS:
            states[lamp]["state"] = "on" if run["lamps_on"] else "off"

        registrations = []
        app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
        app.args = config
        attach_fake_ha(app, states, scheduler)
        app.listen_state = lambda callback, entity, **kwargs: registrations.append((callback, entity, kwargs))
        app.initialize()
        scheduler._pending.clear()

        turn_ons = []
        app.turn_on.side_effect = lambda entity, **kw: (
            app.states[entity].update(state="on"),
            turn_ons.append((clock.now, entity)),
        )
        events = sorted(list(run["events"]) + list(extra_events), key=lambda e: iso_epoch(e[0]))
        for stamp, entity, state in events:
            scheduler.advance(max(0.0, iso_epoch(stamp) - clock.now))
            dispatch(app, registrations, entity, state, stamp)
        scheduler.advance(5.0)
        return app, turn_ons

    def after_run_start(self, run, turn_ons):
        return [t for t in turn_ons if t[0] >= iso_epoch(run["run_start"])]

    def guest_events(self, run):
        """A guest turns the bedroom sensor on halfway between the door closing and zone.home
        turning 0, and it fades out a minute later."""
        door_closed = iso_epoch(next(stamp for stamp, entity, _ in run["events"] if entity == DOOR))
        emptied = iso_epoch(next(stamp for stamp, entity, _ in run["events"] if entity == ZONE))
        halfway = (door_closed + emptied) / 2
        return [(iso_local(halfway), BEDROOM, "on"), (iso_local(halfway + 60), BEDROOM, "off")]

    def trip_stamp(self, run):
        return next(stamp for stamp, entity, state in run["events"] if entity == LIVING and state == "on")

    def test_a_visitor_during_the_run_lifts_the_hold_in_every_recorded_run(self):
        for name, run in RECORDED_RUNS.items():
            with self.subTest(run=name):
                visit = [(iso_local(iso_epoch(self.trip_stamp(run)) - 300), VISITOR, "cleaner")]
                app, turn_ons = self.replay(run, extra_events=visit)
                lit = self.after_run_start(run, turn_ons)

                self.assertEqual(hold_lines(app), [])
                self.assertTrue(lit, "lights must turn on at the recorded motion")
                self.assertTrue(iso_local(lit[0][0]).startswith(run["recorded_turn_on"]))

    def test_a_visitor_present_when_the_vacuum_starts_keeps_the_hold_off_for_the_run(self):
        for name, run in RECORDED_RUNS.items():
            with self.subTest(run=name):
                started = iso_epoch(run["run_start"])
                visit = [
                    (iso_local(started - 60), VISITOR, "guest"),
                    (iso_local(started + 120), VISITOR, "none"),
                ]
                app, turn_ons = self.replay(run, extra_events=visit)
                lit = self.after_run_start(run, turn_ons)

                self.assertIsNone(app._robot_quiet_for)
                self.assertIn(VISITOR, no_hold_lines(app)[0])
                self.assertTrue(lit, "lights must turn on at the recorded motion")
                self.assertTrue(iso_local(lit[0][0]).startswith(run["recorded_turn_on"]))

    def test_without_the_hold_each_recorded_run_lights_the_room(self):
        for name, run in RECORDED_RUNS.items():
            with self.subTest(run=name):
                _, turn_ons = self.replay(run, hold=False)
                lit = self.after_run_start(run, turn_ons)
                self.assertTrue(lit, "the recording must reproduce the unwanted turn-on")
                self.assertTrue(
                    iso_local(lit[0][0]).startswith(run["recorded_turn_on"]),
                    f"first turn-on {iso_local(lit[0][0])} vs recorded {run['recorded_turn_on']}",
                )

    def test_with_the_hold_no_recorded_run_turns_a_light_on_once_the_vacuum_started(self):
        for name, run in RECORDED_RUNS.items():
            with self.subTest(run=name):
                app, turn_ons = self.replay(run)
                emptied = next(stamp for stamp, entity, _ in run["events"] if entity == ZONE)

                self.assertEqual(self.after_run_start(run, turn_ons), [])
                self.assertEqual(app._robot_quiet_for, iso_epoch(emptied))
                self.assertEqual(no_hold_lines(app), [])
                self.assertEqual(len(hold_lines(app)), 1)
                self.assertTrue(
                    all(t > iso_epoch(run["run_start"]) for t in app._robot_on_edge_at.values()),
                    "no quiet-house sensor turned on between the door closing and the run starting",
                )
                self.assertEqual([lamp for lamp in LIGHTS if app.states[lamp]["state"] == "on"], [])

    def test_a_guest_active_before_the_run_lifts_the_hold_in_every_recorded_run(self):
        for name, run in RECORDED_RUNS.items():
            with self.subTest(run=name):
                app, turn_ons = self.replay(run, extra_events=self.guest_events(run))
                lit = self.after_run_start(run, turn_ons)

                self.assertIsNone(app._robot_quiet_for)
                self.assertIn(BEDROOM, no_hold_lines(app)[0])
                self.assertEqual(hold_lines(app), [])
                self.assertTrue(lit, "lights must turn on at the recorded motion")
                self.assertTrue(iso_local(lit[0][0]).startswith(run["recorded_turn_on"]))


if __name__ == "__main__":
    unittest.main()
