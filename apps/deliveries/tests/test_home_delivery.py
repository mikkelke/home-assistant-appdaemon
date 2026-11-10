# tests/test_home_delivery.py - HomeDelivery: Wolt stages from the integration's entities, Nemlig parsing,
# login and polling cadence, the ledger, the published contract and the spoken answer. All data is made up.
# Run from repo root: python3 -m unittest discover -s apps/deliveries/tests

import ast
import copy
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
        sys.modules[name] = types.ModuleType(name)
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import home_delivery as hd  # noqa: E402

NOW = datetime(2026, 11, 10, 16, 48, tzinfo=timezone.utc).timestamp()  # 17:48 in Copenhagen
SENSOR = "sensor.home_delivery"
KITCHEN = "Test Kitchen"

# AppDaemon 4.5.13's private ADAPI, Hass and ADBase members: an app attribute with one of these names
# breaks AppDaemon, as `_log` once did (ActorAttribution, 27.07).
APPDAEMON_PRIVATE = {"_check_entity", "_check_service", "_config_model", "_create_helper", "_domain_service_call",
                     "_entity_service_call", "_get_namespace", "_log", "_logging", "_namespace",
                     "_notify_mobile_app", "_plugin", "_sub_stack", "_template_command"}


def at(minutes=0, now=NOW):
    return now + minutes * 60


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def wolt(venue=KITCHEN, slug="test_kitchen", suffix="", status="pending", kind="IN_PROGRESS", courier=None,
         soon=False, eta=None, items=1, tracking_eta=None):
    attrs = {"order_status_type": kind, "order_status": status, "is_arriving_soon": soon, "venue_name": venue}
    if courier is not None:
        attrs["delivering_your_order"] = courier
    if tracking_eta is not None:
        attrs["delivery_eta"] = stamp(tracking_eta)
    sibling = {"venue_name": venue}
    return {
        f"sensor.wolt_delivery_{slug}{suffix}": {"state": "unknown", "attributes": attrs},
        f"sensor.wolt_order_{slug}_status{suffix}": {"state": status, "attributes": sibling},
        f"sensor.wolt_order_{slug}_estimated_arrival{suffix}":
            {"state": stamp(eta) if eta is not None else "unknown", "attributes": sibling},
        f"sensor.wolt_order_{slug}_item_count{suffix}": {"state": str(items), "attributes": sibling},
    }


def nemlig_order(status="EnRoute", eta=None, estimated=None, booked=None, **extra):
    order = {"id": "1000000001", "orderStatus": status, "deliveryTime": stamp(eta) if eta is not None else None,
             "deliveryInterval": {"start": stamp(estimated[0]), "end": stamp(estimated[1])} if estimated else None,
             "timeslotInterval": {"start": stamp(booked[0]), "end": stamp(booked[1])} if booked else None,
             "deliveryType": "Attended", "deliveryProgress": 1, "price": {"value": 100, "currency": "DKK"}}
    order.update(extra)
    return order


def delivery(**kw):
    values = {"id": "wolt:x", "source": "wolt", "kind": "food", "title": KITCHEN, "stage": "on_the_way"}
    values.update(kw)
    return hd.Delivery(**values)


class WoltStageTests(unittest.TestCase):
    def stage(self, **attrs):
        base = {"order_status_type": "IN_PROGRESS", "order_status": "pending"}
        base.update(attrs)
        return hd.wolt_stage(base)

    def test_active_order_without_courier_is_preparing(self):
        self.assertEqual(self.stage(), "preparing")
        self.assertEqual(self.stage(delivering_your_order=False), "preparing")

    def test_courier_en_route_is_on_the_way_whatever_the_unreliable_status_says(self):
        self.assertEqual(self.stage(delivering_your_order=True, order_status="pending"), "on_the_way")
        self.assertEqual(self.stage(delivering_your_order="true"), "on_the_way")

    def test_arriving_soon_beats_en_route(self):
        self.assertEqual(self.stage(delivering_your_order=True, is_arriving_soon=True), "arriving")

    def test_terminal_statuses(self):
        self.assertEqual(self.stage(order_status="delivered", order_status_type="DELIVERED"), "delivered")
        self.assertEqual(self.stage(order_status="cancelled", order_status_type="CANCELLED"), "cancelled")
        self.assertEqual(self.stage(order_status="failed", order_status_type="FAILED"), "cancelled")

    def test_not_active_and_not_terminal_is_ignored(self):
        self.assertIsNone(self.stage(order_status_type="UNKNOWN"))
        self.assertIsNone(hd.wolt_stage({}))

    def test_delivered_wins_over_a_lingering_en_route_flag(self):
        self.assertEqual(self.stage(order_status="delivered", delivering_your_order=True), "delivered")


class WoltDiscoveryTests(unittest.TestCase):
    def test_orders_are_found_by_name_with_eta_and_items_from_their_siblings(self):
        states = {**wolt(courier=True, eta=at(8), items=3),
                  **wolt(venue="Pizza Test", slug="pizza_test", eta=at(30), items=2),
                  "sensor.washer_state": {"state": "Off", "attributes": {}}}
        found = {d.id: d for d in hd.wolt_deliveries(states)}
        self.assertEqual(set(found), {"wolt:test_kitchen", "wolt:pizza_test"})
        kitchen = found["wolt:test_kitchen"]
        self.assertEqual((kitchen.source, kitchen.kind, kitchen.title, kitchen.stage, kitchen.eta, kitchen.item_count),
                         ("wolt", "food", KITCHEN, "on_the_way", at(8), 3))
        self.assertEqual((found["wolt:pizza_test"].stage, found["wolt:pizza_test"].eta), ("preparing", at(30)))

    def test_eta_falls_back_to_the_tracking_sensor_and_items_to_none(self):
        states = wolt(courier=True, tracking_eta=at(5))
        del states["sensor.wolt_order_test_kitchen_estimated_arrival"]
        del states["sensor.wolt_order_test_kitchen_item_count"]
        [d] = hd.wolt_deliveries(states)
        self.assertEqual((d.eta, d.item_count), (at(5), None))

    def test_a_passed_eta_survives_in_the_estimate_sensor(self):
        [d] = hd.wolt_deliveries(wolt(courier=True, eta=at(-4)))
        self.assertEqual(d.eta, at(-4))

    def test_the_same_venue_twice_keeps_each_orders_siblings_apart(self):
        old = wolt(status="delivered", kind="DELIVERED", items=7)
        new = wolt(suffix="_2", courier=True, eta=at(6), items=2)
        found = {d.id: d for d in hd.wolt_deliveries({**old, **new})}
        self.assertEqual(found["wolt:test_kitchen"].stage, "delivered")
        self.assertEqual((found["wolt:test_kitchen"].eta, found["wolt:test_kitchen"].item_count), (None, 7))
        self.assertEqual((found["wolt:test_kitchen_2"].stage, found["wolt:test_kitchen_2"].eta,
                          found["wolt:test_kitchen_2"].item_count), ("on_the_way", at(6), 2))

    def test_a_venue_named_like_a_sibling_suffix_still_joins(self):
        states = wolt(venue="Status Item Count", slug="status_item_count", courier=True, eta=at(3), items=4)
        [d] = hd.wolt_deliveries(states)
        self.assertEqual((d.eta, d.item_count), (at(3), 4))

    def test_pickup_destination_and_unavailable_sensors_are_not_deliveries(self):
        states = {"sensor.wolt_pickup_test_kitchen": {"state": "pickup", "attributes": wolt()["sensor.wolt_delivery_test_kitchen"]["attributes"]},
                  "sensor.wolt_delivery_other": {"state": "unavailable", "attributes": {"friendly_name": "Wolt"}}}
        self.assertEqual(hd.wolt_deliveries(states), [])

    def test_missing_venue_name_titles_the_card_wolt(self):
        states = wolt()
        states["sensor.wolt_delivery_test_kitchen"]["attributes"].pop("venue_name")
        [d] = hd.wolt_deliveries(states)
        self.assertEqual(d.title, "Wolt")

    def test_garbage_states_do_not_crash(self):
        states = {"sensor.wolt_delivery_a": None, "sensor.wolt_delivery_b": {"attributes": []},
                  "sensor.wolt_order_a_item_count": {"state": "nan", "attributes": None},
                  **wolt(courier=True, eta=at(4))}
        states["sensor.wolt_order_test_kitchen_estimated_arrival"]["state"] = "not a time"
        states["sensor.wolt_order_test_kitchen_item_count"]["state"] = "-3"
        [d] = hd.wolt_deliveries(states)
        self.assertEqual((d.eta, d.item_count), (None, None))


class NemligParseTests(unittest.TestCase):
    def test_nothing_to_track(self):
        for order in (None, {}, [], "x", {"orderStatus": None}, nemlig_order("Reorder", booked=(at(-300), at(-240)))):
            with self.subTest(order=order):
                self.assertIsNone(hd.nemlig_delivery(order, NOW))

    def test_en_route_takes_the_eta_and_prefers_the_estimated_window(self):
        order = nemlig_order("EnRoute", eta=at(25), estimated=(at(15), at(40)), booked=(at(-60), at(120)))
        d = hd.nemlig_delivery(order, NOW)
        self.assertEqual((d.id, d.source, d.kind, d.title, d.stage), ("nemlig", "nemlig", "groceries", "Nemlig", "on_the_way"))
        self.assertEqual((d.eta, d.window_start, d.window_end), (at(25), at(15), at(40)))

    def test_en_route_without_an_estimate_uses_the_booked_window(self):
        d = hd.nemlig_delivery(nemlig_order("EnRoute", booked=(at(-60), at(120))), NOW)
        self.assertEqual((d.eta, d.window_start, d.window_end), (None, at(-60), at(120)))

    def test_any_other_status_is_preparing_and_has_no_eta(self):
        for status in ("Placed", "Packing", "SomethingNew"):
            with self.subTest(status=status):
                d = hd.nemlig_delivery(nemlig_order(status, eta=at(5), booked=(at(60), at(240))), NOW)
                self.assertEqual((d.stage, d.eta, d.window_start, d.window_end), ("preparing", None, at(60), at(240)))

    def test_delivered_carries_when_it_ended(self):
        d = hd.nemlig_delivery(nemlig_order("Delivered", eta=at(-5), booked=(at(-120), at(-30))), NOW)
        self.assertEqual((d.stage, d.ended_at), ("delivered", at(-5)))

    def test_an_order_for_a_later_day_is_not_a_delivery_yet(self):
        tomorrow = datetime(2026, 11, 11, 0, 30).replace(tzinfo=hd.HOUSE_TZ).timestamp()
        self.assertIsNone(hd.nemlig_delivery(nemlig_order("Placed", booked=(tomorrow, tomorrow + 3600)), NOW))
        late = datetime(2026, 11, 10, 23, 30).replace(tzinfo=hd.HOUSE_TZ).timestamp()
        self.assertIsNotNone(hd.nemlig_delivery(nemlig_order("Placed", booked=(late, late + 1800)), NOW))

    def test_an_order_the_shop_never_closed_expires(self):
        order = nemlig_order("Placed", booked=(at(-600), at(-181)))
        self.assertIsNone(hd.nemlig_delivery(order, NOW))
        order = nemlig_order("Placed", booked=(at(-600), at(-179)))
        self.assertIsNotNone(hd.nemlig_delivery(order, NOW))

    def test_malformed_times_are_dropped_not_fatal(self):
        order = nemlig_order("EnRoute", timeslotInterval={"start": "tomorrow", "end": None}, deliveryTime="soon",
                             deliveryInterval=[1, 2])
        d = hd.nemlig_delivery(order, NOW)
        self.assertEqual((d.stage, d.eta, d.window_start, d.window_end), ("on_the_way", None, None, None))

    def test_a_backwards_window_is_ignored(self):
        d = hd.nemlig_delivery(nemlig_order("Placed", booked=(at(120), at(60))), NOW)
        self.assertEqual((d.window_start, d.window_end), (None, None))


class NemligCadenceTests(unittest.TestCase):
    def test_idle_without_a_live_order(self):
        for order in (None, {"orderStatus": "Reorder"}, nemlig_order("Delivered"), {"orderStatus": "Placed"}):
            with self.subTest(order=order):
                self.assertEqual(hd.nemlig_poll_s(order, NOW), hd.NEMLIG_IDLE_S)

    def test_fast_from_an_hour_before_the_booked_window(self):
        self.assertEqual(hd.nemlig_poll_s(nemlig_order("EnRoute"), NOW), hd.NEMLIG_ACTIVE_S)
        self.assertEqual(hd.nemlig_poll_s(nemlig_order("Packing", booked=(at(60), at(240))), NOW), hd.NEMLIG_ACTIVE_S)
        self.assertEqual(hd.nemlig_poll_s(nemlig_order("Packing", booked=(at(-30), at(150))), NOW), hd.NEMLIG_ACTIVE_S)

    def test_the_idle_poll_that_comes_before_the_lead_lands_on_it(self):
        self.assertEqual(hd.nemlig_poll_s(nemlig_order("Placed", booked=(at(70), at(250))), NOW), 600)
        self.assertEqual(hd.nemlig_poll_s(nemlig_order("Placed", booked=(at(600), at(780))), NOW), hd.NEMLIG_IDLE_S)

    def test_back_to_idle_when_the_window_is_long_gone(self):
        self.assertEqual(hd.nemlig_poll_s(nemlig_order("Placed", booked=(at(-600), at(-181))), NOW), hd.NEMLIG_IDLE_S)


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return copy.deepcopy(self._body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Answers by (method, path), from scripted lists; the last answer repeats."""

    def __init__(self, script):
        self.script = {key: list(answers) for key, answers in script.items()}
        self.calls = []

    def _answer(self, method, url, **kw):
        path = url.split(".nemlig.com", 1)[-1] if "www.nemlig.com" in url else url.split(".nemlig.it", 1)[-1]
        self.calls.append((method, path, kw))
        answers = self.script[(method, path)]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)

    def paths(self):
        return [f"{method} {path}" for method, path, _ in self.calls]


FEATURED = ("GET", "/productbff/api/web/orderHistory/orders/featured")


def nemlig_script(order=None, over=None):
    script = {("GET", "/webapi/AntiForgery"): [Response(body={"Header": "h", "Value": "xsrf-1"})],
              ("GET", "/webapi/Token"): [Response(body={"access_token": "bearer-1", "expires_in": 300})],
              ("POST", "/webapi/login"): [Response(body={"DeliveryZoneId": 1})],
              FEATURED: [Response(body={"order": order})]}
    script.update(over or {})
    return script


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class NemligClientTests(unittest.TestCase):
    def make(self, order=None, over=None):
        http, clock = FakeHttp(nemlig_script(order, over)), Clock()
        return hd.NemligClient("someone@example.invalid", "not-a-real-password", http=http, clock=clock), http, clock

    def test_first_poll_logs_in_and_reads_the_featured_order(self):
        client, http, _ = self.make({"orderStatus": "EnRoute"})
        self.assertEqual(client.featured_order(), {"orderStatus": "EnRoute"})
        self.assertEqual(http.paths(), ["GET /webapi/AntiForgery", "GET /webapi/Token", "POST /webapi/login",
                                        "GET /webapi/Token", "GET /productbff/api/web/orderHistory/orders/featured"])
        login = http.calls[2][2]
        self.assertEqual((login["json"]["Username"], login["json"]["Password"]),
                         ("someone@example.invalid", "not-a-real-password"))
        self.assertEqual(login["headers"]["Authorization"], "Bearer bearer-1")
        self.assertEqual(login["headers"]["X-XSRF-TOKEN"], "xsrf-1")
        featured = http.calls[-1][2]["headers"]
        self.assertEqual((featured["Authorization"], featured["X-XSRF-TOKEN"]), ("Bearer bearer-1", "xsrf-1"))
        self.assertNotIn("Authorization", http.calls[1][2]["headers"])
        for _, _, kw in http.calls:
            self.assertNotIn("not-a-real-password", json.dumps(kw["headers"]))
            self.assertTrue(kw["timeout"] > 0)

    def test_the_session_is_reused_until_the_bearer_nears_expiry(self):
        client, http, clock = self.make({"orderStatus": "EnRoute"})
        client.featured_order()
        http.calls.clear()
        clock.now += 200
        client.featured_order()
        self.assertEqual(http.paths(), ["GET /productbff/api/web/orderHistory/orders/featured"])
        http.calls.clear()
        clock.now += 80
        client.featured_order()
        self.assertEqual(http.paths(), ["GET /webapi/Token", "GET /productbff/api/web/orderHistory/orders/featured"])

    def test_a_rejected_bearer_logs_in_again_once(self):
        client, http, _ = self.make({"orderStatus": "EnRoute"}, {FEATURED: [Response(401), Response(body={"order": {"orderStatus": "Packing"}})]})
        self.assertEqual(client.featured_order(), {"orderStatus": "Packing"})
        self.assertEqual(http.paths().count("POST /webapi/login"), 2)

    def test_a_second_rejection_raises(self):
        client, _, _ = self.make(over={FEATURED: [Response(401)]})
        with self.assertRaises(RuntimeError):
            client.featured_order()

    def test_a_long_session_is_renewed_with_a_fresh_login(self):
        client, http, clock = self.make({"orderStatus": "EnRoute"})
        client.featured_order()
        clock.now += hd.NEMLIG_LOGIN_MAX_AGE_S + 1
        client.featured_order()
        self.assertEqual(http.paths().count("POST /webapi/login"), 2)

    def test_no_order_and_odd_bodies_read_as_none(self):
        for body in ({"order": None}, {}, [], None, {"order": "x"}):
            with self.subTest(body=body):
                client, _, _ = self.make(over={FEATURED: [Response(body=body)]})
                self.assertIsNone(client.featured_order())

    def test_rejected_credentials_are_an_auth_failure_that_names_no_secret(self):
        client, _, _ = self.make(over={("POST", "/webapi/login"): [Response(401)]})
        result = hd.poll_nemlig(client)
        self.assertEqual(result["ok"], False)
        self.assertTrue(result["auth"])
        self.assertNotIn("not-a-real-password", result["error"])

    def test_any_other_failure_is_reported_not_raised(self):
        client, _, _ = self.make(over={FEATURED: [Response(500)]})
        result = hd.poll_nemlig(client)
        self.assertEqual((result["ok"], result.get("auth")), (False, None))
        self.assertIn("500", result["error"])
        self.assertEqual(hd.poll_nemlig(types.SimpleNamespace(featured_order=lambda: {"orderStatus": "Reorder"})),
                         {"ok": True, "order": {"orderStatus": "Reorder"}})


class LedgerTests(unittest.TestCase):
    def test_an_ended_delivery_nobody_saw_alive_is_history(self):
        ledger = hd.Ledger()
        self.assertEqual(ledger.update([delivery(stage="delivered"), delivery(id="wolt:y", stage="cancelled")], NOW), [])

    def test_a_delivery_seen_alive_stays_30_minutes_after_it_ends(self):
        ledger = hd.Ledger()
        self.assertEqual(ledger.update([delivery()], NOW)[0].stage, "on_the_way")
        self.assertEqual(ledger.update([delivery(stage="delivered")], at(1))[0].stage, "delivered")
        self.assertEqual(len(ledger.update([delivery(stage="delivered")], at(30.9))), 1)
        self.assertEqual(ledger.update([delivery(stage="delivered")], at(31)), [])
        self.assertEqual(ledger.update([delivery(stage="delivered")], at(32)), [])

    def test_the_first_time_an_ended_delivery_was_seen_ended_counts_not_the_latest(self):
        ledger = hd.Ledger()
        ledger.update([delivery()], NOW)
        ledger.update([delivery(stage="cancelled")], at(1))
        self.assertEqual(len(ledger.update([delivery(stage="cancelled")], at(30))), 1)
        self.assertEqual(ledger.update([delivery(stage="cancelled")], at(31)), [])

    def test_a_source_that_says_when_it_ended_needs_no_prior_sighting(self):
        ledger = hd.Ledger()
        recent = delivery(id="nemlig", stage="delivered", ended_at=at(-10))
        self.assertEqual([d.id for d in ledger.update([recent], NOW)], ["nemlig"])
        self.assertEqual(ledger.update([recent], at(20)), [])
        old = delivery(id="nemlig2", stage="delivered", ended_at=at(-31))
        self.assertEqual(hd.Ledger().update([old], NOW), [])

    def test_an_active_delivery_that_vanishes_is_held_ten_minutes(self):
        ledger = hd.Ledger()
        ledger.update([delivery()], NOW)
        self.assertEqual(len(ledger.update([], at(1))), 1)
        self.assertEqual(len(ledger.update([], at(10.9))), 1)
        self.assertEqual(ledger.update([], at(11)), [])

    def test_a_delivery_that_comes_back_resets_the_hold(self):
        ledger = hd.Ledger()
        ledger.update([delivery()], NOW)
        ledger.update([], at(1))
        ledger.update([delivery()], at(8))
        self.assertEqual(len(ledger.update([], at(9))), 1)
        self.assertEqual(len(ledger.update([], at(18.9))), 1)
        self.assertEqual(ledger.update([], at(19)), [])

    def test_the_latest_observation_replaces_the_held_one(self):
        ledger = hd.Ledger()
        ledger.update([delivery(stage="preparing")], NOW)
        [d] = ledger.update([delivery(stage="arriving", eta=at(2))], at(1))
        self.assertEqual((d.stage, d.eta), ("arriving", at(2)))


class SnapshotTests(unittest.TestCase):
    def test_nothing_on_the_way(self):
        published = hd.snapshot([], NOW)
        self.assertEqual(published["state"], 0)
        attrs = published["attributes"]
        self.assertEqual((attrs["deliveries"], attrs["next"], attrs["courier_expected"]), ([], None, False))
        self.assertEqual(attrs["spoken"], "There's no delivery on the way.")
        self.assertEqual(attrs["reason"], "no active delivery")

    def test_state_counts_only_active_and_sorting_puts_active_first_by_eta_then_window(self):
        late = delivery(id="wolt:late", title="Late", stage="preparing", eta=at(40))
        soon = delivery(id="wolt:soon", title="Soon", stage="on_the_way", eta=at(5))
        window = delivery(id="nemlig", source="nemlig", title="Nemlig", stage="preparing", window_start=at(20), window_end=at(80))
        done = delivery(id="wolt:done", title="Done", stage="delivered", eta=at(-10))
        gone = delivery(id="wolt:gone", title="Gone", stage="cancelled")
        published = hd.snapshot([done, late, gone, window, soon], NOW)
        self.assertEqual(published["state"], 3)
        self.assertEqual([e["id"] for e in published["attributes"]["deliveries"]],
                         ["wolt:soon", "nemlig", "wolt:late", "wolt:done", "wolt:gone"])
        self.assertEqual(published["attributes"]["next"]["id"], "wolt:soon")

    def test_entries_carry_the_contract_with_local_iso_times_and_minutes(self):
        d = delivery(eta=at(8), window_start=at(0), window_end=at(60), item_count=2)
        entry = hd.snapshot([d], NOW)["attributes"]["deliveries"][0]
        self.assertEqual(set(entry), {"id", "source", "kind", "title", "stage", "eta", "window_start", "window_end",
                                      "minutes_left", "item_count"})
        self.assertEqual(entry["eta"], "2026-11-10T17:56:00+01:00")
        self.assertEqual((entry["window_start"], entry["window_end"]), ("2026-11-10T17:48:00+01:00", "2026-11-10T18:48:00+01:00"))
        self.assertEqual((entry["minutes_left"], entry["item_count"]), (8, 2))
        self.assertEqual(hd.snapshot([delivery(eta=NOW + 1)], NOW)["attributes"]["deliveries"][0]["minutes_left"], 1)
        self.assertEqual(hd.snapshot([delivery(eta=at(-3))], NOW)["attributes"]["deliveries"][0]["minutes_left"], 0)

    def test_unknowns_are_none_and_ended_entries_have_no_countdown(self):
        entry = hd.snapshot([delivery(stage="preparing")], NOW)["attributes"]["deliveries"][0]
        self.assertEqual((entry["eta"], entry["window_start"], entry["window_end"], entry["minutes_left"],
                          entry["item_count"]), (None, None, None, None, None))
        entry = hd.snapshot([delivery(stage="delivered", eta=at(-5))], NOW)["attributes"]["deliveries"][0]
        self.assertIsNone(entry["minutes_left"])

    def test_the_published_attributes_are_plain_json(self):
        published = hd.snapshot([delivery(eta=at(8)), delivery(id="nemlig", stage="preparing", window_start=at(5), window_end=at(60))], NOW)
        self.assertEqual(json.loads(json.dumps(published["attributes"])), published["attributes"])

    def test_courier_expected_when_something_is_on_the_way(self):
        for stage in ("on_the_way", "arriving"):
            with self.subTest(stage=stage):
                self.assertEqual(hd.courier_expected([delivery(stage=stage)], NOW), (True, "on the way"))

    def test_courier_expected_from_twenty_minutes_before_the_eta_to_thirty_after(self):
        d = delivery(stage="preparing", eta=NOW)
        for offset, expected in ((-1201, False), (-1200, True), (0, True), (1800, True), (1801, False)):
            with self.subTest(offset=offset):
                self.assertEqual(hd.courier_expected([d], NOW + offset)[0], expected)

    def test_courier_expected_inside_a_booked_window(self):
        d = delivery(source="nemlig", stage="preparing", window_start=at(-10), window_end=at(50))
        self.assertEqual(hd.courier_expected([d], NOW), (True, "inside its window"))
        self.assertEqual(hd.courier_expected([d], at(51)), (False, None))
        self.assertEqual(hd.courier_expected([d], at(-11)), (False, None))

    def test_courier_not_expected_for_an_order_still_in_the_kitchen_or_over(self):
        self.assertEqual(hd.courier_expected([delivery(stage="preparing")], NOW), (False, None))
        self.assertEqual(hd.snapshot([delivery(stage="delivered", eta=NOW)], NOW)["attributes"]["courier_expected"], False)

    def test_reason_says_what_made_the_courier_expected(self):
        attrs = hd.snapshot([delivery(stage="arriving", eta=at(2))], NOW)["attributes"]
        self.assertEqual(attrs["reason"], f"1 active, next {KITCHEN} arriving; courier expected: on the way")
        attrs = hd.snapshot([delivery(stage="preparing")], NOW)["attributes"]
        self.assertEqual(attrs["reason"], f"1 active, next {KITCHEN} preparing; no courier expected yet")


class SpokenTests(unittest.TestCase):
    def say(self, *deliveries, now=NOW):
        return hd.snapshot(list(deliveries), now)["attributes"]["spoken"]

    def test_wolt_on_the_way(self):
        self.assertEqual(self.say(delivery(eta=at(8))), "In about 8 minutes, around 5:56 PM.")
        self.assertEqual(self.say(delivery(stage="arriving", eta=at(0.5))), "In about a minute, around 5:48 PM.")

    def test_wolt_preparing(self):
        self.assertEqual(self.say(delivery(stage="preparing", eta=at(8))),
                         f"{KITCHEN} is still making it. Wolt expects it around 5:56 PM.")
        self.assertEqual(self.say(delivery(stage="preparing")), f"{KITCHEN} is still making it.")

    def test_wolt_late_or_without_an_estimate(self):
        self.assertEqual(self.say(delivery(eta=at(-3))), "It should be there any moment now.")
        self.assertEqual(self.say(delivery()), f"{KITCHEN} is on the way.")

    def test_nemlig(self):
        nemlig = {"id": "nemlig", "source": "nemlig", "kind": "groceries", "title": "Nemlig"}
        self.assertEqual(self.say(delivery(**nemlig, eta=at(-6))), "Nemlig should be there any moment now.")
        self.assertEqual(self.say(delivery(**nemlig, eta=at(-6)), now=at(-12)), "Nemlig is on the way, around 5:42 PM.")
        self.assertEqual(self.say(delivery(**nemlig)), "Nemlig is on the way.")
        booked = delivery(**{**nemlig, "stage": "preparing"}, window_start=at(-108), window_end=at(72))
        self.assertEqual(self.say(booked), "Nemlig is booked for 4 to 7 PM.")
        self.assertEqual(self.say(delivery(**{**nemlig, "stage": "preparing"})), "Nemlig has an order coming.")

    def test_clock_and_windows_across_noon_and_midnight(self):
        midnight = datetime(2026, 11, 10, 0, 5, tzinfo=hd.HOUSE_TZ).timestamp()
        noon = datetime(2026, 11, 10, 12, 30, tzinfo=hd.HOUSE_TZ).timestamp()
        self.assertEqual((hd.clock(midnight), hd.clock(noon)), ("12:05 AM", "12:30 PM"))
        start = datetime(2026, 11, 10, 11, 30, tzinfo=hd.HOUSE_TZ).timestamp()
        end = datetime(2026, 11, 10, 13, 0, tzinfo=hd.HOUSE_TZ).timestamp()
        self.assertEqual(hd.window_text(start, end), "11:30 AM to 1 PM")
        self.assertIsNone(hd.window_text(None, end))

    def test_more_deliveries_are_counted_after_the_next_one(self):
        first = delivery(eta=at(8))
        self.assertEqual(self.say(first, delivery(id="b", eta=at(20))),
                         "In about 8 minutes, around 5:56 PM. There's one more after that.")
        self.assertEqual(self.say(first, delivery(id="b", eta=at(20)), delivery(id="c", eta=at(30))),
                         "In about 8 minutes, around 5:56 PM. There are 2 more after that.")

    def test_a_finished_delivery_is_not_on_the_way(self):
        self.assertEqual(self.say(delivery(stage="delivered")), "There's no delivery on the way.")

    def test_an_unknown_source_still_gets_a_sentence(self):
        self.assertEqual(self.say(delivery(source="airmee", title="Airmee")), "Airmee is on the way.")


class Harness:
    def __init__(self, states=None, now=NOW, **args):
        self.now = now
        self.states = copy.deepcopy(states or {})
        self.published = []
        self.listened = []
        self.logs = []
        self.queue = []
        self.app = hd.HomeDelivery.__new__(hd.HomeDelivery)
        self.app.args = args
        self.app.log = lambda message, level="INFO": self.logs.append((level, message))
        self.app.get_now = lambda: datetime.fromtimestamp(self.now, timezone.utc)
        self.app.get_state = lambda entity=None, **kw: {key: copy.deepcopy(value) for key, value in self.states.items()
                                                       if key.startswith(entity + ".")}
        self.app.set_state = lambda entity, state=None, attributes=None, replace=False: self.published.append(
            (entity, state, copy.deepcopy(attributes), replace))
        self.app.listen_state = lambda callback, entity, **kw: self.listened.append((entity, kw)) or "handle"
        self.app.run_every = lambda *a, **kw: "timer"
        self.app.submit_to_executor = lambda func, *a, callback=None: self.queue.append((func, a, callback))
        self.app.initialize()

    @property
    def last(self):
        entity, state, attributes, replace = self.published[-1]
        return state, attributes

    def tick(self, seconds=0):
        self.now += seconds
        self.app._on_tick({})

    def run_executor(self):
        queue, self.queue = self.queue, []
        for func, args, callback in queue:
            callback(result=func(*args))


class AppTests(unittest.TestCase):
    def test_init_publishes_a_complete_replacing_idle_entity_and_ignores_old_orders(self):
        old = wolt(status="delivered", kind="DELIVERED")
        h = Harness(old)
        entity, state, attrs, replace = h.published[-1]
        self.assertEqual((entity, state, replace), (SENSOR, "0", True))
        self.assertEqual(attrs["deliveries"], [])
        self.assertEqual(attrs["spoken"], "There's no delivery on the way.")
        self.assertEqual(attrs["friendly_name"], "Home delivery")

    def test_a_wolt_order_from_the_kitchen_to_the_door(self):
        h = Harness({**wolt(), **{"sensor.other": {"state": "1", "attributes": {}}}})
        state, attrs = h.last
        self.assertEqual((state, attrs["next"]["stage"], attrs["courier_expected"]), ("1", "preparing", False))
        self.assertEqual(attrs["spoken"], f"{KITCHEN} is still making it.")

        h.states.update(wolt(courier=True, eta=at(16), items=2, tracking_eta=at(16)))
        h.tick(30)
        state, attrs = h.last
        self.assertEqual((attrs["next"]["stage"], attrs["next"]["item_count"], attrs["courier_expected"]),
                         ("on_the_way", 2, True))
        self.assertEqual(attrs["next"]["minutes_left"], 16)

        h.states.update(wolt(courier=True, soon=True, eta=at(16)))
        h.tick(900)
        state, attrs = h.last
        self.assertEqual((attrs["next"]["stage"], attrs["next"]["minutes_left"]), ("arriving", 1))
        self.assertTrue(attrs["spoken"].startswith("In about a minute"))

        h.tick(90)
        self.assertEqual(h.last[1]["next"]["minutes_left"], 0)
        self.assertEqual(h.last[1]["spoken"], "It should be there any moment now.")

        h.states.update(wolt(status="delivered", kind="DELIVERED"))
        h.tick(10)
        state, attrs = h.last
        self.assertEqual((state, attrs["next"], attrs["courier_expected"]), ("0", None, False))
        self.assertEqual([(e["stage"]) for e in attrs["deliveries"]], ["delivered"])

        h.tick(29 * 60)
        self.assertEqual(len(h.last[1]["deliveries"]), 1)
        h.tick(120)
        self.assertEqual(h.last[1]["deliveries"], [])
        self.assertIn(("INFO", "wolt:test_kitchen: new -> preparing"), h.logs)
        self.assertIn(("INFO", "wolt:test_kitchen: arriving -> delivered"), h.logs)

    def test_a_cancelled_order_is_listed_for_half_an_hour_without_counting(self):
        h = Harness(wolt(courier=True, eta=at(10)))
        h.states.update(wolt(status="cancelled", kind="CANCELLED"))
        h.tick(10)
        state, attrs = h.last
        self.assertEqual((state, [e["stage"] for e in attrs["deliveries"]]), ("0", ["cancelled"]))

    def test_every_tracking_sensor_is_listened_to_once_including_ones_that_appear_later(self):
        h = Harness(wolt())
        self.assertEqual([entity for entity, _ in h.listened], ["sensor.wolt_delivery_test_kitchen"])
        self.assertEqual(h.listened[0][1], {"attribute": "all"})
        h.states.update(wolt(venue="Pizza Test", slug="pizza_test"))
        h.tick(30)
        h.tick(30)
        self.assertEqual([entity for entity, _ in h.listened],
                         ["sensor.wolt_delivery_test_kitchen", "sensor.wolt_delivery_pizza_test"])

    def test_a_state_change_publishes_at_once(self):
        h = Harness(wolt())
        before = len(h.published)
        h.states.update(wolt(courier=True, eta=at(9)))
        h.app._on_change("sensor.wolt_delivery_test_kitchen", "all", {}, {}, {})
        self.assertEqual(len(h.published), before + 1)
        self.assertEqual(h.last[1]["next"]["stage"], "on_the_way")

    def test_an_order_whose_sensor_blinks_is_held_not_dropped(self):
        h = Harness(wolt(courier=True, eta=at(10)))
        for entity in list(h.states):
            h.states[entity] = {"state": "unavailable", "attributes": {"friendly_name": "Wolt"}}
        h.tick(60)
        self.assertEqual(h.last[0], "1")
        h.tick(600)
        self.assertEqual(h.last[0], "0")

    def test_two_orders_are_sorted_and_counted(self):
        h = Harness({**wolt(courier=True, eta=at(20)), **wolt(venue="Pizza Test", slug="pizza_test", courier=True, eta=at(6))})
        state, attrs = h.last
        self.assertEqual(state, "2")
        self.assertEqual([e["title"] for e in attrs["deliveries"]], ["Pizza Test", KITCHEN])
        self.assertTrue(attrs["spoken"].endswith("There's one more after that."))

    def test_without_a_nemlig_login_only_wolt_is_tracked(self):
        h = Harness(wolt())
        self.assertEqual(h.last[0], "1")
        self.assertIn("WARNING", [level for level, _ in h.logs])
        h.tick(30)
        self.assertEqual(h.queue, [])

    def test_app_attributes_do_not_shadow_appdaemon(self):
        tree = ast.parse(Path(hd.__file__).read_text())
        klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "HomeDelivery")
        names = {node.attr for node in ast.walk(klass) if isinstance(node, ast.Attribute)
                 and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name) and node.value.id == "self"}
        self.assertTrue(names)
        self.assertTrue(all(name.startswith("_") for name in names), names)
        self.assertEqual(names & APPDAEMON_PRIVATE, set())


class NemligAppTests(unittest.TestCase):
    def make(self, order=None, **over):
        client = types.SimpleNamespace(calls=0)

        def featured_order():
            client.calls += 1
            result = client.next
            if isinstance(result, Exception):
                raise result
            return result

        client.next = order
        client.featured_order = featured_order
        h = Harness(nemlig_user="x", nemlig_pass="y", **over)
        h.app._nemlig = client
        return h, client

    def test_the_first_tick_polls_and_a_live_order_is_published(self):
        order = nemlig_order("EnRoute", eta=at(25), estimated=(at(15), at(40)), booked=(at(-60), at(120)))
        h, client = self.make(order)
        self.assertEqual(h.last[0], "0")
        h.tick(5)
        self.assertEqual(len(h.queue), 1)
        h.run_executor()
        state, attrs = h.last
        self.assertEqual((state, attrs["next"]["id"], attrs["next"]["stage"], attrs["courier_expected"]),
                         ("1", "nemlig", "on_the_way", True))
        self.assertEqual(attrs["spoken"], "Nemlig is on the way, around 6:13 PM.")
        self.assertIn(("INFO", "Nemlig order status: None -> EnRoute"), h.logs)

    def test_polling_follows_the_cadence_and_is_not_started_twice(self):
        h, client = self.make(nemlig_order("EnRoute", eta=at(25)))
        h.tick(5)
        h.tick(30)
        self.assertEqual(len(h.queue), 1)
        h.run_executor()
        h.tick(30)
        h.tick(30)
        self.assertEqual(h.queue, [])
        h.tick(30)
        self.assertEqual(len(h.queue), 1)
        h.run_executor()
        self.assertEqual(len([m for _, m in h.logs if m.startswith("Nemlig order status")]), 1)

    def test_an_idle_account_is_polled_every_fifteen_minutes(self):
        h, client = self.make({"orderStatus": "Reorder"})
        h.tick(5)
        h.run_executor()
        h.tick(hd.NEMLIG_IDLE_S - 30)
        self.assertEqual(h.queue, [])
        h.tick(60)
        self.assertEqual(len(h.queue), 1)

    def test_the_poll_before_a_booked_window_lands_on_the_hour_before_it(self):
        h, client = self.make(nemlig_order("Placed", booked=(at(72), at(192))))
        h.tick(5)
        h.run_executor()
        self.assertEqual(h.last[0], "1")
        self.assertEqual(h.last[1]["spoken"], "Nemlig is booked for 7 to 9 PM.")
        h.tick(690)
        self.assertEqual(h.queue, [])
        h.tick(30)
        self.assertEqual(len(h.queue), 1)

    def test_a_failed_poll_retries_in_five_minutes_and_keeps_the_last_order(self):
        h, client = self.make(nemlig_order("EnRoute", eta=at(25)))
        h.tick(5)
        h.run_executor()
        client.next = RuntimeError("HTTP 503")
        h.tick(120)
        h.run_executor()
        self.assertEqual(h.last[0], "1")
        self.assertTrue(any(level == "WARNING" and "Nemlig poll failed" in message for level, message in h.logs))
        h.tick(240)
        self.assertEqual(h.queue, [])
        h.tick(60)
        self.assertEqual(len(h.queue), 1)

    def test_rejected_credentials_back_off_for_an_hour(self):
        h, client = self.make(None)
        client.next = hd.NemligAuthError("login rejected (HTTP 401)")
        h.tick(5)
        h.run_executor()
        h.tick(3500)
        self.assertEqual(h.queue, [])
        h.tick(120)
        self.assertEqual(len(h.queue), 1)

    def test_an_order_not_confirmed_for_an_hour_is_dropped_after_the_hold(self):
        h, client = self.make(nemlig_order("EnRoute", eta=at(500)))
        h.tick(5)
        h.run_executor()
        client.next = RuntimeError("HTTP 503")
        for _ in range(100):
            h.tick(30)
            h.run_executor()
        self.assertEqual(h.last[0], "1")
        for _ in range(60):
            h.tick(30)
            h.run_executor()
        self.assertEqual(h.last[0], "0")

    def test_delivered_is_listed_until_thirty_minutes_after_the_shop_says_so(self):
        h, client = self.make(nemlig_order("Delivered", eta=at(-5), booked=(at(-120), at(-30))))
        h.tick(5)
        h.run_executor()
        state, attrs = h.last
        self.assertEqual((state, [e["stage"] for e in attrs["deliveries"]]), ("0", ["delivered"]))
        h.tick(25 * 60)
        self.assertEqual(h.last[1]["deliveries"], [])

    def test_the_order_that_ends_after_a_restart_does_not_show_old_deliveries(self):
        h, client = self.make(nemlig_order("Delivered", eta=at(-300), booked=(at(-420), at(-360))))
        h.tick(5)
        h.run_executor()
        self.assertEqual(h.last[1]["deliveries"], [])

    def test_wolt_and_nemlig_share_one_sorted_entity(self):
        h, client = self.make(nemlig_order("EnRoute", eta=at(30)))
        h.states.update(wolt(courier=True, eta=at(10)))
        h.tick(5)
        h.run_executor()
        state, attrs = h.last
        self.assertEqual((state, [e["source"] for e in attrs["deliveries"]]), ("2", ["wolt", "nemlig"]))


if __name__ == "__main__":
    unittest.main()
