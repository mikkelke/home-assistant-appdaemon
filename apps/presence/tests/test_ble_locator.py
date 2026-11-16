# tests/test_ble_locator.py - BleLocator: the likelihood and the filter, the device offset, when the
# answer is Bermuda's area, what a failed dump leaves and the model file.
# The app runs through its real initialize() and tick on a fake clock (T0 plus the seconds a test
# gives; the monotonic clock the dump's stamps are on is MONO0 plus the same), a scheduler that only
# records, a bermuda.dump_devices that answers app.dump(), area states from app.states and a
# set_state that records into app.published. The app has no other AppDaemon call to reach.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import math
import os
import random
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

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

import ble_locator as bl  # noqa: E402

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc).timestamp()
MONO0 = 7_000_000.0

S1, S2, S3, S4, S5 = (f"aa:00:00:00:00:0{i}" for i in range(1, 6))
SCANNERS = (S1, S2, S3, S4, S5)
ROOMS = ["Kitchen", "Living room", "Bedroom"]
MEANS = {"Kitchen": {S1: -50, S2: -70, S3: -85, S4: -90, S5: -60},
         "Living room": {S1: -70, S2: -50, S3: -75, S4: -85, S5: -90},
         "Bedroom": {S1: -90, S2: -80, S3: -50, S4: -70, S5: -75}}
SD = 6.0
CONSTANTS = {"ref_power_db": -55.0, "db_per_decade": 30.0, "floor_db": -100.0, "sd_floor_db": 6.0,
             "unseen_mean_db": -95.0, "unseen_sd_db": 10.0, "p_stay": 0.98, "p_stay_step_s": 5.0,
             "emission_power": 0.3, "emission_floor": 1e-12}

ANNA = {"name": "anna", "address": "89f35c9874aed44c554015fe6eaa3545", "bermuda_area": "sensor.anna_area"}
BEN = {"name": "ben", "address": "7c:d9:f4:14:23:77", "bermuda_area": "sensor.ben_area"}

_DIR = tempfile.TemporaryDirectory()
_SEQ = itertools.count()


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def model_doc(trained_at=None, means=None, rooms=None):
    means, rooms = means or MEANS, rooms or ROOMS
    return {"version": 1, "rooms": rooms,
            "scanners": {s: {"name": f"scanner {s[-1]}", "area": "Kitchen"} for s in SCANNERS},
            "stats": {room: {s: [means[room][s], SD, 50] for s in SCANNERS} for room in rooms},
            "constants": dict(CONSTANTS), "offset_history": [],
            "trained_window": {"start": iso(T0 - 7200), "end": iso(T0 - 3600)},
            "trained_at": trained_at or iso(T0 - 3600)}


def new_path(name):
    return os.path.join(_DIR.name, f"{next(_SEQ)}_{name}")


def write_model(path, doc=None, mtime=None):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(model_doc() if doc is None else doc, f)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def reading(room, delta=0.0, scanners=SCANNERS):
    return {s: MEANS[room][s] + delta for s in scanners}


def blend(a, b, share):
    return {s: MEANS[a][s] + share * (MEANS[b][s] - MEANS[a][s]) for s in SCANNERS}


def envelope(response):
    return {"id": 7, "type": "result", "success": True, "ad_status": "OK", "ad_duration": 0.2,
            "result": {"context": {"id": "01"}, "response": response}}


def advert(scanner, db, now, age=1.0, distance=None):
    """An advert `age` seconds old that reads `db`, or carries `distance` as it is when one is given."""
    if distance is None:
        distance = round(10 ** ((-55.0 - db) / 30.0), 4)
    return {"scanner_address": scanner, "stamp": now - age, "rssi_distance": distance}


def device(address, adverts):
    return {"address": address, "adverts": {f"{address}__{a['scanner_address']}": a for a in adverts}}


def build_dump(world, now):
    """What bermuda.dump_devices answers for `world`, {device address: {scanner: dB}}: a device with an
    advert one second old from each scanner, at the distance that dB means."""
    return envelope({address: device(address, [advert(s, db, now) for s, db in heard.items()])
                     for address, heard in world.items()})


def make_app(args=None, devices=(ANNA,), model=True, areas=None, world=None):
    """BleLocator with initialize() actually run and the AppDaemon primitives faked: a settable clock, a
    scheduler that only records, tasks that run when asked, states from app.states, a set_state that
    records into app.published and a bermuda.dump_devices that answers app.dump()."""
    app = bl.BleLocator.__new__(bl.BleLocator)
    app.args = {"model_file": new_path("model.json"), "log_file": new_path("log.jsonl"),
                "state_file": new_path("state.json"), "devices": [dict(d) for d in devices], **(args or {})}
    if model:
        write_model(app.args["model_file"], None if model is True else model)
    app.states = {d["bermuda_area"]: "Kitchen" for d in devices}
    app.states.update(areas or {})
    app.world = {d["address"]: {} for d in devices}
    app.world.update(world or {})
    app.clock = {"now": T0}
    app.logged, app.tasks, app.service_calls, app.scheduled, app.published = [], [], [], [], []
    app.delay = 0
    app.dump = lambda: build_dump(app.world, app._monotonic())

    async def call_service(service, **kw):
        app.service_calls.append((service, kw))
        if app.delay:
            await asyncio.sleep(app.delay)
        return app.dump()

    async def set_state(entity, **kw):
        if isinstance(getattr(app, "publish_error", None), Exception):
            raise app.publish_error
        app.published.append({"entity": entity, **kw})

    async def run_in_executor(func, *args):
        return func(*args)

    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.run_every = lambda callback, start, interval: app.scheduled.append((callback, start, interval))
    app.create_task = app.tasks.append
    app.call_service = call_service
    app.set_state = set_state
    app.run_in_executor = run_in_executor
    app.log = lambda message, level="INFO": app.logged.append((level, message))
    app._epoch = lambda: app.clock["now"]
    app._monotonic = lambda: MONO0 + app.clock["now"] - T0
    app.initialize()
    return app


def tick(app, seconds):
    """One scheduled tick at T0 + seconds, run to the end."""
    app.clock["now"] = T0 + seconds
    app._on_tick({})
    while app.tasks:
        asyncio.run(app.tasks.pop(0))


def ticks(app, first, count, step=5):
    for i in range(count):
        tick(app, first + i * step)


def hear(app, readings, address=ANNA["address"]):
    app.world[address] = dict(readings)


def last(app, name="anna"):
    entity = f"sensor.{name}_ble_room"
    return next(p for p in reversed(app.published) if p["entity"] == entity)


def states_of(app, name="anna"):
    return [p["state"] for p in app.published if p["entity"] == f"sensor.{name}_ble_room"]


def logged(app, level):
    return [message for lvl, message in app.logged if lvl == level]


def log_lines(app):
    try:
        with open(app.log_file, encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


class Likelihood(unittest.TestCase):
    def setUp(self):
        self.model = bl.Model(model_doc())

    def test_a_scanner_that_is_not_heard_adds_nothing_to_any_room(self):
        lls = self.model.log_likelihoods({S1: -50.0, S2: -70.0})
        for room in ROOMS:
            expected = sum(-0.5 * ((db - MEANS[room][s]) / SD) ** 2 - math.log(SD) for s, db in ((S1, -50.0), (S2, -70.0)))
            self.assertAlmostEqual(lls[room], expected)

    def test_a_missing_scanner_is_not_filled_with_a_weak_reading(self):
        heard = self.model.log_likelihoods({S1: -50.0, S2: -70.0})
        filled = self.model.log_likelihoods({S1: -50.0, S2: -70.0, S3: -100.0, S4: -100.0, S5: -100.0})
        self.assertNotAlmostEqual(heard["Kitchen"], filled["Kitchen"])
        self.assertEqual(self.model.log_likelihoods({}), {room: 0.0 for room in ROOMS})

    def test_a_pair_the_room_has_no_statistics_for_reads_minus_95_with_sd_10(self):
        doc = model_doc()
        del doc["stats"]["Kitchen"][S3]
        lls = bl.Model(doc).log_likelihoods({S3: -80.0})
        self.assertAlmostEqual(lls["Kitchen"], -0.5 * ((-80.0 + 95.0) / 10.0) ** 2 - math.log(10.0))
        self.assertAlmostEqual(lls["Bedroom"], -0.5 * ((-80.0 + 50.0) / SD) ** 2 - math.log(SD))

    def test_the_device_offset_is_subtracted_from_every_reading(self):
        weaker = self.model.log_likelihoods({s: db - 8 for s, db in reading("Kitchen").items()}, offset_db=-8.0)
        self.assertEqual(weaker, self.model.log_likelihoods(reading("Kitchen")))

    def test_the_best_room_is_the_one_whose_means_the_readings_follow(self):
        for room in ROOMS:
            probs = self.model.posterior(self.model.log_likelihoods(reading(room)))
            self.assertEqual(ROOMS[max(range(3), key=probs.__getitem__)], room)
            self.assertAlmostEqual(sum(probs), 1.0)

    def test_the_best_offset_fits_the_readings_to_the_room_weighted_by_one_over_sd_squared(self):
        self.assertAlmostEqual(self.model.best_offset("Kitchen", reading("Kitchen", delta=-8.0)), -8.0)
        doc = model_doc()
        doc["stats"]["Kitchen"][S1][1] = 3.0
        model = bl.Model(doc)
        readings = {S1: MEANS["Kitchen"][S1] - 10.0, S2: MEANS["Kitchen"][S2] - 2.0}
        w1, w2 = 1 / 9.0, 1 / 36.0
        self.assertAlmostEqual(model.best_offset("Kitchen", readings), (-10.0 * w1 + -2.0 * w2) / (w1 + w2))

    def test_readings_are_the_fresh_numeric_adverts_of_scanners_the_model_knows(self):
        now = MONO0 + 500.0
        outsider = "ee:ee:ee:ee:ee:ee"
        dev = device("d", [
            advert(S1, -50, now, age=0.0), advert(S2, -60, now, age=120.0), advert(S3, -70, now, age=120.5),
            advert(S4, -80, now, age=-1.0), advert(S5, -60, now, distance="None"), advert(outsider, -60, now)])
        dev["adverts"]["zero"] = advert(S3, -60, now, distance=0.0)
        dev["adverts"]["junk"] = "x"
        readings = bl.readings_from(dev, self.model, now, 120)
        self.assertEqual(set(readings), {S1, S2})
        self.assertAlmostEqual(readings[S1], -50.0, places=2)
        self.assertEqual(bl.readings_from(None, self.model, now, 120), {})

    def test_a_scanner_with_two_adverts_counts_once_by_its_newest(self):
        now = MONO0 + 500.0
        dev = {"adverts": {"old": advert(S1, -80, now, age=9.0), "new": advert(S1, -55, now, age=2.0),
                           "older": advert(S1, -90, now, age=20.0)}}
        readings = bl.readings_from(dev, self.model, now, 120)
        self.assertEqual(list(readings), [S1])
        self.assertAlmostEqual(readings[S1], -55.0, places=2)

    def test_a_far_scanner_reads_no_weaker_than_the_floor_and_scanner_addresses_compare_in_lower_case(self):
        now = MONO0 + 500.0
        dev = {"adverts": {"a": advert(S1.upper(), -99, now, distance=200.0)}}
        self.assertEqual(bl.readings_from(dev, self.model, now, 120), {S1: -100.0})


class Filter(unittest.TestCase):
    def test_the_stay_probability_follows_the_seconds_since_the_last_update(self):
        model = bl.Model(model_doc())
        flat = [1 / 3] * 3
        for before, after in zip(flat, model.advance(flat, flat, 5)):
            self.assertAlmostEqual(before, after)
        belief = model.advance([1.0, 0.0, 0.0], flat, 10)
        stay = 0.98 ** 2
        self.assertAlmostEqual(belief[0], stay)
        self.assertAlmostEqual(belief[1], (1 - stay) / 2)
        self.assertAlmostEqual(sum(belief), 1.0)

    def test_an_emission_is_floored_and_raised_to_the_power(self):
        model = bl.Model(model_doc())
        belief = model.advance([0.5, 0.5, 0.0], [1.0, 0.0, 0.0], 5)
        self.assertAlmostEqual(belief[1] / belief[0], 1e-12 ** 0.3, delta=1e-6)

    def test_a_room_that_fits_wins_and_is_reported_with_its_runner_up(self):
        app = make_app(world={ANNA["address"]: reading("Kitchen")})
        ticks(app, 5, 3)
        shown = last(app)
        self.assertEqual((shown["state"], shown["attributes"]["source"]), ("Kitchen", "model"))
        self.assertGreater(shown["attributes"]["confidence"], 0.9)
        self.assertIn(shown["attributes"]["runner_up"], ("Living room", "Bedroom"))
        self.assertLess(shown["attributes"]["runner_up_p"], 0.1)

    def test_one_odd_tick_does_not_switch_the_room_but_consistent_evidence_does(self):
        app = make_app()
        hear(app, reading("Kitchen"))
        ticks(app, 5, 10)
        self.assertEqual(states_of(app)[-1], "Kitchen")
        model = bl.Model(model_doc())
        odd = blend("Kitchen", "Living room", 0.6)
        probs = model.posterior(model.log_likelihoods(odd))
        self.assertEqual(ROOMS[max(range(3), key=probs.__getitem__)], "Living room")

        hear(app, odd)
        tick(app, 55)
        self.assertEqual(states_of(app)[-1], "Kitchen")
        hear(app, reading("Kitchen"))
        ticks(app, 60, 3)
        self.assertEqual(set(states_of(app)[-3:]), {"Kitchen"})

        hear(app, odd)
        seen = []
        for i in range(8):
            tick(app, 75 + 5 * i)
            seen.append(states_of(app)[-1])
        self.assertEqual(seen[0], "Kitchen")
        self.assertEqual(seen[-1], "Living room")
        self.assertLessEqual(seen.index("Living room"), 5)

    def test_clear_evidence_for_another_room_switches_within_a_tick_or_two(self):
        app = make_app()
        hear(app, reading("Kitchen"))
        ticks(app, 5, 10)
        hear(app, reading("Bedroom"))
        ticks(app, 55, 2)
        self.assertEqual(states_of(app)[-1], "Bedroom")

    def test_the_belief_starts_again_after_a_gap_longer_than_fresh_s(self):
        weak = blend("Kitchen", "Bedroom", 0.52)
        model = bl.Model(model_doc())
        probs = model.posterior(model.log_likelihoods(weak))
        self.assertEqual(ROOMS[max(range(3), key=probs.__getitem__)], "Bedroom")
        self.assertEqual(ROOMS[max(range(3), key=model.advance([1.0, 0.0, 0.0], probs, 126).__getitem__)], "Kitchen")

        for gap, room in ((126, "Bedroom"), (115, "Kitchen")):
            with self.subTest(gap=gap):
                app = make_app()
                hear(app, reading("Kitchen"))
                ticks(app, 5, 10)
                hear(app, weak)
                tick(app, 50 + gap)
                self.assertEqual(states_of(app)[-1], room)

    def test_the_belief_starts_again_when_a_new_model_lists_other_rooms_or_the_same_in_another_order(self):
        weak = blend("Bedroom", "Kitchen", 0.52)
        for rooms in (["Bedroom", "Living room", "Kitchen"], ["Hall", "Kitchen"]):
            with self.subTest(rooms=rooms):
                means = {**MEANS, "Hall": MEANS["Bedroom"]}
                app = make_app()
                hear(app, reading("Kitchen"))
                ticks(app, 5, 10)
                write_model(app.model_file, model_doc(means=means, rooms=rooms), mtime=T0 + 100)
                hear(app, weak)
                tick(app, 55)
                self.assertEqual((app._tracks[0].rooms, len(app._tracks[0].belief)), (rooms, len(rooms)))
                self.assertEqual(states_of(app)[-1], "Kitchen")


class Offset(unittest.TestCase):
    def test_a_device_8_db_weaker_is_learned_and_the_room_stays_right(self):
        app = make_app(args={"offset_tau_s": 600})
        rng = random.Random(7)
        for i in range(200):
            hear(app, {s: db - 8 + rng.gauss(0, 3) for s, db in reading("Kitchen").items()})
            tick(app, 5 * (i + 1))
        shown = last(app)
        self.assertEqual(shown["state"], "Kitchen")
        self.assertAlmostEqual(shown["attributes"]["offset_db"], -8.0, delta=1.0)
        self.assertEqual(set(states_of(app)), {"Kitchen"})

    def test_a_device_that_starts_wrong_by_8_db_in_the_living_room_is_located_after_the_first_ticks(self):
        app = make_app()
        hear(app, reading("Living room", delta=-8.0))
        ticks(app, 5, 30)
        self.assertEqual(last(app)["state"], "Living room")
        self.assertAlmostEqual(last(app)["attributes"]["offset_db"], -8.0, delta=0.5)

    def test_the_offset_is_a_mean_until_it_spans_tau_and_then_an_exponential_average(self):
        app = make_app(args={"offset_tau_s": 60})
        hear(app, reading("Kitchen", delta=-8.0))
        ticks(app, 5, 60)
        track = app._tracks[0]
        self.assertAlmostEqual(track.offset, -8.0, places=3)
        self.assertEqual(track.offset_s, 60)
        hear(app, reading("Kitchen"))
        ticks(app, 305, 12)
        self.assertAlmostEqual(track.offset, -8.0 * (1 - 5 / 60) ** 12, delta=0.05)

    def test_a_tick_with_fewer_than_four_fresh_scanners_answers_but_does_not_move_the_offset(self):
        app = make_app()
        hear(app, reading("Kitchen", delta=-8.0, scanners=(S1, S2, S3)))
        ticks(app, 5, 5)
        self.assertEqual(last(app)["attributes"]["source"], "model")
        self.assertEqual(app._tracks[0].offset, 0.0)
        hear(app, reading("Kitchen", delta=-8.0, scanners=(S1, S2, S3, S4)))
        tick(app, 30)
        self.assertNotEqual(app._tracks[0].offset, 0.0)

    def test_a_bermuda_answer_never_moves_the_offset(self):
        app = make_app(areas={"sensor.anna_area": "Landing"})
        hear(app, reading("Kitchen", delta=-8.0))
        ticks(app, 5, 5)
        self.assertEqual((last(app)["attributes"]["source"], app._tracks[0].offset, app._tracks[0].offset_s), ("bermuda", 0.0, 0.0))

    def test_offsets_are_saved_at_most_once_a_minute_and_read_back_at_start(self):
        app = make_app()
        hear(app, reading("Kitchen", delta=-8.0))
        with patch.object(bl, "write_json", wraps=bl.write_json) as write:
            ticks(app, 5, 24)
            self.assertEqual(write.call_count, 2)
        with open(app.state_file, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["version"], 1)
        self.assertAlmostEqual(saved["offsets"][ANNA["address"]]["db"], -8.0, delta=0.01)
        self.assertEqual(saved["offsets"][ANNA["address"]]["s"], 65.0)
        again = make_app(args={"state_file": app.state_file, "model_file": app.model_file})
        self.assertAlmostEqual(again._tracks[0].offset, -8.0, delta=0.01)
        self.assertEqual(again._tracks[0].offset_s, 65.0)

    def test_the_state_file_is_written_when_the_app_stops(self):
        app = make_app()
        hear(app, reading("Kitchen", delta=-8.0))
        ticks(app, 5, 2)
        os.remove(app.state_file)
        app.terminate()
        self.assertAlmostEqual(bl.read_state(app.state_file)[ANNA["address"]][0], -8.0, delta=0.01)

    def test_a_state_file_that_cannot_be_read_means_no_offsets(self):
        path = new_path("state.json")
        for content in ("", "[1]", "{", '{"version": 1, "offsets": {"x": {"db": "a", "s": 1}}}', '{"version": 2, "offsets": {}}'):
            with self.subTest(content=content):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.assertEqual(bl.read_state(path), {})
        self.assertEqual(bl.read_state(new_path("none.json")), {})


class Fallback(unittest.TestCase):
    def run_one(self, area, readings=None, **kw):
        app = make_app(areas={"sensor.anna_area": area}, **kw)
        hear(app, reading("Kitchen") if readings is None else readings)
        ticks(app, 5, 3)
        return app, last(app)

    def assert_bermuda(self, shown, state, fresh):
        self.assertEqual(shown["state"], state)
        a = shown["attributes"]
        self.assertEqual((a["source"], a["fresh_scanners"]), ("bermuda", fresh))
        self.assertEqual((a["confidence"], a["runner_up"], a["runner_up_p"]), (None, None, None))

    def test_the_model_answers_when_bermuda_has_a_model_room_and_enough_scanners_are_fresh(self):
        _, shown = self.run_one("Living room", reading("Kitchen"))
        self.assertEqual((shown["state"], shown["attributes"]["source"]), ("Kitchen", "model"))

    def test_fewer_than_min_scanners_fresh_gives_bermuda_s_area(self):
        app, shown = self.run_one("Living room", reading("Kitchen", scanners=(S1, S2)))
        self.assert_bermuda(shown, "Living room", 2)
        hear(app, reading("Kitchen", scanners=(S1, S2, S3)))
        tick(app, 30)
        self.assertEqual(last(app)["attributes"]["source"], "model")

    def test_min_scanners_comes_from_the_yaml(self):
        _, shown = self.run_one("Kitchen", reading("Kitchen", scanners=(S1, S2, S3)), args={"min_scanners": 4})
        self.assert_bermuda(shown, "Kitchen", 3)

    def test_a_stale_scanner_is_not_fresh(self):
        app = make_app(areas={"sensor.anna_area": "Bedroom"})
        now = app._monotonic
        app.dump = lambda: envelope({ANNA["address"]: device(ANNA["address"], [
            advert(S1, -50, now()), advert(S2, -70, now()), advert(S3, -85, now(), age=121.0), advert(S4, -90, now(), distance="None")])})
        tick(app, 5)
        self.assert_bermuda(last(app), "Bedroom", 2)

    def test_a_bermuda_area_the_model_has_no_room_for_is_published_as_it_is(self):
        for area in ("Landing", "not_home", "Kristines room", "Technical room", "kitchen"):
            with self.subTest(area=area):
                _, shown = self.run_one(area)
                self.assert_bermuda(shown, area, 5)

    def test_no_bermuda_area_at_all_is_published_as_unknown(self):
        for area in (None, "", "unknown"):
            with self.subTest(area=area):
                _, shown = self.run_one(area)
                self.assert_bermuda(shown, "unknown", 5)
        _, shown = self.run_one("unavailable")
        self.assert_bermuda(shown, "unavailable", 5)

    def test_a_missing_model_file_is_a_bermuda_answer_and_one_warning(self):
        app = make_app(model=False, areas={"sensor.anna_area": "Hallway"})
        hear(app, reading("Kitchen"))
        ticks(app, 5, 6)
        self.assert_bermuda(last(app), "Hallway", 0)
        self.assertEqual(last(app)["attributes"]["model_date"], None)
        self.assertEqual(len(logged(app, "WARNING")), 1)
        self.assertIn("does not exist", logged(app, "WARNING")[0])

    def test_a_model_older_than_max_model_age_days_is_a_bermuda_answer_and_one_warning(self):
        old = iso(T0 - 15 * 86400)
        app = make_app(model=model_doc(trained_at=old), areas={"sensor.anna_area": "Kitchen"})
        hear(app, reading("Bedroom"))
        ticks(app, 5, 6)
        self.assert_bermuda(last(app), "Kitchen", 0)
        self.assertEqual(len(logged(app, "WARNING")), 1)
        self.assertIn("older than 14 days", logged(app, "WARNING")[0])
        self.assertEqual(last(app)["attributes"]["model_date"], None)

    def test_a_model_is_stale_only_past_max_model_age_days_and_the_limit_comes_from_the_yaml(self):
        fresh = make_app(model=model_doc(trained_at=iso(T0 - 13 * 86400)))
        hear(fresh, reading("Bedroom"))
        tick(fresh, 5)
        self.assertEqual(last(fresh)["attributes"]["source"], "model")
        longer = make_app(args={"max_model_age_days": 30}, model=model_doc(trained_at=iso(T0 - 20 * 86400)))
        hear(longer, reading("Bedroom"))
        tick(longer, 5)
        self.assertEqual(last(longer)["attributes"]["source"], "model")

    def test_a_model_that_ages_past_the_limit_while_the_app_runs_stops_answering(self):
        app = make_app(model=model_doc(trained_at=iso(T0 - 14 * 86400 + 600)))
        hear(app, reading("Bedroom"))
        tick(app, 5)
        self.assertEqual(last(app)["attributes"]["source"], "model")
        tick(app, 700)
        self.assertEqual(last(app)["attributes"]["source"], "bermuda")
        self.assertEqual(len(logged(app, "WARNING")), 1)

    def test_a_model_file_that_is_no_usable_model_is_a_bermuda_answer_and_one_warning(self):
        broken = {"not json": None, "wrong version": {**model_doc(), "version": 2},
                  "one room": {**model_doc(rooms=["Kitchen"])},
                  "no constants": {k: v for k, v in model_doc().items() if k != "constants"},
                  "bad sd": {**model_doc(), "stats": {r: {s: [-50, 0, 3] for s in SCANNERS} for r in ROOMS}},
                  "bad date": {**model_doc(), "trained_at": "yesterday"},
                  "bad p_stay": {**model_doc(), "constants": {**CONSTANTS, "p_stay": 1.0}}}
        for name, doc in broken.items():
            with self.subTest(name):
                app = make_app(model=False if doc is None else doc, areas={"sensor.anna_area": "Kitchen"})
                if doc is None:
                    with open(app.model_file, "w", encoding="utf-8") as f:
                        f.write("{")
                hear(app, reading("Bedroom"))
                ticks(app, 5, 4)
                self.assert_bermuda(last(app), "Kitchen", 0)
                self.assertEqual(len(logged(app, "WARNING")), 1)
                self.assertIn("unusable", logged(app, "WARNING")[0])

    def test_a_device_missing_from_a_dump_that_holds_others_has_no_readings_and_is_warned_about_once(self):
        app = make_app(devices=(ANNA, BEN), areas={"sensor.anna_area": "Kitchen", "sensor.ben_area": "Bedroom"})
        app.world = {BEN["address"]: reading("Bedroom")}
        ticks(app, 5, 4)
        self.assert_bermuda(last(app, "anna"), "Kitchen", 0)
        self.assertEqual(last(app, "ben")["attributes"]["source"], "model")
        self.assertEqual(len([m for m in logged(app, "WARNING") if "holds no device" in m]), 1)

    def test_a_dump_in_another_envelope_shape_still_reaches_the_devices(self):
        devices = {ANNA["address"].upper(): device(ANNA["address"], [advert(S1, -50, MONO0)])}
        for resp in (envelope(devices), {"result": {"response": devices}}, {"response": devices}, devices):
            with self.subTest(resp=list(resp)):
                self.assertEqual(list(bl.devices_from(resp)), [ANNA["address"]])
        for resp in (None, [], "x", 5, {}, {"success": False, "error": {"code": "x", "message": "y"}},
                     envelope({}), envelope({"a": "not a device"}), envelope({"a": {"adverts": None}})):
            with self.subTest(resp=resp):
                self.assertEqual(bl.devices_from(resp), {})


class FailedDump(unittest.TestCase):
    def failing(self, app, answer):
        def dump():
            if isinstance(answer, Exception):
                raise answer
            return answer
        app.dump = dump

    def settled(self):
        app = make_app()
        hear(app, reading("Kitchen"))
        ticks(app, 5, 4)
        return app

    def test_a_dump_that_fails_keeps_the_last_state_and_writes_nothing(self):
        refused = {"success": False, "error": {"code": "not_found", "message": "Service not found"}}
        for answer in (RuntimeError("bermuda is down"), {}, None, "junk", refused, envelope({}),
                       envelope({BEN["address"]: device(BEN["address"], [])})):
            with self.subTest(answer=answer):
                app = self.settled()
                published, lines, belief = len(app.published), len(log_lines(app)), list(app._tracks[0].belief)
                self.failing(app, answer)
                ticks(app, 25, 5)
                self.assertEqual(len(app.published), published)
                self.assertEqual(len(log_lines(app)), lines)
                self.assertEqual(app._tracks[0].belief, belief)
                self.assertEqual(states_of(app)[-1], "Kitchen")
                self.assertEqual(len(logged(app, "WARNING")), 1)

    def test_a_dump_that_hangs_is_skipped_after_the_timeout(self):
        app = self.settled()
        published = len(app.published)
        app.delay = 5
        with patch.object(bl, "CALL_TIMEOUT_S", 0.01):
            ticks(app, 25, 3)
        self.assertEqual(len(app.published), published)
        self.assertEqual(len(logged(app, "WARNING")), 1)

    def test_the_filter_carries_on_when_the_dump_comes_back_inside_fresh_s(self):
        app = self.settled()
        good = app.dump
        self.failing(app, RuntimeError("down"))
        ticks(app, 25, 4)
        app.dump = good
        hear(app, reading("Kitchen"))
        tick(app, 60)
        self.assertEqual(states_of(app)[-1], "Kitchen")
        self.assertEqual(len(logged(app, "WARNING")), 1)
        self.assertEqual(len([m for m in logged(app, "INFO") if "answers again" in m]), 1)

    def test_one_tick_is_skipped_while_the_previous_one_is_still_running(self):
        app = make_app()
        hear(app, reading("Kitchen"))
        app.clock["now"] = T0 + 5
        app._on_tick({})
        app._on_tick({})
        self.assertEqual(len(app.tasks), 1)
        asyncio.run(app.tasks.pop(0))
        app._on_tick({})
        self.assertEqual(len(app.tasks), 1)
        asyncio.run(app.tasks.pop(0))

    def test_a_publish_that_fails_does_not_stop_the_log_or_the_next_tick(self):
        app = make_app()
        hear(app, reading("Kitchen"))
        app.publish_error = RuntimeError("HA is down")
        ticks(app, 5, 3)
        self.assertEqual(app.published, [])
        self.assertEqual(len(log_lines(app)), 1)
        self.assertEqual(len(logged(app, "WARNING")), 1)
        app.publish_error = None
        tick(app, 20)
        self.assertEqual(states_of(app), ["Kitchen"])

    def test_a_log_that_cannot_be_written_is_warned_about_once_and_the_sensor_still_publishes(self):
        app = make_app(args={"log_file": os.path.join(_DIR.name, "no", "such", "dir", "log.jsonl")})
        hear(app, reading("Kitchen"))
        ticks(app, 5, 30)
        self.assertEqual(len(logged(app, "WARNING")), 1)
        self.assertIn("could not write", logged(app, "WARNING")[0])
        self.assertEqual(states_of(app)[-1], "Kitchen")


class ModelFile(unittest.TestCase):
    def test_the_model_is_read_again_when_its_mtime_changes_and_not_otherwise(self):
        app = make_app(model=False)
        write_model(app.model_file, model_doc(trained_at=iso(T0 - 3600)), mtime=T0 + 1)
        hear(app, reading("Kitchen"))
        with patch.object(bl, "Model", wraps=bl.Model) as parsed:
            ticks(app, 5, 4)
            self.assertEqual(parsed.call_count, 1)
        self.assertEqual(last(app)["attributes"]["model_date"], iso(T0 - 3600)[:10])
        self.assertEqual(last(app)["state"], "Kitchen")

        swapped = {"Kitchen": MEANS["Bedroom"], "Living room": MEANS["Living room"], "Bedroom": MEANS["Kitchen"]}
        write_model(app.model_file, model_doc(trained_at=iso(T0 - 90000), means=swapped), mtime=T0 + 2)
        with patch.object(bl, "Model", wraps=bl.Model) as parsed:
            ticks(app, 30, 4)
            self.assertEqual(parsed.call_count, 1)
        self.assertEqual(last(app)["attributes"]["model_date"], iso(T0 - 90000)[:10])
        self.assertEqual(last(app)["state"], "Bedroom")
        self.assertEqual(len([m for m in logged(app, "INFO") if "loaded" in m]), 2)

    def test_a_model_file_that_appears_later_is_picked_up_and_one_that_goes_away_is_missed(self):
        app = make_app(model=False, areas={"sensor.anna_area": "Hallway"})
        hear(app, reading("Kitchen"))
        ticks(app, 5, 2)
        self.assertEqual(last(app)["attributes"]["source"], "bermuda")
        write_model(app.model_file)
        app.states["sensor.anna_area"] = "Kitchen"
        ticks(app, 15, 2)
        self.assertEqual(last(app)["attributes"]["source"], "model")
        os.remove(app.model_file)
        ticks(app, 25, 2)
        self.assertEqual(last(app)["attributes"]["source"], "bermuda")
        self.assertEqual(len(logged(app, "WARNING")), 2)

    def test_a_model_replaced_by_a_broken_one_stops_answering_until_a_good_one_is_written(self):
        app = make_app()
        hear(app, reading("Kitchen"))
        ticks(app, 5, 2)
        with open(app.model_file, "w", encoding="utf-8") as f:
            f.write("{")
        os.utime(app.model_file, (T0 + 50, T0 + 50))
        ticks(app, 15, 2)
        self.assertEqual(last(app)["attributes"]["source"], "bermuda")
        write_model(app.model_file, mtime=T0 + 60)
        ticks(app, 25, 2)
        self.assertEqual(last(app)["attributes"]["source"], "model")
        self.assertEqual(len(logged(app, "WARNING")), 1)
        self.assertEqual(len([m for m in logged(app, "INFO") if "usable" in m]), 1)

    def test_the_default_model_file_sits_beside_the_module(self):
        app = bl.BleLocator.__new__(bl.BleLocator)
        app.args = {"devices": [dict(ANNA)]}
        app.log = lambda message, level="INFO": None
        app.run_every = lambda *a: None
        app.initialize()
        here = Path(bl.__file__).resolve().parent
        self.assertEqual((app.model_file, app.log_file, app.state_file),
                         tuple(str(here / name) for name in ("ble_locator_model.json", "ble_locator_log.jsonl",
                                                             "ble_locator_state.json")))


class Output(unittest.TestCase):
    def test_the_sensor_carries_the_documented_attributes(self):
        app = make_app(devices=(ANNA, BEN))
        app.world = {ANNA["address"]: reading("Living room"), BEN["address"]: reading("Bedroom")}
        app.states.update({"sensor.anna_area": "Living room", "sensor.ben_area": "Bedroom"})
        tick(app, 5)
        shown = last(app, "anna")
        self.assertEqual(shown["state"], "Living room")
        self.assertTrue(shown["replace"])
        self.assertEqual(set(shown["attributes"]), {"friendly_name", "icon", "confidence", "runner_up", "runner_up_p", "source",
                                                    "fresh_scanners", "offset_db", "model_date"})
        a = shown["attributes"]
        self.assertEqual((a["source"], a["fresh_scanners"], a["model_date"]), ("model", 5, iso(T0 - 3600)[:10]))
        self.assertEqual(last(app, "ben")["entity"], "sensor.ben_ble_room")
        self.assertEqual(a["friendly_name"], "Anna BLE room")

    def test_a_published_room_change_and_each_minute_per_device_write_one_log_line(self):
        app = make_app()
        hear(app, reading("Kitchen"))
        ticks(app, 5, 25)
        lines = log_lines(app)
        self.assertEqual([round(line["t"] - T0) for line in lines], [5, 65, 125])
        self.assertEqual(set(lines[0]), {"t", "device", "room", "confidence", "runner_up", "source", "bermuda_area",
                                         "fresh_scanners", "offset_db"})
        self.assertEqual((lines[0]["device"], lines[0]["room"], lines[0]["source"], lines[0]["bermuda_area"],
                          lines[0]["fresh_scanners"]), ("anna", "Kitchen", "model", "Kitchen", 5))
        hear(app, reading("Bedroom"))
        tick(app, 130)
        tick(app, 135)
        self.assertEqual([(line["room"], round(line["t"] - T0)) for line in log_lines(app)[3:]], [("Bedroom", 130)])

    def test_every_tick_publishes_every_device_and_each_is_logged_on_its_own_clock(self):
        app = make_app(devices=(ANNA, BEN))
        hear(app, reading("Kitchen"))
        hear(app, reading("Kitchen"), BEN["address"])
        ticks(app, 5, 3)
        self.assertEqual(len(app.published), 6)
        self.assertEqual([line["device"] for line in log_lines(app)], ["anna", "ben"])

    def test_the_dump_is_asked_once_for_every_device_with_spaces_between_the_addresses(self):
        app = make_app(devices=(ANNA, BEN))
        ticks(app, 5, 2)
        self.assertEqual(len(app.service_calls), 2)
        self.assertEqual(app.service_calls[0], ("bermuda/dump_devices", {
            "return_response": True, "suppress_log_messages": True, "redact": False,
            "addresses": f"{ANNA['address']} {BEN['address']}"}))


class Setup(unittest.TestCase):
    def test_defaults_and_the_first_tick_one_interval_after_start(self):
        app = make_app()
        self.assertEqual((app.interval_s, app.fresh_s, app.min_scanners, app.offset_tau_s, app.max_model_age_days),
                         (5.0, 120.0, 3, 3600.0, 14.0))
        self.assertEqual(app.scheduled, [(app._on_tick, "now+5", 5.0)])

    def test_the_yaml_values_pass_through(self):
        app = make_app(args={"interval_s": 10, "fresh_s": 60, "min_scanners": 4, "offset_tau_s": 900,
                             "max_model_age_days": 3})
        self.assertEqual((app.interval_s, app.fresh_s, app.min_scanners, app.offset_tau_s, app.max_model_age_days),
                         (10.0, 60.0, 4, 900.0, 3.0))
        self.assertEqual(app.scheduled[0][1:], ("now+10", 10.0))

    def test_a_value_that_would_break_the_app_fails_at_start(self):
        for args in ({"interval_s": 0}, {"fresh_s": -1}, {"min_scanners": 0}, {"offset_tau_s": 0},
                     {"max_model_age_days": 0}, {"devices": []}, {"devices": [{"name": "x", "address": "y"}]},
                     {"devices": ["x"]}, {"devices": [dict(ANNA), dict(ANNA, name="other")]},
                     {"devices": [dict(ANNA), dict(BEN, name="anna")]}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    make_app(args=args)

    def test_addresses_are_lower_cased_and_the_sensor_is_named_after_the_device(self):
        app = make_app(devices=(dict(ANNA, address="7C:D9:F4:14:23:77"),))
        self.assertEqual((app._tracks[0].address, app._tracks[0].entity), ("7c:d9:f4:14:23:77", "sensor.anna_ble_room"))

    def test_the_shipped_yaml_runs_the_class_with_the_three_devices_and_the_documented_values(self):
        path = Path(bl.__file__).with_suffix(".yaml")
        text = path.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?m)^\s*log:")
        config = yaml.safe_load(text)["BleLocator"]
        self.assertEqual((config["module"], config["class"]), ("ble_locator", "BleLocator"))
        self.assertEqual({d["name"]: d["address"] for d in config["devices"]}, {
            "mikkel": "d6462f88944242db9e31bdff598edc25_100_40004", "claudia": "89f35c9874aed44c554015fe6eaa3545",
            "rober2_beacon": "7c:d9:f4:14:23:77"})
        self.assertEqual({d["name"]: d["bermuda_area"] for d in config["devices"]}, {
            "mikkel": "sensor.bermuda_d6462f88944242db9e31bdff598edc25_100_40004_area",
            "claudia": "sensor.claudias_iphone_ble_area", "rober2_beacon": "sensor.mp1_142377_area"})
        self.assertEqual({k: config[k] for k in ("interval_s", "fresh_s", "min_scanners", "offset_tau_s",
                                                 "max_model_age_days")}, {
            "interval_s": 5, "fresh_s": 120, "min_scanners": 3, "offset_tau_s": 3600, "max_model_age_days": 14})
        args = {k: v for k, v in config.items() if k not in ("module", "class")}
        app = make_app(args=args, devices=config["devices"])
        self.assertEqual([t.entity for t in app._tracks], ["sensor.mikkel_ble_room", "sensor.claudia_ble_room",
                                                           "sensor.rober2_beacon_ble_room"])


class TrainerAgrees(unittest.TestCase):
    """scripts/ble_locator_train.py scores the model held-out with its own copy of the arithmetic; its model
    file must be one the app reads and both must give the same numbers."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[3] / "scripts" / "ble_locator_train.py"
        spec = importlib.util.spec_from_file_location("ble_locator_train", path)
        cls.trainer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.trainer)

    def points(self):
        rng = random.Random(3)
        out = []
        for room in ROOMS:
            for i in range(40):
                out.append({"room": room, "db": {s: max(m + rng.gauss(0, 5), -100.0) for s, m in MEANS[room].items()
                                                 if not (s == S4 and i % 5 == 0)}})
        return out

    def test_the_file_the_trainer_writes_is_a_model_the_app_reads_and_scores_alike(self):
        t = self.trainer
        stats = t.fit(self.points())
        by_address = {s: {"address": s, "name": f"scanner {s[-1]}", "area": "Kitchen"} for s in SCANNERS}
        doc = t.model_document(ROOMS, by_address, stats, (T0 - 7200, T0 - 3600), {})
        model = bl.Model(json.loads(json.dumps(doc)))
        self.assertEqual(model.rooms, ROOMS)
        self.assertEqual(model.c, {k: float(doc["constants"][k]) for k in bl.CONSTANTS})
        rounded = {room: {a: tuple(v) for a, v in per.items()} for room, per in doc["stats"].items()}
        rng = random.Random(11)
        belief_t = belief_a = [1 / 3] * 3
        for _ in range(60):
            readings = {s: rng.uniform(-100, -45) for s in SCANNERS if rng.random() < 0.8}
            offset = rng.uniform(-8, 3)
            theirs = t.log_likelihoods(rounded, ROOMS, readings, offset)
            ours = model.log_likelihoods(readings, offset)
            for room in ROOMS:
                self.assertAlmostEqual(theirs[room], ours[room])
            if not readings:
                continue
            probs_t, probs_a = t.posterior(theirs, ROOMS), model.posterior(ours)
            self.assertEqual(probs_t, probs_a)
            dt = rng.choice((5, 5, 10, 25))
            belief_t, belief_a = t.advance(belief_t, probs_t, dt), model.advance(belief_a, probs_a, dt)
            self.assertEqual(belief_t, belief_a)

    def test_the_trainer_and_the_app_turn_a_distance_into_the_same_reading(self):
        model = bl.Model(model_doc())
        for metres in (0.3, 1.0, 4.2, 17.0, 31.6, 80.0):
            self.assertEqual(self.trainer.to_db(metres), model.to_db(metres))
        self.assertEqual(self.trainer.CONSTANTS["p_stay"], CONSTANTS["p_stay"])
        self.assertEqual(self.trainer.CONSTANTS, CONSTANTS)


if __name__ == "__main__":
    unittest.main()
