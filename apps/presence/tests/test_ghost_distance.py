# tests/test_ghost_distance.py - GhostDistance: the pure trigger and cap decision, then the
# episode lifecycle (dwell, one press, recording window, JSONL line) against a fake clock.
# Same __new__ + monkeypatched-callables harness as the other tests in this directory.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call

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

import ghost_distance as gd  # noqa: E402

ZONE = "zone.home"
PRESENCE = "binary_sensor.dining_room_presence_presence"
BUTTON = "button.dining_room_presence_track_target_distance"
DISTANCE = "sensor.dining_room_presence_target_distance"

TZ = timezone(timedelta(hours=2))
T0 = datetime(2026, 10, 21, 15, 0, tzinfo=TZ).timestamp()

ENTITIES = {"home_zone": ZONE, "presence": PRESENCE, "track_button": BUTTON, "distance": DISTANCE}
BASE_ARGS = {**ENTITIES, "dwell_min": 3, "record_min": 5, "max_episodes_per_day": 3}

EMPTY_AND_PRESENT = {ZONE: "0", PRESENCE: "on", DISTANCE: "0"}


def stamp(seconds):
    return datetime.fromtimestamp(T0 + seconds, TZ).isoformat(timespec="seconds")


def make_app(states=None, args=None):
    """GhostDistance with initialize() actually run and the AppDaemon primitives faked: a
    settable clock, a state dict behind get_state, and recording stubs. set_state, turn_on, notify
    and the like are deliberately left unstubbed: this app must never call them."""
    app = gd.GhostDistance.__new__(gd.GhostDistance)
    app.args = dict(args if args is not None else BASE_ARGS)
    app.states = dict(states or {})
    app.clock = {"now": T0}
    app.logged = []
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.get_now = lambda: datetime.fromtimestamp(app.clock["now"], TZ)
    app.log = lambda message, level="INFO": app.logged.append((level, message))
    app.listen_state = MagicMock()
    app.run_every = MagicMock()
    app.call_service = MagicMock()
    app.initialize()
    return app


def at(app, seconds):
    app.clock["now"] = T0 + seconds


def tick(app, seconds):
    at(app, seconds)
    app._tick({})


def set_zone(app, seconds, state):
    at(app, seconds)
    old, app.states[ZONE] = app.states.get(ZONE), state
    app._on_zone(ZONE, "state", old, state, {})


def set_presence(app, seconds, state):
    at(app, seconds)
    old, app.states[PRESENCE] = app.states.get(PRESENCE), state
    app._on_presence(PRESENCE, "state", old, state, {})


def set_distance(app, seconds, state):
    at(app, seconds)
    old, app.states[DISTANCE] = app.states.get(DISTANCE), state
    app._on_distance(DISTANCE, "state", old, state, {})


def lines(app):
    path = app.record_file
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def presses(app):
    return app.call_service.call_count


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log_file = str(Path(tmp.name) / "ghost_distance_log.jsonl")

    def make_app(self, states=None, **args):
        return make_app(states, {**BASE_ARGS, "log_file": self.log_file, **args})


class HomeEmpty(unittest.TestCase):
    def test_only_zero_is_an_empty_house(self):
        self.assertTrue(gd.home_empty("0"))
        for state in ("1", "2", "unavailable", "unknown", "", None):
            self.assertFalse(gd.home_empty(state), state)


class PressDue(unittest.TestCase):
    def due(self, held_s=180, **over):
        args = {"pressed": False, "recording": False, "episodes_today": 0, "max_episodes": 3, **over}
        return gd.press_due(held_s, 180, **args)

    def test_due_once_held_for_the_dwell(self):
        self.assertFalse(self.due(held_s=179.9))
        self.assertTrue(self.due(held_s=180))
        self.assertTrue(self.due(held_s=900))

    def test_not_due_when_the_two_do_not_hold_together(self):
        self.assertFalse(self.due(held_s=None))

    def test_not_due_after_the_episodes_press_or_while_a_recording_is_open(self):
        self.assertFalse(self.due(pressed=True))
        self.assertFalse(self.due(recording=True))

    def test_daily_cap(self):
        self.assertTrue(self.due(episodes_today=2))
        self.assertFalse(self.due(episodes_today=3))
        self.assertFalse(self.due(episodes_today=4))


class DistanceValue(unittest.TestCase):
    def test_numbers_become_floats_and_anything_else_stays_raw(self):
        self.assertEqual(gd.distance_value("1.42"), 1.42)
        self.assertEqual(gd.distance_value("0"), 0.0)
        self.assertEqual(gd.distance_value("unknown"), "unknown")
        self.assertEqual(gd.distance_value("unavailable"), "unavailable")
        self.assertIsNone(gd.distance_value(None))


class Initialize(Base):
    def test_arguments_listeners_and_tick(self):
        app = self.make_app(dwell_min=4, record_min=6, max_episodes_per_day=2)
        self.assertEqual((app.dwell_s, app.record_s, app.max_episodes), (240.0, 360.0, 2))
        self.assertEqual(str(app.record_file), self.log_file)
        self.assertEqual({c.args[1] for c in app.listen_state.call_args_list}, {ZONE, PRESENCE, DISTANCE})
        app.run_every.assert_called_once_with(app._tick, f"now+{gd.TICK_S}", gd.TICK_S)

    def test_defaults_and_log_file_next_to_the_module(self):
        app = make_app(args=ENTITIES)
        self.assertEqual((app.dwell_s, app.record_s, app.max_episodes), (180.0, 300.0, 3))
        self.assertEqual(app.record_file, Path(gd.__file__).resolve().parent / "ghost_distance_log.jsonl")

    def test_a_state_already_holding_at_start_counts_from_the_start(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 179)
        self.assertEqual(presses(app), 0)
        tick(app, 180)
        self.assertEqual(presses(app), 1)

    def test_log_messages_are_ascii(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 180)
        tick(app, 480)
        self.assertGreaterEqual(len(app.logged), 3)
        for _, message in app.logged:
            self.assertTrue(message.isascii(), message)


class Trigger(Base):
    def test_presses_the_button_once_the_dwell_has_held(self):
        app = self.make_app({ZONE: "0", DISTANCE: "0"})
        set_presence(app, 30, "on")
        tick(app, 209)
        self.assertEqual(presses(app), 0)
        tick(app, 210)
        self.assertEqual(app.call_service.call_args_list, [call("button/press", entity_id=BUTTON)])

    def test_dwell_counts_from_the_later_of_the_two_edges(self):
        app = self.make_app({ZONE: "1", PRESENCE: "off", DISTANCE: "0"})
        set_presence(app, 0, "on")
        set_zone(app, 120, "0")
        tick(app, 299)
        self.assertEqual(presses(app), 0)
        tick(app, 300)
        self.assertEqual(presses(app), 1)

    def test_no_press_while_someone_is_home(self):
        app = self.make_app({ZONE: "1", PRESENCE: "off"})
        set_presence(app, 0, "on")
        for seconds in range(10, 1200, 10):
            tick(app, seconds)
        self.assertEqual(presses(app), 0)

    def test_presence_off_before_the_dwell_prevents_the_press(self):
        app = self.make_app({ZONE: "0", PRESENCE: "off"})
        set_presence(app, 0, "on")
        set_presence(app, 100, "off")
        for seconds in range(10, 1200, 10):
            tick(app, seconds)
        self.assertEqual(presses(app), 0)

    def test_an_unavailable_zone_is_not_an_empty_house(self):
        app = self.make_app({ZONE: "unavailable", PRESENCE: "on"})
        tick(app, 600)
        self.assertEqual(presses(app), 0)
        set_zone(app, 600, "0")
        tick(app, 779)
        self.assertEqual(presses(app), 0)
        tick(app, 780)
        self.assertEqual(presses(app), 1)


class Episodes(Base):
    def run_episode(self, app, start):
        """Presence on, the dwell, the press, the whole recording window, presence off."""
        set_presence(app, start, "on")
        tick(app, start + 180)
        tick(app, start + 480)
        set_presence(app, start + 490, "off")

    def test_one_press_per_episode(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        for seconds in range(10, 3600, 10):
            tick(app, seconds)
        self.assertEqual(presses(app), 1)
        self.assertEqual(len(lines(app)), 1)

    def test_presence_off_and_on_again_is_a_new_episode(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 180)
        tick(app, 480)
        set_presence(app, 510, "off")
        set_presence(app, 600, "on")
        tick(app, 779)
        self.assertEqual(presses(app), 1)
        tick(app, 780)
        self.assertEqual(presses(app), 2)

    def test_someone_coming_home_and_leaving_again_is_a_new_episode(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 180)
        tick(app, 480)
        set_zone(app, 500, "1")
        set_zone(app, 560, "0")
        tick(app, 739)
        self.assertEqual(presses(app), 1)
        tick(app, 740)
        self.assertEqual(presses(app), 2)

    def test_an_episode_that_qualifies_during_a_recording_presses_when_it_closes(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 180)
        set_presence(app, 200, "off")
        set_presence(app, 220, "on")
        tick(app, 400)
        self.assertEqual(presses(app), 1)
        tick(app, 479)
        self.assertEqual(presses(app), 1)
        tick(app, 480)
        self.assertEqual(presses(app), 2)
        self.assertEqual(len(lines(app)), 1)

    def test_an_episode_that_ended_while_a_recording_was_open_is_not_pressed_late(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 180)
        set_presence(app, 200, "off")
        set_presence(app, 220, "on")
        set_presence(app, 300, "off")
        tick(app, 480)
        tick(app, 900)
        self.assertEqual(presses(app), 1)

    def test_at_most_three_episodes_a_day_and_the_count_restarts_next_day(self):
        app = self.make_app({ZONE: "0", PRESENCE: "off", DISTANCE: "0"})
        for start in (0, 600, 1200, 1800):
            self.run_episode(app, start)
        self.assertEqual(presses(app), 3)
        self.assertEqual(len(lines(app)), 3)
        next_day = (datetime(2026, 10, 22, 0, 5, tzinfo=TZ).timestamp() - T0)
        self.run_episode(app, next_day)
        self.assertEqual(presses(app), 4)

    def test_midnight_inside_an_episode_does_not_press_again(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        shift = datetime(2026, 10, 21, 23, 55, tzinfo=TZ).timestamp() - T0
        set_zone(app, shift, "unavailable")
        set_zone(app, shift, "0")
        tick(app, shift + 180)
        self.assertEqual(presses(app), 1)
        for seconds in range(190, 1500, 10):
            tick(app, shift + seconds)
        self.assertEqual(presses(app), 1)


class Recording(Base):
    def test_line_holds_the_start_reading_every_change_and_a_final_read(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 180)
        set_distance(app, 185, "1.35")
        set_distance(app, 250, "unavailable")
        set_distance(app, 260, "2.1")
        self.assertEqual(lines(app), [])
        tick(app, 479)
        self.assertEqual(lines(app), [])
        tick(app, 484)
        self.assertEqual(lines(app), [{
            "start": stamp(180),
            "presence_went_off": False,
            "readings": [[stamp(180), 0.0], [stamp(185), 1.35], [stamp(250), "unavailable"],
                         [stamp(260), 2.1], [stamp(484), 2.1]],
        }])

    def test_presence_going_off_inside_the_window_is_flagged(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 180)
        set_presence(app, 250, "off")
        tick(app, 480)
        self.assertEqual([record["presence_went_off"] for record in lines(app)], [True])

    def test_presence_going_unavailable_ends_the_episode_but_is_not_off(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 180)
        set_presence(app, 250, "unavailable")
        set_presence(app, 260, "on")
        tick(app, 480)
        self.assertEqual([record["presence_went_off"] for record in lines(app)], [False])
        tick(app, 800)
        self.assertEqual(presses(app), 2)

    def test_presence_going_off_after_the_window_is_not(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 180)
        tick(app, 480)
        set_presence(app, 500, "off")
        self.assertEqual([record["presence_went_off"] for record in lines(app)], [False])

    def test_changes_and_presence_edges_outside_a_recording_are_ignored(self):
        app = self.make_app({ZONE: "0", PRESENCE: "off", DISTANCE: "0"})
        set_distance(app, 10, "3.0")
        set_presence(app, 20, "on")
        set_presence(app, 30, "off")
        self.assertEqual(lines(app), [])
        self.assertEqual(presses(app), 0)

    def test_one_info_summary_per_episode(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        tick(app, 180)
        set_distance(app, 190, "1.5")
        tick(app, 480)
        summaries = [(level, message) for level, message in app.logged if message.startswith("GhostDistance: episode ")]
        self.assertEqual(len(summaries), 1)
        level, message = summaries[0]
        self.assertEqual(level, "INFO")
        self.assertIn(stamp(180), message)
        self.assertIn("3 reading(s)", message)
        self.assertIn("final distance 1.5", message)
        self.assertIn("presence went off: False", message)


class Failures(Base):
    def test_a_failed_press_is_not_retried_not_recorded_and_still_counts(self):
        app = self.make_app(EMPTY_AND_PRESENT)
        app.call_service.side_effect = RuntimeError("boom")
        for seconds in range(10, 1200, 10):
            tick(app, seconds)
        self.assertEqual(presses(app), 1)
        self.assertEqual(lines(app), [])
        self.assertEqual(app._episodes, 1)
        self.assertEqual([level for level, message in app.logged if "failed" in message], ["WARNING"])

    def test_an_unwritable_log_warns_with_the_record(self):
        missing = str(Path(self.log_file).parent / "missing" / "ghost_distance_log.jsonl")
        app = make_app(EMPTY_AND_PRESENT, {**BASE_ARGS, "log_file": missing})
        tick(app, 180)
        tick(app, 480)
        warnings = [message for level, message in app.logged if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn(missing, warnings[0])
        self.assertIn('"presence_went_off": false', warnings[0])
        self.assertTrue(any(message.startswith("GhostDistance: episode ") for _, message in app.logged))


if __name__ == "__main__":
    unittest.main()
