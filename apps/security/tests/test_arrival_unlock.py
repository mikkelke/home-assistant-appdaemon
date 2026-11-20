# tests/test_arrival_unlock.py - ArrivalUnlock: when a landing sighting is armed, the window it opens
# and closes, the arrival and missed JSONL lines, and the tolerance for landing entities that do not
# exist yet. The app is driven through its real initialize() with a fake clock, a fake scheduler and
# fake states. call_service only records, and without an unlock switch that is on nothing may be
# called (a source check below pins which services the app can call at all); every AppDaemon call is
# also checked to be made without the app's lock held. The Unlocking tests cover the real unlock.
# Run from repo root: python3 -m unittest discover -s apps/security/tests -q

from __future__ import annotations

import ast
import itertools
import json
import math
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

import arrival_unlock as au  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    COPENHAGEN = ZoneInfo("Europe/Copenhagen")
except (ImportError, KeyError):  # no time zone database on this machine
    COPENHAGEN = None

TZ = timezone(timedelta(hours=1))
T0 = datetime(2026, 11, 10, 11, 0, tzinfo=TZ).timestamp()

TRACKER = "device_tracker.phone"
AREA = "sensor.bermuda_phone_area"
LANDING = "Landing"
DISTANCE = "sensor.bermuda_phone_distance_to_ble_proxy_landing"
ONLINE = "sensor.ble_proxy_landing_uptime"
LOCK = "sensor.apartment_lock"
DOOR = "binary_sensor.apartment_door_open"
KEYPAD = "lock.yale"
ENTITIES = {"tracker": TRACKER, "landing_area_sensor": AREA, "landing_area": LANDING,
            "landing_distance": DISTANCE, "landing_online": ONLINE, "lock": LOCK, "door": DOOR,
            "keypad": KEYPAD}

_DIR = tempfile.TemporaryDirectory()
_SEQ = itertools.count()


def new_log_file():
    return os.path.join(_DIR.name, f"arrival_unlock_log_{next(_SEQ)}.jsonl")


def iso(seconds):
    return datetime.fromtimestamp(T0 + seconds, TZ).isoformat(timespec="seconds")


def utc(seconds):
    """A last_changed the way Home Assistant writes it."""
    return datetime.fromtimestamp(T0 + seconds, timezone.utc).isoformat()


class CountingLock:
    def __init__(self):
        self.held = 0

    def __enter__(self):
        self.held += 1

    def __exit__(self, *exc):
        self.held -= 1


def make_app(args=None, states=None, away_for=3600, missing=(), drop=(), tz=TZ):
    """ArrivalUnlock with initialize() actually run and the AppDaemon primitives faked: a settable
    clock (seconds after T0), a scheduler that only records, and states that get_state reads. The
    tracker has been not_home for away_for seconds unless states says otherwise; `missing` entities
    do not exist (get_state answers None) and `drop` removes yaml keys."""
    app = au.ArrivalUnlock.__new__(au.ArrivalUnlock)
    app.args = {**ENTITIES, "log_file": new_log_file(), **(args or {})}
    for key in drop:
        del app.args[key]
    app.states = {TRACKER: "not_home", AREA: "unknown", DISTANCE: "unknown", ONLINE: "unavailable",
                  LOCK: "locked", DOOR: "off", KEYPAD: "locked", **(states or {})}
    app.attrs = {(TRACKER, "last_changed"): utc(-away_for), (KEYPAD, "changed_by"): "Auto Lock"}
    for entity in missing:
        app.states.pop(entity, None)
        app.attrs = {key: value for key, value in app.attrs.items() if key[0] != entity}
    app.clock = {"now": T0}
    app.logged, app.timers, app.scheduled = [], [], []
    order = itertools.count()

    def api():
        assert getattr(app._lock, "held", 0) == 0, "an AppDaemon call was made while holding the app's lock"

    def get_state(entity, attribute=None, **kw):
        api()
        return app.attrs.get((entity, attribute)) if attribute else app.states.get(entity)

    def run_in(callback, delay, **kw):
        api()
        app.scheduled.append((delay, kw))
        app.timers.append((app.clock["now"] + delay, next(order), callback, kw))

    def call_service(service, **kw):
        api()
        if app.fail_service.get(service):
            raise RuntimeError(app.fail_service[service])
        app.calls.append((app.clock["now"] - T0, service, kw))

    app.calls, app.fail_service, app.fired = [], {}, []
    app.call_service = call_service
    app.fire_event = lambda event, **kw: app.fired.append((app.clock["now"] - T0, event, kw))
    app.get_state = get_state
    app.get_now = lambda: datetime.fromtimestamp(app.clock["now"], tz)
    app._now = lambda: app.clock["now"]
    app.log = lambda message, level="INFO": app.logged.append((level, message))
    app.listen_state = MagicMock()
    app.run_in = run_in
    app.initialize()
    app._lock = CountingLock()
    return app


def advance(app, seconds):
    """Fires every timer due by T0 + seconds in order, then sets the clock there."""
    target = T0 + seconds
    while True:
        due = [t for t in app.timers if t[0] <= target]
        if not due:
            break
        timer = min(due, key=lambda t: t[:2])
        app.timers.remove(timer)
        app.clock["now"] = max(app.clock["now"], timer[0])
        timer[2](dict(timer[3]))
    app.clock["now"] = target


def sight(app, seconds, distance=None, old="Hall"):
    """The phone's area becomes the landing; `distance` is the landing distance sensor's state."""
    advance(app, seconds)
    if distance is not None:
        app.states[DISTANCE] = distance
    app.states[AREA] = LANDING
    app._on_area(AREA, None, old, LANDING, {})


def open_door(app, seconds):
    advance(app, seconds)
    app.states[DOOR] = "on"
    app._on_door(DOOR, None, "off", "on", {})


def wifi_home(app, seconds, old="not_home"):
    advance(app, seconds)
    app.states[TRACKER] = "home"
    app.attrs[(TRACKER, "last_changed")] = utc(seconds)
    app._on_tracker(TRACKER, None, old, "home", {})


def wifi_away(app, seconds, old="home"):
    advance(app, seconds)
    app.states[TRACKER] = "not_home"
    app.attrs[(TRACKER, "last_changed")] = utc(seconds)
    app._on_tracker(TRACKER, None, old, "not_home", {})


def keypad(app, seconds, by, old=None):
    advance(app, seconds)
    app.attrs[(KEYPAD, "changed_by")] = by
    app._on_keypad(KEYPAD, "changed_by", old, by, {})


def proxy_up(app, seconds, old="unavailable", new="12"):
    advance(app, seconds)
    app.states[ONLINE] = new
    app._on_online(ONLINE, None, old, new, {})


def lines(app):
    try:
        with open(app.log_file, encoding="utf-8") as f:
            return [json.loads(row) for row in f.read().splitlines()]
    except FileNotFoundError:
        return []


def warnings(app):
    return [message for level, message in app.logged if level == "WARNING"]


class Arming(unittest.TestCase):
    def test_a_sighting_while_armed_opens_one_window_and_schedules_its_close(self):
        app = make_app()
        sight(app, 5, "4.2")
        self.assertEqual([delay for delay, _ in app.scheduled], [180])
        self.assertEqual(lines(app), [])
        self.assertIn(("INFO", "ArrivalUnlock: window 1 opened, phone in 'Landing', an unlock would happen now"),
                      app.logged)
        advance(app, 5 + 180)
        (line,) = lines(app)
        self.assertEqual(line["would_unlock"], {"t": iso(5), "distance": 4.2, "lock": "locked", "door": "off"})
        self.assertEqual((line["start"], line["tracker_away_since"]), (iso(5), iso(-3600)))

    def test_every_condition_has_to_hold(self):
        cases = {"tracker home": {"states": {TRACKER: "home"}},
                 "away one second short of away_min": {"away_for": 599},
                 "lock unlocked": {"states": {LOCK: "unlocked"}},
                 "lock locking": {"states": {LOCK: "locking"}},
                 "lock unknown": {"states": {LOCK: "unknown"}},
                 "lock unavailable": {"states": {LOCK: "unavailable"}},
                 "door open": {"states": {DOOR: "on"}},
                 "door unknown": {"states": {DOOR: "unknown"}},
                 "door unavailable": {"states": {DOOR: "unavailable"}},
                 "tracker missing": {"missing": (TRACKER,)},
                 "lock missing": {"missing": (LOCK,)},
                 "door missing": {"missing": (DOOR,)}}
        for name, case in cases.items():
            with self.subTest(name):
                app = make_app(**case)
                sight(app, 0)
                advance(app, 5000)
                self.assertEqual((app.scheduled, lines(app)), ([], []))

    def test_away_for_exactly_away_min_arms(self):
        app = make_app(away_for=600)
        sight(app, 0)
        self.assertEqual([delay for delay, _ in app.scheduled], [180])

    def test_any_state_but_home_counts_as_away(self):
        for state in ("not_home", "unavailable", "unknown", "work"):
            with self.subTest(state):
                app = make_app(states={TRACKER: state})
                sight(app, 0)
                self.assertEqual([delay for delay, _ in app.scheduled], [180])

    def test_a_tracker_without_a_usable_last_changed_never_arms(self):
        for value in (None, "garbage", "2026-11-10T10:00:00"):
            with self.subTest(value):
                app = make_app()
                app.attrs[(TRACKER, "last_changed")] = value
                sight(app, 0)
                self.assertEqual(app.scheduled, [])

    def test_the_tracker_is_read_at_the_sighting_so_a_new_absence_is_not_armed_yet(self):
        app = make_app(states={TRACKER: "home"})
        wifi_away(app, 10)
        sight(app, 100)
        self.assertEqual(app.scheduled, [])
        sight(app, 610)
        self.assertEqual([delay for delay, _ in app.scheduled], [180])


class Sightings(unittest.TestCase):
    def test_the_landing_distance_is_read_at_the_sighting_and_logged_as_a_number_or_null(self):
        for name, state, expected in (("a number", "4.2", 4.2), ("unknown", "unknown", None),
                                      ("unavailable", "unavailable", None), ("text", "far", None),
                                      ("not finite", "inf", None)):
            with self.subTest(name):
                app = make_app()
                sight(app, 5, state)
                app.states[DISTANCE] = "9.9"
                advance(app, 185)
                (line,) = lines(app)
                self.assertEqual(line["would_unlock"]["distance"], expected)

    def test_a_landing_distance_that_does_not_exist_or_is_not_configured_is_null(self):
        for name, case in (("missing", {"missing": (DISTANCE,)}), ("not configured", {"drop": ("landing_distance",)})):
            with self.subTest(name):
                app = make_app(**case)
                if name == "not configured":
                    app.states[DISTANCE] = "4.2"
                sight(app, 5)
                advance(app, 185)
                (line,) = lines(app)
                self.assertIsNone(line["would_unlock"]["distance"])

    def test_the_landing_area_again_inside_an_open_window_changes_nothing(self):
        app = make_app()
        sight(app, 5, "6.0")
        app.states[AREA] = "Hall"
        sight(app, 20, "3.5")
        self.assertEqual([delay for delay, _ in app.scheduled], [180])
        advance(app, 5 + 180)
        (line,) = lines(app)
        self.assertEqual(line["would_unlock"], {"t": iso(5), "distance": 6.0, "lock": "locked", "door": "off"})

    def test_the_lock_and_door_states_in_the_line_are_those_of_the_sighting(self):
        app = make_app()
        sight(app, 5)
        app.states[LOCK] = "unlocked"
        open_door(app, 20)
        advance(app, 50)
        (line,) = lines(app)
        self.assertEqual(line["would_unlock"]["lock"], "locked")
        self.assertEqual(line["would_unlock"]["door"], "off")

    def test_a_sighting_after_the_close_opens_a_new_window(self):
        app = make_app()
        sight(app, 0)
        advance(app, 180)
        sight(app, 200)
        advance(app, 380)
        self.assertEqual([(row["start"], row["end"]) for row in lines(app)],
                         [(iso(0), iso(180)), (iso(200), iso(380))])
        self.assertIn(("INFO", "ArrivalUnlock: window 2 opened, phone in 'Landing', an unlock would happen now"),
                      app.logged)


class Closing(unittest.TestCase):
    def test_the_door_opening_closes_the_window_30_s_later_and_gives_the_lead(self):
        app = make_app()
        sight(app, 5)
        open_door(app, 20)
        self.assertEqual([delay for delay, _ in app.scheduled], [180, 30])
        advance(app, 49)
        self.assertEqual(lines(app), [])
        advance(app, 50)
        (line,) = lines(app)
        self.assertEqual(set(line), {"kind", "start", "end", "proxy_online", "tracker_away_since", "would_unlock",
                                     "door_open", "wifi_home", "keypad", "outcome", "lead_s"})
        self.assertEqual((line["kind"], line["start"], line["end"], line["door_open"]),
                         ("arrival", iso(5), iso(50), iso(20)))
        self.assertEqual((line["outcome"], line["lead_s"]), ("entered", 15))
        self.assertIn(("INFO", f"ArrivalUnlock: window 1 closed, entered, lead 15 s -> {app.log_file.name}"),
                      app.logged)

    def test_no_door_closes_the_window_after_window_s_without_a_lead(self):
        app = make_app()
        sight(app, 5)
        advance(app, 5 + 180 - 1)
        self.assertEqual(lines(app), [])
        advance(app, 5 + 180)
        (line,) = lines(app)
        self.assertEqual((line["outcome"], line["start"], line["end"], line["door_open"]),
                         ("no_door", iso(5), iso(185), None))
        self.assertNotIn("lead_s", line)
        self.assertIn(("INFO", f"ArrivalUnlock: window 1 closed, no_door -> {app.log_file.name}"), app.logged)

    def test_a_door_opening_late_in_the_window_closes_it_at_window_s(self):
        app = make_app()
        sight(app, 0)
        open_door(app, 170)
        advance(app, 179)
        self.assertEqual(lines(app), [])
        advance(app, 180)
        (line,) = lines(app)
        self.assertEqual((line["outcome"], line["end"], line["lead_s"]), ("entered", iso(180), 170))
        advance(app, 200)
        self.assertEqual(len(lines(app)), 1)

    def test_only_the_first_door_opening_counts(self):
        app = make_app()
        sight(app, 0)
        open_door(app, 20)
        app.states[DOOR] = "off"
        open_door(app, 25)
        self.assertEqual([delay for delay, _ in app.scheduled], [180, 30])
        advance(app, 5000)
        (line,) = lines(app)
        self.assertEqual((line["door_open"], line["end"], line["lead_s"]), (iso(20), iso(50), 20))

    def test_a_door_opening_with_no_window_schedules_nothing_and_is_not_in_the_next_window(self):
        app = make_app()
        open_door(app, 10)
        self.assertEqual(app.scheduled, [])
        app.states[DOOR] = "off"
        sight(app, 60)
        advance(app, 240)
        (line,) = lines(app)
        self.assertEqual((line["outcome"], line["door_open"]), ("no_door", None))

    def test_a_timer_of_an_earlier_window_does_not_close_the_next_one(self):
        app = make_app()
        sight(app, 0)
        open_door(app, 20)
        advance(app, 50)
        self.assertEqual(len(lines(app)), 1)
        app.states[DOOR] = "off"
        app.states[AREA] = "Hall"
        sight(app, 60)
        advance(app, 200)
        self.assertEqual(len(lines(app)), 1)
        advance(app, 240)
        self.assertEqual([(row["start"], row["outcome"]) for row in lines(app)],
                         [(iso(0), "entered"), (iso(60), "no_door")])

    def test_the_file_is_appended_one_ascii_json_object_per_line_and_may_be_missing(self):
        app = make_app()
        self.assertFalse(os.path.exists(app.log_file))
        sight(app, 0)
        advance(app, 180)
        sight(app, 300)
        keypad(app, 310, "Mikkel Ø")
        advance(app, 480)
        with open(app.log_file, encoding="utf-8") as f:
            raw = f.read()
        self.assertTrue(raw.endswith("\n"))
        self.assertTrue(raw.isascii())
        rows = [json.loads(row) for row in raw.splitlines()]
        self.assertEqual([row["start"] for row in rows], [iso(0), iso(300)])
        self.assertEqual(rows[1]["keypad"][0]["changed_by"], "Mikkel Ø")


class Recording(unittest.TestCase):
    def test_keypad_changed_by_changes_inside_the_window_are_kept_with_time_and_value(self):
        app = make_app()
        keypad(app, 1, "Before")
        sight(app, 5)
        keypad(app, 8, "Mikkel")
        keypad(app, 14, "Auto Lock")
        keypad(app, 20, None)
        advance(app, 185)
        keypad(app, 200, "After")
        (line,) = lines(app)
        self.assertEqual(line["keypad"], [{"t": iso(8), "changed_by": "Mikkel"},
                                          {"t": iso(14), "changed_by": "Auto Lock"},
                                          {"t": iso(20), "changed_by": None}])

    def test_a_window_without_keypad_changes_has_an_empty_list(self):
        app = make_app()
        sight(app, 5)
        advance(app, 185)
        self.assertEqual(lines(app)[0]["keypad"], [])

    def test_wifi_home_is_the_tracker_flip_inside_the_window(self):
        app = make_app()
        sight(app, 0)
        open_door(app, 20)
        wifi_home(app, 35)
        advance(app, 50)
        (line,) = lines(app)
        self.assertEqual((line["wifi_home"], line["outcome"]), (iso(35), "entered"))

    def test_only_the_first_flip_to_home_in_a_window_is_kept(self):
        app = make_app()
        sight(app, 0)
        wifi_home(app, 10)
        wifi_away(app, 20)
        wifi_home(app, 30)
        advance(app, 180)
        (line,) = lines(app)
        self.assertEqual(line["wifi_home"], iso(10))

    def test_a_flip_after_the_window_closed_is_not_in_the_line_and_not_missed_either(self):
        app = make_app()
        sight(app, 0)
        open_door(app, 20)
        advance(app, 50)
        wifi_home(app, 60)
        (line,) = lines(app)
        self.assertEqual((line["kind"], line["wifi_home"]), ("arrival", None))


class ProxyOnline(unittest.TestCase):
    def window(self, up, sighting):
        app = make_app()
        if up is not None:
            proxy_up(app, up)
        sight(app, sighting)
        advance(app, sighting + 180)
        return lines(app)[0]["proxy_online"]

    def test_a_power_up_within_5_minutes_before_the_window_is_kept(self):
        self.assertEqual(self.window(0, 120), iso(0))
        self.assertEqual(self.window(0, 300), iso(0))

    def test_an_older_power_up_or_none_at_all_is_null(self):
        self.assertIsNone(self.window(0, 301))
        self.assertIsNone(self.window(None, 120))

    def test_a_power_up_after_the_window_opened_is_not_reported(self):
        app = make_app()
        sight(app, 10)
        proxy_up(app, 20)
        advance(app, 190)
        self.assertIsNone(lines(app)[0]["proxy_online"])

    def test_only_leaving_unavailable_or_missing_is_a_power_up(self):
        app = make_app()
        steps = [(None, "5", True), ("5", "65", False), ("65", "unavailable", False),
                 ("unavailable", "unknown", True), ("unknown", "3", False), ("unavailable", None, False),
                 (None, "unknown", True), ("unknown", "unavailable", False), ("unavailable", "7", True),
                 ("7", None, False)]
        for number, (old, new, powered) in enumerate(steps, start=1):
            before = app._proxy_online
            proxy_up(app, number * 10, old, new)
            with self.subTest(old=old, new=new):
                self.assertEqual(app._proxy_online != before, powered)
        self.assertEqual(app._proxy_online, T0 + 90)

    def test_the_proxy_coming_up_as_unknown_counts_and_its_first_value_later_does_not_move_it(self):
        app = make_app()
        proxy_up(app, 0, "unavailable", "unknown")
        proxy_up(app, 40, "unknown", "42")
        sight(app, 100)
        advance(app, 280)
        self.assertEqual(lines(app)[0]["proxy_online"], iso(0))

    def test_a_sighting_before_the_first_uptime_value_still_has_the_power_up(self):
        app = make_app()
        proxy_up(app, 0, "unavailable", "unknown")
        sight(app, 10)
        proxy_up(app, 60, "unknown", "58")
        advance(app, 190)
        self.assertEqual(lines(app)[0]["proxy_online"], iso(0))


class Missed(unittest.TestCase):
    def test_the_flip_home_after_a_long_absence_with_no_window_is_a_missed_arrival(self):
        app = make_app()
        wifi_home(app, 100)
        self.assertEqual(lines(app), [{"kind": "missed", "t": iso(100), "away_since": iso(-3600), "door_open": None,
                                       "proxy_online": None, "landing_seen": None,
                                       "last_landing_distance_state": "unknown"}])
        message = f"ArrivalUnlock: tracker home after 62 min away and no window opened, missed -> {app.log_file.name}"
        self.assertIn(("INFO", message), app.logged)

    def test_the_latest_door_opening_within_3_minutes_is_carried(self):
        for flip, expected in ((230, iso(50)), (231, None)):
            with self.subTest(flip=flip):
                app = make_app()
                open_door(app, 50)
                wifi_home(app, flip)
                self.assertEqual(lines(app)[0]["door_open"], expected)

    def test_the_landing_state_and_a_recent_power_up_are_carried(self):
        app = make_app()
        proxy_up(app, 10)
        app.states[DISTANCE] = "9.4"
        wifi_home(app, 100)
        self.assertEqual((lines(app)[0]["last_landing_distance_state"], lines(app)[0]["proxy_online"]),
                         ("9.4", iso(10)))

    def test_a_landing_sighting_while_not_armed_is_carried_for_5_minutes(self):
        for flip, expected in ((350, iso(50)), (351, None)):
            with self.subTest(flip=flip):
                app = make_app(states={LOCK: "unlocked"})
                sight(app, 50)
                self.assertEqual(app.scheduled, [])
                wifi_home(app, flip)
                (line,) = lines(app)
                self.assertEqual((line["kind"], line["landing_seen"]), ("missed", expected))

    def test_a_power_up_more_than_5_minutes_before_the_flip_is_null(self):
        app = make_app()
        proxy_up(app, 0)
        wifi_home(app, 301)
        self.assertIsNone(lines(app)[0]["proxy_online"])

    def test_an_absence_shorter_than_away_min_is_not_missed(self):
        app = make_app(states={TRACKER: "home"})
        wifi_away(app, 10)
        wifi_home(app, 609)
        self.assertEqual(lines(app), [])
        wifi_away(app, 700)
        wifi_home(app, 1300)
        self.assertEqual([(row["kind"], row["away_since"]) for row in lines(app)], [("missed", iso(700))])

    def test_a_window_opened_in_the_previous_5_minutes_means_not_missed(self):
        for flip, kinds in ((299, ["arrival"]), (301, ["arrival", "missed"])):
            with self.subTest(flip=flip):
                app = make_app()
                sight(app, 0)
                advance(app, 180)
                wifi_home(app, flip)
                self.assertEqual([row["kind"] for row in lines(app)], kinds)

    def test_a_flip_with_no_known_absence_is_not_missed(self):
        app = make_app(states={TRACKER: "home"})
        app._on_tracker(TRACKER, None, "unavailable", "home", {})
        self.assertEqual(lines(app), [])

    def test_a_state_between_away_and_home_restarts_the_absence_like_last_changed_does(self):
        app = make_app()
        advance(app, 100)
        app._on_tracker(TRACKER, None, "not_home", "unavailable", {})
        wifi_home(app, 200, old="unavailable")
        self.assertEqual(lines(app), [])


class MissingEntities(unittest.TestCase):
    def test_the_app_starts_and_waits_when_the_landing_entities_do_not_exist(self):
        app = make_app(missing=(AREA, DISTANCE, ONLINE))
        self.assertEqual((app.scheduled, lines(app), warnings(app)), ([], [], []))
        proxy_up(app, 10, None, "3")
        sight(app, 20, old=None)
        advance(app, 200)
        (line,) = lines(app)
        self.assertEqual((line["proxy_online"], line["would_unlock"]),
                         (iso(10), {"t": iso(20), "distance": None, "lock": "locked", "door": "off"}))

    def test_a_missed_line_says_null_for_a_landing_distance_that_does_not_exist_or_is_not_configured(self):
        for name, case in (("missing", {"missing": (DISTANCE, ONLINE)}), ("not configured", {"drop": ("landing_distance",)})):
            with self.subTest(name):
                app = make_app(**case)
                wifi_home(app, 100)
                (line,) = lines(app)
                self.assertEqual((line["kind"], line["last_landing_distance_state"], line["proxy_online"]),
                                 ("missed", None, None))

    def test_a_tracker_that_does_not_exist_at_start_has_no_absence_to_miss(self):
        app = make_app(missing=(TRACKER,))
        wifi_home(app, 100)
        self.assertEqual(lines(app), [])


class Stopping(unittest.TestCase):
    def test_stopping_closes_and_writes_the_open_window_as_is(self):
        app = make_app()
        sight(app, 0)
        open_door(app, 20)
        advance(app, 30)
        app.terminate()
        (line,) = lines(app)
        self.assertEqual((line["end"], line["outcome"], line["lead_s"]), (iso(30), "entered", 20))
        self.assertIsNone(app._win)
        advance(app, 5000)
        self.assertEqual(len(lines(app)), 1)

    def test_stopping_with_no_window_writes_nothing(self):
        app = make_app()
        app.terminate()
        self.assertFalse(os.path.exists(app.log_file))

    def test_an_unwritable_log_file_is_a_warning_not_a_crash(self):
        app = make_app(args={"log_file": os.path.join(_DIR.name, "missing", "arrival_unlock_log.jsonl")})
        sight(app, 0)
        advance(app, 180)
        wifi_home(app, 1000)
        self.assertEqual([m.split(",")[0] for m in warnings(app)],
                         ["ArrivalUnlock: window 1 lost", "ArrivalUnlock: missed record lost"])
        self.assertIsNone(app._win)
        for _, message in app.logged:
            self.assertTrue(message.isascii(), message)


class Config(unittest.TestCase):
    def test_the_arguments_default_and_override(self):
        app = make_app()
        self.assertEqual((app.away_s, app.window_s, app.landing_area), (600, 180, "Landing"))
        self.assertEqual(make_app(args={"log_file": None}).log_file,
                         Path(au.__file__).resolve().parent / "arrival_unlock_log.jsonl")
        app = make_app(args={"away_min": 1, "window_s": 30, "landing_area": "Stairs"}, away_for=59)
        sight(app, 0)
        self.assertEqual(app.scheduled, [])
        sight(app, 1)
        self.assertEqual([delay for delay, _ in app.scheduled], [30])
        self.assertEqual(app.listen_state.call_args_list[0].kwargs, {"new": "Stairs"})

    def test_a_window_that_is_not_positive_fails_at_start(self):
        for bad in (0, -5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make_app(args={"window_s": bad})

    def test_every_key_but_the_landing_distance_is_required(self):
        for key in ENTITIES:
            with self.subTest(key):
                if key == "landing_distance":
                    self.assertIsNone(make_app(drop=(key,)).distance_entity)
                else:
                    with self.assertRaises(KeyError):
                        make_app(drop=(key,))

    def test_it_listens_to_the_area_changing_to_the_landing_and_to_the_other_four_entities(self):
        app = make_app()
        self.assertEqual([(c.args[1], c.kwargs) for c in app.listen_state.call_args_list],
                         [(AREA, {"new": LANDING}), (ONLINE, {}), (TRACKER, {}), (DOOR, {"new": "on"}),
                          (KEYPAD, {"attribute": "changed_by"})])

    def test_the_absence_at_start_comes_from_the_trackers_last_changed(self):
        app = make_app(away_for=1800)
        self.assertEqual(app._away_since, T0 - 1800)
        self.assertIsNone(make_app(states={TRACKER: "home"})._away_since)
        self.assertIsNone(make_app(missing=(TRACKER,))._away_since)


class Clock(unittest.TestCase):
    @unittest.skipIf(COPENHAGEN is None, "no time zone database")
    def test_durations_stay_real_seconds_when_the_clock_goes_back_inside_a_window(self):
        app = make_app(tz=COPENHAGEN)
        t = datetime(2026, 10, 25, 0, 59, 50, tzinfo=timezone.utc).timestamp()
        app.attrs[(TRACKER, "last_changed")] = datetime.fromtimestamp(t - 3600, timezone.utc).isoformat()
        app.clock["now"] = t
        app._on_area(AREA, None, "unknown", LANDING, {})
        app.clock["now"] = t + 30
        app._on_door(DOOR, None, "off", "on", {})
        app.clock["now"] = t + 60
        app._on_expired({"wid": 1})
        (line,) = lines(app)
        self.assertEqual((line["start"], line["door_open"], line["end"], line["lead_s"]),
                         ("2026-10-25T02:59:50+02:00", "2026-10-25T02:00:20+01:00",
                          "2026-10-25T02:00:50+01:00", 30))


class Helpers(unittest.TestCase):
    def test_distance_of_is_a_finite_number_or_none(self):
        self.assertEqual([au.distance_of(v) for v in ("3.215", "8", " 2.5 ", 4, 0.0, "-1")],
                         [3.215, 8.0, 2.5, 4.0, 0.0, -1.0])
        for value in (None, "", "unknown", "unavailable", "x1", "nan", "inf", "-inf", math.nan, math.inf, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(au.distance_of(value))

    def test_epoch_of_reads_home_assistants_timestamps(self):
        expected = datetime(2026, 11, 10, 10, 0, 5, tzinfo=timezone.utc).timestamp()
        for raw in ("2026-11-10T10:00:05+00:00", "2026-11-10T10:00:05Z", "2026-11-10T11:00:05+01:00"):
            with self.subTest(raw):
                self.assertEqual(au.epoch_of(raw), expected)
        self.assertEqual(au.epoch_of("2026-11-10T10:00:05.123456+00:00"), expected + 0.123456)
        for raw in (None, "", "garbage", "2026-11-10T10:00:05", 5, "unknown"):
            with self.subTest(raw):
                self.assertIsNone(au.epoch_of(raw))

    def test_within_is_zero_to_the_limit_before_now(self):
        self.assertEqual([au.within(stamp, 1000, 300) for stamp in (1000, 700, 699, 1001, None)],
                         [1000, 700, None, None, None])
        self.assertEqual(au.within(0.0, 100, 300), 0.0)

    def test_armed_needs_all_four(self):
        self.assertTrue(au.armed("not_home", 0, "locked", "off", 600, 600))
        for args in (("home", 0, "locked", "off"), ("not_home", 1, "locked", "off"), ("not_home", None, "locked", "off"),
                     ("not_home", 0, "unlocked", "off"), ("not_home", 0, "locked", "on"),
                     ("not_home", 0, None, "off"), ("not_home", 0, "locked", None), ("not_home", 700, "locked", "off")):
            with self.subTest(args):
                self.assertFalse(au.armed(*args, 600, 600))


SWITCH = "input_boolean.arrival_unlock_phone"
BLE = "lock.yale_bt"
UNLOCKING = {"unlock_switch": SWITCH, "unlock_lock": BLE, "unlock_fallback": KEYPAD,
             "notify": "notify.mobile_app_phone", "name": "Claudia"}


def unlocking_app(states=None, **kw):
    return make_app(args={**UNLOCKING, **kw.pop("args", {})},
                    states={SWITCH: "on", BLE: "locked", **(states or {})}, **kw)


def services(app):
    return [(t, service, kw.get("entity_id")) for t, service, kw in app.calls]


class Unlocking(unittest.TestCase):
    def test_a_sighting_while_armed_and_switched_on_unlocks_the_bluetooth_lock_without_a_push(self):
        app = unlocking_app()
        sight(app, 5)
        self.assertEqual(services(app), [(5, "lock/unlock", BLE)])
        self.assertIn((60, {"wid": 1, "t": T0 + 5, "entity": BLE}), app.scheduled)

    def test_an_unlock_tells_the_timeline_who_arrived(self):
        app = unlocking_app()
        sight(app, 5)
        self.assertEqual(app.fired, [(5, "lock_unlocked_on_arrival", {"lock": BLE, "by": "Claudia"})])

    def test_a_failed_unlock_tells_the_timeline_nothing(self):
        app = unlocking_app()
        app.fail_service["lock/unlock"] = "timeout"
        sight(app, 5)
        self.assertEqual(app.fired, [])

    def test_nothing_is_called_without_the_switch_with_it_off_or_missing_or_when_not_armed(self):
        cases = {"no switch configured": make_app(),
                 "switch off": unlocking_app(states={SWITCH: "off"}),
                 "switch missing": unlocking_app(missing=(SWITCH,)),
                 "not armed": unlocking_app(states={LOCK: "unlocked"})}
        for name, app in cases.items():
            with self.subTest(name):
                sight(app, 5)
                open_door(app, 20)
                advance(app, 400)
                self.assertEqual(app.calls, [])

    def test_the_cloud_lock_is_used_when_the_bluetooth_lock_is_not_available(self):
        for name, app in {"unavailable": unlocking_app(states={BLE: "unavailable"}),
                          "unknown": unlocking_app(states={BLE: "unknown"}),
                          "missing": unlocking_app(missing=(BLE,))}.items():
            with self.subTest(name):
                sight(app, 0)
                self.assertEqual(services(app)[0], (0, "lock/unlock", KEYPAD))

    def test_without_a_fallback_the_bluetooth_lock_is_tried_anyway(self):
        app = unlocking_app(states={BLE: "unavailable"}, args={"unlock_fallback": None})
        sight(app, 0)
        self.assertEqual(services(app)[0], (0, "lock/unlock", BLE))

    def test_a_second_sighting_inside_the_window_does_not_unlock_again(self):
        app = unlocking_app()
        sight(app, 0)
        sight(app, 10)
        self.assertEqual([s for _, s, _ in services(app)].count("lock/unlock"), 1)

    def test_an_opened_door_is_not_relocked_and_the_line_carries_the_unlock(self):
        app = unlocking_app()
        sight(app, 0)
        open_door(app, 20)
        app.states[LOCK] = "unlocked"
        advance(app, 400)
        self.assertEqual([s for _, s, _ in services(app)], ["lock/unlock"])
        (line,) = lines(app)
        self.assertEqual((line["outcome"], line["unlock"], line["relocked"]),
                         ("entered", {"t": iso(0), "entity": BLE, "error": None}, None))

    def test_a_door_not_opened_within_relock_s_is_locked_again_and_pushed(self):
        app = unlocking_app()
        sight(app, 0)
        app.states[LOCK] = "unlocked"
        advance(app, 400)
        self.assertEqual(services(app)[1:], [(60, "lock/lock", BLE), (60, "notify/mobile_app_phone", None)])
        self.assertEqual(app.calls[2][2]["title"], "Apartment door locked again")
        (line,) = lines(app)
        self.assertEqual((line["outcome"], line["relocked"]), ("no_door", iso(60)))

    def test_the_relock_uses_the_entity_that_unlocked(self):
        app = unlocking_app(states={BLE: "unavailable"})
        sight(app, 0)
        app.states[LOCK] = "unlocked"
        advance(app, 60)
        self.assertEqual(services(app)[1], (60, "lock/lock", KEYPAD))

    def test_no_relock_when_already_locked_or_the_door_stands_open(self):
        for name, states in {"locked": {LOCK: "locked"}, "door open": {LOCK: "unlocked", DOOR: "on"}}.items():
            with self.subTest(name):
                app = unlocking_app()
                sight(app, 0)
                app.states.update(states)
                advance(app, 400)
                self.assertNotIn("lock/lock", [s for _, s, _ in services(app)])

    def test_a_door_opening_before_the_unlock_does_not_cancel_the_relock(self):
        app = unlocking_app()
        open_door(app, 0)
        app.states[DOOR] = "off"
        sight(app, 100)
        app.states[LOCK] = "unlocked"
        advance(app, 160)
        self.assertEqual(services(app)[1], (160, "lock/lock", BLE))

    def test_a_failed_unlock_is_a_warning_with_no_push_and_still_gets_its_relock_check(self):
        app = unlocking_app()
        app.fail_service["lock/unlock"] = "timeout"
        sight(app, 0)
        self.assertEqual(app.calls, [])
        self.assertTrue(any("unlocking lock.yale_bt failed" in w for w in warnings(app)))
        app.states[LOCK] = "unlocked"
        advance(app, 400)
        self.assertEqual([s for _, s, _ in services(app)], ["lock/lock", "notify/mobile_app_phone"])
        self.assertEqual(lines(app)[0]["unlock"]["error"], "RuntimeError: timeout")

    def test_a_failed_push_or_relock_is_only_a_warning(self):
        app = unlocking_app()
        app.fail_service["notify/mobile_app_phone"] = "offline"
        sight(app, 0)
        app.states[LOCK] = "unlocked"
        advance(app, 400)
        self.assertEqual([s for _, s, _ in services(app)], ["lock/unlock", "lock/lock"])
        self.assertEqual(len(warnings(app)), 1)

        app = unlocking_app()
        app.fail_service["lock/lock"] = "jammed"
        sight(app, 0)
        app.states[LOCK] = "unlocked"
        advance(app, 400)
        self.assertEqual(services(app), [(0, "lock/unlock", BLE)])
        self.assertEqual(len(warnings(app)), 1)
        self.assertIsNone(lines(app)[0]["relocked"])

    def test_no_notify_configured_means_no_push(self):
        app = unlocking_app(args={"notify": None})
        sight(app, 0)
        app.states[LOCK] = "unlocked"
        advance(app, 400)
        self.assertEqual([s for _, s, _ in services(app)], ["lock/unlock", "lock/lock"])

    def test_relock_s_must_be_positive_and_shorter_than_the_window(self):
        for bad in (0, -1, 180, 200):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                unlocking_app(args={"relock_s": bad})
        self.assertEqual(unlocking_app(args={"relock_s": 30}).relock_s, 30)

    def test_a_switch_needs_the_lock_to_unlock(self):
        with self.assertRaises(KeyError):
            make_app(args={"unlock_switch": SWITCH})


class Services(unittest.TestCase):
    def test_the_app_calls_only_lock_unlock_lock_its_push_and_the_timeline_event(self):
        tree = ast.parse(Path(au.__file__).read_text(encoding="utf-8"))
        (cls,) = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        own = {node.name for node in cls.body if isinstance(node, ast.FunctionDef)}
        calls = [node for node in ast.walk(cls)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"]
        self.assertLessEqual({c.func.attr for c in calls} - own,
                             {"listen_state", "get_state", "run_in", "get_now", "log", "call_service", "fire_event"})
        targets = [ast.unparse(c.args[0]) for c in calls if c.func.attr == "call_service"]
        self.assertEqual(sorted(targets), ["'lock/lock'", "'lock/unlock'", "self.notify_service"])


if __name__ == "__main__":
    unittest.main()
