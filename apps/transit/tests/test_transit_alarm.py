# tests/test_transit_alarm.py - Rescue-window semantics for cancellations (2026-09-04).
#
# On a high-frequency corridor a cancelled departure that the NEXT train picks up within
# rescue_window_min costs the traveller almost no waiting time.  Counting those raw made the
# Metro M3 (2-4 min headway) report "Disrupted" for what is operationally a non-event; the
# S-tog's passenger_impact mode had always absorbed them.  These tests pin the new behaviour
# and the cases that must STILL alert.
#
# Departures are built as offsets from the real clock, so the suite is time-of-day independent
# (_parse_dt's day-rollover rule handles the midnight boundary).
#
# Run from repo root: python3 -m unittest discover -s apps/transit/tests -q

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# transit_alarm imports aiohttp at module scope for the Rejseplanen fetch; none of the pure
# evaluation logic under test touches it, so a stub keeps the suite runnable without the dep.
if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

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

import transit_alarm as ta  # noqa: E402

METRO_ROUTE = {
    "sensor_id": "enghave_metro_kbh",
    "transport_name": "Metro M3",
    "evaluation_mode": "high_frequency",
    "rescue_window_min": 5,
}


def dep(offset_min: int, *, line: str = "M3", cancelled: bool = False, delay_min: int = 0) -> dict:
    """One Rejseplanen-shaped departure `offset_min` from now, optionally delayed/cancelled."""
    now = datetime.now()
    sched = now + timedelta(minutes=offset_min)
    rt = sched + timedelta(minutes=delay_min)
    out = {
        "date": sched.strftime("%Y-%m-%d"),
        "time": sched.strftime("%H:%M:%S"),
        "rtDate": rt.strftime("%Y-%m-%d"),
        "rtTime": rt.strftime("%H:%M:%S"),
        "ProductAtStop": {"displayNumber": line},
    }
    if cancelled:
        out["cancelled"] = True
    return out


class TestHighFrequencyRescueWindow(unittest.TestCase):
    def setUp(self) -> None:
        self.app = ta.TransitAlarm.__new__(ta.TransitAlarm)

    def _evaluate(self, departures: list[dict]) -> dict:
        return self.app._evaluate(departures, METRO_ROUTE, 5, "Metro M3")

    def test_two_cancellations_absorbed_by_next_train_is_not_a_disruption(self):
        """The reported bug: 2 cancels on a 2-min headway, each covered ~2 min later."""
        res = self._evaluate([
            dep(2), dep(4, cancelled=True), dep(6), dep(8), dep(10, cancelled=True), dep(12),
        ])
        self.assertEqual(res["severity"], 0)
        self.assertFalse(res["has_issues"])
        self.assertTrue(
            any("absorbed by following departures" in i for i in res["issues"]),
            res["issues"],
        )

    def test_cancellations_leaving_a_real_hole_still_disrupt(self):
        """Nothing viable for 12 min after the cancellations - that is a genuine outage."""
        res = self._evaluate([
            dep(2, cancelled=True), dep(4, cancelled=True), dep(6, cancelled=True), dep(18), dep(20),
        ])
        self.assertEqual(res["severity"], 3)
        self.assertTrue(res["has_issues"])

    def test_single_unrescued_cancellation_stays_ok(self):
        """One gap is not a disruption on this corridor - the >=2 threshold is unchanged."""
        res = self._evaluate([dep(2, cancelled=True), dep(20), dep(22)])
        self.assertEqual(res["severity"], 0)

    def test_earlier_train_does_not_rescue_a_later_cancellation(self):
        """You cannot travel back in time: a departure BEFORE the cancelled one never rescues it."""
        res = self._evaluate([
            dep(1), dep(3), dep(20, cancelled=True), dep(40, cancelled=True), dep(50),
        ])
        self.assertEqual(res["severity"], 3)

    def test_delays_alone_never_alert_on_high_frequency(self):
        """Unchanged: high_frequency ignores per-train delay, however large."""
        res = self._evaluate([dep(2, delay_min=15), dep(5, delay_min=20), dep(9, delay_min=18)])
        self.assertEqual(res["severity"], 0)

    def test_past_departures_are_excluded(self):
        """Two cancellations that have already gone must not hold the line Disrupted."""
        res = self._evaluate([
            dep(-30, cancelled=True), dep(-20, cancelled=True), dep(3), dep(6), dep(9),
        ])
        self.assertEqual(res["severity"], 0)


class TestUnrescuedCancellationHelper(unittest.TestCase):
    """The shared helper both high_frequency and the dashboard's rule are built on."""

    def _n(self, minutes: int, cancelled: bool) -> dict:
        return {
            "effective": datetime(2026, 9, 4, 8, 0) + timedelta(minutes=minutes),
            "cancelled": cancelled,
            "line": "M3",
            "time_label": "08:00",
        }

    def test_rescuer_exactly_at_window_edge_does_not_rescue(self):
        cands = [self._n(0, True), self._n(5, False)]
        self.assertEqual(len(ta.TransitAlarm._unrescued_cancellations(cands, 5)), 1)

    def test_rescuer_inside_window_rescues(self):
        cands = [self._n(0, True), self._n(4, False)]
        self.assertEqual(ta.TransitAlarm._unrescued_cancellations(cands, 5), [])

    def test_simultaneous_viable_departure_rescues(self):
        cands = [self._n(0, True), self._n(0, False)]
        self.assertEqual(ta.TransitAlarm._unrescued_cancellations(cands, 5), [])

    def test_all_cancelled_means_none_rescued(self):
        cands = [self._n(0, True), self._n(2, True), self._n(4, True)]
        self.assertEqual(len(ta.TransitAlarm._unrescued_cancellations(cands, 5)), 3)


class TestRescueWindowResolution(unittest.TestCase):
    """_rescue_window_for is the single source of truth: the evaluators use it AND it is published
    on the sensor, so the dashboard cannot drift from this yaml."""

    def test_high_frequency_defaults_to_five(self):
        route = {"evaluation_mode": "high_frequency"}
        self.assertEqual(ta.TransitAlarm._rescue_window_for(route), 5)

    def test_explicit_yaml_value_wins(self):
        route = {"evaluation_mode": "high_frequency", "rescue_window_min": 3}
        self.assertEqual(ta.TransitAlarm._rescue_window_for(route), 3)

    def test_passenger_impact_defaults_to_the_delay_threshold(self):
        route = {"evaluation_mode": "passenger_impact", "delay_threshold_min": 10}
        self.assertEqual(ta.TransitAlarm._rescue_window_for(route), 10)

    def test_passenger_impact_explicit_window_beats_delay_threshold(self):
        route = {"evaluation_mode": "passenger_impact", "delay_threshold_min": 10, "rescue_window_min": 5}
        self.assertEqual(ta.TransitAlarm._rescue_window_for(route), 5)

    def test_infrequent_strict_has_no_window(self):
        """An hourly line has no alternative departure to be rescued by."""
        route = {"evaluation_mode": "infrequent_strict", "delay_threshold_min": 10}
        self.assertIsNone(ta.TransitAlarm._rescue_window_for(route))

    def test_narrowing_the_window_in_yaml_changes_the_verdict(self):
        """Proves the published value is the one that actually drives evaluation."""
        app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        board = [dep(2), dep(4, cancelled=True), dep(7), dep(9, cancelled=True), dep(12)]

        wide = dict(METRO_ROUTE, rescue_window_min=5)
        self.assertEqual(app._evaluate(board, wide, 5, "Metro M3")["severity"], 0)

        narrow = dict(METRO_ROUTE, rescue_window_min=2)
        self.assertEqual(app._evaluate(board, narrow, 5, "Metro M3")["severity"], 3)


ODENSE = "8600512"
KBH_IC_ROUTE = {
    "sensor_id": "kbh_ic_odense",
    "transport_name": "IC -> Odense",
    "stop_id": "8600626",
    "direction_id": ODENSE,
    "arrival_stop_id": ODENSE,
    "line_categories": ["IC", "ICL", "ECE", "Re"],
    "evaluation_mode": "infrequent_strict",
    "delay_threshold_min": 10,
}


def passlist_stop(ext_id: str, arrives: datetime, *, rt_arrives: datetime | None = None) -> dict:
    out = {
        "extId": ext_id,
        "name": f"Stop {ext_id}",
        "arrDate": arrives.strftime("%Y-%m-%d"),
        "arrTime": arrives.strftime("%H:%M:%S"),
    }
    if rt_arrives is not None:
        out["rtArrDate"] = rt_arrives.strftime("%Y-%m-%d")
        out["rtArrTime"] = rt_arrives.strftime("%H:%M:%S")
    return out


def ic_train(
    departs: datetime,
    *,
    category: str | None = "ECE",
    delay_min: int = 0,
    stops: list[dict] | None = None,
) -> dict:
    """One KBH H departure as the board returns it; `stops` is the passlist, absent when None."""
    rt = departs + timedelta(minutes=delay_min)
    out = {
        "date": departs.strftime("%Y-%m-%d"),
        "time": departs.strftime("%H:%M:%S"),
        "rtDate": rt.strftime("%Y-%m-%d"),
        "rtTime": rt.strftime("%H:%M:%S"),
        "ProductAtStop": {"displayNumber": "IC 123"},
    }
    if category is not None:
        out["ProductAtStop"]["catOut"] = category
    if stops is not None:
        out["Stops"] = {"Stop": stops}
    return out


class TestCategoryAndTravelTime(unittest.TestCase):
    """KBH H -> Odense: every departure carries its category and, with a passlist, its trip length."""

    def setUp(self) -> None:
        self.app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        self.departs = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=20)

    def _published(self, *board: dict, route: dict = KBH_IC_ROUTE) -> list[dict]:
        return self.app._evaluate(list(board), route, 10, "IC -> Odense")["departures"]

    def _arrives(self, after_min: int) -> datetime:
        return self.departs + timedelta(minutes=after_min)

    def _odense(self, after_min: int, **kwargs) -> dict:
        return passlist_stop(ODENSE, self._arrives(after_min), **kwargs)

    def test_category_passes_through(self):
        [d] = self._published(ic_train(self.departs, category="ICL"))
        self.assertEqual(d["category"], "ICL")

    def test_missing_category_is_an_empty_string(self):
        route = dict(KBH_IC_ROUTE, line_categories=[])
        [d] = self._published(ic_train(self.departs, category=None), route=route)
        self.assertEqual(d["category"], "")

    def test_route_without_an_arrival_stop_gets_category_but_no_travel_time(self):
        route = {k: v for k, v in KBH_IC_ROUTE.items() if k != "arrival_stop_id"}
        [d] = self._published(ic_train(self.departs, stops=[self._odense(71)]), route=route)
        self.assertEqual(d["category"], "ECE")
        self.assertNotIn("travel_min", d)
        self.assertNotIn("arrival_time", d)

    def test_travel_time_from_scheduled_times(self):
        stops = [
            passlist_stop("8600700", self._arrives(30)),
            self._odense(71),
            passlist_stop("8600800", self._arrives(95)),
        ]
        [d] = self._published(ic_train(self.departs, stops=stops))
        self.assertEqual(d["travel_min"], 71)
        self.assertEqual(d["arrival_time"], self._arrives(71).strftime("%H:%M"))

    def test_realtime_arrival_is_preferred(self):
        stop = self._odense(71, rt_arrives=self._arrives(76))
        [d] = self._published(ic_train(self.departs, stops=[stop]))
        self.assertEqual(d["travel_min"], 76)
        self.assertEqual(d["arrival_time"], self._arrives(76).strftime("%H:%M"))

    def test_realtime_arrival_time_without_its_date_uses_the_scheduled_date(self):
        """A late train arriving after midnight: the date is only on the scheduled arrival."""
        departs = (datetime.now() + timedelta(days=1)).replace(hour=23, minute=30, second=0, microsecond=0)
        stop = passlist_stop(ODENSE, departs + timedelta(minutes=71))
        stop["rtArrTime"] = (departs + timedelta(minutes=74)).strftime("%H:%M:%S")
        [d] = self._published(ic_train(departs, stops=[stop]))
        self.assertEqual(d["travel_min"], 74)

    def test_travel_time_runs_from_the_delayed_departure_to_the_delayed_arrival(self):
        stop = self._odense(71, rt_arrives=self._arrives(80))
        [d] = self._published(ic_train(self.departs, delay_min=5, stops=[stop]))
        self.assertEqual(d["travel_min"], 75)

    def test_no_passlist_or_no_arrival_stop_in_it_means_no_travel_time(self):
        base = ic_train(self.departs)
        no_time = {"extId": ODENSE, "name": "Odense St."}
        cases = {
            "no Stops key": base,
            "Stops null": {**base, "Stops": None},
            "empty Stops": {**base, "Stops": {}},
            "Stop null": {**base, "Stops": {"Stop": None}},
            "other stops only": ic_train(self.departs, stops=[passlist_stop("8600700", self._arrives(30))]),
            "arrival stop without a time": ic_train(self.departs, stops=[no_time]),
        }
        for label, entry in cases.items():
            with self.subTest(label):
                [d] = self._published(entry)
                self.assertEqual(d["category"], "ECE")
                self.assertNotIn("travel_min", d)
                self.assertNotIn("arrival_time", d)

    def test_an_arrival_before_the_departure_is_not_a_travel_time(self):
        [d] = self._published(ic_train(self.departs, stops=[self._odense(-5)]))
        self.assertNotIn("travel_min", d)
        self.assertNotIn("arrival_time", d)

    def test_arrival_stop_id_given_as_a_yaml_number_still_matches(self):
        route = dict(KBH_IC_ROUTE, arrival_stop_id=int(ODENSE))
        [d] = self._published(ic_train(self.departs, stops=[self._odense(71)]), route=route)
        self.assertEqual(d["travel_min"], 71)

    def test_duplicate_departures_keep_category_and_travel_time(self):
        train = ic_train(self.departs, stops=[self._odense(71)])
        published = self._published(train, dict(train))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["category"], "ECE")
        self.assertEqual(published[0]["travel_min"], 71)
        self.assertEqual(published[0]["arrival_time"], self._arrives(71).strftime("%H:%M"))


class FakeResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def json(self, content_type=None) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests: list[dict] = []

    def get(self, url: str, params: dict, timeout) -> FakeResponse:
        self.requests.append(params)
        return FakeResponse(self.payload)


class TestPasslistRequest(unittest.TestCase):
    """The stop list makes the response bigger, so only a route with an arrival stop asks for it."""

    def setUp(self) -> None:
        self.app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        self.app.access_id = "key"
        self.app.max_journeys = 25
        self.app.duration_min = 60

        async def enabled(route: dict) -> bool:
            return True

        self.app._route_is_enabled = enabled
        timeout = mock.patch.object(ta.aiohttp, "ClientTimeout", create=True, new=lambda total: total)
        timeout.start()
        self.addCleanup(timeout.stop)

        departs = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=20)
        stops = [passlist_stop(ODENSE, departs + timedelta(minutes=71))]
        self.board = {"Departure": [ic_train(departs, stops=stops)]}

    def _check(self, route: dict) -> tuple[dict, dict]:
        session = FakeSession(self.board)
        result = asyncio.run(self.app._check_route(route, session))
        return session.requests[0], result

    def test_a_route_with_an_arrival_stop_asks_for_the_passlist(self):
        params, result = self._check(KBH_IC_ROUTE)
        self.assertEqual(params["passlist"], 1)
        self.assertEqual(result["departures"][0]["category"], "ECE")
        self.assertEqual(result["departures"][0]["travel_min"], 71)

    def test_a_route_without_one_does_not(self):
        route = {k: v for k, v in KBH_IC_ROUTE.items() if k != "arrival_stop_id"}
        params, result = self._check(route)
        self.assertNotIn("passlist", params)
        self.assertNotIn("travel_min", result["departures"][0])


def mdk_message(group: str, name: str = "Vi kører efter planen", *, clear: bool = True) -> dict:
    """One active message as m.dk's operations data writes it: the line group padded with spaces."""
    return {
        "name": name,
        "lineSetup": {"key": "test-key", "lineGroup": f"{group:<10}", "clearMessages": "Vi kører efter planen"},
        "creatorName": None,
        "createDate": "11/05/2026 21:26",
        "icon": None,
        "isClearMessage": clear,
        "isSentToTwitter": False,
    }


def mdk_data(*messages: dict, warning: bool = False) -> dict:
    return {"installations": [], "activeMessages": list(messages), "activeWarning": warning}


CLEAR_TEXT = "Vi kører efter planen"
DISRUPTION_TEXT = "Signalfejl mellem Nørrebro og Østerport"
NORMAL_DAY = mdk_data(mdk_message("M1/M2"), mdk_message("M3/M4"))
M3_DISRUPTED = mdk_data(mdk_message("M1/M2"), mdk_message("M3/M4", DISRUPTION_TEXT, clear=False))
M1_DISRUPTED = mdk_data(mdk_message("M1/M2", DISRUPTION_TEXT, clear=False), mdk_message("M3/M4"))
RAW_NORMAL_DAY = f"M1/M2: {CLEAR_TEXT} | M3/M4: {CLEAR_TEXT}"


class TestParseMdkStatus(unittest.TestCase):
    def test_a_normal_day_is_an_all_clear_for_each_line_group(self):
        status = ta.parse_mdk_status(NORMAL_DAY)
        self.assertEqual(
            [(m["group"], m["lines"], m["name"], m["clear"]) for m in status["messages"]],
            [("M1/M2", ["M1", "M2"], CLEAR_TEXT, True), ("M3/M4", ["M3", "M4"], CLEAR_TEXT, True)],
        )
        self.assertEqual(status["raw"], RAW_NORMAL_DAY)
        self.assertIs(status["warning"], False)

    def test_a_line_group_is_trimmed_and_split_on_slash_with_each_part_trimmed(self):
        for group in ("M3/M4     ", "  M3 / M4 ", "m3/m4"):
            with self.subTest(group):
                [message] = ta.parse_mdk_status(mdk_data(mdk_message(group)))["messages"]
                self.assertEqual(message["lines"], ["M3", "M4"])
                self.assertEqual(message["group"], group.strip())
        [single] = ta.parse_mdk_status(mdk_data(mdk_message("M1")))["messages"]
        self.assertEqual(single["lines"], ["M1"])

    def test_a_message_that_is_not_an_all_clear_keeps_its_text(self):
        [message] = ta.parse_mdk_status(mdk_data(mdk_message("M3/M4", "Signalfejl\n  på   M3", clear=False)))["messages"]
        self.assertFalse(message["clear"])
        self.assertEqual(message["name"], "Signalfejl på M3")

    def test_no_messages_is_a_valid_status(self):
        status = ta.parse_mdk_status(mdk_data())
        self.assertEqual((status["messages"], status["raw"]), ([], ""))

    def test_installations_are_ignored(self):
        data = dict(NORMAL_DAY, installations=[{"name": "Elevator out of order"}])
        self.assertEqual(ta.parse_mdk_status(data), ta.parse_mdk_status(NORMAL_DAY))

    def test_the_warning_flag_is_recorded_as_it_is(self):
        self.assertIs(ta.parse_mdk_status(mdk_data(warning=True))["warning"], True)
        self.assertIsNone(ta.parse_mdk_status(dict(NORMAL_DAY, activeWarning="yes"))["warning"])
        self.assertIsNone(ta.parse_mdk_status({"activeMessages": []})["warning"])

    def test_anything_else_is_refused(self):
        good = mdk_message("M3/M4")

        def changed(**changes) -> dict:
            return {**good, **changes}

        def without(key: str) -> dict:
            return {k: v for k, v in good.items() if k != key}

        garbage = {
            "a list": [],
            "text": "<html>maintenance</html>",
            "nothing": None,
            "a number": 3,
            "no activeMessages": {"installations": [], "activeWarning": False},
            "activeMessages null": {"activeMessages": None},
            "activeMessages an object": {"activeMessages": {"name": CLEAR_TEXT}},
            "a message that is text": {"activeMessages": [CLEAR_TEXT]},
            "a message without lineSetup": {"activeMessages": [without("lineSetup")]},
            "lineSetup null": {"activeMessages": [changed(lineSetup=None)]},
            "a message without lineGroup": {"activeMessages": [changed(lineSetup={"key": "k"})]},
            "a blank lineGroup": {"activeMessages": [changed(lineSetup={"lineGroup": "   "})]},
            "a lineGroup that is not text": {"activeMessages": [changed(lineSetup={"lineGroup": 3})]},
            "a message without a name": {"activeMessages": [without("name")]},
            "a blank name": {"activeMessages": [changed(name=" ")]},
            "a message without isClearMessage": {"activeMessages": [without("isClearMessage")]},
            "isClearMessage as text": {"activeMessages": [changed(isClearMessage="true")]},
            "one bad message among good ones": {"activeMessages": [good, without("name")]},
        }
        for label, data in garbage.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    ta.parse_mdk_status(data)


MDK_METRO = dict(
    METRO_ROUTE, stop_name="Enghave Plads", stop_id="8603344", direction_id="8603330", lines=["M3"],
    operator_status="mdk", operator_status_lines=["M3"],
)
QUIET = {
    "has_issues": False, "no_service": True, "issues": [], "future_dep_times": [],
    "next_departure": None, "mins_to_next": None, "severity": 0, "departures": [],
}
OPERATOR_KEYS = {
    "operator_status", "operator_status_ok", "operator_status_raw", "operator_status_warning", "operator_status_source",
}


class PublishedRoute:
    """What `_update_status_sensors` writes for one route result."""

    def __init__(self, route: dict, res: dict) -> None:
        app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        app.args = {}
        app.duration_min = 60
        app.log = lambda *args, **kwargs: None
        services: dict[str, dict] = {}
        sensors: list[tuple[str, dict]] = []

        async def call_service(service: str, **kwargs) -> None:
            services[service] = kwargs

        app.call_service = call_service
        app.set_state = lambda entity, **kwargs: sensors.append((entity, kwargs))
        asyncio.run(app._update_status_sensors([(route, res)]))
        self.status = services["input_select/select_option"]["option"]
        self.info = services["input_text/set_value"]["value"]
        [(self.entity, sensor)] = sensors
        self.state = sensor["state"]
        self.attributes = sensor["attributes"]


class TestOperatorStatusDecision(unittest.TestCase):
    """What m.dk says about the route's lines decides the Metro route; Rejseplanen's own findings still count."""

    def decide(self, res: dict, data: dict | None, route: dict = MDK_METRO) -> dict:
        status = None if data is None else ta.parse_mdk_status(data)
        return ta.TransitAlarm._apply_operator_status(route, res, status)

    def publish(self, res: dict, data: dict | None, route: dict = MDK_METRO) -> PublishedRoute:
        return PublishedRoute(route, self.decide(res, data, route))

    def board(self, *departures: dict) -> dict:
        app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        return app._evaluate(list(departures), MDK_METRO, 5, "Metro M3")

    def test_an_all_clear_without_times_is_ok_and_says_so(self):
        out = self.publish(QUIET, NORMAL_DAY)
        self.assertEqual((out.status, out.state), ("OK", "OK"))
        self.assertEqual(out.info, "Enghave Plads: Normal service - no times")
        self.assertEqual(out.attributes["issues"], [])
        self.assertEqual(out.attributes["departures"], [])

    def test_an_all_clear_when_rejseplanen_fails_is_ok_not_unavailable(self):
        for error in ("HTTP 500", "unknown API error"):
            with self.subTest(error):
                out = self.publish({"error": error, "has_issues": True}, NORMAL_DAY)
                self.assertEqual((out.status, out.state), ("OK", "OK"))
                self.assertEqual(out.info, "Enghave Plads: Normal service - no times")

    def test_an_all_clear_keeps_rejseplanens_cancellations(self):
        res = self.board(dep(2, cancelled=True), dep(4, cancelled=True), dep(6, cancelled=True), dep(18), dep(20))
        out = self.publish(res, NORMAL_DAY)
        self.assertEqual(out.status, "Disrupted")
        self.assertTrue(out.attributes["issues"])

    def test_an_all_clear_with_times_changes_nothing_else(self):
        res = self.board(dep(2), dep(5), dep(8))
        decided = self.decide(res, NORMAL_DAY)
        self.assertIs(decided["operator_status_ok"], True)
        self.assertFalse(decided["no_times"])
        self.assertEqual({k: v for k, v in decided.items() if k not in OPERATOR_KEYS | {"no_times"}}, res)
        out = PublishedRoute(MDK_METRO, decided)
        self.assertEqual(out.status, "OK")
        self.assertIn("Next:", out.info)
        self.assertEqual(len(out.attributes["departures"]), 3)

    def test_a_message_that_is_not_an_all_clear_is_disrupted_with_its_text_as_the_issue(self):
        out = self.publish(QUIET, M3_DISRUPTED)
        self.assertEqual((out.status, out.state), ("Disrupted", "Disrupted"))
        self.assertEqual(out.attributes["issues"], [f"Metro M3: {DISRUPTION_TEXT}"])
        self.assertIn(DISRUPTION_TEXT, out.info)

    def test_a_disruption_outranks_healthy_departures(self):
        out = self.publish(self.board(dep(2), dep(5), dep(8)), M3_DISRUPTED)
        self.assertEqual(out.status, "Disrupted")
        self.assertEqual(out.attributes["issues"][0], f"Metro M3: {DISRUPTION_TEXT}")

    def test_a_disruption_keeps_the_departure_issues_after_its_own(self):
        res = self.board(dep(2), dep(4, cancelled=True), dep(6), dep(8))
        self.assertTrue(res["issues"])
        out = self.publish(res, M3_DISRUPTED)
        self.assertEqual(out.attributes["issues"], [f"Metro M3: {DISRUPTION_TEXT}", *res["issues"]])

    def test_a_disruption_is_disrupted_even_when_rejseplanen_fails(self):
        out = self.publish({"error": "HTTP 500", "has_issues": True}, M3_DISRUPTED)
        self.assertEqual(out.status, "Disrupted")

    def test_a_disruption_on_other_lines_changes_nothing(self):
        for label, data in {"M1/M2": M1_DISRUPTED, "no M3/M4 group": mdk_data(mdk_message("M1/M2", DISRUPTION_TEXT, clear=False))}.items():
            with self.subTest(label):
                out = self.publish(QUIET, data)
                self.assertEqual((out.status, out.state), ("OK", "OK"))
                self.assertEqual(out.info, "Enghave Plads: Normal service - no times")
                self.assertEqual(out.attributes["issues"], [])
                self.assertIs(out.attributes["operator_status_ok"], True)

    def test_no_message_for_the_routes_lines_is_normal(self):
        for label, data in {"only M1/M2": mdk_data(mdk_message("M1/M2")), "none at all": mdk_data()}.items():
            with self.subTest(label):
                out = self.publish(QUIET, data)
                self.assertEqual((out.status, out.info), ("OK", "Enghave Plads: Normal service - no times"))
                self.assertIs(out.attributes["operator_status_ok"], True)
                self.assertIsNone(out.attributes["operator_status"])

    def test_a_line_group_counts_for_each_of_its_lines_and_only_whole_lines(self):
        cases = {
            ("M4",): "Disrupted",
            ("M3", "M1"): "Disrupted",
            ("m3",): "Disrupted",
            ("M1",): "OK",
            ("M2", "M5"): "OK",
        }
        for lines, expected in cases.items():
            with self.subTest(lines):
                route = dict(MDK_METRO, operator_status_lines=list(lines))
                self.assertEqual(self.publish(QUIET, M3_DISRUPTED, route).status, expected)
        other = mdk_data(mdk_message("M30/M31", DISRUPTION_TEXT, clear=False))
        self.assertEqual(self.publish(QUIET, other).status, "OK")

    def test_a_route_with_several_lines_follows_any_of_them(self):
        route = dict(MDK_METRO, operator_status_lines=["M1", "M3"])
        self.assertEqual(self.publish(QUIET, M1_DISRUPTED, route).status, "Disrupted")
        self.assertEqual(self.publish(QUIET, M3_DISRUPTED, route).status, "Disrupted")
        self.assertEqual(self.publish(QUIET, NORMAL_DAY, route).status, "OK")

    def test_one_message_that_is_not_an_all_clear_among_clear_ones_disrupts_with_that_text(self):
        data = mdk_data(mdk_message("M3/M4"), mdk_message("M3/M4", DISRUPTION_TEXT, clear=False), mdk_message("M1/M2"))
        out = self.publish(QUIET, data)
        self.assertEqual(out.status, "Disrupted")
        self.assertEqual(out.attributes["issues"], [f"Metro M3: {DISRUPTION_TEXT}"])
        self.assertEqual(out.attributes["operator_status"], DISRUPTION_TEXT)

    def test_each_disruption_text_is_an_issue_once(self):
        data = mdk_data(
            mdk_message("M3/M4", "Signalfejl", clear=False),
            mdk_message("M3/M4", "Signalfejl", clear=False),
            mdk_message("M3/M4", "Sporarbejde", clear=False),
        )
        out = self.publish(QUIET, data)
        self.assertEqual(out.attributes["issues"], ["Metro M3: Signalfejl", "Metro M3: Sporarbejde"])
        self.assertEqual(out.attributes["operator_status"], "Signalfejl | Sporarbejde")

    def test_the_status_text_is_the_routes_own_messages_and_the_raw_text_is_everything(self):
        cases = {
            "all clear": (NORMAL_DAY, CLEAR_TEXT, RAW_NORMAL_DAY),
            "M3 disrupted": (M3_DISRUPTED, DISRUPTION_TEXT, f"M1/M2: {CLEAR_TEXT} | M3/M4: {DISRUPTION_TEXT}"),
            "M1 disrupted": (M1_DISRUPTED, CLEAR_TEXT, f"M1/M2: {DISRUPTION_TEXT} | M3/M4: {CLEAR_TEXT}"),
        }
        for label, (data, text, raw) in cases.items():
            with self.subTest(label):
                attributes = self.publish(QUIET, data).attributes
                self.assertEqual(attributes["operator_status"], text)
                self.assertEqual(attributes["operator_status_raw"], raw)

    def test_the_warning_is_published_and_changes_no_verdict(self):
        out = self.publish(QUIET, mdk_data(mdk_message("M1/M2"), mdk_message("M3/M4"), warning=True))
        self.assertEqual(out.status, "OK")
        self.assertIs(out.attributes["operator_status_warning"], True)

    def test_no_status_leaves_the_rejseplanen_verdict_alone(self):
        cases = {
            "quiet period": (QUIET, "OK", "Enghave Plads: Quiet period - no trains in next 60 min"),
            "api error": ({"error": "HTTP 500", "has_issues": True}, "Unavailable", "Enghave Plads: Unavailable"),
        }
        for label, (res, status, info) in cases.items():
            with self.subTest(label):
                out = self.publish(res, None)
                self.assertEqual((out.status, out.info), (status, info))
        cancelled = self.board(dep(2, cancelled=True), dep(4, cancelled=True), dep(6, cancelled=True), dep(18), dep(20))
        self.assertEqual(self.publish(cancelled, None).status, "Disrupted")
        self.assertEqual(self.publish(self.board(dep(2), dep(5)), None).status, "OK")

    def test_a_paused_route_stays_paused(self):
        out = self.publish({"disabled": True, "has_issues": False, "severity": 0}, M3_DISRUPTED)
        self.assertEqual((out.status, out.info), ("OK", "Enghave Plads: Paused"))

    def test_the_sensor_names_its_source_and_the_status(self):
        out = self.publish(QUIET, NORMAL_DAY)
        self.assertEqual(out.attributes["operator_status"], CLEAR_TEXT)
        self.assertIs(out.attributes["operator_status_ok"], True)
        self.assertEqual(out.attributes["operator_status_raw"], RAW_NORMAL_DAY)
        self.assertIs(out.attributes["operator_status_warning"], False)
        self.assertEqual(out.attributes["operator_status_source"], "m.dk")
        self.assertEqual(out.entity, "sensor.transit_enghave_metro_kbh")
        disrupted = self.publish(QUIET, M3_DISRUPTED).attributes
        self.assertIs(disrupted["operator_status_ok"], False)
        self.assertEqual(disrupted["operator_status"], DISRUPTION_TEXT)
        unread = self.publish(QUIET, None).attributes
        for key in OPERATOR_KEYS - {"operator_status_source"}:
            self.assertIsNone(unread[key], key)
        self.assertEqual(unread["operator_status_source"], "m.dk")

    def test_every_existing_attribute_is_still_published(self):
        expected = {
            "friendly_name", "icon", "next_departure", "mins_to_next", "upcoming", "departures", "issues", "lines",
            "rescue_window_min", "delay_threshold_min", "last_checked", "data_as_of",
        }
        for data in (NORMAL_DAY, M3_DISRUPTED, None):
            with self.subTest(data is None):
                keys = set(self.publish(QUIET, data).attributes)
                self.assertEqual(keys - OPERATOR_KEYS, expected)

    def test_a_route_that_does_not_follow_an_operator_publishes_none_of_it(self):
        out = PublishedRoute(METRO_ROUTE, dict(QUIET))
        self.assertFalse(OPERATOR_KEYS & set(out.attributes))
        self.assertEqual(out.info, "Quiet period - no trains in next 60 min")

    def test_the_push_texts_do_not_call_a_normal_metro_a_problem(self):
        app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        app.duration_min = 60
        sensor = self.decide(QUIET, NORMAL_DAY)
        results = [(MDK_METRO, sensor), (KBH_IC_ROUTE, {"has_issues": True, "issues": ["IC: delayed"], "severity": 2})]
        message = app._build_message(results)
        self.assertIn("✅ Metro M3: normal service, no times", message)
        self.assertIn("Head to Enghave Plads (Metro M3, no info) - IC -> Odense has problems", message)
        self.assertEqual(app._build_all_clear_message([(MDK_METRO, sensor)]), "✅ Metro M3: normal service, no times")

    def test_a_disrupted_metro_without_times_does_not_say_next_none(self):
        app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        message = app._build_message([(MDK_METRO, self.decide(QUIET, M3_DISRUPTED))])
        self.assertIn("⚠️ Transit problems - Metro M3: no times", message)
        self.assertNotIn("None", message)


class SiteResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body

    async def __aenter__(self) -> SiteResponse:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def json(self, content_type=None):
        return json.loads(self.body)


class SiteSession:
    """Serves the Rejseplanen board and m.dk's operations data from one session and remembers every URL asked for."""

    def __init__(
        self, mdk: dict | str = "", *, status: int = 200, error: Exception | None = None, board: dict | None = None
    ) -> None:
        self.body = mdk if isinstance(mdk, str) else json.dumps(mdk)
        self.status, self.error = status, error
        self.board = board if board is not None else {"Departure": []}
        self.urls: list[str] = []

    def get(self, url: str, params: dict | None = None, timeout=None):
        self.urls.append(url)
        if url == ta.MDK_STATUS_URL:
            if self.error is not None:
                raise self.error
            return SiteResponse(self.status, self.body)
        return FakeResponse(self.board)

    @property
    def mdk_requests(self) -> int:
        return self.urls.count(ta.MDK_STATUS_URL)


class OneSession:
    """Stands in for aiohttp.ClientSession: every `async with ClientSession()` yields the same session."""

    def __init__(self, session: SiteSession) -> None:
        self.session = session

    def __call__(self) -> OneSession:
        return self

    async def __aenter__(self) -> SiteSession:
        return self.session

    async def __aexit__(self, *exc_info) -> bool:
        return False


class OperatorStatusFetchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        self.app.args = {}
        self.app.access_id = "key"
        self.app.max_journeys = 25
        self.app.duration_min = 60
        self.app._mdk_text = None
        self.app._mdk_warned_at = None
        self.logs: list[tuple[str, str]] = []
        self.app.log = lambda message, level="INFO": self.logs.append((level, message))

        async def enabled(route: dict) -> bool:
            return True

        self.app._route_is_enabled = enabled
        patch = mock.patch.object(ta.aiohttp, "ClientTimeout", create=True, new=lambda total: total)
        patch.start()
        self.addCleanup(patch.stop)

    def levels(self, level: str) -> list[str]:
        return [message for lvl, message in self.logs if lvl == level]

    def fetch(self, session: SiteSession) -> dict | None:
        return asyncio.run(self.app._fetch_mdk_status(session))

    def fetch_all(self, session: SiteSession, routes: list[dict]) -> dict[str, dict]:
        self.app.routes = routes
        patch = mock.patch.object(ta.aiohttp, "ClientSession", create=True, new=OneSession(session))
        patch.start()
        self.addCleanup(patch.stop)
        return {route["sensor_id"]: res for route, res in asyncio.run(self.app._fetch_all_routes())}


class TestMdkFetch(OperatorStatusFetchTestCase):
    def test_reads_the_operations_data_with_the_trailing_slash(self):
        session = SiteSession(NORMAL_DAY)
        status = self.fetch(session)
        self.assertEqual(status["raw"], RAW_NORMAL_DAY)
        self.assertEqual(session.urls, ["https://m.dk/api/operationsdata/"])

    def test_every_failure_is_no_status(self):
        failures = {
            "HTTP 503": SiteSession("busy", status=503),
            "not JSON": SiteSession("<html>maintenance</html>"),
            "no activeMessages": SiteSession({"installations": [], "activeWarning": False}),
            "a changed message format": SiteSession({"activeMessages": [{"name": CLEAR_TEXT}]}),
            "connection error": SiteSession(error=OSError("connection refused")),
            "timeout": SiteSession(error=asyncio.TimeoutError()),
        }
        for label, session in failures.items():
            with self.subTest(label):
                self.assertIsNone(self.fetch(session))

    def test_logs_info_only_when_the_messages_change(self):
        self.fetch(SiteSession(NORMAL_DAY))
        self.fetch(SiteSession(NORMAL_DAY))
        self.assertEqual(self.levels("INFO"), ["m.dk metro status: M1/M2: Vi korer efter planen | M3/M4: Vi korer efter planen"])
        self.fetch(SiteSession(M3_DISRUPTED))
        self.fetch(SiteSession(M3_DISRUPTED))
        self.assertEqual(len(self.levels("INFO")), 2)

    def test_logs_are_ascii(self):
        self.fetch(SiteSession(mdk_data(mdk_message("M3/M4", "Ingen tog til Nørreport – følg skiltene", clear=False))))
        self.fetch(SiteSession(error=OSError("kan ikke nå m.dk: æøå")))
        for _, message in self.logs:
            self.assertTrue(message.isascii(), message)
        self.assertEqual(self.levels("INFO"), ["m.dk metro status: M3/M4: Ingen tog til Norreport - folg skiltene"])

    def test_no_messages_is_logged_as_such(self):
        self.assertEqual(self.fetch(SiteSession(mdk_data()))["messages"], [])
        self.assertEqual(self.levels("INFO"), ["m.dk metro status: no messages"])

    def test_a_failure_is_warned_about_at_most_every_half_hour(self):
        down = SiteSession(error=OSError("connection refused"))
        for _ in range(5):
            self.fetch(down)
        self.assertEqual(len(self.levels("WARNING")), 1)
        self.assertIn("connection refused", self.levels("WARNING")[0])
        self.app._mdk_warned_at = datetime.now() - ta.MDK_WARN_EVERY - timedelta(seconds=1)
        self.fetch(down)
        self.assertEqual(len(self.levels("WARNING")), 2)

    def test_a_failure_says_what_was_wrong_with_the_data(self):
        self.fetch(SiteSession({"installations": []}))
        self.assertIn("no activeMessages list", self.levels("WARNING")[0])

    def test_a_recovery_to_the_same_messages_is_not_a_change(self):
        self.fetch(SiteSession(NORMAL_DAY))
        self.fetch(SiteSession(error=OSError("down")))
        self.fetch(SiteSession(NORMAL_DAY))
        self.assertEqual(len(self.levels("INFO")), 1)


class TestOperatorStatusPerCheck(OperatorStatusFetchTestCase):
    METRO_FRB = dict(MDK_METRO, sensor_id="enghave_metro_frb", transport_name="Metro M3 -> Frederiksberg")
    STOG = {
        "sensor_id": "carlsberg_stog_kbh", "transport_name": "S-tog", "stop_id": "8600631",
        "evaluation_mode": "passenger_impact",
    }

    def test_both_metro_routes_share_one_fetch(self):
        session = SiteSession(NORMAL_DAY)
        results = self.fetch_all(session, [MDK_METRO, self.METRO_FRB, self.STOG])
        self.assertEqual(session.mdk_requests, 1)
        for sensor_id in ("enghave_metro_kbh", "enghave_metro_frb"):
            self.assertEqual(results[sensor_id]["operator_status"], CLEAR_TEXT)
            self.assertIs(results[sensor_id]["operator_status_ok"], True)
            self.assertTrue(results[sensor_id]["no_times"])
            self.assertFalse(results[sensor_id]["has_issues"])
        self.assertFalse(OPERATOR_KEYS & set(results["carlsberg_stog_kbh"]))
        self.assertTrue(results["carlsberg_stog_kbh"]["no_service"])

    def test_a_check_without_a_metro_route_never_asks_m_dk(self):
        session = SiteSession(NORMAL_DAY)
        self.fetch_all(session, [self.STOG])
        self.assertEqual(session.mdk_requests, 0)

    def test_each_check_fetches_afresh(self):
        session = SiteSession(NORMAL_DAY)
        self.fetch_all(session, [MDK_METRO])
        self.fetch_all(session, [MDK_METRO])
        self.assertEqual(session.mdk_requests, 2)

    def test_a_disruption_on_the_metro_lines_makes_every_metro_route_disrupted(self):
        results = self.fetch_all(SiteSession(M3_DISRUPTED), [MDK_METRO, self.METRO_FRB])
        for res in results.values():
            self.assertEqual(res["severity"], 3)
            self.assertEqual(res["issues"][0].split(": ", 1)[1], DISRUPTION_TEXT)

    def test_a_disruption_on_other_lines_leaves_every_metro_route_ok(self):
        results = self.fetch_all(SiteSession(M1_DISRUPTED), [MDK_METRO, self.METRO_FRB])
        for res in results.values():
            self.assertEqual(res["severity"], 0)
            self.assertFalse(res["has_issues"])
            self.assertIs(res["operator_status_ok"], True)
            self.assertEqual(res["operator_status_raw"], f"M1/M2: {DISRUPTION_TEXT} | M3/M4: {CLEAR_TEXT}")

    def test_unreadable_data_leaves_every_route_as_rejseplanen_made_it(self):
        for label, down in {
            "error": SiteSession(error=OSError("down")),
            "not JSON": SiteSession("<html></html>"),
            "no activeMessages": SiteSession({"installations": []}),
            "HTTP 500": SiteSession("oops", status=500),
        }.items():
            with self.subTest(label):
                results = self.fetch_all(down, [MDK_METRO, self.STOG])
                metro = results["enghave_metro_kbh"]
                self.assertIsNone(metro["operator_status"])
                self.assertTrue(metro["no_service"])
                self.assertNotIn("no_times", metro)
                self.assertNotIn("operator_status_ok", metro)
                self.assertEqual(metro["severity"], 0)
                self.assertEqual(
                    {k: v for k, v in metro.items() if k != "operator_status"},
                    {k: v for k, v in results["carlsberg_stog_kbh"].items()},
                )

    def test_rejseplanen_times_and_an_all_clear_agree_when_both_exist(self):
        board = {"Departure": [dep(2), dep(5), dep(8)]}
        results = self.fetch_all(SiteSession(NORMAL_DAY, board=board), [MDK_METRO])
        metro = results["enghave_metro_kbh"]
        self.assertFalse(metro["no_times"])
        self.assertEqual(len(metro["departures"]), 3)
        self.assertEqual(metro["severity"], 0)


class TestOperatorStatusConfig(unittest.TestCase):
    """The deployed yaml: the Metro follows m.dk's M3 messages everywhere it is polled, nothing else does."""

    @classmethod
    def setUpClass(cls) -> None:
        class Loader(yaml.SafeLoader):
            pass

        Loader.add_constructor("!secret", lambda loader, node: "secret")
        text = (Path(__file__).resolve().parents[1] / "transit_alarm.yaml").read_text()
        cls.apps = yaml.load(text, Loader=Loader)
        cls.routes = [(app, route) for app, cfg in cls.apps.items() for route in cfg["routes"]]

    def test_the_metro_routes_follow_m_dk_for_m3_in_every_instance_that_polls_them(self):
        metro = [(app, route) for app, route in self.routes if route["sensor_id"].startswith("enghave_metro_")]
        self.assertEqual({route["sensor_id"] for _, route in metro}, {"enghave_metro_kbh", "enghave_metro_frb"})
        self.assertIn(("TransitMikkel", "enghave_metro_kbh"), {(app, route["sensor_id"]) for app, route in metro})
        for app, route in metro:
            with self.subTest(f"{app} {route['sensor_id']}"):
                self.assertEqual(route.get("operator_status"), "mdk")
                self.assertEqual(route.get("operator_status_lines"), ["M3"])

    def test_no_other_route_follows_an_operator(self):
        for app, route in self.routes:
            if not route["sensor_id"].startswith("enghave_metro_"):
                with self.subTest(f"{app} {route['sensor_id']}"):
                    self.assertNotIn("operator_status", route)
                    self.assertNotIn("operator_status_lines", route)

    def test_every_source_named_is_known(self):
        for _, route in self.routes:
            self.assertIn(route.get("operator_status"), (None, *ta.OPERATOR_STATUS_SOURCES))

    def test_every_instance_in_the_yaml_starts(self):
        for name, args in self.apps.items():
            with self.subTest(name):
                app = ta.TransitAlarm.__new__(ta.TransitAlarm)
                app.args = args
                app.log = lambda *args, **kwargs: None
                for method in ("listen_state", "run_every", "run_in", "create_task"):
                    setattr(app, method, lambda *args, **kwargs: None)
                app.initialize()
                self.assertEqual(app.routes, args["routes"])

    def start(self, route: dict) -> None:
        app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        app.args = {"access_id": "key", "routes": [route]}
        app.log = lambda *args, **kwargs: None
        app.initialize()

    def test_an_unknown_source_is_refused_at_start(self):
        with self.assertRaisesRegex(ValueError, "invalid operator_status 'nope'"):
            self.start(dict(MDK_METRO, operator_status="nope"))

    def test_a_followed_route_must_name_its_lines(self):
        without = {k: v for k, v in MDK_METRO.items() if k != "operator_status_lines"}
        for label, route in {
            "missing": without,
            "empty": dict(MDK_METRO, operator_status_lines=[]),
            "a bare string": dict(MDK_METRO, operator_status_lines="M3"),
            "a blank line": dict(MDK_METRO, operator_status_lines=["M3", " "]),
            "a number": dict(MDK_METRO, operator_status_lines=[3]),
        }.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, "needs operator_status_lines"):
                    self.start(route)


if __name__ == "__main__":
    unittest.main()
