# tests/test_door_entry.py - DoorEntry: who came in or went out at a door opening, judged from each
# resident's Bluetooth area history and wifi tracker. The pure judge comes first; the app is then
# driven through its real initialize() with a fake clock, a scheduler that only records and fake
# states. The fake app has no call_service, so anything but publishing its own entity would fail.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import door_entry as de  # noqa: E402

TZ = timezone(timedelta(hours=1))
T0 = datetime(2026, 11, 16, 12, 0, tzinfo=TZ).timestamp()
L = "Landing"


def judge(area, tracker, t_open=1000.0):
    return de.judge(area, tracker, t_open, 120, 30, 300, L)


class HeardSince(unittest.TestCase):
    def test_the_spell_reaching_into_the_window_and_when_it_began(self):
        cases = [
            ([(0, "unknown"), (950, L)], 950),
            ([(0, "Hallway")], 0),
            ([(0, "Hallway"), (900, "unknown")], 0),
            ([(0, "Hallway"), (870, "unknown")], None),
            ([(0, "unknown"), (500, "Hallway"), (950, "unknown")], 500),
            ([(0, "Hallway"), (500, "unknown"), (1020, "Kitchen")], 1020),
            ([(0, "unknown"), (1100, L)], None),
            ([], None),
        ]
        for history, expected in cases:
            with self.subTest(history=history):
                self.assertEqual(de.heard_since(history, 880, 1030), expected)


class Judge(unittest.TestCase):
    def test_away_on_wifi_and_heard_on_the_landing_before_the_door_came_in(self):
        verdict, ev = judge([(0, "unknown"), (988, L), (1003, "Hallway")], [(0, "not_home"), (1016, "home")])
        self.assertEqual((verdict, ev["first_heard_s"], ev["wifi_away"]), ("entered", -12, True))

    def test_wifi_joining_before_the_window_still_counts_as_away_at_its_start(self):
        verdict, _ = judge([(0, "unknown"), (980, L)], [(0, "not_home"), (972, "home")])
        self.assertEqual(verdict, "entered")

    def test_a_phone_first_heard_inside_after_the_door_came_in(self):
        verdict, ev = judge([(0, "unknown"), (1012, "Kitchen")], [(0, "not_home")])
        self.assertEqual((verdict, ev["first_heard_s"]), ("entered", 12))

    def test_a_quick_return_wifi_missed_is_caught_by_five_unheard_minutes(self):
        verdict, ev = judge([(0, "Hallway"), (500, "unknown"), (990, L)], [(0, "home")])
        self.assertEqual((verdict, ev["wifi_away"], ev["ble_away"]), ("entered", False, True))

    def test_unheard_for_less_than_five_minutes_with_wifi_home_is_not_away(self):
        verdict, _ = judge([(0, "Hallway"), (700, "unknown"), (990, L)], [(0, "home")])
        self.assertIsNone(verdict)

    def test_away_but_never_heard_is_nobody(self):
        self.assertIsNone(judge([(0, "unknown")], [(0, "not_home")])[0])

    def test_inside_then_on_the_landing_after_the_door_went_out(self):
        verdict, ev = judge([(0, "Hallway"), (1004, L), (1014, "Guest bathroom")], [(0, "home")])
        self.assertEqual((verdict, ev["landing_after_s"], ev["inside_before"]), ("left", 4, True))

    def test_already_on_the_landing_as_the_door_opens_went_out(self):
        verdict, ev = judge([(0, "Hallway"), (998, L)], [(0, "home")])
        self.assertEqual((verdict, ev["landing_after_s"]), ("left", 0.0))

    def test_on_the_landing_only_after_the_window_is_nothing(self):
        self.assertIsNone(judge([(0, "Hallway"), (1031, L)], [(0, "home")])[0])

    def test_home_and_inside_while_someone_else_uses_the_door_is_nothing(self):
        self.assertIsNone(judge([(0, "Bedroom"), (1010, "Hallway")], [(0, "home")])[0])

    def test_a_bermuda_reload_blip_does_not_make_a_resident_away(self):
        verdict, _ = judge([(0, "Bedroom"), (900, "unavailable"), (902, "Bedroom")], [(0, "home")])
        self.assertIsNone(verdict)

    def test_describe(self):
        ev = {"Mikkel": {"first_heard_s": -20, "landing_after_s": None},
              "Claudia": {"first_heard_s": None, "landing_after_s": 9}}
        self.assertEqual(de.describe(["Mikkel"], ["Claudia"], ev),
                         "Mikkel came in (phone first heard 20 s before the door opened); "
                         "Claudia went out (phone on the landing 9 s after the door opened).")
        self.assertEqual(de.describe([], [], ev), "No resident's phone explains the door opening.")


MIKKEL_AREA, MIKKEL_WIFI = "sensor.mikkel_area", "device_tracker.mikkel"
CLAUDIA_AREA, CLAUDIA_WIFI = "sensor.claudia_area", "device_tracker.claudia"
DOOR, ONLINE, ENTITY = "binary_sensor.door", "sensor.landing_uptime", "sensor.door_entry"
_DIR = tempfile.TemporaryDirectory()


def utc(seconds):
    return datetime.fromtimestamp(T0 + seconds, timezone.utc).isoformat()


def iso(seconds):
    return datetime.fromtimestamp(T0 + seconds, TZ).isoformat(timespec="seconds")


def make_app(states=None, changed=-3600, published=False):
    app = de.DoorEntry.__new__(de.DoorEntry)
    app.args = {"entity": ENTITY, "door": DOOR, "landing_area": L, "landing_online": ONLINE,
                "log_file": os.path.join(_DIR.name, f"log_{id(app)}.jsonl"),
                "residents": [{"name": "Mikkel", "area_sensor": MIKKEL_AREA, "tracker": MIKKEL_WIFI},
                              {"name": "Claudia", "area_sensor": CLAUDIA_AREA, "tracker": CLAUDIA_WIFI}]}
    app.states = {MIKKEL_AREA: "unknown", MIKKEL_WIFI: "not_home", CLAUDIA_AREA: "Bedroom",
                  CLAUDIA_WIFI: "home", ONLINE: "unavailable", DOOR: "off", **(states or {})}
    if published:
        app.states[ENTITY] = "left"
    app.clock = {"now": T0}
    app.timers, app.published, app.logged = [], [], []

    def get_state(entity, attribute=None, **kw):
        if attribute == "last_changed":
            return utc(changed) if entity in app.states else None
        return app.states.get(entity)

    def set_state(entity, state=None, attributes=None, replace=False, **kw):
        app.published.append((entity, state, attributes))
        app.states[entity] = state

    app.get_state = get_state
    app.set_state = set_state
    app.get_now = lambda: datetime.fromtimestamp(app.clock["now"], TZ)
    app._now = lambda: app.clock["now"]
    app.listen_state = MagicMock()
    app.run_in = lambda cb, delay, **kw: app.timers.append((app.clock["now"] + delay, cb, kw))
    app.log = lambda message, level="INFO": app.logged.append((level, message))
    app.initialize()
    return app


def at(app, seconds, entity, new):
    app.clock["now"] = T0 + seconds
    old = app.states.get(entity)
    app.states[entity] = new
    if entity == DOOR:
        if new == "on":
            app._on_door(entity, None, old, new, {})
    else:
        app._on_change(entity, None, old, new, {})


def run_timers(app, until):
    for when, cb, kw in sorted(app.timers, key=lambda t: t[0]):
        if when <= T0 + until:
            app.clock["now"] = when
            cb(dict(kw))
    app.timers = [t for t in app.timers if t[0] > T0 + until]


def lines(app):
    try:
        with open(app.log_file, encoding="utf-8") as f:
            return [json.loads(row) for row in f.read().splitlines()]
    except FileNotFoundError:
        return []


class App(unittest.TestCase):
    def test_starts_at_none_and_listens_to_every_input(self):
        app = make_app()
        (entity, state, attributes), = app.published
        self.assertEqual((entity, state, attributes["entered"], attributes["reason"]),
                         (ENTITY, "none", [], "No door opening judged since AppDaemon started."))
        watched = {c.args[1] for c in app.listen_state.call_args_list}
        self.assertEqual(watched, {MIKKEL_AREA, MIKKEL_WIFI, CLAUDIA_AREA, CLAUDIA_WIFI, ONLINE, DOOR})

    def test_a_restart_keeps_the_published_state(self):
        self.assertEqual(make_app(published=True).published, [])

    def test_an_arrival_is_published_post_s_after_the_door_with_its_reason_and_logged(self):
        app = make_app()
        at(app, 5, ONLINE, "1.2")
        at(app, 40, MIKKEL_AREA, L)
        at(app, 60, DOOR, "on")
        at(app, 70, MIKKEL_AREA, "Hallway")
        self.assertEqual(len(app.published), 1)
        run_timers(app, 90)
        _, state, attributes = app.published[-1]
        self.assertEqual((state, attributes["entered"], attributes["left"], attributes["door_opened_at"],
                          attributes["decided_at"], attributes["landing_proxy"]),
                         ("entered", ["Mikkel"], [], iso(60), iso(90), "on"))
        self.assertEqual(attributes["reason"], "Mikkel came in (phone first heard 20 s before the door opened).")
        (line,) = lines(app)
        self.assertEqual((line["state"], line["evidence"]["Mikkel"]["wifi_away"]), ("entered", True))

    def test_one_coming_in_while_another_goes_out_is_entered_with_both_named(self):
        app = make_app()
        at(app, 50, MIKKEL_AREA, L)
        at(app, 60, DOOR, "on")
        at(app, 64, CLAUDIA_AREA, L)
        run_timers(app, 90)
        _, state, attributes = app.published[-1]
        self.assertEqual((state, attributes["entered"], attributes["left"]), ("entered", ["Mikkel"], ["Claudia"]))

    def test_a_departure_alone_is_left(self):
        app = make_app()
        at(app, 60, DOOR, "on")
        at(app, 69, CLAUDIA_AREA, L)
        run_timers(app, 90)
        self.assertEqual(app.published[-1][1:2], ("left",))

    def test_no_phone_explains_it_and_a_dark_landing_is_said(self):
        app = make_app()
        at(app, 60, DOOR, "on")
        run_timers(app, 90)
        _, state, attributes = app.published[-1]
        self.assertEqual((state, attributes["landing_proxy"]), ("unknown", "off"))
        self.assertEqual(attributes["reason"], "No resident's phone explains the door opening. "
                                               "The landing proxy had no power around the opening.")

    def test_old_history_is_pruned_but_the_state_held_is_kept(self):
        app = make_app()
        for i in range(1, 200):
            at(app, i * 10, ONLINE, str(i))
        history = app._history[ONLINE]
        self.assertLessEqual(history[1][0] - history[0][0], 10)
        self.assertGreaterEqual(history[0][0], T0 + 1990 - app.keep_s - 10)
        self.assertEqual(de.state_at(history, T0 + 1990), "199")

    def test_bad_windows_fail_at_start(self):
        for key, value in (("post_s", 0), ("pre_s", -1), ("away_s", -1)):
            with self.subTest(key=key):
                app = de.DoorEntry.__new__(de.DoorEntry)
                app.args = {"door": DOOR, "landing_area": L, "residents": [], key: value}
                app.get_now = lambda: datetime.fromtimestamp(T0, TZ)
                with self.assertRaises(ValueError):
                    app.initialize()

    def test_the_app_never_calls_a_service(self):
        source = Path(de.__file__).read_text()
        for call in ("call_service", "turn_on", "turn_off", "notify", "fire_event"):
            self.assertNotIn(call, source)


if __name__ == "__main__":
    unittest.main()
