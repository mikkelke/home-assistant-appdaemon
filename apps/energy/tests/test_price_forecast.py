# tests/test_price_forecast.py - PriceForecast: reading Stromligning's response, pricing its hours the way the price
# entity does, fetching them and the published entity.
# Run from repo root: python3 -m unittest discover -s apps/energy/tests -q
#
# price_entity.json is sensor.energi_data_service as fetched on 2026-10-14 and eds_dayahead.json the Energinet
# day-ahead prices (open data) of the same two days; both are verbatim. stromligning.json keeps the structure, keys
# and timestamps of a /api/prices response with synthetic prices: this repo is public and the data is Stromligning's.

import copy
import io
import json
import sys
import types
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
        sys.modules[name] = types.ModuleType(name)
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import price_forecast as pf  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG = yaml.safe_load((Path(__file__).resolve().parents[1] / "price_forecast.yaml").read_text(encoding="utf-8"))["PriceForecast"]
NOW = datetime(2026, 10, 14, 14, 10, 0, tzinfo=timezone.utc)  # 16:10 in Copenhagen
UPDATED = "2026-10-14T16:10:00+02:00"
AGAIN = "Price forecast is fetched again"


def fixture(name):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def epoch(text):
    return int(utc(text).timestamp())


def entity_attributes(**changes):
    return {**copy.deepcopy(fixture("price_entity.json")["attributes"]), **changes}


class Hourly(unittest.TestCase):
    T = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)

    def test_quarters_average_to_their_hour(self):
        slots = [(self.T + timedelta(minutes=15 * i), 15, float(i)) for i in range(4)]
        self.assertEqual(pf.hourly(slots), {int(self.T.timestamp()): 1.5})

    def test_a_partial_or_doubled_hour_is_dropped(self):
        partial = [(self.T + timedelta(minutes=15 * i), 15, 1.0) for i in range(3)]
        doubled = [(self.T, 60, 1.0), (self.T, 60, 2.0)]
        self.assertEqual(pf.hourly(partial), {})
        self.assertEqual(pf.hourly(doubled), {})

    def test_a_slot_without_a_value_leaves_its_hour_incomplete(self):
        slots = [(self.T, 30, 1.0), (self.T + timedelta(minutes=30), 30, None)]
        self.assertEqual(pf.hourly(slots), {})


class Tariffs(unittest.TestCase):
    def setUp(self):
        self.tariffs = fixture("price_entity.json")["attributes"]["tariffs"]

    def test_every_additional_tariff_is_added_to_the_hours_own(self):
        table = pf.tariff_table(self.tariffs)
        self.assertEqual(len(table), 24)
        self.assertAlmostEqual(table[3], 0.133 + 0.054 + 0.09 + 0.01)
        self.assertAlmostEqual(table[8], 0.398 + 0.054 + 0.09 + 0.01)
        self.assertAlmostEqual(table[18], 1.194 + 0.054 + 0.09 + 0.01)

    def test_an_incomplete_table_is_none(self):
        hours = {str(hour): 0.1 for hour in range(24)}
        for tariffs in (None, {}, "x", {"tariffs": {"0": 0.1}, "additional_tariffs": {}},
                        {"tariffs": {**hours, "5": None}}, {"tariffs": hours, "additional_tariffs": {"elafgift": None}},
                        {"tariffs": hours, "additional_tariffs": ["elafgift"]}):
            with self.subTest(tariffs):
                self.assertIsNone(pf.tariff_table(tariffs))
        self.assertEqual(pf.tariff_table({"tariffs": hours}), [0.1] * 24)


class ParseSpot(unittest.TestCase):
    def setUp(self):
        self.resp = fixture("stromligning.json")

    def test_quarters_and_hours_come_out_as_hourly_spot(self):
        spot = pf.parse_spot(self.resp)
        self.assertEqual(len(spot), 28)
        self.assertEqual(sorted(spot), list(range(epoch("2026-10-15T20:00:00Z"), epoch("2026-10-17T00:00:00Z"), 3600)))
        self.assertEqual(spot[epoch("2026-10-15T20:00:00Z")], 0.396891)
        self.assertEqual(spot[epoch("2026-10-15T22:00:00Z")], 0.35)
        self.assertEqual(spot[epoch("2026-10-16T23:00:00Z")], 0.261926)

    def test_the_slots_of_an_hour_are_averaged_and_a_partial_hour_drops(self):
        item = self.resp["prices"][-1]
        quarters = []
        for i in range(7):
            quarter = copy.deepcopy(item)
            quarter["date"] = f"2026-10-17T{10 + i // 4:02d}:{15 * (i % 4):02d}:00.000Z"
            quarter["resolution"] = "15m"
            quarter["details"]["electricity"]["value"] = float(i)
            quarters.append(quarter)
        self.assertEqual(pf.parse_spot({"prices": quarters}), {epoch("2026-10-17T10:00:00Z"): 1.5})

    def test_a_slot_in_another_unit_or_resolution_leaves_its_hour_out(self):
        euro = copy.deepcopy(self.resp)
        euro["prices"][8]["details"]["electricity"]["unit"] = "EUR/MWh"
        odd = copy.deepcopy(self.resp)
        odd["prices"][9]["resolution"] = "2h"
        for resp in (euro, odd):
            self.assertEqual(len(pf.parse_spot(resp)), 27)

    def test_nothing_usable(self):
        for resp in (None, [], {}, {"error": "rate limited"}, {"prices": "x"}, {"prices": [None, "x", {}]}):
            with self.subTest(resp):
                self.assertEqual(pf.parse_spot(resp), {})


class PricesLikeThePriceEntity(unittest.TestCase):
    def setUp(self):
        self.entity = fixture("price_entity.json")["attributes"]
        self.table = pf.tariff_table(self.entity["tariffs"])

    def day_ahead(self):
        records = fixture("eds_dayahead.json")["records"]
        return pf.hourly([(utc(record["TimeUTC"] + "Z"), 15, record["DayAheadPriceDKK"] / 1000) for record in records])

    def test_the_spot_of_the_published_hours_gives_the_prices_the_entity_published(self):
        points = pf.price_hours(self.day_ahead(), self.table, 0.25, 0.012, 1, date(2026, 10, 14))
        published = self.entity["raw_today"] + self.entity["raw_tomorrow"]
        self.assertEqual(len(points), 48)
        self.assertEqual([point["hour"] for point in points], [point["hour"] for point in published])
        errors = [abs(point["price"] - known["price"]) for point, known in zip(points, published)]
        self.assertLess(max(errors), 0.0011)

    def test_a_forecast_hour_is_the_total_stromligning_gives_it_to_within_the_rounding_of_the_tariffs(self):
        spot = pf.parse_spot(fixture("stromligning.json"))
        points = pf.price_hours(spot, self.table, 0.25, 0.012, 1, date(2026, 10, 16))
        totals = {epoch(item["date"]): item["price"]["total"] for item in fixture("stromligning.json")["prices"]}
        self.assertEqual(len(points), 26)
        for point in points:
            with self.subTest(point["hour"]):
                self.assertAlmostEqual(point["price"], totals[int(datetime.fromisoformat(point["hour"]).timestamp())], delta=0.0015)

    def test_an_hour_is_spot_plus_addon_with_vat_plus_the_hours_tariffs(self):
        spot = {epoch("2026-10-16T07:00:00Z"): 0.847487, epoch("2026-10-16T15:00:00Z"): 0.690587}
        self.assertEqual(pf.price_hours(spot, self.table, 0.25, 0.012, 1, date(2026, 10, 16)), [
            {"hour": "2026-10-16T09:00:00+02:00", "price": 1.626},
            {"hour": "2026-10-16T17:00:00+02:00", "price": 2.226},
        ])

    def test_hours_before_the_first_day_are_left_out(self):
        spot = pf.parse_spot(fixture("stromligning.json"))
        early = pf.price_hours(spot, self.table, 0.25, 0.012, 1, date(2026, 10, 15))
        late = pf.price_hours(spot, self.table, 0.25, 0.012, 1, date(2026, 10, 16))
        self.assertEqual((early[0]["hour"], len(early)), ("2026-10-15T22:00:00+02:00", 28))
        self.assertEqual((late[0]["hour"], late[-1]["hour"]), ("2026-10-16T00:00:00+02:00", "2026-10-17T01:00:00+02:00"))
        self.assertEqual(early[2:], late)
        self.assertEqual(pf.price_hours(spot, self.table, 0.25, 0.012, 1, date(2026, 10, 18)), [])

    def test_ore_are_one_hundred_times_the_kroner(self):
        spot = {epoch("2026-10-16T07:00:00Z"): 0.847487}
        self.assertEqual(pf.price_hours(spot, self.table, 0.25, 0.012, 100, date(2026, 10, 16))[0]["price"], 162.636)

    def test_the_tariff_is_the_local_hours_across_the_clock_changes(self):
        table = [float(hour) for hour in range(24)]
        autumn = {epoch("2026-10-24T22:00:00Z") + 3600 * i: 0.0 for i in range(25)}
        points = pf.price_hours(autumn, table, 0.0, 0.0, 1, date(2026, 10, 25))
        self.assertEqual([point["price"] for point in points], [0, 1, 2, 2] + list(range(3, 24)))
        self.assertEqual([point["hour"] for point in points[1:5]], [
            "2026-10-25T01:00:00+02:00", "2026-10-25T02:00:00+02:00", "2026-10-25T02:00:00+01:00", "2026-10-25T03:00:00+01:00"])
        spring = {epoch("2027-03-27T23:00:00Z") + 3600 * i: 0.0 for i in range(23)}
        points = pf.price_hours(spring, table, 0.0, 0.0, 1, date(2027, 3, 28))
        self.assertEqual([point["price"] for point in points], [0, 1] + list(range(3, 24)))
        self.assertEqual(points[2]["hour"], "2027-03-28T03:00:00+02:00")


class FakeResponse:
    def __init__(self, body):
        self.body = body.encode("utf-8")
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]


def serving(body):
    return patch.object(pf.urllib.request, "urlopen", return_value=FakeResponse(body))


class LoadSpot(unittest.TestCase):
    def load(self, params=None):
        return pf.load_spot(pf.URL, params or {})

    def test_asks_for_the_forecast_with_a_generic_user_agent_and_a_timeout(self):
        response = FakeResponse(json.dumps(fixture("stromligning.json")))
        params = {"supplierId": "radius_c", "forecast": "true", "from": "2026-10-14T22:00:00Z"}
        with patch.object(pf.urllib.request, "urlopen", return_value=response) as urlopen:
            result = self.load(params)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://stromligning.dk/api/prices?supplierId=radius_c&forecast=true&from=2026-10-14T22%3A00%3A00Z")
        self.assertEqual(request.header_items(), [("User-agent", "home-assistant-appdaemon")])
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 20})
        self.assertEqual(response.read_sizes, [pf.MAX_BYTES])
        self.assertEqual(result, {"ok": True, "spot": pf.parse_spot(fixture("stromligning.json"))})

    def test_a_failed_request_comes_back_as_a_short_reason(self):
        limited = urllib.error.HTTPError(pf.URL, 429, "Too Many Requests", {}, io.BytesIO())
        self.addCleanup(limited.close)
        for error, reason in ((limited, "HTTPError: HTTP Error 429: Too Many Requests"),
                              (urllib.error.URLError(OSError(-2, "Name or service not known")),
                               "URLError: <urlopen error [Errno -2] Name or service not known>"),
                              (TimeoutError("timed out"), "TimeoutError: timed out")):
            with self.subTest(reason), patch.object(pf.urllib.request, "urlopen", side_effect=error):
                self.assertEqual(self.load(), {"ok": False, "error": reason})

    def test_a_long_reason_is_cut_short(self):
        with patch.object(pf.urllib.request, "urlopen", side_effect=OSError("x" * 500)):
            result = self.load()
        self.assertEqual((result["ok"], len(result["error"])), (False, 100))

    def test_a_reply_that_is_not_a_price_list_is_a_failure(self):
        with serving("<html><body>Service busy"):
            result = self.load()
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("JSONDecodeError: "), result["error"])
        for body in ("{}", '{"error": "rate limited"}', '{"prices": []}', "[]"):
            with self.subTest(body), serving(body):
                self.assertEqual(self.load(), {"ok": False, "error": "no whole hours in the response"})


class Harness:
    """The app as AppDaemon would run it, with its scheduler, executor, clock, state reads and entity writes recorded."""

    def __init__(self, price_attributes=None, **args):
        self.now = NOW
        self.states = {"sensor.energi_data_service": entity_attributes() if price_attributes is None else price_attributes}
        self.published, self.logs, self.daily, self.listened, self.events, self.later, self.queue = [], [], [], [], [], [], []
        self.app = pf.PriceForecast.__new__(pf.PriceForecast)
        self.app.args = args
        self.app.log = lambda message, level="INFO": self.logs.append((level, message))
        self.app.get_now = lambda: self.now
        self.app.get_state = lambda entity, attribute=None: self.states.get(entity)
        self.app.set_state = lambda entity, state=None, attributes=None, replace=False, check_existence=True: (
            self.published.append((entity, state, copy.deepcopy(attributes), replace, check_existence)))
        self.app.run_daily = lambda callback, start, **kwargs: self.daily.append((callback, start))
        self.app.listen_state = lambda callback, entity, **kwargs: self.listened.append((callback, entity, kwargs))
        self.app.listen_event = lambda callback, event: self.events.append((callback, event))
        self.app.run_in = lambda callback, delay, **kwargs: self.later.append((callback, delay))
        self.app.submit_to_executor = lambda func, *a, callback=None: self.queue.append((func, a, callback))
        self.app.initialize()

    def answer(self, result):
        self.app._on_result(result=result)

    def messages(self, level):
        return [message for logged, message in self.logs if logged == level]

    @property
    def last(self):
        entity, state, attributes, replace, check_existence = self.published[-1]
        return state, attributes


GOOD = {"ok": True, "spot": pf.parse_spot(fixture("stromligning.json"))}
BAD = {"ok": False, "error": "TimeoutError: timed out"}
LIMITED = "HTTPError: HTTP Error 429: Too Many Requests"
REASON = "From Strømligning at 16:10, 26 h to 17.10 01:00"


class App(unittest.TestCase):
    def test_start_fetches_at_once_and_follows_the_clock_and_the_price_entity(self):
        h = Harness(**CONFIG)
        func, args, callback = h.queue[0]
        self.assertEqual((len(h.queue), func, callback), (1, pf.load_spot, h.app._on_result))
        self.assertEqual(args, (pf.URL, {"supplierId": "radius_c", "priceArea": "DK2", "productId": "vindstoed_vestroem",
                                         "forecast": "true", "from": "2026-10-14T22:00:00Z"}))
        self.assertEqual([start.strftime("%H:%M") for callback, start in h.daily], ["06:45", "12:40", "15:30", "20:45"])
        self.assertTrue(all(callback == h.app._on_tick for callback, start in h.daily))
        self.assertEqual(h.listened, [(h.app._on_tomorrow_valid, "sensor.energi_data_service", {"attribute": "tomorrow_valid"})])
        self.assertEqual(h.events, [(h.app._on_plugin_started, "plugin_started")])
        self.assertEqual(h.published, [])

    def test_every_fetch_asks_from_the_start_of_tomorrow(self):
        h = Harness(**CONFIG)
        h.now = datetime(2026, 10, 14, 22, 30, tzinfo=timezone.utc)
        h.app._on_tick({})
        self.assertEqual(h.queue[-1][1][1]["from"], "2026-10-15T22:00:00Z")

    def test_the_yaml_names_the_module_and_class_and_drives_the_app(self):
        self.assertEqual((CONFIG["module"], CONFIG["class"]), ("price_forecast", "PriceForecast"))
        self.assertIs(getattr(pf, CONFIG["class"]), pf.PriceForecast)
        h = Harness(**CONFIG)
        h.answer(GOOD)
        self.assertEqual(h.published[-1][0], CONFIG["entity"])

    def test_the_defaults_in_the_code_are_the_values_in_the_yaml(self):
        configured, default = Harness(**CONFIG), Harness()
        self.assertEqual(default.queue[0][1], configured.queue[0][1])
        self.assertEqual([start for callback, start in default.daily], [start for callback, start in configured.daily])
        self.assertEqual(default.listened[0][1], CONFIG["price_entity"])
        for h in (configured, default):
            h.answer(GOOD)
        self.assertEqual(default.published, configured.published)
        self.assertEqual(default.published[-1][0], CONFIG["entity"])
        h = Harness()
        h.answer(BAD)
        self.assertEqual(h.later, [(h.app._on_tick, CONFIG["retry_minutes"] * 60)])

    def test_a_good_fetch_publishes_the_whole_entity_replacing_the_old_one(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        self.assertEqual(len(h.published), 1)
        entity, state, attributes, replace, check_existence = h.published[0]
        self.assertEqual((entity, state, replace, check_existence), ("sensor.electricity_price_forecast", "ok", True, False))
        self.assertEqual(set(attributes), {"friendly_name", "icon", "source", "updated", "reason", "forecast"})
        self.assertEqual((attributes["source"], attributes["updated"], attributes["reason"]), ("stromligning", UPDATED, REASON))
        forecast = attributes["forecast"]
        self.assertEqual(len(forecast), 26)
        self.assertEqual(forecast[1], {"hour": "2026-10-16T01:00:00+02:00", "price": 0.754})
        self.assertEqual(forecast[9], {"hour": "2026-10-16T09:00:00+02:00", "price": 1.626})
        self.assertEqual(forecast[-1], {"hour": "2026-10-17T01:00:00+02:00", "price": 0.629})
        self.assertEqual(h.messages("WARNING"), [])
        self.assertNotIn(AGAIN, h.messages("INFO"))

    def test_the_forecast_has_the_shape_of_the_price_entitys_own(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        own = fixture("price_entity.json")["attributes"]["forecast"]
        forecast = h.last[1]["forecast"]
        self.assertEqual(set(forecast[0]), set(own[0]))
        self.assertEqual([point["hour"] for point in forecast], [point["hour"] for point in own[:len(forecast)]])
        self.assertTrue(all(round(point["price"], 3) == point["price"] for point in forecast))

    def test_until_tomorrow_is_published_the_hours_stromligning_has_are_priced_from_it(self):
        h = Harness(entity_attributes(tomorrow_valid=False), **CONFIG)
        h.answer(GOOD)
        forecast = h.last[1]["forecast"]
        self.assertEqual([point["hour"] for point in forecast[:3]], [
            "2026-10-15T22:00:00+02:00", "2026-10-15T23:00:00+02:00", "2026-10-16T00:00:00+02:00"])
        self.assertEqual((len(forecast), forecast[0]["price"], forecast[1]["price"]), (28, 1.063, 1.019))

    def test_a_price_entity_in_ore_gets_ore(self):
        h = Harness(entity_attributes(use_cent=True), **CONFIG)
        h.answer(GOOD)
        self.assertEqual(h.last[1]["forecast"][9], {"hour": "2026-10-16T09:00:00+02:00", "price": 162.636})

    def test_a_newer_fetch_replaces_the_forecast_and_the_time(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        h.now = NOW + timedelta(hours=5)
        h.answer({"ok": True, "spot": {epoch("2026-10-16T07:00:00Z"): 1.0}})
        state, attributes = h.last
        self.assertEqual((state, attributes["updated"], len(attributes["forecast"])), ("ok", "2026-10-14T21:10:00+02:00", 1))
        self.assertEqual(attributes["reason"], "From Strømligning at 21:10, 1 h to 16.10 09:00")

    def test_a_price_entity_without_tariffs_keeps_the_forecast_and_says_why(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        good = h.last[1]["forecast"]
        h.states["sensor.energi_data_service"] = entity_attributes(tariffs={"tariffs": {}, "additional_tariffs": {}})
        h.answer(GOOD)
        state, attributes = h.last
        self.assertEqual((state, attributes["forecast"]), ("stale", good))
        self.assertEqual(attributes["reason"],
                         "Not fetched (no tariffs on sensor.energi_data_service); keeping the forecast from 16:10, 26 h to 17.10 01:00")
        h.states.clear()
        h.answer(GOOD)
        self.assertEqual(h.last[0], "stale")

    def test_hours_that_all_come_before_the_first_day_are_no_forecast(self):
        h = Harness(**CONFIG)
        h.answer({"ok": True, "spot": {epoch("2026-10-15T07:00:00Z"): 1.0}})
        state, attributes = h.last
        self.assertEqual((state, attributes["forecast"], h.messages("WARNING")), ("unknown", [], ["Price forecast not fetched: no forecast hours from 16.10"]))

    def test_a_first_fetch_that_fails_publishes_unknown_with_the_reason_and_tries_again(self):
        h = Harness(**CONFIG)
        h.answer(BAD)
        entity, state, attributes, replace, check_existence = h.published[-1]
        self.assertEqual((entity, state, replace), ("sensor.electricity_price_forecast", "unknown", True))
        self.assertEqual(attributes, {"friendly_name": "Electricity price forecast", "icon": "mdi:chart-line", "source": "stromligning",
                                      "reason": "No forecast yet: TimeoutError: timed out", "forecast": []})
        self.assertEqual(h.messages("WARNING"), ["Price forecast not fetched: TimeoutError: timed out"])
        self.assertEqual(h.later, [(h.app._on_tick, 900)])
        h.answer(BAD)
        self.assertEqual(h.later, [(h.app._on_tick, 900)] * 2)
        self.assertEqual(len(h.messages("WARNING")), 1)

    def test_a_failure_keeps_the_last_forecast_warns_once_for_the_streak_and_waits_for_the_next_refresh(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        good = h.last[1]["forecast"]
        for reason in (LIMITED, "TimeoutError: timed out", "JSONDecodeError: Expecting value"):
            h.answer({"ok": False, "error": reason})
            state, attributes = h.last
            self.assertEqual((state, attributes["forecast"], attributes["updated"]), ("stale", good, UPDATED))
            self.assertEqual(attributes["reason"], f"Not fetched ({reason}); keeping the forecast from 16:10, 26 h to 17.10 01:00")
        self.assertEqual(h.messages("WARNING"), ["Price forecast not fetched: " + LIMITED])
        self.assertEqual(h.later, [])

    def test_recovery_logs_info_and_a_new_streak_warns_again(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        h.answer(BAD)
        h.answer(BAD)
        h.answer(GOOD)
        state, attributes = h.last
        self.assertEqual((state, attributes["reason"]), ("ok", REASON))
        self.assertEqual(h.messages("INFO").count(AGAIN), 1)
        h.answer(GOOD)
        self.assertEqual(h.messages("INFO").count(AGAIN), 1)
        h.answer(BAD)
        self.assertEqual(len(h.messages("WARNING")), 2)

    def test_an_answer_that_is_no_result_counts_as_a_failure(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        for result in (None, {}, {"ok": False}, "boom"):
            h.answer(result)
            self.assertEqual(h.last[0], "stale")
            self.assertIn("(no result)", h.last[1]["reason"])

    def test_tomorrow_valid_turning_true_fetches_once_a_day(self):
        h = Harness(**CONFIG)
        rise = ("sensor.energi_data_service", "tomorrow_valid", False, True, {})
        h.app._on_tomorrow_valid(*rise)
        self.assertEqual(len(h.queue), 2)
        h.app._on_tomorrow_valid(*rise)
        h.app._on_tomorrow_valid("sensor.energi_data_service", "tomorrow_valid", True, False, {})
        h.app._on_tomorrow_valid("sensor.energi_data_service", "tomorrow_valid", True, True, {})
        self.assertEqual(len(h.queue), 2)
        h.now = NOW + timedelta(days=1)
        h.app._on_tomorrow_valid(*rise)
        self.assertEqual(len(h.queue), 3)

    def test_a_home_assistant_restart_publishes_the_entity_again(self):
        h = Harness(**CONFIG)
        h.answer(GOOD)
        h.answer(BAD)
        before = h.published[-1]
        h.app._on_plugin_started("plugin_started", {}, {})
        self.assertEqual(len(h.published), 3)
        self.assertEqual(h.published[-1], before)

    def test_a_restart_before_the_first_fetch_publishes_unknown(self):
        h = Harness(**CONFIG)
        h.app._on_plugin_started("plugin_started", {}, {})
        state, attributes = h.last
        self.assertEqual((state, attributes["forecast"], attributes["reason"]), ("unknown", [], "No forecast yet"))
        self.assertNotIn("updated", attributes)

    def test_the_whole_path_from_the_response_to_the_entity(self):
        h = Harness(**CONFIG)
        func, args, callback = h.queue.pop(0)
        with serving(json.dumps(fixture("stromligning.json"))):
            callback(result=func(*args))
        state, attributes = h.last
        self.assertEqual((state, len(attributes["forecast"]), attributes["forecast"][9]["price"]), ("ok", 26, 1.626))
        h.app._on_tick({})
        func, args, callback = h.queue.pop(0)
        with patch.object(pf.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            callback(result=func(*args))
        state, attributes = h.last
        self.assertEqual((state, len(attributes["forecast"])), ("stale", 26))


if __name__ == "__main__":
    unittest.main()
