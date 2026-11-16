# tests/test_ble_scanner_watch.py - BleScannerWatch: what makes a scanner deaf, one push per
# episode, the checks that are skipped, and the notify contract.
# The app is driven through its real initialize() with a fake clock (HA's monotonic clock, in
# seconds after MONO0), a fake scheduler and a fake bermuda.dump_devices that answers app.dump().
# The fake app has no set_state, listen_state or other service, so anything but the dump, the
# notifier and the log would fail the test.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import asyncio
import sys
import types
import unittest
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

import ble_scanner_watch as bsw  # noqa: E402

MONO0 = 7_000_000.0  # time.monotonic() at minute 0

LIFT = "5c:01:3b:53:2b:12"
DINING = "9c:cc:01:33:d5:c6"
COFFEE = "9c:cc:01:33:d8:d6"
LANDING = "c8:85:41:68:25:52"
NAMES = {LIFT: "Living room TV lift", DINING: "BLE proxy dining room",
         COFFEE: "BLE proxy coffee bar", LANDING: "BLE proxy landing"}
HEALTHY = {LIFT: [3, 60], DINING: [5, 40], COFFEE: [8]}
MINUTES = range(0, 130, 10)


def envelope(devices):
    return {"success": True, "result": {"context": {"id": "01"}, "response": devices}}


def device(addr, **fields):
    return {"address": addr, "name": None, "last_seen": 0.0, "_is_scanner": False, "adverts": {}, **fields}


def build_dump(world, now):
    """What bermuda.dump_devices answers for `world`, {scanner address: [age in seconds of the latest
    advert of each device that scanner heard]}: every scanner as a scanner device, and one device per
    age with an advert from that scanner."""
    devices = {scanner: device(scanner, name=NAMES.get(scanner), _is_scanner=True) for scanner in world}
    for k, (scanner, ages) in enumerate(world.items()):
        for i, age in enumerate(ages):
            addr = f"aa:bb:cc:{k:02x}:00:{i:02x}"
            advert = {"scanner_address": scanner, "name": NAMES.get(scanner), "area_name": None,
                      "stamp": now - age, "rssi": -70.0}
            devices[addr] = device(addr, last_seen=now - age, adverts={f"{addr}__{scanner}": advert})
    return envelope(devices)


def make_app(args=None, world=None):
    """BleScannerWatch with initialize() actually run and the AppDaemon primitives faked: a settable
    clock, a scheduler that only records, tasks that run when asked, a notifier that records its
    pushes and a bermuda.dump_devices that answers app.dump() (by default app.world)."""
    app = bsw.BleScannerWatch.__new__(bsw.BleScannerWatch)
    app.args = dict(args or {})
    app.world = dict(HEALTHY if world is None else world)
    app.clock = {"now": MONO0}
    app.logged, app.tasks, app.service_calls, app.scheduled, app.pushes = [], [], [], [], []
    app.delay = 0
    app.sent = 1
    app.dump = lambda: build_dump(app.world, app._now())

    async def notify(**kw):
        app.pushes.append((minute(app), kw))
        if isinstance(app.sent, Exception):
            raise app.sent
        return app.sent

    async def call_service(service, **kw):
        app.service_calls.append((service, kw))
        if app.delay:
            await asyncio.sleep(app.delay)
        return app.dump()

    app.notifier = types.SimpleNamespace(notify=notify)
    app.get_app = MagicMock(return_value=app.notifier)
    app.run_every = lambda callback, start, interval: app.scheduled.append((callback, start, interval))
    app.create_task = app.tasks.append
    app.call_service = call_service
    app._now = lambda: app.clock["now"]
    app.log = lambda message, level="INFO": app.logged.append((level, message))
    app.initialize()
    return app


def minute(app):
    return (app.clock["now"] - MONO0) / 60


def check(app, at_minute):
    """One scheduled check at `at_minute`, run to the end."""
    app.clock["now"] = MONO0 + at_minute * 60
    app._tick({})
    while app.tasks:
        asyncio.run(app.tasks.pop(0))


def run(app, minutes=MINUTES):
    for at_minute in minutes:
        check(app, at_minute)


def logged(app, level):
    return [message for lvl, message in app.logged if lvl == level]


def pushed_at(app):
    return [(at_minute, kw["message"]) for at_minute, kw in app.pushes]


def lift_message(minutes):
    return f"Living room TV lift has heard no Bluetooth devices for {minutes} min. Rebooting it usually fixes it."


class DeafRule(unittest.TestCase):
    def test_a_scanner_deaf_for_30_minutes_is_pushed_once_with_the_contract(self):
        app = make_app(world={LIFT: [], DINING: [5, 40], COFFEE: [8]})
        run(app)
        self.assertEqual(pushed_at(app), [(30, lift_message(30))])
        self.assertEqual(app.pushes[0][1], {"title": "Bluetooth scanner deaf", "message": lift_message(30),
                                            "target": "user"})

    def test_no_push_before_deaf_min(self):
        app = make_app(world={LIFT: [], DINING: [5]})
        run(app, (0, 10, 20))
        self.assertEqual(app.pushes, [])
        check(app, 30)
        self.assertEqual(len(app.pushes), 1)

    def test_a_scanner_that_hears_devices_is_never_pushed_or_logged_as_deaf(self):
        app = make_app()
        run(app)
        self.assertEqual(app.pushes, [])
        self.assertEqual(logged(app, "INFO"), [logged(app, "INFO")[0]])
        self.assertIn("initialized", logged(app, "INFO")[0])
        self.assertEqual(logged(app, "WARNING"), [])

    def test_a_scanner_with_no_adverts_at_all_is_deaf(self):
        app = make_app(world={LIFT: [], DINING: [5]})
        devices = bsw.devices_from(app.dump())
        self.assertEqual(devices[LIFT]["adverts"], {})
        self.assertFalse(any(LIFT in key for dev in devices.values() for key in dev["adverts"]))
        run(app)
        self.assertEqual([at_minute for at_minute, _ in pushed_at(app)], [30])

    def test_every_scanner_deaf_at_once_is_judged_too(self):
        app = make_app(world={LIFT: [], DINING: [], COFFEE: []})
        run(app)
        self.assertEqual([at_minute for at_minute, _ in pushed_at(app)], [30, 30, 30])
        self.assertEqual({kw["message"].split(" has heard")[0] for _, kw in app.pushes},
                         {NAMES[LIFT], NAMES[DINING], NAMES[COFFEE]})

    def test_stale_adverts_that_are_still_a_sane_age_count_as_deaf_not_as_a_clock_problem(self):
        app = make_app(world={LIFT: [1000, 80000], DINING: [1000]})
        run(app)
        self.assertEqual(len(app.pushes), 2)
        self.assertEqual(logged(app, "WARNING"), [])

    def test_only_the_deaf_scanner_is_named_and_the_others_stay_quiet(self):
        app = make_app(world={LIFT: [3], DINING: [], COFFEE: [8], LANDING: [9]})
        run(app)
        self.assertEqual(len(app.pushes), 1)
        self.assertIn("BLE proxy dining room has heard no Bluetooth devices for 30 min.", app.pushes[0][1]["message"])

    def test_an_advert_exactly_fresh_s_old_is_heard_and_one_second_older_is_not(self):
        heard = make_app(world={LIFT: [120.0], DINING: [5]})
        run(heard)
        self.assertEqual(heard.pushes, [])
        deaf = make_app(world={LIFT: [121.0], DINING: [5]})
        run(deaf)
        self.assertEqual(len(deaf.pushes), 1)

    def test_fresh_s_and_deaf_min_and_interval_min_come_from_the_yaml(self):
        app = make_app(args={"fresh_s": 300, "deaf_min": 20, "interval_min": 5}, world={LIFT: [250], DINING: [5]})
        self.assertEqual(app.scheduled[0][2], 300)
        run(app, range(0, 60, 5))
        self.assertEqual(app.pushes, [])
        app.world = {LIFT: [301], DINING: [5]}
        run(app, range(60, 120, 5))
        self.assertEqual(pushed_at(app), [(80, lift_message(20))])

    def test_the_count_is_per_scanner_from_the_adverts_own_scanner_address(self):
        now = MONO0 + 100.0
        devices = {
            LIFT: device(LIFT, name=NAMES[LIFT], _is_scanner=True),
            DINING: device(DINING, name=NAMES[DINING], _is_scanner=True),
            "aa:bb:cc:00:00:01": device("aa:bb:cc:00:00:01", adverts={
                "aa:bb:cc:00:00:01__elsewhere": {"scanner_address": DINING, "stamp": now - 5},
                "aa:bb:cc:00:00:01__other": {"scanner_address": COFFEE, "stamp": now - 5},
                "aa:bb:cc:00:00:01__old": {"scanner_address": LIFT, "stamp": now - 500},
            }),
        }
        scanners, heard, mismatch = bsw.read_dump(devices, now, 120)
        self.assertEqual({a: len(heard.get(a, ())) for a in scanners}, {LIFT: 0, DINING: 1})
        self.assertFalse(mismatch)


class Episodes(unittest.TestCase):
    def test_recovery_is_logged_not_pushed_and_rearms_for_the_next_episode(self):
        app = make_app(world={LIFT: [], DINING: [5]})
        run(app, (0, 10, 20, 30))
        self.assertEqual(pushed_at(app), [(30, lift_message(30))])
        run(app, (40, 50))
        self.assertEqual(len(app.pushes), 1)

        app.world = {LIFT: [4], DINING: [5]}
        check(app, 60)
        self.assertEqual(len(app.pushes), 1)
        self.assertEqual(len([m for m in logged(app, "INFO") if "hears 1 device(s) again after 6 deaf" in m]), 1)

        app.world = {LIFT: [], DINING: [5]}
        run(app, (70, 80, 90))
        self.assertEqual(len(app.pushes), 1)
        check(app, 100)
        self.assertEqual(pushed_at(app), [(30, lift_message(30)), (100, lift_message(30))])
        run(app, (110, 120, 130))
        self.assertEqual(len(app.pushes), 2)

    def test_a_deaf_spell_shorter_than_deaf_min_starts_again_from_zero(self):
        app = make_app(world={LIFT: [], DINING: [5]})
        run(app, (0, 10, 20))
        app.world = {LIFT: [2], DINING: [5]}
        check(app, 30)
        app.world = {LIFT: [], DINING: [5]}
        run(app, (40, 50, 60))
        self.assertEqual(app.pushes, [])
        check(app, 70)
        self.assertEqual([at_minute for at_minute, _ in pushed_at(app)], [70])

    def test_deaf_start_push_and_recovery_are_logged_at_info_once_each(self):
        app = make_app(world={LIFT: [], DINING: [5]})
        run(app, (0, 10, 20, 30, 40))
        app.world = {LIFT: [1], DINING: [5]}
        check(app, 50)
        info = logged(app, "INFO")[1:]
        self.assertEqual(len(info), 3)
        self.assertIn("Living room TV lift (5c:01:3b:53:2b:12) is deaf", info[0])
        self.assertIn("deaf for 30 min, pushing the owner", info[1])
        self.assertIn("hears 1 device(s) again after 5 deaf check(s)", info[2])

    def test_every_check_logs_the_counts_at_debug(self):
        app = make_app(world={LIFT: [], DINING: [5, 40], COFFEE: [8]})
        run(app, (0, 10))
        debug = logged(app, "DEBUG")
        self.assertEqual(len(debug), 2)
        self.assertIn("BLE proxy coffee bar 1, BLE proxy dining room 2, Living room TV lift 0", debug[0])

    def test_one_scanner_recovering_does_not_touch_the_streak_of_another(self):
        app = make_app(world={LIFT: [], DINING: []})
        run(app, (0, 10))
        app.world = {LIFT: [], DINING: [5]}
        run(app, (20, 30))
        self.assertEqual([m["message"].split(" has")[0] for _, m in app.pushes], [NAMES[LIFT]])


class Ignore(unittest.TestCase):
    def test_an_ignored_scanner_never_pushes_or_starts_a_streak(self):
        app = make_app(args={"ignore": [LANDING]}, world={LIFT: [3], LANDING: []})
        run(app)
        self.assertEqual(app.pushes, [])
        self.assertEqual(app._deaf, {})
        self.assertFalse([m for m in logged(app, "INFO") if "landing" in m.lower()])

    def test_ignoring_one_scanner_leaves_a_deaf_neighbour_judged(self):
        app = make_app(args={"ignore": [LANDING]}, world={LIFT: [], LANDING: [], DINING: [5]})
        run(app)
        self.assertEqual(pushed_at(app), [(30, lift_message(30))])

    def test_the_yaml_address_is_matched_case_insensitively(self):
        app = make_app(args={"ignore": [LANDING.upper()]}, world={LANDING: [], DINING: [5]})
        run(app)
        self.assertEqual(app.pushes, [])

    def test_the_shipped_yaml_ignores_the_landing_proxy(self):
        text = (Path(bsw.__file__).with_suffix(".yaml")).read_text(encoding="utf-8")
        self.assertIn(f'"{LANDING}"', text)
        self.assertIn("landing light", text)


class SkippedChecks(unittest.TestCase):
    def test_a_failed_or_empty_dump_is_never_read_as_deaf(self):
        refused = {"success": False, "error": {"code": "not_found", "message": "Service not found"}}
        for answer in (RuntimeError("bermuda is down"), {}, None, "junk", refused, envelope({}),
                       envelope({"context": {"id": "01"}}), envelope({LIFT: "not a device"})):
            with self.subTest(answer=answer):
                app = make_app()

                def dump(answer=answer):
                    if isinstance(answer, Exception):
                        raise answer
                    return answer

                app.dump = dump
                run(app)
                self.assertEqual(app.pushes, [])
                self.assertEqual(app._deaf, {})
                self.assertEqual(len(logged(app, "WARNING")), len(MINUTES))
                self.assertEqual(logged(app, "INFO"), [logged(app, "INFO")[0]])

    def test_a_dump_that_hangs_is_skipped_after_the_timeout(self):
        app = make_app(world={LIFT: [], DINING: [5]})
        app.delay = 5
        with patch.object(bsw, "DUMP_TIMEOUT_S", 0.01):
            run(app)
        self.assertEqual(app.pushes, [])
        self.assertEqual(len(logged(app, "WARNING")), len(MINUTES))

    def test_the_dump_is_asked_for_every_device_and_with_a_response(self):
        app = make_app()
        check(app, 0)
        self.assertEqual(app.service_calls, [("bermuda/dump_devices", {"return_response": True})])

    def test_a_failed_check_in_the_middle_neither_counts_as_deaf_nor_ends_the_streak(self):
        app = make_app(world={LIFT: [], DINING: [5]})
        run(app, (0, 10))
        good = app.dump

        def failing():
            raise RuntimeError("bermuda is down")

        app.dump = failing
        check(app, 20)
        app.dump = good
        check(app, 30)
        self.assertEqual(app.pushes, [])
        check(app, 40)
        self.assertEqual([at_minute for at_minute, _ in pushed_at(app)], [40])

    def test_a_dump_without_scanner_devices_is_skipped(self):
        app = make_app()
        devices = bsw.devices_from(app.dump())
        app.dump = lambda: envelope({a: d for a, d in devices.items() if not d["_is_scanner"]})
        run(app)
        self.assertEqual(app.pushes, [])
        self.assertEqual(len(logged(app, "WARNING")), len(MINUTES))
        self.assertIn("no scanner devices", logged(app, "WARNING")[0])

    def test_adverts_that_are_all_in_the_future_or_over_a_day_old_are_a_clock_mismatch_warned_once(self):
        for name, ages in (("future", [-500, -20]), ("ancient", [90000, 200000]), ("both", [-500, 90000])):
            with self.subTest(name):
                app = make_app(world={LIFT: ages, DINING: ages})
                run(app)
                self.assertEqual(app.pushes, [])
                self.assertEqual(app._deaf, {})
                self.assertEqual(len(logged(app, "WARNING")), 1)
                self.assertIn("the clocks differ", logged(app, "WARNING")[0])
                self.assertEqual(logged(app, "DEBUG"), [])

    def test_a_dump_where_no_advert_has_a_usable_stamp_is_skipped_like_a_mismatch(self):
        app = make_app()
        devices = bsw.devices_from(app.dump())
        for dev in devices.values():
            for advert in dev["adverts"].values():
                advert.pop("stamp")
        app.dump = lambda: envelope(devices)
        run(app)
        self.assertEqual(app.pushes, [])
        self.assertEqual(len(logged(app, "WARNING")), 1)

    def test_one_sane_advert_among_wild_ones_is_not_a_mismatch(self):
        app = make_app(world={LIFT: [], DINING: [-500, 90000, 5]})
        run(app)
        self.assertEqual(logged(app, "WARNING"), [])
        self.assertEqual([at_minute for at_minute, _ in pushed_at(app)], [30])

    def test_the_mismatch_warning_comes_again_after_a_check_that_reads_fine(self):
        app = make_app(world={LIFT: [-500], DINING: [-500]})
        run(app, (0, 10))
        self.assertEqual(len(logged(app, "WARNING")), 1)
        app.world = {LIFT: [5], DINING: [5]}
        check(app, 20)
        app.world = {LIFT: [-500], DINING: [-500]}
        run(app, (30, 40))
        self.assertEqual(len(logged(app, "WARNING")), 2)

    def test_a_scanner_with_no_adverts_and_a_dump_with_no_adverts_at_all_is_not_a_mismatch(self):
        app = make_app(world={LIFT: [], DINING: []})
        run(app, (0,))
        self.assertEqual(logged(app, "WARNING"), [])
        self.assertEqual(set(app._deaf), {LIFT, DINING})


class Notify(unittest.TestCase):
    def test_a_push_that_raises_is_tried_again_at_the_next_check_and_then_kept(self):
        app = make_app(world={LIFT: [], DINING: [5]})
        app.sent = RuntimeError("boom")
        run(app, (0, 10, 20, 30))
        self.assertEqual(len(app.pushes), 1)
        self.assertEqual(len(logged(app, "WARNING")), 1)
        self.assertIn("notify failed", logged(app, "WARNING")[0])
        app.sent = 1
        run(app, (40, 50, 60, 70))
        self.assertEqual(pushed_at(app), [(30, lift_message(30)), (40, lift_message(40))])

    def test_a_push_that_reached_nobody_is_tried_again_at_the_next_check(self):
        app = make_app(world={LIFT: [], DINING: [5]})
        app.sent = 0
        run(app, (0, 10, 20, 30, 40))
        self.assertEqual([at_minute for at_minute, _ in pushed_at(app)], [30, 40])
        self.assertEqual(len([m for m in logged(app, "WARNING") if "no recipients" in m]), 2)
        app.sent = 2
        run(app, (50, 60, 70))
        self.assertEqual([at_minute for at_minute, _ in pushed_at(app)], [30, 40, 50])

    def test_a_scanner_whose_push_failed_still_recovers_cleanly(self):
        app = make_app(world={LIFT: [], DINING: [5]})
        app.sent = 0
        run(app, (0, 10, 20, 30))
        app.world = {LIFT: [3], DINING: [5]}
        run(app, (40, 50, 60))
        self.assertEqual(len(app.pushes), 1)
        self.assertEqual(app._deaf, {})

    def test_the_notifier_is_only_ever_called_with_title_message_and_target(self):
        app = make_app(world={LIFT: [], DINING: []})
        run(app)
        for _, kw in app.pushes:
            self.assertEqual(set(kw), {"title", "message", "target"})
            self.assertEqual((kw["title"], kw["target"]), ("Bluetooth scanner deaf", "user"))


class Setup(unittest.TestCase):
    def test_defaults_and_the_first_check_one_minute_after_start(self):
        app = make_app(args={})
        self.assertEqual((app.interval_min, app.fresh_s, app.deaf_min, app.ignore), (10.0, 120.0, 30.0, set()))
        self.assertEqual(app.scheduled, [(app._tick, "now+60", 600)])
        app.get_app.assert_called_once_with("MobileNotifier")

    def test_the_yaml_values_pass_through_and_ignore_is_lowercased(self):
        app = make_app(args={"interval_min": 5, "fresh_s": 90, "deaf_min": 45, "ignore": ["C8:85:41:68:25:52"]})
        self.assertEqual((app.interval_min, app.fresh_s, app.deaf_min), (5.0, 90.0, 45.0))
        self.assertEqual(app.ignore, {LANDING})
        self.assertEqual(app.scheduled[0][2], 300)

    def test_a_value_that_would_break_the_schedule_fails_at_start(self):
        for args in ({"interval_min": 0}, {"interval_min": -5}, {"fresh_s": 0}, {"deaf_min": -1}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    make_app(args=args)

    def test_the_shipped_yaml_names_the_class_and_its_notifier_dependency(self):
        text = (Path(bsw.__file__).with_suffix(".yaml")).read_text(encoding="utf-8")
        self.assertIn("module: ble_scanner_watch", text)
        self.assertIn("class: BleScannerWatch", text)
        self.assertIn("- MobileNotifier", text)
        self.assertNotRegex(text, r"(?m)^\s*log:")


class Reading(unittest.TestCase):
    NOW = 1_000_000.0

    def read(self, devices, fresh_s=120):
        return bsw.read_dump(devices, self.NOW, fresh_s)

    def test_every_envelope_shape_reaches_the_devices(self):
        devices = {LIFT: device(LIFT, _is_scanner=True)}
        for resp in (envelope(devices), {"result": {"response": devices}}, {"response": devices},
                     {"success": True, "result": devices}, devices):
            with self.subTest(resp=resp):
                self.assertEqual(bsw.devices_from(resp), devices)

    def test_anything_else_is_no_devices(self):
        for resp in (None, [], "x", 5, {}, {"success": False, "error": {"code": "x", "message": "y"}},
                     {"result": {"context": {"id": "01"}}}, envelope({LIFT: "not a device"}),
                     envelope({LIFT: {"name": "no adverts"}}), envelope({LIFT: {"adverts": None}})):
            with self.subTest(resp=resp):
                self.assertEqual(bsw.devices_from(resp), {})

    def test_the_age_limits_are_inclusive_at_zero_and_fresh_s(self):
        advert = lambda age: {"scanner_address": LIFT, "stamp": self.NOW - age}  # noqa: E731
        devices = {name: device(name, adverts={"k": advert(age)}) for name, age in
                   (("now", 0), ("limit", 120), ("over", 121), ("future", -1), ("recent", 30))}
        _, heard, mismatch = self.read(devices)
        self.assertEqual(heard[LIFT], {"now", "limit", "recent"})
        self.assertFalse(mismatch)

    def test_a_device_with_two_adverts_from_one_scanner_counts_once(self):
        devices = {"d": device("d", adverts={"d__a": {"scanner_address": LIFT, "stamp": self.NOW - 5},
                                             "d__b": {"scanner_address": LIFT.upper(), "stamp": self.NOW - 9}})}
        self.assertEqual(self.read(devices)[1], {LIFT: {"d"}})

    def test_scanner_addresses_are_compared_in_lowercase(self):
        devices = {LIFT.upper(): device(LIFT.upper(), name="Lift", _is_scanner=True),
                   "d": device("d", adverts={"k": {"scanner_address": LIFT.upper(), "stamp": self.NOW - 5}})}
        scanners, heard, _ = self.read(devices)
        self.assertEqual((scanners, heard), ({LIFT: "Lift"}, {LIFT: {"d"}}))

    def test_a_scanner_without_a_name_is_named_by_its_address(self):
        devices = {LIFT: device(LIFT, _is_scanner=True), DINING: device(DINING, name="", _is_scanner=True)}
        self.assertEqual(self.read(devices)[0], {LIFT: LIFT, DINING: DINING})

    def test_only_a_flag_that_is_exactly_true_makes_a_scanner(self):
        devices = {name: device(name, _is_scanner=flag) for name, flag in
                   (("yes", True), ("no", False), ("text", "true"), ("one", 1), ("none", None))}
        self.assertEqual(set(self.read(devices)[0]), {"yes"})

    def test_adverts_that_are_not_dicts_or_have_no_usable_stamp_are_ignored(self):
        devices = {"d": device("d", adverts={"a": None, "b": "x", "c": {"scanner_address": LIFT, "stamp": None},
                                             "d": {"scanner_address": LIFT, "stamp": True},
                                             "e": {"scanner_address": LIFT, "stamp": float("nan")},
                                             "f": {"scanner_address": None, "stamp": self.NOW - 1}})}
        scanners, heard, mismatch = self.read(devices)
        self.assertEqual((scanners, heard), ({}, {}))
        self.assertFalse(mismatch)

    def test_the_mismatch_is_adverts_with_no_stamp_between_zero_and_a_day(self):
        def devices(*ages):
            return {"d": device("d", adverts={str(i): {"scanner_address": LIFT, "stamp": self.NOW - age}
                                              for i, age in enumerate(ages)})}

        for ages, expected in (((), False), ((-1, 90000), True), ((90000,), True), ((-5,), True),
                               ((2000, 90000), False), ((0,), False), ((86400,), False), ((86401, -1), True),
                               ((-1, 5), False)):
            with self.subTest(ages=ages):
                self.assertEqual(self.read(devices(*ages))[2], expected)


if __name__ == "__main__":
    unittest.main()
