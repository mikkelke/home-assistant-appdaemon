# tests/test_price_forecast_tape.py - the tape's source parsers, the JSONL append and one run with a
# failing source. Run from repo root: python3 -m unittest discover -s apps/energy/tests -q
#
# Fixtures are responses fetched on 2026-10-14. eds_dayahead.json is verbatim (Energinet open
# data). The third-party forecasts keep their structure, keys and timestamps, with synthetic prices:
# this repo is public, and Smartere Elforbrug's licence forbids redistributing its forecasts. The
# Open Spot Forecast fixtures follow the attribute layouts of its v0.2.1 code.

import asyncio
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass", "appdaemon.plugins.hass.hassapi"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["appdaemon.plugins.hass.hassapi"].Hass = object

import price_forecast_tape as tape  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name):
    with open(FIXTURES / name) as f:
        return json.load(f)


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def values(line):
    return {utc(hour): value for hour, value in line["points"]}


class Hourly(unittest.TestCase):
    T = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)

    def test_quarters_average_to_their_hour(self):
        slots = [(self.T + timedelta(minutes=15 * i), 15, float(i)) for i in range(4)]
        self.assertEqual(tape.hourly(slots), {int(self.T.timestamp()): 1.5})

    def test_a_partial_or_doubled_hour_is_dropped(self):
        partial = [(self.T + timedelta(minutes=15 * i), 15, 1.0) for i in range(3)]
        doubled = [(self.T, 60, 1.0), (self.T, 60, 2.0)]
        self.assertEqual(tape.hourly(partial), {})
        self.assertEqual(tape.hourly(doubled), {})

    def test_a_slot_without_a_value_leaves_its_hour_incomplete(self):
        slots = [(self.T, 30, 1.0), (self.T + timedelta(minutes=30), 30, None)]
        self.assertEqual(tape.hourly(slots), {})


class PriceEntity(unittest.TestCase):
    def setUp(self):
        self.attrs = fixture("price_entity.json")["attributes"]

    def test_carnot_is_all_in_with_the_tariffs_it_was_priced_on(self):
        line = tape.parse_carnot(self.attrs)
        self.assertEqual((line["source"], line["kind"], line["unit"], line["basis"]), ("carnot", "forecast", "DKK/kWh", "all_in"))
        self.assertEqual(len(line["points"]), 136)
        self.assertEqual(line["points"][0], ["2026-10-15T22:00:00Z", self.attrs["forecast"][0]["price"]])
        self.assertEqual(line["points"][-1][0], "2026-10-21T13:00:00Z")
        table = line["tariffs_incl_vat"]
        self.assertEqual(len(table), 24)
        self.assertAlmostEqual(table[3], 0.133 + 0.054 + 0.09 + 0.01)
        self.assertAlmostEqual(table[18], 1.194 + 0.054 + 0.09 + 0.01)

    def test_carnot_needs_its_tariffs(self):
        attrs = dict(self.attrs, tariffs={"tariffs": {"0": 0.1}, "additional_tariffs": {}})
        self.assertIsNone(tape.parse_carnot(attrs))
        self.assertIsNone(tape.parse_carnot(dict(self.attrs, forecast=[])))
        self.assertIsNone(tape.parse_carnot(None))

    def test_actuals_take_tomorrow_only_once_it_is_valid(self):
        line = tape.parse_price_actuals(self.attrs)
        self.assertEqual((line["source"], line["kind"], line["basis"]), ("eds_sensor", "actual", "all_in"))
        self.assertEqual(len(line["points"]), 48)
        self.assertEqual(line["points"][0], ["2026-10-13T22:00:00Z", 1.338])
        self.assertEqual(len(line["tariffs_incl_vat"]), 24)
        self.assertEqual(len(tape.parse_price_actuals(dict(self.attrs, tomorrow_valid=False))["points"]), 24)

    def test_quarter_hour_raw_prices_average_per_hour(self):
        start = datetime(2026, 10, 14, 0, tzinfo=tape.TZ)
        raw = [{"hour": (start + timedelta(minutes=15 * i)).isoformat(), "price": 1.0 + (i % 4)} for i in range(96)]
        line = tape.parse_price_actuals({"raw_today": raw, "tomorrow_valid": False})
        self.assertEqual(len(line["points"]), 24)
        self.assertEqual(line["points"][0], ["2026-10-13T22:00:00Z", 2.5])

    def test_cent_prices_are_labelled(self):
        self.assertEqual(tape.parse_price_actuals(dict(self.attrs, use_cent=True))["unit"], "ore/kWh")


class Stromligning(unittest.TestCase):
    def setUp(self):
        self.resp = fixture("stromligning.json")

    def test_only_forecast_slots_are_kept_as_spot(self):
        line = tape.parse_stromligning(self.resp)
        forecast = [p for p in self.resp["prices"] if p.get("forecast")]
        self.assertEqual((line["unit"], line["basis"]), ("DKK/kWh", "spot"))
        self.assertEqual(len(line["points"]), len(forecast))
        self.assertEqual(line["points"][0], ["2026-10-15T22:00:00Z", forecast[0]["details"]["electricity"]["value"]])

    def test_quarter_forecast_slots_average_and_a_partial_hour_drops(self):
        item = self.resp["prices"][-1]
        quarters = []
        for i in range(7):
            q = copy.deepcopy(item)
            q["date"] = f"2026-10-17T{10 + i // 4:02d}:{15 * (i % 4):02d}:00.000Z"
            q["resolution"] = "15m"
            q["details"]["electricity"]["value"] = float(i)
            quarters.append(q)
        line = tape.parse_stromligning({"prices": quarters})
        self.assertEqual(line["points"], [["2026-10-17T10:00:00Z", 1.5]])

    def test_nothing_usable(self):
        self.assertIsNone(tape.parse_stromligning({"prices": [p for p in self.resp["prices"] if not p.get("forecast")]}))
        self.assertIsNone(tape.parse_stromligning({"error": "rate limited"}))


class Smartere(unittest.TestCase):
    def setUp(self):
        self.resp = fixture("smartere.json")

    def test_dk2_in_eur_per_mwh_on_utc_hours(self):
        line = tape.parse_smartere(self.resp)
        self.assertEqual((line["unit"], line["basis"]), ("EUR/MWh", "spot"))
        self.assertEqual(len(line["points"]), 131)
        self.assertEqual(line["points"][0], ["2026-10-14T13:00:00Z", self.resp["DK2"][0]["Price"]])
        self.assertEqual(line["points"][-1][0], "2026-10-19T23:00:00Z")

    def test_created_is_danish_local_time(self):
        self.assertEqual(tape.parse_smartere(self.resp)["created"], "2026-10-14T12:48:41Z")

    def test_other_area_and_missing_area(self):
        self.assertEqual(tape.parse_smartere(self.resp, "DK1")["points"][0][1], self.resp["DK1"][0]["Price"])
        self.assertIsNone(tape.parse_smartere(self.resp, "SE4"))


class EnergyPriceForecastEU(unittest.TestCase):
    def setUp(self):
        self.resp = fixture("epf.json")

    def test_forecast_entries_hourly(self):
        line = tape.parse_epf(self.resp)
        self.assertEqual((line["unit"], line["basis"]), ("DKK/kWh", "spot"))
        self.assertEqual(len(line["points"]), 48)
        self.assertEqual(line["points"][0], ["2026-10-14T15:00:00Z", self.resp["entries"][0]["value"]])
        self.assertEqual(line["created"], "2026-10-14T14:09:53Z")

    def test_day_ahead_entries_are_not_forecasts(self):
        for entry in self.resp["entries"][:10]:
            entry["source"] = "day_ahead"
        self.assertEqual(len(tape.parse_epf(self.resp)["points"]), 38)

    def test_unknown_unit(self):
        self.assertIsNone(tape.parse_epf(dict(self.resp, unit="SEK/kWh")))


class OpenSpotForecast(unittest.TestCase):
    def setUp(self):
        self.detailed = fixture("osf_detailed.json")["attributes"]
        self.compact = fixture("osf_compact.json")["attributes"]
        self.table = tape.tariff_table(fixture("price_entity.json")["attributes"]["tariffs"])

    def test_detailed_skips_confirmed_slots_and_removes_vat(self):
        line = tape.parse_osf(self.detailed)
        self.assertEqual((line["source"], line["unit"], line["basis"], line["is_ml_model"]), ("osf", "DKK/kWh", "spot", True))
        self.assertEqual(len(line["points"]), 25)
        first = [p["price"] for p in self.detailed["predictions"][4:8]]
        self.assertEqual(line["points"][0][0], "2026-10-14T15:00:00Z")
        self.assertAlmostEqual(line["points"][0][1], sum(first) / 4 / 1.25, places=6)

    def test_surcharge_and_unit_are_undone(self):
        attrs = copy.deepcopy(self.detailed)
        attrs["surcharge"] = 100.0
        for item in attrs["predictions"]:
            item["price"] = (item["price"] / 1.25 * 1000 + 100.0) * 1.25
            item["unit"] = "DKK/MWh"
        expected = tape.parse_osf(self.detailed)["points"]
        for (hour, value), (hour2, value2) in zip(tape.parse_osf(attrs)["points"], expected):
            self.assertEqual(hour, hour2)
            self.assertAlmostEqual(value, value2, places=6)

    def test_compact_with_tariffs_is_all_in_on_the_runs_table(self):
        line = tape.parse_osf(self.compact, self.table)
        self.assertEqual(line["basis"], "all_in")
        self.assertEqual(len(line["points"]), 28)
        self.assertEqual(line["points"][0][1], self.compact["predictions"]["t"][2])
        self.assertEqual((line["tariffs_incl_vat"], line["vat"], line["addon_ex_vat"]), (self.table, 0.25, 0.0))

    def test_tariffs_without_a_table_or_no_entity(self):
        self.assertIsNone(tape.parse_osf(self.compact, None))
        self.assertIsNone(tape.parse_osf(None))
        self.assertIsNone(tape.parse_osf({"friendly_name": "unavailable"}))


class EnergiDataServiceSpot(unittest.TestCase):
    def setUp(self):
        self.resp = fixture("eds_dayahead.json")

    def test_quarters_to_hourly_dkk_per_kwh(self):
        line = tape.parse_eds_dayahead(self.resp)
        first = [r["DayAheadPriceDKK"] for r in self.resp["records"][:4]]
        self.assertEqual((line["kind"], line["unit"], line["basis"]), ("actual", "DKK/kWh", "spot"))
        self.assertEqual(len(line["points"]), 48)
        self.assertEqual(line["points"][0][0], "2026-10-13T22:00:00Z")
        self.assertAlmostEqual(line["points"][0][1], sum(first) / 4 / 1000, places=6)
        self.assertAlmostEqual(line["dkk_per_eur"], 7.475, places=2)

    def test_an_hour_missing_a_quarter_is_dropped(self):
        del self.resp["records"][5]
        self.assertEqual(len(tape.parse_eds_dayahead(self.resp)["points"]), 47)


class AppendJsonl(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "nested", "tape.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def lines(self):
        with open(self.path, "rb") as f:
            return f.read().decode("ascii").split("\n")

    def test_one_line_per_record_in_order(self):
        self.assertEqual(tape.append_jsonl(self.path, [{"n": 1}, {"n": 2, "text": "a\nb"}]), 2)
        self.assertEqual(tape.append_jsonl(self.path, [{"n": 3, "name": "Strømligning"}]), 1)
        rows = self.lines()
        self.assertEqual(rows[-1], "")
        self.assertEqual([json.loads(r)["n"] for r in rows[:-1]], [1, 2, 3])
        self.assertEqual(json.loads(rows[2])["name"], "Strømligning")

    def test_a_cut_short_line_never_swallows_the_next_record(self):
        tape.append_jsonl(self.path, [{"n": 1}])
        with open(self.path, "a") as f:
            f.write('{"n": 2, "points": [[')
        tape.append_jsonl(self.path, [{"n": 3}])
        rows = self.lines()
        self.assertEqual(json.loads(rows[0]), {"n": 1})
        with self.assertRaises(ValueError):
            json.loads(rows[1])
        self.assertEqual(json.loads(rows[2]), {"n": 3})

    def test_records_that_are_not_strict_json_are_skipped(self):
        self.assertEqual(tape.append_jsonl(self.path, [{"x": float("nan")}, {"x": 1}, {"x": object()}]), 1)
        self.assertEqual(self.lines(), ['{"x":1}', ""])

    def test_an_empty_batch_writes_nothing(self):
        self.assertEqual(tape.append_jsonl(self.path, []), 0)
        self.assertFalse(os.path.exists(self.path))

    def test_concurrent_batches_do_not_interleave(self):
        def writer(thread):
            for batch in range(50):
                tape.append_jsonl(self.path, [{"t": thread, "b": batch, "i": i, "pad": "x" * 2000} for i in range(3)])

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = [json.loads(r) for r in self.lines()[:-1]]
        self.assertEqual(len(rows), 8 * 50 * 3)
        for start in range(0, len(rows), 3):
            batch = rows[start:start + 3]
            self.assertEqual({(r["t"], r["b"]) for r in batch}, {(batch[0]["t"], batch[0]["b"])})
            self.assertEqual([r["i"] for r in batch], [0, 1, 2])


class FakeTape(tape.PriceForecastTape):
    """The app without AppDaemon: state reads, the executor and the log are local."""

    def __init__(self, path, states, web):
        self.tape_file, self.price_entity, self.osf_entity = path, "sensor.energi_data_service", "sensor.osf"
        self.timeout_s, self._running, self.logged = 5, False, []
        self.states, self.web = states, web

    def log(self, message, level="INFO"):
        self.logged.append((level, message))

    async def get_state(self, entity_id, attribute=None):
        return self.states.get(entity_id)

    async def run_in_executor(self, func, *args):
        return func(*args)

    async def _fetch_web(self):
        return self.web


class Run(unittest.TestCase):
    def test_a_failing_source_is_logged_and_the_rest_written(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "tape.jsonl")
            app = FakeTape(path, {"sensor.energi_data_service": fixture("price_entity.json")}, [])
            fetched = "2026-10-14T13:00:00Z"
            app.web = [("stromligning", None, "HTTP 503"),
                       app._parsed("smartere", tape.parse_smartere, fixture("smartere.json"), fetched),
                       app._parsed("epf", lambda resp: resp["entries"][0]["value"] / 0, fixture("epf.json"), fetched)]
            asyncio.run(app._run("12:40"))
            with open(path) as f:
                rows = [json.loads(line) for line in f]
        self.assertEqual([r["source"] for r in rows], ["carnot", "eds_sensor", "smartere"])
        self.assertTrue(all(r["v"] == 1 and r["trigger"] == "12:40" and r["run"] == rows[0]["run"] for r in rows))
        warnings = [m for level, m in app.logged if level == "WARNING"]
        self.assertTrue(any("stromligning HTTP 503" in m for m in warnings))
        self.assertTrue(any("epf failed: ZeroDivisionError" in m for m in warnings))
        self.assertIn("osf absent", app.logged[-1][1])
        self.assertFalse(app._running)

    def test_a_run_never_overlaps_the_previous_one(self):
        app = FakeTape("/nonexistent/never-written", {}, [])
        app._running = True
        asyncio.run(app._run("15:30"))
        self.assertIn("skipped", app.logged[-1][1])


if __name__ == "__main__":
    unittest.main()
