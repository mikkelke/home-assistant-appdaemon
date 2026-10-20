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
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
