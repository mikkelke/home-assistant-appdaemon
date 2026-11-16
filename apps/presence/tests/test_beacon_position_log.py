# tests/test_beacon_position_log.py - BeaconPositionLog: the window around a robot run, the line a
# sample appends, which adverts it carries and what a failed call leaves in it.
# The app is driven through its real initialize() with a fake clock, a fake scheduler and fake
# roborock and bermuda services. The fake app has no set_state, notify or other service, so
# anything but reading states and those two calls would fail the test; every AppDaemon call is also
# checked to be made without the app's lock held (a sample is written on an executor thread that
# holds it, and an AppDaemon call waits for the event loop).
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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

import beacon_position_log as bpl  # noqa: E402

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc).timestamp()
MONO0 = 7_000_000.0  # time.monotonic() at T0

VACUUM = "vacuum.rober2"
ROOM = "sensor.rober2_current_room"
BEACON = "7c:d9:f4:14:23:77"
COFFEE, DINING, BATH = "9c:cc:01:33:d8:d6", "9c:cc:01:33:d5:c6", "10:51:db:8e:da:8a"
SCANNERS = {COFFEE: "BLE proxy coffee bar", DINING: "BLE proxy dining room", BATH: "Bathroom display"}
POSITION_CALL = ("roborock/get_vacuum_current_position",
                 {"entity_id": VACUUM, "return_response": True, "suppress_log_messages": True})
DUMP_CALL = ("bermuda/dump_devices", {"addresses": BEACON.upper(), "redact": False, "return_response": True,
                                      "suppress_log_messages": True})

_DIR = tempfile.TemporaryDirectory()
_SEQ = itertools.count()
APPS = []


def new_log_file():
    return os.path.join(_DIR.name, f"beacon_position_log_{next(_SEQ)}.jsonl")


def envelope(response):
    return {"id": 7, "type": "result", "success": True, "ad_status": "OK", "ad_duration": 0.2,
            "result": {"context": {"id": "01"}, "response": response}}


def rises(error):
    def fail():
        raise error
    return fail


class CountingLock:
    def __init__(self):
        self.held = 0

    def __enter__(self):
        self.held += 1

    def __exit__(self, *exc):
        self.held -= 1


class Bermuda:
    """bermuda.dump_devices for the beacon: the adverts each scanner heard, of which, like Bermuda,
    only the newest ten are kept, newest first, on the monotonic clock plus `skew`."""

    def __init__(self, app):
        self.app = app
        self.area = "Kitchen"
        self.names = dict(SCANNERS)
        self.skew = 0
        self.heard = {scanner: [] for scanner in SCANNERS}

    def hear(self, scanner, rssi, at):
        """An advert heard `at` seconds after T0."""
        self.heard[scanner].append((MONO0 + at, rssi))

    def dump(self):
        now = self.app._monotonic()
        adverts = {}
        for scanner, heard in self.heard.items():
            kept = sorted(h for h in heard if h[0] <= now)[-10:][::-1]
            if kept:
                adverts[f"{BEACON}__{scanner}"] = {
                    "scanner_address": scanner, "name": self.names[scanner],
                    "hist_stamp": [s + self.skew for s, _ in kept], "hist_rssi": [r for _, r in kept]}
        return envelope({BEACON: {"address": BEACON, "area_name": self.area, "last_seen": now,
                                  "adverts": adverts}})


def make_app(args=None, state="docked"):
    """BeaconPositionLog with initialize() actually run and the AppDaemon primitives faked: a
    settable clock (seconds after T0), a scheduler that only records, tasks that run when asked,
    and roborock and bermuda services that answer app.position() and app.dump()."""
    app = bpl.BeaconPositionLog.__new__(bpl.BeaconPositionLog)
    app.args = {"log_file": new_log_file(), **(args or {})}
    app.states = {VACUUM: state, ROOM: "Kitchen1"}
    app.clock = {"now": T0}
    app.logged, app.timers, app.tasks, app.service_calls, app.scheduled, app.canceled = [], [], [], [], [], []
    app.violations = []
    app.delay = 0
    app.bermuda = Bermuda(app)
    app.dump = app.bermuda.dump
    app.position = lambda: envelope({VACUUM: {"x": 1000 + round(100 * (app.clock["now"] - T0)), "y": 5000}})
    app.order = itertools.count()
    handles = itertools.count(1)

    def api(name):
        if app._lock.held:
            app.violations.append(name)

    def add_timer(callback, due, kw, every=None):
        handle = f"timer{next(handles)}"
        app.timers.append({"handle": handle, "due": due, "order": next(app.order), "cb": callback, "kw": kw,
                           "every": every})
        return handle

    def get_state(entity, **kw):
        api("get_state")
        return app.states.get(entity)

    def run_in(callback, delay, **kw):
        api("run_in")
        app.scheduled.append(("run_in", callback.__name__, delay, kw))
        return add_timer(callback, app.clock["now"] + delay, kw)

    def run_every(callback, start, interval, **kw):
        api("run_every")
        app.scheduled.append(("run_every", start, interval, kw))
        assert start == "immediate"
        return add_timer(callback, app.clock["now"], kw, every=interval)

    def cancel_timer(handle, silent=False):
        api("cancel_timer")
        app.canceled.append(handle)
        app.timers[:] = [t for t in app.timers if t["handle"] != handle]
        return True

    def create_task(coro):
        api("create_task")
        app.tasks.append(coro)

    async def call_service(service, **kw):
        api("call_service")
        app.service_calls.append((service, kw))
        if app.delay:
            await asyncio.sleep(app.delay)
        return app.position() if service == bpl.POSITION_SERVICE else app.dump()

    async def run_in_executor(func, *args):
        api("run_in_executor")
        return func(*args)

    app.get_state = get_state
    app._epoch = lambda: app.clock["now"]
    app._monotonic = lambda: MONO0 + app.clock["now"] - T0
    app.log = lambda message, level="INFO": app.logged.append((level, message))
    app.listen_state = MagicMock()
    app.run_in = run_in
    app.run_every = run_every
    app.cancel_timer = cancel_timer
    app.create_task = create_task
    app.call_service = call_service
    app.run_in_executor = run_in_executor
    with patch.object(bpl.threading, "RLock", CountingLock):
        app.initialize()
    APPS.append(app)
    return app


def run_tasks(app):
    while app.tasks:
        asyncio.run(app.tasks.pop(0))


def advance(app, seconds):
    """Fires every timer due by T0 + seconds in order, running the samples they start."""
    target = T0 + seconds
    assert target >= app.clock["now"]
    while True:
        due = [t for t in app.timers if t["due"] <= target]
        if not due:
            break
        timer = min(due, key=lambda t: (t["due"], t["order"]))
        app.clock["now"] = max(app.clock["now"], timer["due"])
        if timer["every"] is None:
            app.timers.remove(timer)
        else:
            timer["due"] += timer["every"]
            timer["order"] = next(app.order)
        timer["cb"](dict(timer["kw"]))
        run_tasks(app)
    app.clock["now"] = target


def set_vacuum(app, seconds, state):
    advance(app, seconds)
    old, app.states[VACUUM] = app.states[VACUUM], state
    app._on_vacuum(VACUUM, None, old, state, {})
    advance(app, seconds)


def lines(app):
    try:
        with open(app.log_file, encoding="utf-8") as f:
            return [json.loads(row) for row in f.read().splitlines()]
    except FileNotFoundError:
        return []


def samples(app):
    return [row for row in lines(app) if "event" not in row]


def events(app):
    return [row for row in lines(app) if "event" in row]


def adverts(app, scanner):
    return [advert for row in samples(app) for advert in row["adv"].get(scanner, [])]


def warnings(app):
    return [message for level, message in app.logged if level == "WARNING"]


def closed(app, text):
    return ("INFO", f"BeaconPositionLog window closed: {text} -> {app.log_file.name}")


def tearDownModule():
    _DIR.cleanup()


class Case(unittest.TestCase):
    def tearDown(self):
        apps, APPS[:] = list(APPS), []
        for app in apps:
            self.assertEqual(app.violations, [])


class Window(Case):
    def test_it_listens_to_the_vacuum_state(self):
        app = make_app()
        self.assertEqual([c.args for c in app.listen_state.call_args_list], [(app._on_vacuum, VACUUM)])

    def test_cleaning_opens_a_window_sampled_every_interval_and_closed_tail_s_after_docked(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 52, "docked")
        advance(app, 111)
        self.assertEqual([row["t"] for row in samples(app)], [T0 + 5 * i for i in range(23)])
        self.assertEqual([row["event"] for row in events(app)], ["start"])
        advance(app, 112)
        self.assertEqual(events(app), [{"t": T0, "event": "start", "scanners": {}},
                                       {"t": T0 + 112, "event": "end"}])
        self.assertEqual([row.get("event") for row in lines(app)], ["start"] + [None] * 23 + ["end"])
        advance(app, 900)
        self.assertEqual(len(lines(app)), 25)
        self.assertEqual(len(app.service_calls), 46)
        self.assertEqual(app.timers, [])
        self.assertEqual(app.canceled, ["timer1"])
        self.assertIn(("INFO", f"BeaconPositionLog window opened: {VACUUM} is cleaning"), app.logged)
        self.assertIn(closed(app, "23 samples"), app.logged)

    def test_the_vacuum_state_is_logged_in_the_tail_too(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 7, "returning")
        set_vacuum(app, 22, "docked")
        advance(app, 100)
        self.assertEqual([row["vacuum"] for row in samples(app)],
                         ["cleaning"] * 2 + ["returning"] * 3 + ["docked"] * 12)

    def test_returning_keeps_the_window_open_until_it_docks(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 31, "returning")
        advance(app, 300)
        self.assertEqual([row["event"] for row in events(app)], ["start"])
        self.assertEqual(len(samples(app)), 61)
        set_vacuum(app, 302, "docked")
        advance(app, 363)
        self.assertEqual(events(app)[-1], {"t": T0 + 362, "event": "end"})

    def test_a_vacuum_active_again_inside_the_tail_keeps_the_window_and_cancels_the_close(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 22, "paused")
        set_vacuum(app, 52, "cleaning")
        self.assertEqual(app.canceled, ["timer2"])
        self.assertIn(("INFO", f"BeaconPositionLog window kept: {VACUUM} is cleaning again"), app.logged)
        advance(app, 90)
        self.assertEqual([row["event"] for row in events(app)], ["start"])
        set_vacuum(app, 92, "docked")
        advance(app, 153)
        self.assertEqual(events(app), [{"t": T0, "event": "start", "scanners": {}}, {"t": T0 + 152, "event": "end"}])
        self.assertEqual(app.canceled, ["timer2", "timer1"])

    def test_a_pause_longer_than_the_tail_ends_the_window_and_the_next_run_opens_a_new_one(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 22, "paused")
        advance(app, 83)
        self.assertEqual([(row["event"], row["t"]) for row in events(app)], [("start", T0), ("end", T0 + 82)])
        advance(app, 199)
        self.assertEqual(len(lines(app)), 19)
        set_vacuum(app, 200, "cleaning")
        set_vacuum(app, 222, "docked")
        advance(app, 283)
        self.assertEqual([(row["event"], row["t"]) for row in events(app)],
                         [("start", T0), ("end", T0 + 82), ("start", T0 + 200), ("end", T0 + 282)])

    def test_nothing_is_sampled_or_written_while_docked_or_in_any_other_state(self):
        app = make_app(state="docked")
        advance(app, 600)
        for i, state in enumerate(("idle", "paused", "error", "unavailable", "unknown", "docked")):
            set_vacuum(app, 700 + 100 * i, state)
        app._on_vacuum(VACUUM, None, "docked", None, {})
        advance(app, 5000)
        self.assertEqual((app.service_calls, app.timers, app.scheduled, app.tasks), ([], [], [], []))
        self.assertFalse(os.path.exists(app.log_file))

    def test_a_vacuum_already_cleaning_when_the_app_starts_opens_a_window(self):
        app = make_app(state="cleaning")
        self.assertEqual(app.scheduled, [("run_every", "immediate", 5.0, {"wid": 1})])
        advance(app, 12)
        self.assertEqual([row["t"] for row in samples(app)], [T0, T0 + 5, T0 + 10])

    def test_a_repeated_state_callback_neither_extends_the_tail_nor_opens_a_second_window(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        app._on_vacuum(VACUUM, None, "cleaning", "cleaning", {})
        set_vacuum(app, 22, "docked")
        advance(app, 40)
        app._on_vacuum(VACUUM, None, "docked", "idle", {})
        advance(app, 83)
        self.assertEqual([(row["event"], row["t"]) for row in events(app)], [("start", T0), ("end", T0 + 82)])
        self.assertEqual([s[0] for s in app.scheduled], ["run_every", "run_in"])

    def test_the_timers_are_an_immediate_run_every_and_one_run_in_per_tail(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 10, "docked")
        self.assertEqual(app.scheduled, [("run_every", "immediate", 5.0, {"wid": 1}),
                                         ("run_in", "_on_tail", 60.0, {"wid": 1, "token": 2})])

    def test_a_zero_tail_closes_at_once(self):
        app = make_app(args={"tail_s": 0})
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 12, "docked")
        self.assertEqual(events(app)[-1], {"t": T0 + 12, "event": "end"})
        self.assertEqual(app.timers, [])

    def test_the_arguments_default_and_override(self):
        app = make_app()
        self.assertEqual((app.vacuum_entity, app.room_entity, app.beacon, app.active_states, app.tail_s,
                          app.interval_s),
                         (VACUUM, ROOM, BEACON, frozenset({"cleaning", "returning"}), 60, 5))
        self.assertEqual(make_app(args={"log_file": None}).log_file,
                         Path(bpl.__file__).resolve().parent / "beacon_position_log.jsonl")
        self.assertEqual(make_app(args={"active_states": "cleaning"}).active_states, frozenset({"cleaning"}))
        app = make_app(args={"vacuum": "vacuum.other", "room": "sensor.other", "beacon": "AA:BB:CC:00:00:01",
                             "active_states": ["mopping"], "tail_s": 2, "interval_s": 1.5})
        self.assertEqual([c.args[1] for c in app.listen_state.call_args_list], ["vacuum.other"])
        app.states.update({"vacuum.other": "mopping", "sensor.other": "Hall"})
        app._on_vacuum("vacuum.other", None, "docked", "mopping", {})
        advance(app, 3)
        self.assertEqual([row["t"] for row in samples(app)], [T0, T0 + 1.5, T0 + 3])
        self.assertEqual({row["room"] for row in samples(app)}, {"Hall"})
        self.assertEqual({kw["addresses"] for service, kw in app.service_calls if service == DUMP_CALL[0]},
                         {"AA:BB:CC:00:00:01"})
        app._on_vacuum("vacuum.other", None, "mopping", "cleaning", {})
        self.assertEqual(app.scheduled[-1], ("run_in", "_on_tail", 2.0, {"wid": 1, "token": 2}))

    def test_an_interval_that_is_not_positive_or_a_negative_tail_fails_at_start(self):
        for bad in ({"interval_s": 0}, {"interval_s": -5}, {"tail_s": -1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make_app(args=bad)


class Samples(Case):
    def test_a_sample_line_holds_position_room_state_area_and_adverts(self):
        app = make_app()
        app.states[ROOM] = "Hallway"
        app.bermuda.hear(COFFEE, -61, at=2.26)
        app.bermuda.hear(COFFEE, -58, at=3.81)
        app.bermuda.hear(DINING, -80, at=4.41)
        set_vacuum(app, 0, "cleaning")
        advance(app, 5)
        first, second = samples(app)
        self.assertEqual(first, {"t": T0, "x": 1000, "y": 5000, "room": "Hallway", "vacuum": "cleaning",
                                 "bermuda_area": "Kitchen", "adv": {}})
        self.assertEqual(second, {"t": T0 + 5, "x": 1500, "y": 5000, "room": "Hallway", "vacuum": "cleaning",
                                  "bermuda_area": "Kitchen",
                                  "adv": {COFFEE: [[round(T0 + 2.26, 1), -61], [round(T0 + 3.81, 1), -58]],
                                          DINING: [[round(T0 + 4.41, 1), -80]]}})
        self.assertEqual(list(second), ["t", "x", "y", "room", "vacuum", "bermuda_area", "adv"])

    def test_a_sample_without_a_room_or_an_area_logs_null_for_them(self):
        app = make_app()
        app.states.pop(ROOM)
        app.bermuda.area = None
        set_vacuum(app, 0, "cleaning")
        (row,) = samples(app)
        self.assertEqual((row["room"], row["bermuda_area"], row["vacuum"]), (None, None, "cleaning"))

    def test_both_services_are_called_for_the_vacuum_and_the_beacon_with_a_response_asked_for(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        self.assertEqual(sorted(app.service_calls), sorted([POSITION_CALL, DUMP_CALL]))

    def test_the_first_sample_is_preceded_by_a_start_line_naming_every_scanner_in_its_dump(self):
        app = make_app()
        app.bermuda.hear(COFFEE, -60, at=-3)
        app.bermuda.hear(DINING, -70, at=-100)
        app.bermuda.hear(BATH, -90, at=-300)
        set_vacuum(app, 0, "cleaning")
        advance(app, 5)
        rows = lines(app)
        self.assertEqual(rows[0], {"t": T0, "event": "start", "scanners": SCANNERS})
        self.assertEqual([row.get("event") for row in rows], ["start", None, None])

    def test_each_window_has_its_own_start_line_timed_at_its_open(self):
        app = make_app()
        set_vacuum(app, 3, "cleaning")
        set_vacuum(app, 20, "docked")
        advance(app, 80)
        set_vacuum(app, 300.2, "cleaning")
        set_vacuum(app, 320, "docked")
        advance(app, 400)
        self.assertEqual([(row["event"], row["t"]) for row in events(app)],
                         [("start", T0 + 3), ("end", T0 + 80), ("start", T0 + 300.2), ("end", T0 + 380)])

    def test_the_file_holds_one_compact_ascii_json_object_per_line_appended_across_windows(self):
        app = make_app()
        app.bermuda.names[COFFEE] = "Køkken proxy"
        app.bermuda.hear(COFFEE, -59, at=-1)
        app.bermuda.hear(COFFEE, -60, at=0.5)
        self.assertFalse(os.path.exists(app.log_file))
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 12, "docked")
        advance(app, 100)
        set_vacuum(app, 200, "cleaning")
        advance(app, 205)
        app.terminate()
        with open(app.log_file, encoding="utf-8") as f:
            raw = f.read()
        self.assertTrue(raw.isascii() and raw.endswith("\n"))
        self.assertNotIn(" ", raw.replace("K\\u00f8kken proxy", ""))
        self.assertEqual(json.loads(raw.splitlines()[0])["scanners"], {COFFEE: "Køkken proxy"})
        self.assertEqual([row["event"] for row in events(app)], ["start", "end", "start", "end"])


class Adverts(Case):
    def test_overlapping_histories_write_every_advert_exactly_once_in_order(self):
        app = make_app()
        for i in range(1, 31):
            app.bermuda.hear(COFFEE, -50 - i, at=i - 0.5)
        dining = (3.3, 10.3, 17.3, 24.3)
        for i, at in enumerate(dining):
            app.bermuda.hear(DINING, -80 - i, at=at)
        set_vacuum(app, 0, "cleaning")
        advance(app, 30)
        self.assertEqual(adverts(app, COFFEE), [[round(T0 + i - 0.5, 1), -50 - i] for i in range(1, 31)])
        self.assertEqual(adverts(app, DINING), [[round(T0 + at, 1), -80 - i] for i, at in enumerate(dining)])
        self.assertEqual([len(row["adv"].get(COFFEE, [])) for row in samples(app)], [0] + [5] * 6)
        self.assertEqual([sorted(row["adv"]) for row in samples(app)][:4],
                         [[], sorted([COFFEE, DINING]), [COFFEE], sorted([COFFEE, DINING])])

    def test_adverts_from_before_the_window_opened_are_not_written(self):
        app = make_app()
        for at in (-300, -20, -5, -1):
            app.bermuda.hear(COFFEE, -70, at=at)
        app.bermuda.hear(COFFEE, -60, at=2.2)
        set_vacuum(app, 0, "cleaning")
        advance(app, 10)
        self.assertEqual(adverts(app, COFFEE), [[round(T0 + 2.2, 1), -60]])

    def test_a_scanner_that_first_hears_the_beacon_mid_window_is_written_when_it_does(self):
        app = make_app()
        app.bermuda.hear(COFFEE, -60, at=-1)
        app.bermuda.hear(COFFEE, -61, at=1)
        app.bermuda.hear(BATH, -75, at=17.2)
        set_vacuum(app, 0, "cleaning")
        advance(app, 20)
        self.assertEqual(events(app)[0]["scanners"], {COFFEE: SCANNERS[COFFEE]})
        self.assertEqual(adverts(app, BATH), [[round(T0 + 17.2, 1), -75]])

    def test_a_new_window_writes_nothing_heard_before_it_opened(self):
        app = make_app()
        for at, rssi in ((2.2, -62), (100.2, -63), (205.2, -64)):
            app.bermuda.hear(COFFEE, rssi, at=at)
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 12, "docked")
        advance(app, 80)
        set_vacuum(app, 200, "cleaning")
        advance(app, 210)
        app.terminate()
        self.assertEqual(adverts(app, COFFEE), [[round(T0 + 2.2, 1), -62], [round(T0 + 205.2, 1), -64]])

    def test_the_epoch_is_now_minus_the_age_to_a_tenth_of_a_second(self):
        app = make_app()
        for at, rssi in ((1.04, -61), (1.26, -62), (4.31, -63), (4.99, -64)):
            app.bermuda.hear(COFFEE, rssi, at=at)
        set_vacuum(app, 0, "cleaning")
        advance(app, 5)
        self.assertEqual(adverts(app, COFFEE), [[T0 + 1.0, -61], [T0 + 1.3, -62], [T0 + 4.3, -63], [T0 + 5.0, -64]])

    def test_stamps_not_on_this_clock_are_not_written_and_warned_once_per_window(self):
        for skew in (1_000_000, -1_000_000):
            with self.subTest(skew=skew):
                app = make_app()
                app.bermuda.skew = skew
                app.bermuda.hear(COFFEE, -60, at=1)
                set_vacuum(app, 0, "cleaning")
                advance(app, 20)
                self.assertEqual([row["adv"] for row in samples(app)], [{}] * 5)
                self.assertTrue(all(row["x"] is not None for row in samples(app)))
                self.assertEqual(len(warnings(app)), 1)
                self.assertIn("monotonic clock", warnings(app)[0])
                app.bermuda.skew = 0
                advance(app, 25)
                self.assertEqual(adverts(app, COFFEE), [[round(T0 + 1, 1), -60]])
                set_vacuum(app, 30, "docked")
                advance(app, 100)
                set_vacuum(app, 200, "cleaning")
                app.bermuda.skew = skew
                advance(app, 210)
                self.assertEqual(len(warnings(app)), 2)
                app.terminate()


class Failures(Case):
    def one_sample(self, app, **answers):
        for name, answer in answers.items():
            setattr(app, name, answer if callable(answer) else (lambda answer=answer: answer))
        set_vacuum(app, 0, "cleaning")
        (row,) = samples(app)
        return row

    def test_a_failed_position_call_logs_nulls_and_still_logs_everything_else(self):
        cases = {
            "raises": rises(RuntimeError("roborock is down")),
            "refused": {"success": False, "error": {"code": "home_assistant_error", "message": "no position"}},
            "timed out": {"success": False, "ad_status": "TIMEOUT", "ad_duration": 10.0},
            "other entity": envelope({"vacuum.other": {"x": 1, "y": 2}}),
            "no coordinates": envelope({VACUUM: {}}),
            "junk coordinates": envelope({VACUUM: {"x": "31875", "y": None}}),
            "bool and nan": envelope({VACUUM: {"x": True, "y": math.nan}}),
            "only x": envelope({VACUUM: {"x": 31875}}),
            "nothing": None,
        }
        for name, answer in cases.items():
            with self.subTest(name):
                app = make_app()
                app.bermuda.hear(COFFEE, -60, at=-1)
                row = self.one_sample(app, position=answer)
                self.assertEqual((row["x"], row["y"]), (None, None))
                self.assertEqual((row["t"], row["room"], row["vacuum"], row["bermuda_area"]),
                                 (T0, "Kitchen1", "cleaning", "Kitchen"))
                (warning,) = warnings(app)
                self.assertIn("roborock.get_vacuum_current_position", warning)
                app.terminate()
                self.assertIn(closed(app, "1 samples (1 without a position)"), app.logged)

    def test_a_working_position_call_after_a_failed_one_logs_its_position_again(self):
        app = make_app()
        good = app.position
        app.position = rises(RuntimeError("roborock is down"))
        set_vacuum(app, 0, "cleaning")
        app.position = good
        advance(app, 5)
        self.assertEqual([(row["x"], row["y"]) for row in samples(app)], [(None, None), (1500, 5000)])

    def test_a_failed_dump_logs_an_empty_adv_and_no_area_but_keeps_the_position(self):
        cases = {
            "raises": rises(RuntimeError("bermuda is down")),
            "refused": {"success": False, "error": {"code": "not_found", "message": "Service not found"}},
            "no devices": envelope({}),
            "other device": envelope({"aa:bb:cc:00:00:01": {"area_name": "Hall", "adverts": {}}}),
            "not a device": envelope({BEACON: "no"}),
            "nothing": None,
        }
        for name, answer in cases.items():
            with self.subTest(name):
                app = make_app()
                row = self.one_sample(app, dump=answer)
                self.assertEqual((row["x"], row["y"], row["adv"], row["bermuda_area"]), (1000, 5000, {}, None))
                self.assertEqual((row["room"], row["vacuum"]), ("Kitchen1", "cleaning"))
                (warning,) = warnings(app)
                self.assertIn("bermuda.dump_devices", warning)
                self.assertEqual(lines(app)[0], {"t": T0, "event": "start", "scanners": {}})
                app.terminate()
                self.assertIn(closed(app, "1 samples (1 without a dump)"), app.logged)

    def test_a_beacon_listed_in_capitals_is_still_found(self):
        app = make_app()
        good = app.bermuda.dump
        app.dump = lambda: {"result": {"response": {key.upper(): value
                                                    for key, value in good()["result"]["response"].items()}}}
        row = self.one_sample(app)
        self.assertEqual(row["bermuda_area"], "Kitchen")
        self.assertEqual(warnings(app), [])

    def test_adverts_missed_by_a_failed_dump_are_written_by_the_next_one(self):
        app = make_app()
        for i in range(1, 30):
            app.bermuda.hear(COFFEE, -50 - i, at=i - 0.5)
        calls = itertools.count()
        good = app.bermuda.dump

        def flaky():
            if next(calls) in (1, 3):
                raise RuntimeError("bermuda is down")
            return good()

        app.dump = flaky
        set_vacuum(app, 0, "cleaning")
        advance(app, 30)
        self.assertEqual([len(row["adv"].get(COFFEE, [])) for row in samples(app)], [0, 0, 10, 0, 10, 5, 4])
        self.assertEqual(adverts(app, COFFEE), [[round(T0 + i - 0.5, 1), -50 - i] for i in range(1, 30)])
        self.assertEqual(len(warnings(app)), 1)

    def test_each_kind_of_failure_is_warned_once_per_window_and_counted_when_it_closes(self):
        app = make_app()
        app.position = rises(RuntimeError("roborock is down"))
        app.dump = rises(RuntimeError("bermuda is down"))
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 22, "docked")
        advance(app, 83)
        self.assertEqual(len(warnings(app)), 2)
        self.assertIn("roborock is down", warnings(app)[0])
        self.assertIn("bermuda is down", warnings(app)[1])
        self.assertIn(closed(app, "17 samples (17 without a position, 17 without a dump)"), app.logged)
        set_vacuum(app, 200, "cleaning")
        advance(app, 206)
        self.assertEqual(len(warnings(app)), 4)
        app.terminate()

    def test_a_call_that_hangs_is_cut_off_at_the_timeout(self):
        app = make_app()
        app.delay = 5
        with patch.object(bpl, "CALL_TIMEOUT_S", 0.01):
            set_vacuum(app, 0, "cleaning")
            advance(app, 11)
        self.assertEqual([(row["x"], row["y"], row["adv"], row["bermuda_area"]) for row in samples(app)],
                         [(None, None, {}, None)] * 3)
        self.assertEqual(len(warnings(app)), 2)
        self.assertIn("TimeoutError", warnings(app)[0])

    def test_a_tick_is_skipped_while_the_previous_sample_is_still_running(self):
        app = make_app()
        real, held = app.create_task, []
        app.create_task = held.append
        set_vacuum(app, 0, "cleaning")
        advance(app, 12)
        self.assertEqual(len(held), 1)
        asyncio.run(held.pop())
        app.create_task = real
        set_vacuum(app, 12, "docked")
        advance(app, 80)
        self.assertEqual([row["t"] for row in samples(app)], [T0] + [T0 + 15 + 5 * i for i in range(12)])
        self.assertIn(closed(app, "13 samples (2 ticks skipped)"), app.logged)

    def test_a_sample_that_could_not_be_started_does_not_block_the_next_tick(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        real = app.create_task

        def broken(coro):
            coro.close()
            raise RuntimeError("no loop")

        app.create_task = broken
        with self.assertRaises(RuntimeError):
            app._on_tick({"wid": 1})
        app.create_task = real
        advance(app, 5)
        self.assertEqual([row["t"] for row in samples(app)], [T0, T0 + 5])

    def test_a_sample_that_answers_after_the_window_closed_is_dropped_and_leaves_no_lines(self):
        app = make_app()
        held = []
        app.create_task = held.append
        set_vacuum(app, 0, "cleaning")
        app.terminate()
        asyncio.run(held.pop())
        self.assertEqual(lines(app), [])
        self.assertFalse(os.path.exists(app.log_file))

    def test_a_sample_that_answers_after_the_end_line_is_not_written_behind_it(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        real, held = app.create_task, []
        app.create_task = held.append
        advance(app, 5)
        app.terminate()
        asyncio.run(held.pop())
        app.create_task = real
        self.assertEqual([row.get("event") for row in lines(app)], ["start", None, "end"])

    def test_an_unwritable_log_file_loses_that_sample_not_the_adverts_or_the_window(self):
        missing = os.path.join(_DIR.name, f"missing{next(_SEQ)}")
        app = make_app(args={"log_file": os.path.join(missing, "beacon_position_log.jsonl")})
        heard = (1, 3, 5, 7, 9, 11, 13)
        for at in heard:
            app.bermuda.hear(COFFEE, -60 - at, at=at)
        set_vacuum(app, 0, "cleaning")
        advance(app, 12)
        self.assertEqual(len(warnings(app)), 1)
        self.assertIn("could not write", warnings(app)[0])
        os.makedirs(missing)
        advance(app, 15)
        set_vacuum(app, 16, "docked")
        advance(app, 80)
        rows = lines(app)
        self.assertEqual(rows[0], {"t": T0, "event": "start", "scanners": {COFFEE: SCANNERS[COFFEE]}})
        self.assertEqual(rows[1]["t"], T0 + 15)
        self.assertEqual(rows[-1]["event"], "end")
        self.assertEqual(adverts(app, COFFEE), [[round(T0 + at, 1), -60 - at] for at in heard])
        self.assertIn(closed(app, "13 samples (3 lost to write errors)"), app.logged)

    def test_an_end_line_that_cannot_be_written_is_a_warning_not_a_crash(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        app.log_file = Path(_DIR.name) / "gone" / "beacon_position_log.jsonl"
        app.terminate()
        self.assertIsNone(app._win)
        self.assertEqual(len(warnings(app)), 1)
        self.assertIn("end of window not written", warnings(app)[0])

    def test_the_lock_guard_of_these_tests_does_catch_a_call_made_under_the_lock(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        with app._lock:
            app._on_tick({"wid": 1})
        self.assertEqual(app.violations, ["get_state", "get_state", "create_task"])
        app.violations.clear()
        for coro in app.tasks:
            coro.close()
        app.tasks.clear()


class Stopping(Case):
    def test_stopping_closes_the_window_with_an_end_line_and_cancels_the_ticks(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        advance(app, 12)
        app.terminate()
        self.assertEqual([row.get("event") for row in lines(app)], ["start", None, None, None, "end"])
        self.assertEqual(events(app)[1], {"t": T0 + 12, "event": "end"})
        self.assertEqual((app.timers, app.canceled, app._win), ([], ["timer1"], None))
        advance(app, 900)
        self.assertEqual(len(lines(app)), 5)

    def test_stopping_in_the_tail_cancels_the_tick_and_the_pending_close(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 12, "docked")
        app.terminate()
        self.assertEqual(sorted(app.canceled), ["timer1", "timer2"])
        self.assertEqual(app.timers, [])
        advance(app, 900)
        self.assertEqual([row.get("event") for row in lines(app)], ["start", None, None, None, "end"])

    def test_stopping_with_no_window_writes_nothing(self):
        app = make_app()
        app.terminate()
        self.assertFalse(os.path.exists(app.log_file))
        self.assertEqual(app.canceled, [])

    def test_a_window_closed_before_its_first_sample_leaves_no_lines(self):
        app = make_app(state="cleaning")
        app.terminate()
        self.assertFalse(os.path.exists(app.log_file))
        self.assertIn(closed(app, "0 samples"), app.logged)

    def test_timers_of_an_earlier_window_do_nothing_to_the_next_one(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        app.terminate()
        set_vacuum(app, 50, "cleaning")
        tasks = len(app.tasks)
        app._on_tick({"wid": 1})
        app._on_tail({"wid": 1, "token": 5})
        self.assertEqual((len(app.tasks), app._win["id"]), (tasks, 2))
        self.assertEqual([row["event"] for row in events(app)], ["start", "end", "start"])

    def test_a_close_timer_that_was_cancelled_by_a_new_run_does_nothing_when_it_still_fires(self):
        app = make_app()
        set_vacuum(app, 0, "cleaning")
        set_vacuum(app, 12, "paused")
        stale = next(t for t in app.timers if t["cb"] == app._on_tail)
        set_vacuum(app, 20, "cleaning")
        set_vacuum(app, 30, "paused")
        stale["cb"](dict(stale["kw"]))
        self.assertIsNotNone(app._win)
        advance(app, 85)
        self.assertEqual([row["event"] for row in events(app)], ["start"])
        advance(app, 91)
        self.assertEqual([row["event"] for row in events(app)], ["start", "end"])


class Unwrapping(unittest.TestCase):
    def test_every_envelope_shape_reaches_the_response(self):
        body = {VACUUM: {"x": 1, "y": 2}}
        for resp in (envelope(body), {"result": {"response": body}}, {"response": body},
                     {"success": True, "result": body}, body):
            with self.subTest(resp=resp):
                self.assertEqual(bpl.response_from(resp), body)

    def test_a_refused_call_gives_the_envelope_and_anything_else_nothing(self):
        refused = {"success": False, "error": {"code": "x", "message": "y"}}
        self.assertEqual(bpl.response_from(refused), refused)
        for resp in (None, [], "x", 5):
            with self.subTest(resp=resp):
                self.assertEqual(bpl.response_from(resp), {})

    def test_the_position_is_the_vacuums_x_and_y_or_nothing(self):
        self.assertEqual(bpl.position_from(envelope({VACUUM: {"x": 31875, "y": 30383}}), VACUUM), (31875, 30383))
        self.assertEqual(bpl.position_from(envelope({VACUUM: {"x": 1.5, "y": -2}}), VACUUM), (1.5, -2))
        for resp in (None, {}, envelope({}), envelope({VACUUM: None}), envelope({VACUUM: {"x": 1}}),
                     envelope({VACUUM: {"x": "1", "y": 2}}), envelope({VACUUM: {"x": True, "y": 2}}),
                     envelope({VACUUM: {"x": math.inf, "y": 2}}), envelope({"vacuum.other": {"x": 1, "y": 2}})):
            with self.subTest(resp=resp):
                self.assertEqual(bpl.position_from(resp, VACUUM), (None, None))

    def test_the_beacon_is_the_device_under_its_address_in_any_case(self):
        device = {"area_name": "Kitchen"}
        self.assertEqual(bpl.beacon_from(envelope({BEACON: device}), BEACON), device)
        self.assertEqual(bpl.beacon_from(envelope({BEACON.upper(): device}), BEACON), device)
        for resp in (None, {}, envelope({}), envelope({BEACON: "x"}), envelope({"aa:bb:cc:00:00:01": device})):
            with self.subTest(resp=resp):
                self.assertIsNone(bpl.beacon_from(resp, BEACON))


class Reading(unittest.TestCase):
    NOW = 1000.0

    def device(self, scanners):
        return {"adverts": {f"{BEACON}__{scanner}": {"scanner_address": scanner, "name": f"name of {scanner}",
                                                     "hist_stamp": stamps, "hist_rssi": rssis}
                            for scanner, (stamps, rssis) in scanners.items()}}

    def read(self, device, last=None, since=0.0):
        return bpl.read_adverts(device, self.NOW, last or {}, since)

    def test_new_pairs_come_oldest_first_above_the_last_written_and_the_open(self):
        device = self.device({COFFEE: ([990, 985, 980, 975], [-60, -61, -62, -63]), DINING: ([999, 940], [-70, -71])})
        fresh, names, mismatch = self.read(device, last={COFFEE: 980}, since=950)
        self.assertEqual(fresh, {COFFEE: [(985, -61), (990, -60)], DINING: [(999, -70)]})
        self.assertEqual(names, {COFFEE: f"name of {COFFEE}", DINING: f"name of {DINING}"})
        self.assertFalse(mismatch)

    def test_a_scanner_with_nothing_new_is_left_out_but_still_named(self):
        fresh, names, _ = self.read(self.device({COFFEE: ([990], [-60])}), last={COFFEE: 990})
        self.assertEqual((fresh, list(names)), ({}, [COFFEE]))

    def test_junk_is_skipped_and_a_missing_scanner_address_comes_from_the_key(self):
        device = {"adverts": {
            f"{BEACON}__{COFFEE}": {"scanner_address": COFFEE.upper(), "name": "A",
                                    "hist_stamp": [990, "x", None, 980, True, math.nan],
                                    "hist_rssi": [-60, -61, -62, "n/a", -64, -65]},
            f"{BEACON}__{DINING}": {"name": "B", "hist_stamp": [995, 994], "hist_rssi": [-70]},
            f"{BEACON}__{BATH}": {"scanner_address": BATH, "name": "C", "hist_stamp": "no", "hist_rssi": [-1]},
            f"{BEACON}__aa:bb:cc:00:00:01": "not an advert"}}
        fresh, names, mismatch = self.read(device)
        self.assertEqual(fresh, {COFFEE: [(990, -60)], DINING: [(995, -70)]})
        self.assertEqual(names, {COFFEE: "A", DINING: "B", BATH: "C"})
        self.assertFalse(mismatch)

    def test_no_device_or_no_adverts_is_nothing(self):
        for device in (None, {}, {"adverts": None}, {"adverts": []}, {"adverts": {}}):
            with self.subTest(device=device):
                self.assertEqual(self.read(device), ({}, {}, False))

    def test_stamps_none_of_which_are_a_day_back_to_a_few_seconds_ahead_are_a_mismatch(self):
        for name, stamps in (("future", [self.NOW + 100, self.NOW + 6]),
                             ("ancient", [self.NOW - 90000, self.NOW - 100000])):
            with self.subTest(name):
                fresh, names, mismatch = self.read(self.device({COFFEE: (stamps, [-60] * len(stamps))}))
                self.assertEqual((fresh, list(names), mismatch), ({}, [COFFEE], True))

    def test_an_old_stamp_beside_a_current_one_is_no_mismatch(self):
        fresh, _, mismatch = self.read(self.device({COFFEE: ([self.NOW - 3, self.NOW - 90000], [-60, -61])}),
                                       since=-1e9)
        self.assertFalse(mismatch)
        self.assertEqual(fresh, {COFFEE: [(self.NOW - 90000, -61), (self.NOW - 3, -60)]})


if __name__ == "__main__":
    unittest.main()
