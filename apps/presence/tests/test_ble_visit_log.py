# tests/test_ble_visit_log.py - BleVisitLog: a window per unlock or cleaner visit, the snapshot
# schedule, what counts as seen, the per-address record and the JSONL line a window ends in.
# The app is driven through its real initialize() with a fake clock, a fake scheduler and a fake
# bermuda.dump_devices. The fake app has no set_state, notify or other service, so anything but
# reading states and that one dump would fail the test; every AppDaemon call is also checked to
# be made without the app's lock held (the event loop that keeps a snapshot takes it too).
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
from datetime import datetime, timedelta, timezone
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

import ble_visit_log as bvl  # noqa: E402

TZ = timezone(timedelta(hours=1))
T0 = datetime(2026, 11, 10, 11, 0, tzinfo=TZ).timestamp()
MONO0 = 7_000_000.0  # time.monotonic() at T0

LOCK = "lock.yale"
VISITOR = "sensor.untracked_visitor"
PERSONS = {"person.mikkel": {"state": "home"}, "person.kristine": {"state": "not_home"},
           "person.claudia": {"state": "home"}}
ADDR_A = "AA:BB:CC:00:00:01"
ADDR_B = "AA:BB:CC:00:00:02"
ADDR_C = "AA:BB:CC:00:00:03"
ADDR_D = "AA:BB:CC:00:00:04"

_DIR = tempfile.TemporaryDirectory()
_SEQ = itertools.count()


def new_log_file():
    return os.path.join(_DIR.name, f"ble_visit_log_{next(_SEQ)}.jsonl")


def iso(seconds):
    return datetime.fromtimestamp(T0 + seconds, TZ).isoformat(timespec="seconds")


def envelope(devices):
    return {"success": True, "result": {"context": {"id": "01"}, "response": devices}}


def device(addr, **fields):
    return {"address": addr, "name": None, "manufacturer": None, "address_type": "public",
            "name_bt_local_name": None, "beacon_uuid": None, "beacon_major": None, "beacon_minor": None,
            "area_id": None, "area_rssi": None, "last_seen": 0.0, **fields}


class CountingLock:
    def __init__(self):
        self.held = 0

    def __enter__(self):
        self.held += 1

    def __exit__(self, *exc):
        self.held -= 1


def make_app(args=None, persons=None):
    """BleVisitLog with initialize() actually run and the AppDaemon primitives faked: a settable
    clock (seconds after T0), a scheduler that only records, tasks that run when asked, and a
    bermuda.dump_devices that answers app.dump()."""
    app = bvl.BleVisitLog.__new__(bvl.BleVisitLog)
    app.args = {"log_file": new_log_file(), **(args or {})}
    app.states = {LOCK: "locked", VISITOR: "none"}
    app.attrs = {(LOCK, "changed_by"): "Auto Lock", (VISITOR, "who"): "", (VISITOR, "reason"): ""}
    app.persons = {k: dict(v) for k, v in (PERSONS if persons is None else persons).items()}
    app.clock = {"now": T0}
    app.logged, app.timers, app.tasks, app.service_calls, app.scheduled = [], [], [], [], []
    app.delay = 0
    app.dump = lambda: envelope({ADDR_A: device(ADDR_A, last_seen=app._monotonic() - 5)})
    order = itertools.count()

    def api():
        assert app._lock.held == 0, "an AppDaemon call was made while holding the app's lock"

    def get_state(entity, attribute=None, **kw):
        api()
        if entity == "person":
            return app.persons
        return app.attrs.get((entity, attribute)) if attribute else app.states.get(entity)

    def run_in(callback, delay, **kw):
        api()
        app.scheduled.append((delay, kw))
        app.timers.append((app.clock["now"] + delay, next(order), callback, kw))

    def create_task(coro):
        api()
        app.tasks.append(coro)

    async def call_service(service, **kw):
        app.service_calls.append((service, kw))
        if app.delay:
            await asyncio.sleep(app.delay)
        return app.dump()

    app.get_state = get_state
    app.get_now = lambda: datetime.fromtimestamp(app.clock["now"], TZ)
    app._now = app.get_now
    app._monotonic = lambda: MONO0 + app.clock["now"] - T0
    app.log = lambda message, level="INFO": app.logged.append((level, message))
    app.listen_state = MagicMock()
    app.run_in = run_in
    app.create_task = create_task
    app.call_service = call_service
    app.initialize()
    app._lock = CountingLock()
    return app


def serve(app, specs):
    """bermuda.dump_devices answers `specs`, {address: fields}; an `age` field becomes a last_seen
    that many seconds before the dump. The dict is read on every call, so a test can change it."""
    def answer():
        out = {}
        for addr, fields in specs.items():
            fields = dict(fields)
            age = fields.pop("age", None)
            if age is not None:
                fields["last_seen"] = app._monotonic() - age
            out[addr] = device(addr, **fields)
        return envelope(out)
    app.dump = answer


def run_tasks(app):
    while app.tasks:
        asyncio.run(app.tasks.pop(0))


def advance(app, seconds):
    """Fires every timer due by T0 + seconds in order, running the snapshots they start."""
    target = T0 + seconds
    while True:
        due = [t for t in app.timers if t[0] <= target]
        if not due:
            break
        timer = min(due, key=lambda t: t[:2])
        app.timers.remove(timer)
        app.clock["now"] = max(app.clock["now"], timer[0])
        timer[2](dict(timer[3]))
        run_tasks(app)
    app.clock["now"] = target


def unlock(app, seconds, old="locked"):
    advance(app, seconds)
    app.states[LOCK] = "unlocked"
    app._on_unlock(LOCK, None, old, "unlocked", {})


def cleaner(app, seconds, who="Liga Cleaning", reason="Liga Cleaning unlocked the front door at 11:00"):
    advance(app, seconds)
    app.states[VISITOR] = "cleaner"
    app.attrs[(VISITOR, "who")] = who
    app.attrs[(VISITOR, "reason")] = reason
    app._on_cleaner(VISITOR, None, "none", "cleaner", {})


def lines(app):
    try:
        with open(app.log_file, encoding="utf-8") as f:
            return [json.loads(row) for row in f.read().splitlines()]
    except FileNotFoundError:
        return []


def warnings(app):
    return [message for level, message in app.logged if level == "WARNING"]


class Window(unittest.TestCase):
    def test_it_listens_for_the_unlock_and_the_cleaner_whatever_the_old_state(self):
        app = make_app()
        listened = {c.args[1]: c.kwargs for c in app.listen_state.call_args_list}
        self.assertEqual(listened, {LOCK: {"new": "unlocked"}, VISITOR: {"new": "cleaner"}})

    def test_an_unlock_opens_a_window_with_its_snapshots_and_close_scheduled(self):
        app = make_app()
        unlock(app, 5)
        snapshots = [due - (T0 + 5) for due, _, cb, _ in app.timers if cb == app._on_due]
        self.assertEqual(sorted(snapshots), [20 + 300 * i for i in range(36)])
        self.assertEqual([due - (T0 + 5) for due, _, cb, _ in app.timers if cb == app._on_expired], [10800])
        self.assertEqual(lines(app), [])
        self.assertIn(("INFO", "BLE window opened by unlock: 36 snapshots planned, 2 persons home"), app.logged)

    def test_the_window_closes_after_window_min_and_writes_exactly_one_line(self):
        app = make_app()
        unlock(app, 5)
        advance(app, 5 + 10800 - 1)
        self.assertEqual(lines(app), [])
        advance(app, 5 + 10800)
        (line,) = lines(app)
        self.assertEqual(set(line), {"start", "end", "trigger", "changed_by", "persons_home", "events",
                                     "snapshots", "devices"})
        self.assertEqual((line["start"], line["end"], line["trigger"]), (iso(5), iso(5 + 10800), "unlock"))
        self.assertEqual(line["snapshots"], 36)
        self.assertEqual(app.service_calls, [("bermuda/dump_devices", {"return_response": True})] * 36)
        self.assertIn(("INFO", f"BLE window closed: 36 snapshots, 1 devices -> {app.log_file.name}"), app.logged)
        advance(app, 5 + 30000)
        self.assertEqual(len(lines(app)), 1)

    def test_the_line_is_one_json_object_appended_to_a_file_that_may_be_missing(self):
        app = make_app()
        self.assertFalse(os.path.exists(app.log_file))
        unlock(app, 0)
        advance(app, 10800)
        unlock(app, 20000)
        advance(app, 20000 + 10800)
        with open(app.log_file, encoding="utf-8") as f:
            raw = f.read()
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual([json.loads(row)["start"] for row in raw.splitlines()], [iso(0), iso(20000)])

    def test_a_second_unlock_and_a_cleaner_visit_in_an_open_window_are_events_not_windows(self):
        app = make_app()
        unlock(app, 0)
        planned = len(app.scheduled)
        unlock(app, 600)
        cleaner(app, 700)
        self.assertEqual(len(app.scheduled), planned)
        self.assertIn(("INFO", "BLE visitor noted in the open window"), app.logged)
        advance(app, 10800)
        (line,) = lines(app)
        self.assertEqual((line["start"], line["trigger"]), (iso(0), "unlock"))
        self.assertEqual([(e["t"], e["kind"]) for e in line["events"]],
                         [(iso(0), "unlock"), (iso(600), "unlock"), (iso(700), "visitor")])
        self.assertIsNone(line["events"][0]["state"])
        self.assertEqual(line["events"][2]["state"], {"state": "cleaner", "who": "Liga Cleaning",
                                                     "reason": "Liga Cleaning unlocked the front door at 11:00"})

    def test_a_cleaner_visit_opens_a_window_when_none_is_open(self):
        app = make_app()
        cleaner(app, 7)
        advance(app, 7 + 10800)
        (line,) = lines(app)
        self.assertEqual((line["start"], line["trigger"]), (iso(7), "visitor"))
        self.assertEqual([e["kind"] for e in line["events"]], ["visitor"])
        self.assertEqual(line["events"][0]["state"]["who"], "Liga Cleaning")

    def test_the_next_trigger_after_a_close_opens_a_new_window(self):
        app = make_app()
        unlock(app, 0)
        advance(app, 10800)
        cleaner(app, 12000)
        advance(app, 12000 + 10800)
        self.assertEqual([(row["start"], row["trigger"]) for row in lines(app)],
                         [(iso(0), "unlock"), (iso(12000), "visitor")])

    def test_persons_home_is_who_was_home_when_the_window_opened(self):
        app = make_app()
        unlock(app, 0)
        app.persons["person.kristine"]["state"] = "home"
        advance(app, 10800)
        self.assertEqual(lines(app)[0]["persons_home"], ["person.claudia", "person.mikkel"])

    def test_nobody_home_is_an_empty_list(self):
        app = make_app(persons={})
        unlock(app, 0)
        advance(app, 10800)
        self.assertEqual(lines(app)[0]["persons_home"], [])

    def test_the_arguments_default_and_override(self):
        app = make_app()
        self.assertEqual((app.lock_entity, app.visitor_entity, app.window_s, app.interval_s, app.first_delay_s),
                         (LOCK, VISITOR, 10800, 300, 20))
        self.assertEqual(make_app(args={"log_file": None}).log_file,
                         Path(bvl.__file__).resolve().parent / "ble_visit_log.jsonl")
        app = make_app(args={"lock": "lock.other", "visitor": "sensor.other", "window_min": 1,
                             "interval_s": 10, "first_delay_s": 2})
        self.assertEqual([c.args[1] for c in app.listen_state.call_args_list], ["lock.other", "sensor.other"])
        app._on_unlock("lock.other", None, "locked", "unlocked", {})
        self.assertEqual([delay for delay, _ in app.scheduled], [2, 12, 22, 32, 42, 52, 60])

    def test_a_snapshot_due_exactly_when_the_window_closes_is_not_taken(self):
        app = make_app(args={"window_min": 1, "interval_s": 30, "first_delay_s": 0})
        unlock(app, 0)
        self.assertEqual([delay for delay, _ in app.scheduled], [0, 30, 60])
        self.assertEqual([t[2] for t in app.timers].count(app._on_due), 2)

    def test_a_window_or_interval_that_is_not_positive_fails_at_start(self):
        for bad in ({"window_min": 0}, {"interval_s": 0}, {"interval_s": -5}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make_app(args=bad)


class ChangedBy(unittest.TestCase):
    def test_the_opening_unlock_is_read_at_the_first_snapshot_not_in_the_callback(self):
        app = make_app()
        unlock(app, 5)
        self.assertIsNone(app._win["events"][0]["changed_by"])
        advance(app, 9)
        app.attrs[(LOCK, "changed_by")] = "Liga Cleaning"
        advance(app, 24)
        self.assertIsNone(app._win["events"][0]["changed_by"])
        advance(app, 25)
        self.assertEqual(app._win["events"][0]["changed_by"], "Liga Cleaning")
        app.attrs[(LOCK, "changed_by")] = "Auto Lock"
        advance(app, 5 + 10800)
        (line,) = lines(app)
        self.assertEqual((line["changed_by"], line["events"][0]["changed_by"]), ("Liga Cleaning",) * 2)

    def test_a_later_event_is_read_at_the_snapshot_after_it(self):
        app = make_app()
        app.attrs[(LOCK, "changed_by")] = "Mikkel Example"
        unlock(app, 0)
        advance(app, 20)
        unlock(app, 100)
        app.attrs[(LOCK, "changed_by")] = "Auto Lock"
        advance(app, 319)
        self.assertIsNone(app._win["events"][1]["changed_by"])
        advance(app, 320)
        self.assertEqual([e["changed_by"] for e in app._win["events"]], ["Mikkel Example", "Auto Lock"])
        advance(app, 10800)
        self.assertEqual(lines(app)[0]["changed_by"], "Mikkel Example")

    def test_an_event_after_the_last_snapshot_keeps_changed_by_null(self):
        app = make_app()
        unlock(app, 0)
        unlock(app, 10700)
        advance(app, 10800)
        self.assertEqual([e["changed_by"] for e in lines(app)[0]["events"]], ["Auto Lock", None])

    def test_a_cleaner_visit_that_opens_a_window_gets_the_lock_name_at_the_first_snapshot(self):
        app = make_app()
        cleaner(app, 0)
        app.attrs[(LOCK, "changed_by")] = "Liga Cleaning"
        advance(app, 10800)
        self.assertEqual(lines(app)[0]["changed_by"], "Liga Cleaning")


class Seen(unittest.TestCase):
    NOW = 1_000_000.0

    def read(self, last_seen, before=None):
        return bvl.read_dump({addr: {"last_seen": value} for addr, value in last_seen.items()},
                             self.NOW, 330, before or {})

    def test_a_device_is_seen_when_its_age_is_zero_to_the_limit(self):
        seen, stamps, mismatch = self.read({"now": self.NOW, "limit": self.NOW - 330, "over": self.NOW - 331,
                                            "recent": self.NOW - 100})
        self.assertEqual(sorted(seen), ["limit", "now", "recent"])
        self.assertEqual(stamps["over"], self.NOW - 331)
        self.assertFalse(mismatch)

    def test_a_device_from_the_future_is_not_seen_but_does_not_make_a_mismatch_beside_a_seen_one(self):
        seen, _, mismatch = self.read({"fresh": self.NOW - 10, "future": self.NOW + 500})
        self.assertEqual((seen, mismatch), (["fresh"], False))

    def test_stale_devices_alone_are_a_quiet_house_not_a_mismatch(self):
        seen, _, mismatch = self.read({"a": self.NOW - 2000, "b": self.NOW - 80000})
        self.assertEqual((seen, mismatch), ([], False))

    def test_nothing_recent_with_a_negative_or_day_old_age_is_a_mismatch(self):
        for name, last_seen in (("future", self.NOW + 500), ("ancient", self.NOW - 90000)):
            with self.subTest(name):
                seen, _, mismatch = self.read({"a": last_seen, "b": self.NOW - 2000})
                self.assertEqual((seen, mismatch), ([], True))

    def test_in_a_mismatch_a_device_is_seen_when_its_last_seen_grew(self):
        before = {"grew": self.NOW + 100, "same": self.NOW + 100, "shrank": self.NOW + 100}
        seen, stamps, mismatch = self.read({"grew": self.NOW + 105, "same": self.NOW + 100,
                                            "shrank": self.NOW + 90, "new": self.NOW + 5}, before)
        self.assertEqual((seen, mismatch), (["grew"], True))
        self.assertEqual(stamps["new"], self.NOW + 5)

    def test_a_device_without_a_usable_last_seen_is_ignored_and_never_a_mismatch(self):
        devices = {name: {"last_seen": value} for name, value in
                   (("zero", 0), ("none", None), ("text", "5"), ("bool", True), ("nan", math.nan),
                    ("inf", math.inf), ("negative", -4.0))}
        devices["missing"] = {}
        self.assertEqual(bvl.read_dump(devices, 100.0, 330, {}), ([], {}, False))

    def test_the_apps_clock_is_what_decides_in_a_window(self):
        app = make_app(args={"window_min": 10})
        serve(app, {ADDR_A: {"age": 5}, ADDR_B: {"age": 330}, ADDR_C: {"age": 331},
                    "AA:BB:CC:00:00:04": {"age": -1}, "AA:BB:CC:00:00:05": {"last_seen": 0}})
        unlock(app, 0)
        advance(app, 600)
        (line,) = lines(app)
        self.assertEqual((line["snapshots"], sorted(line["devices"])), (2, [ADDR_A, ADDR_B]))
        self.assertEqual([line["devices"][a]["n"] for a in (ADDR_A, ADDR_B)], [2, 2])
        self.assertEqual(warnings(app), [])

    def test_a_host_that_has_just_booted_does_not_see_devices_that_were_never_seen(self):
        app = make_app(args={"window_min": 10})
        app._monotonic = lambda: 100.0
        serve(app, {ADDR_A: {"last_seen": 0}})
        unlock(app, 0)
        advance(app, 600)
        self.assertEqual(lines(app)[0]["devices"], {})

    def test_a_clock_mismatch_warns_once_per_window_and_counts_what_grew(self):
        app = make_app(args={"window_min": 15})
        specs = {ADDR_A: {"last_seen": MONO0 + 100000.0}, ADDR_B: {"last_seen": MONO0 + 100000.0}}
        serve(app, specs)
        unlock(app, 0)
        advance(app, 20)
        self.assertEqual(lines(app), [])
        specs[ADDR_A]["last_seen"] += 7
        advance(app, 320)
        specs[ADDR_A]["last_seen"] += 7
        specs[ADDR_B]["last_seen"] += 7
        advance(app, 900)
        (line,) = lines(app)
        self.assertEqual(line["snapshots"], 3)
        self.assertEqual({a: line["devices"][a]["n"] for a in line["devices"]}, {ADDR_A: 2, ADDR_B: 1})
        self.assertEqual(len(warnings(app)), 1)
        self.assertIn("monotonic clock mismatch", warnings(app)[0])
        unlock(app, 2000)
        advance(app, 2000 + 900)
        self.assertEqual(len(warnings(app)), 2)

    def test_a_quiet_house_snapshots_without_devices_or_warnings(self):
        app = make_app(args={"window_min": 10})
        serve(app, {ADDR_A: {"age": 2000}, ADDR_B: {"age": 5000}})
        unlock(app, 0)
        advance(app, 600)
        (line,) = lines(app)
        self.assertEqual((line["snapshots"], line["devices"]), (2, {}))
        self.assertEqual(warnings(app), [])


class Aggregation(unittest.TestCase):
    def test_a_device_is_summed_up_across_snapshots(self):
        app = make_app(args={"window_min": 10})
        specs = {ADDR_A: {"age": 5, "name": "Watch", "manufacturer": "Acme", "address_type": "public",
                          "area_id": "kitchen", "area_rssi": -80},
                 ADDR_B: {"age": 400, "area_id": "hallway", "area_rssi": -50},
                 ADDR_C: {"age": 3, "area_id": "hallway"}}
        serve(app, specs)
        unlock(app, 0)
        advance(app, 20)
        specs.clear()
        specs.update({ADDR_A: {"age": 5, "name": "", "name_bt_local_name": "W1", "area_id": "hallway",
                               "area_rssi": -60, "beacon_uuid": "uuid-1", "beacon_major": 1, "beacon_minor": 2},
                      ADDR_B: {"age": 10, "area_id": "kitchen", "area_rssi": -90},
                      ADDR_C: {"age": 3, "area_id": "hallway"},
                      ADDR_D: {"age": 3}})
        advance(app, 600)
        (line,) = lines(app)
        self.assertEqual(line["snapshots"], 2)
        self.assertEqual(line["devices"][ADDR_A], {
            "name": "Watch", "manufacturer": "Acme", "address_type": "public", "local_name": "W1",
            "beacon": {"uuid": "uuid-1", "major": 1, "minor": 2}, "first": iso(20), "last": iso(320),
            "n": 2, "areas": ["hallway", "kitchen"], "best_rssi": -60})
        plain = {"name": None, "manufacturer": None, "address_type": "public", "local_name": None, "beacon": None}
        self.assertEqual(line["devices"][ADDR_B], {
            **plain, "first": iso(320), "last": iso(320), "n": 1, "areas": ["kitchen"], "best_rssi": -90})
        self.assertEqual(line["devices"][ADDR_C], {
            **plain, "first": iso(20), "last": iso(320), "n": 2, "areas": ["hallway"], "best_rssi": None})
        self.assertEqual(line["devices"][ADDR_D], {
            **plain, "first": iso(320), "last": iso(320), "n": 1, "areas": [], "best_rssi": None})

    def test_update_device_keeps_the_strongest_reading_and_ignores_junk_ones(self):
        devices = {}
        for rssi in (-70, "n/a", True, -60, None, -90, math.nan):
            bvl.update_device(devices, ADDR_A, device(ADDR_A, area_rssi=rssi), "t")
        self.assertEqual(devices[ADDR_A]["best_rssi"], -60)
        self.assertEqual(devices[ADDR_A]["n"], 7)


class Failures(unittest.TestCase):
    def test_a_failing_service_skips_the_snapshot_and_the_window_goes_on(self):
        app = make_app(args={"window_min": 10})
        good = app.dump
        calls = itertools.count()

        def flaky():
            if next(calls) == 0:
                raise RuntimeError("bermuda is down")
            return good()

        app.dump = flaky
        unlock(app, 0)
        advance(app, 600)
        (line,) = lines(app)
        self.assertEqual((line["snapshots"], line["devices"][ADDR_A]["n"]), (1, 1))
        self.assertEqual(len(warnings(app)), 1)
        self.assertIn("bermuda.dump_devices failed", warnings(app)[0])
        self.assertIn("bermuda is down", warnings(app)[0])

    def test_a_dump_that_hangs_is_skipped_after_the_timeout(self):
        app = make_app(args={"window_min": 10})
        app.delay = 5
        unlock(app, 0)
        with patch.object(bvl, "DUMP_TIMEOUT_S", 0.01):
            advance(app, 600)
        (line,) = lines(app)
        self.assertEqual((line["snapshots"], line["devices"]), (0, {}))
        self.assertEqual(len(warnings(app)), 2)

    def test_an_empty_or_refused_dump_is_skipped(self):
        refused = {"success": False, "error": {"code": "not_found", "message": "Service not found"}}
        for answer in ({}, None, "junk", refused, envelope({}), envelope({"context": {"id": "01"}})):
            with self.subTest(answer=answer):
                app = make_app(args={"window_min": 10})
                app.dump = lambda answer=answer: answer
                unlock(app, 0)
                advance(app, 600)
                (line,) = lines(app)
                self.assertEqual((line["snapshots"], line["devices"]), (0, {}))
                self.assertEqual(len(warnings(app)), 2)
                self.assertIn("returned no devices", warnings(app)[0])

    def test_an_unwritable_log_file_is_a_warning_not_a_crash(self):
        app = make_app(args={"log_file": os.path.join(_DIR.name, "missing", "ble_visit_log.jsonl")})
        unlock(app, 0)
        advance(app, 10800)
        self.assertEqual(len([m for m in warnings(app) if m.startswith("BLE window lost")]), 1)
        self.assertIsNone(app._win)

    def test_log_lines_and_the_file_are_ascii(self):
        app = make_app(args={"window_min": 10})
        specs = {ADDR_A: {"age": 5, "name": "Mikkels ur \u00f8", "area_id": "k\u00f8kken"}}
        serve(app, specs)
        answers = iter([RuntimeError("fejl \u00f8"), {"success": False, "error": {"message": "fejl \u00e5"}}])
        good = app.dump

        def varied():
            answer = next(answers, None)
            if isinstance(answer, Exception):
                raise answer
            return good() if answer is None else answer

        app.dump = varied
        unlock(app, 0)
        advance(app, 600)
        unlock(app, 700)
        advance(app, 700 + 600)
        self.assertEqual(len(warnings(app)), 2)
        for _, message in app.logged:
            self.assertTrue(message.isascii(), message)
        with open(app.log_file, encoding="utf-8") as f:
            raw = f.read()
        self.assertTrue(raw.isascii())
        self.assertEqual(json.loads(raw.splitlines()[1])["devices"][ADDR_A]["name"], "Mikkels ur \u00f8")


class Stopping(unittest.TestCase):
    def test_stopping_closes_and_writes_the_open_window_as_is(self):
        app = make_app()
        unlock(app, 0)
        advance(app, 700)
        app.terminate()
        (line,) = lines(app)
        self.assertEqual((line["start"], line["end"], line["snapshots"]), (iso(0), iso(700), 3))
        self.assertIsNone(app._win)
        advance(app, 30000)
        self.assertEqual(len(lines(app)), 1)

    def test_stopping_with_no_window_writes_nothing(self):
        app = make_app()
        app.terminate()
        self.assertFalse(os.path.exists(app.log_file))

    def test_a_snapshot_that_answers_after_the_window_closed_is_dropped(self):
        app = make_app()
        unlock(app, 0)
        held = []
        app.create_task = held.append
        advance(app, 20)
        self.assertEqual(len(held), 1)
        win = app._win
        app.terminate()
        asyncio.run(held.pop())
        (line,) = lines(app)
        self.assertEqual(line["snapshots"], 0)
        self.assertEqual((win["snapshots"], win["devices"]), (0, {}))

    def test_timers_of_an_earlier_window_do_nothing_to_the_next_one(self):
        app = make_app()
        unlock(app, 0)
        app.terminate()
        app.timers.clear()
        unlock(app, 50)
        app._on_due({"wid": 1})
        app._on_expired({"wid": 1})
        self.assertEqual((app.tasks, app._win["id"]), ([], 2))
        self.assertEqual(len(lines(app)), 1)


class Unwrapping(unittest.TestCase):
    def test_every_envelope_shape_reaches_the_devices(self):
        devices = {ADDR_A: device(ADDR_A, last_seen=5.0)}
        for resp in (envelope(devices), {"result": {"response": devices}}, {"response": devices},
                     {"success": True, "result": devices}, devices):
            with self.subTest(resp=resp):
                self.assertEqual(bvl.devices_from(resp), devices)

    def test_anything_else_is_no_devices(self):
        for resp in (None, [], "x", 5, {}, {"success": False, "error": {"code": "x", "message": "y"}},
                     {"result": {"context": {"id": "01"}}}, envelope({ADDR_A: "not a device"}),
                     envelope({ADDR_A: {"name": "no last_seen"}})):
            with self.subTest(resp=resp):
                self.assertEqual(bvl.devices_from(resp), {})


if __name__ == "__main__":
    unittest.main()
