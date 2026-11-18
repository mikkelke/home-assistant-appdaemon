# tests/test_home_delivery.py - HomeDelivery: Wolt stages from the integration's entities, Nemlig parsing,
# login and polling cadence, Parcel Aggregator parcels, the ledger, the published contract and the spoken
# answer. All data is made up.
# Run from repo root: python3 -m unittest discover -s apps/deliveries/tests

import ast
import asyncio
import copy
import json
import re
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

try:
    import jinja2.sandbox
except ImportError:
    jinja2 = None

NOW = datetime(2026, 11, 10, 16, 48, tzinfo=timezone.utc).timestamp()  # 17:48 in Copenhagen
SENSOR = "sensor.home_delivery"
KITCHEN = "Test Kitchen"
SHIPPED = Path(__file__).resolve().parents[1] / "home_delivery.yaml"

# AppDaemon 4.5.13's private ADAPI, Hass and ADBase members: an app attribute with one of these names
# breaks AppDaemon, as `_log` once did (ActorAttribution, 27.07).
APPDAEMON_PRIVATE = {"_check_entity", "_check_service", "_config_model", "_create_helper", "_domain_service_call",
                     "_entity_service_call", "_get_namespace", "_log", "_logging", "_namespace",
                     "_notify_mobile_app", "_plugin", "_sub_stack", "_template_command"}


def appdaemon_render_result(raw):
    """What AppDaemon's render_template hands the app: the raw text, or its literal_eval when that parses."""
    try:
        return ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return raw


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


OWNER = "person.mikkel"
AGGREGATOR_INCOMING = "sensor.parcel_aggregator_incoming_parcels"
AGGREGATOR_DELIVERED = "sensor.parcel_aggregator_delivered_parcels"


def parcel(carrier="PostNord", barcode="TESTPN0001", status="out_for_delivery", sender="Test Shop", start=None,
           end=None, delivered_at=None, **extra):
    """One parcel as the Parcel Aggregator publishes it: ISO times, null where the carrier says nothing."""
    values = {"carrier": carrier, "barcode": barcode, "sender": sender, "receiver": None, "status": status,
              "raw_status": "Test status", "delivered": status == "delivered",
              "delivered_at": stamp(delivered_at) if delivered_at is not None else None,
              "planned_from": stamp(start) if start is not None else None,
              "planned_to": stamp(end) if end is not None else None, "pickup": False, "pickup_point": None,
              "url": None, "weight": None, "dimensions": None, "history": None}
    values.update(extra)
    return values


def aggregator(*parcels):
    return {"state": str(len(parcels)), "attributes": {"parcels": list(parcels), "friendly_name": "Parcels"}}


def local_time(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=hd.HOUSE_TZ).timestamp()


INTERCOM_VISIT = "sensor.intercom_visit"
BUTTONS = {"airmee": "button.airmee_test_refresh", "postnord": "button.postnord_test_refresh"}


def intercom(*rings):
    """sensor.intercom_visit as AbbWelcomeBridge publishes it: a (door, ring time) per open visit, active while any."""
    visits = [{"door": door, "ring_at": stamp(ring_at)} for door, ring_at in rings]
    return {"state": "active" if visits else "idle", "attributes": {"visits": visits}}


def shipped_refresh_buttons():
    """The refresh_buttons map of the shipped home_delivery.yaml, read without PyYAML."""
    found, inside = {}, False
    for line in SHIPPED.read_text().splitlines():
        if line.strip() == "refresh_buttons:":
            inside = True
        elif inside:
            match = re.fullmatch(r"\s+(\w+): (button\.\w+)", line)
            if not match:
                break
            found[match[1]] = match[2]
    return found


LANDING = "binary_sensor.landing_test_presence"
APARTMENT_DOOR = "binary_sensor.apartment_test_door"
APARTMENT_BELL = "binary_sensor.apartment_test_bell"
LANDING_ARGS = {"landing_entity": LANDING, "apartment_door_entity": APARTMENT_DOOR,
                "apartment_doorbell_entity": APARTMENT_BELL, "owner_entity": OWNER, "notify_target": "owner_test"}
AIRMEE_SENTENCE = "Your Airmee parcel is probably outside the door."
POSTNORD_SENTENCE = "Your PostNord parcel is probably outside the door."
AIRMEE_PARCEL = ("Airmee", "TESTAM0001")
POSTNORD_PARCEL = ("PostNord", "TESTPN0001")


def shipped_setting(key):
    """A top-level `key: value` of the shipped home_delivery.yaml, read without PyYAML."""
    for line in SHIPPED.read_text().splitlines():
        match = re.fullmatch(rf"  {key}: (\S+)", line)
        if match:
            return match[1]


def landing_watch(source="airmee", carrier="Airmee", until=NOW + hd.LANDING_WATCH_S, **kw):
    return hd.LandingWatch(source, carrier, until, **kw)


def out_for_delivery(*parcels):
    """The aggregator's incoming sensor listing these (carrier, barcode) parcels as out for delivery."""
    return {AGGREGATOR_INCOMING: aggregator(*[parcel(carrier=carrier, barcode=barcode)
                                              for carrier, barcode in parcels])}


def presses(h):
    return [data["entity_id"] for service, data in h.calls if service == "button/press"]


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


class WoltStatesTests(unittest.TestCase):
    def test_the_marked_json_becomes_states_by_entity_id(self):
        raw = "J" + json.dumps([{"entity_id": "sensor.wolt_a", "state": "x", "attributes": {"k": 1}},
                                {"entity_id": "sensor.wolt_b", "state": "y", "attributes": {}}])
        self.assertEqual(hd.wolt_states(raw), {"sensor.wolt_a": {"entity_id": "sensor.wolt_a", "state": "x", "attributes": {"k": 1}},
                                              "sensor.wolt_b": {"entity_id": "sensor.wolt_b", "state": "y", "attributes": {}}})
        self.assertEqual(hd.wolt_states("J[]"), {})

    def test_malformed_items_are_skipped(self):
        raw = "J" + json.dumps([None, 3, {"state": "x"}, {"entity_id": 4}, {"entity_id": "sensor.wolt_a", "state": "x"}])
        self.assertEqual(list(hd.wolt_states(raw)), ["sensor.wolt_a"])

    def test_anything_else_is_an_error(self):
        for raw in (None, "", [], "[]", "J", "Jnot json", 'J{"a": 1', 7):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    hd.wolt_states(raw)


@unittest.skipIf(jinja2 is None, "jinja2 is not installed")
class WoltSensorsTemplateTests(unittest.TestCase):
    def render(self, sensors):
        environment = jinja2.sandbox.SandboxedEnvironment()
        environment.filters["to_json"] = json.dumps
        fake = types.SimpleNamespace(sensor=[types.SimpleNamespace(entity_id=entity_id, state=value["state"],
                                                                   attributes=value["attributes"])
                                             for entity_id, value in sensors.items()])
        return appdaemon_render_result(environment.from_string(hd.WOLT_SENSORS).render(states=fake))

    def test_only_wolt_sensors_come_out_with_their_state_and_attributes(self):
        sensors = {**wolt(courier=True, eta=at(8), items=3), "sensor.washer_state": {"state": "Off", "attributes": {}},
                   "sensor.woltish": {"state": "1", "attributes": {}}}
        out = hd.wolt_states(self.render(sensors))
        self.assertEqual(set(out), {key for key in sensors if key.startswith("sensor.wolt_")})
        tracking = out["sensor.wolt_delivery_test_kitchen"]
        self.assertEqual((tracking["state"], tracking["attributes"]), ("unknown", sensors["sensor.wolt_delivery_test_kitchen"]["attributes"]))
        self.assertEqual([d.stage for d in hd.wolt_deliveries(out)], ["on_the_way"])

    def test_json_types_survive(self):
        attributes = {"flag": True, "nothing": None, "list": [1, "a"], "number": 2.5}
        out = hd.wolt_states(self.render({"sensor.wolt_x": {"state": "s", "attributes": attributes}}))
        self.assertEqual(out["sensor.wolt_x"]["attributes"], attributes)

    def test_appdaemon_leaves_even_an_empty_result_a_string(self):
        self.assertEqual(self.render({}), "J[]")
        self.assertEqual(hd.wolt_states(self.render({})), {})


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
        self.assertEqual(attrs["upcoming"], [])
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


class ParcelStageTests(unittest.TestCase):
    def one(self, **kw):
        return hd.parcel_delivery(parcel(**kw), NOW)

    def test_out_for_delivery_is_on_the_way(self):
        d = self.one()
        self.assertEqual((d.id, d.source, d.kind, d.title, d.stage), ("postnord:TESTPN0001", "postnord", "parcel", "Test Shop", "on_the_way"))
        self.assertEqual((d.carrier, d.eta, d.window_start, d.window_end, d.item_count), ("PostNord", None, None, None, None))
        self.assertEqual(self.one(start=at(120), end=at(180)).stage, "on_the_way")
        self.assertEqual(self.one(start=at(120)).stage, "on_the_way")

    def test_it_is_arriving_once_the_expected_time_is_within_fifteen_minutes(self):
        for start, end, stage in ((at(15.5), at(60), "on_the_way"), (at(15), at(60), "arriving"), (at(1), at(60), "arriving"),
                                  (at(-30), at(30), "arriving"), (at(-90), at(-30), "arriving"),
                                  (at(15.5), None, "on_the_way"), (at(15), None, "arriving"), (at(-5), None, "arriving")):
            with self.subTest(start=start, end=end):
                self.assertEqual(self.one(start=start, end=end).stage, stage)

    def test_registered_or_in_transit_with_an_expected_time_today_is_preparing(self):
        for status in ("registered", "in_transit"):
            with self.subTest(status=status):
                d = self.one(status=status, start=at(60), end=at(180))
                self.assertEqual((d.stage, d.eta, d.window_start, d.window_end), ("preparing", None, at(60), at(180)))
                d = self.one(status=status, start=at(120))
                self.assertEqual((d.stage, d.eta, d.window_start, d.window_end), ("preparing", at(120), None, None))

    def test_today_is_the_house_calendar_day_of_the_expected_time(self):
        for status in ("registered", "in_transit"):
            with self.subTest(status=status):
                late = local_time(10, 23, 30)
                self.assertEqual(self.one(status=status, start=late, end=late + 1800).stage, "preparing")
                after_midnight = local_time(11, 0, 30)
                self.assertIsNone(self.one(status=status, start=after_midnight, end=after_midnight + 3600))
                self.assertIsNone(self.one(status=status, start=at(24 * 60), end=at(25 * 60)))
                self.assertIsNone(self.one(status=status, start=at(-24 * 60), end=at(-23 * 60)))
                self.assertEqual(self.one(status=status, start=local_time(10, 0, 5)).stage, "preparing")

    def test_without_an_expected_time_a_parcel_that_is_not_out_is_not_listed(self):
        for status in ("registered", "in_transit"):
            with self.subTest(status=status):
                self.assertIsNone(self.one(status=status))

    def test_nothing_else_is_listed_even_with_a_time_today(self):
        for status in ("at_pickup_point", "returning", "problem", "unknown", "", None, 7, ["delivered"], "Delivered"):
            with self.subTest(status=status):
                self.assertIsNone(self.one(status=status, start=at(60), end=at(180)))
                self.assertIsNone(self.one(status=status))

    def test_old_parcels_the_carrier_still_lists_stay_out(self):
        years_ago = local_time(10, 12) - 700 * 86400
        for status in ("returning", "registered", "in_transit"):
            with self.subTest(status=status):
                self.assertIsNone(self.one(status=status))
                self.assertIsNone(self.one(status=status, start=years_ago, end=years_ago + 7200))
                self.assertIsNone(self.one(status=status, start=years_ago))

    def test_delivered_carries_when_it_ended(self):
        d = self.one(status="delivered", delivered_at=at(-5))
        self.assertEqual((d.stage, d.ended_at, d.eta), ("delivered", at(-5), None))
        self.assertIsNone(self.one(status="delivered").ended_at)
        self.assertEqual(self.one(status="delivered", delivered_at=at(-5)).title, "Test Shop")

    def test_a_window_needs_both_ends_in_order_and_one_time_is_an_eta(self):
        d = self.one(start=at(60), end=at(120))
        self.assertEqual((d.eta, d.window_start, d.window_end), (None, at(60), at(120)))
        d = self.one(start=at(60))
        self.assertEqual((d.eta, d.window_start, d.window_end), (at(60), None, None))
        for end in (at(60), at(30)):
            d = self.one(start=at(60), end=end)
            self.assertEqual((d.eta, d.window_start, d.window_end), (at(60), None, None))
        d = self.one(end=at(60))
        self.assertEqual((d.eta, d.window_start, d.window_end, d.stage), (None, None, None, "on_the_way"))

    def test_times_are_read_with_their_offset_and_naive_ones_are_ignored(self):
        z = self.one(planned_from="2026-11-10T17:00:00Z", planned_to="2026-11-10T19:00:00Z")
        self.assertEqual((z.window_start, z.window_end), (datetime(2026, 11, 10, 17, tzinfo=timezone.utc).timestamp(),
                                                         datetime(2026, 11, 10, 19, tzinfo=timezone.utc).timestamp()))
        local = self.one(planned_from="2026-11-10T18:30:00+01:00")
        self.assertEqual(local.eta, datetime(2026, 11, 10, 17, 30, tzinfo=timezone.utc).timestamp())
        for value in ("2026-11-10T18:30:00", "2026-11-10", "tomorrow", "", 12, [], {}):
            with self.subTest(value=value):
                d = self.one(status="in_transit", planned_from=value, planned_to=value)
                self.assertIsNone(d)
                d = self.one(planned_from=value, planned_to=value)
                self.assertEqual((d.stage, d.eta, d.window_start, d.window_end), ("on_the_way", None, None, None))

    def test_the_title_is_the_sender_else_the_carrier_with_stray_whitespace_gone(self):
        self.assertEqual(self.one(sender="  Test   Shop \n").title, "Test Shop")
        for sender in (None, "", "   ", 5, [], {}):
            with self.subTest(sender=sender):
                self.assertEqual(self.one(sender=sender).title, "PostNord")

    def test_the_source_is_the_carrier_name_as_a_slug(self):
        for carrier, source in (("PostNord", "postnord"), ("Airmee", "airmee"), ("GLS", "gls"), ("DHL (DE)", "dhl_de"),
                                ("Budbee", "budbee"), ("DAO", "dao"), ("DHL", "dhl"), ("UPS", "ups"),
                                ("  Danske   Fragtmænd ", "danske_fragtmaend")):
            with self.subTest(carrier=carrier):
                d = self.one(carrier=carrier, barcode=" TESTX1 ")
                self.assertEqual((d.source, d.id, d.carrier), (source, f"{source}:TESTX1", " ".join(carrier.split())))

    def test_the_danish_letters_of_a_carrier_are_spelled_out_in_its_slug_and_kept_in_its_name(self):
        for carrier, source in (("Danske Fragtmænd", "danske_fragtmaend"), ("DANSKE FRAGTMÆND", "danske_fragtmaend"),
                                ("Ærø Ålbæk Ørsted", "aeroe_aalbaek_oersted")):
            with self.subTest(carrier=carrier):
                d = self.one(carrier=carrier, barcode="TESTX1")
                self.assertEqual((d.source, d.id, d.carrier), (source, f"{source}:TESTX1", carrier))

    def test_posten_bring_is_named_bring_in_the_carrier_and_the_title_but_not_in_the_source(self):
        for carrier in ("Posten Bring", "  posten   BRING "):
            with self.subTest(carrier=carrier):
                d = self.one(carrier=carrier, barcode="TESTBR1", sender=None)
                self.assertEqual((d.source, d.id, d.carrier, d.title),
                                 ("posten_bring", "posten_bring:TESTBR1", "Bring", "Bring"))
        self.assertEqual(self.one(carrier="Posten Bring", sender="Test Shop").title, "Test Shop")

    def test_unusable_parcels_are_skipped(self):
        for bad in (None, 3, "x", [], {}, parcel(carrier=None), parcel(carrier=""), parcel(carrier="()"),
                    parcel(barcode=None), parcel(barcode=""), parcel(barcode=12345), parcel(carrier=["PostNord"])):
            with self.subTest(parcel=bad):
                self.assertIsNone(hd.parcel_delivery(bad, NOW))


class ParcelDeliveriesTests(unittest.TestCase):
    def test_both_lists_are_read_and_junk_is_skipped(self):
        incoming = [parcel(), None, parcel(barcode="TESTPN0002", status="returning"),
                    parcel(carrier="Airmee", barcode="TESTAM0001", status="in_transit", start=at(60), end=at(120))]
        delivered = [parcel(barcode="TESTPN0003", status="delivered", delivered_at=at(-3))]
        found = {d.id: d.stage for d in hd.parcel_deliveries([incoming, delivered], NOW)}
        self.assertEqual(found, {"postnord:TESTPN0001": "on_the_way", "airmee:TESTAM0001": "preparing",
                                 "postnord:TESTPN0003": "delivered"})

    def test_a_parcel_in_two_lists_is_delivered_if_either_says_so(self):
        out, done = parcel(), parcel(status="delivered", delivered_at=at(-1))
        for lists in ([[out], [done]], [[done], [out]]):
            with self.subTest(lists=lists):
                [d] = hd.parcel_deliveries(lists, NOW)
                self.assertEqual(d.stage, "delivered")
        [d] = hd.parcel_deliveries([[parcel(sender="First")], [parcel(sender="Second")]], NOW)
        self.assertEqual(d.title, "First")

    def test_the_same_barcode_at_two_carriers_is_two_parcels(self):
        found = hd.parcel_deliveries([[parcel(), parcel(carrier="Airmee")]], NOW)
        self.assertEqual({d.id for d in found}, {"postnord:TESTPN0001", "airmee:TESTPN0001"})

    def test_no_lists_no_parcels(self):
        self.assertEqual(hd.parcel_deliveries([[], []], NOW), [])
        self.assertEqual(hd.parcel_deliveries([], NOW), [])


class AmazonCopiesTests(unittest.TestCase):
    @staticmethod
    def amazon(item, order="028-0000001-0000001", status="in_transit", tracking=None, **times):
        """As the aggregator passes it on: no raw, the barcode Amazon's tracking id, else order id plus item."""
        return parcel(carrier="Amazon", barcode=tracking or f"{order}-t{item}", status=status, sender=None, **times)

    def barcodes(self, lists):
        return [[p["barcode"] for p in parcels] for parcels in hd.without_amazon_copies(lists)]

    def test_the_items_of_one_order_are_one_parcel(self):
        items = [self.amazon("1", start=at(60)), self.amazon("2", start=at(60)),
                 self.amazon("3", order="028-0000002-0000002", start=at(60))]
        today = hd.local_day(NOW).isoformat()
        self.assertEqual(self.barcodes([items, []]),
                         [[f"028-0000001-0000001-{today}", f"028-0000002-0000002-{today}"], []])

    def test_an_order_sent_on_two_days_is_two_parcels(self):
        items = [self.amazon("1", start=at(60)), self.amazon("2", start=at(60 * 24 * 2)), self.amazon("3")]
        self.assertEqual(len(hd.without_amazon_copies([items])[0]), 3)

    def test_an_order_is_delivered_only_when_every_item_is(self):
        items = [self.amazon("1", status="delivered", delivered_at=at(-5)), self.amazon("2", start=at(60))]
        [[order]] = hd.without_amazon_copies([items])
        self.assertEqual(order["status"], "in_transit")

    def test_a_carrier_parcel_from_amazon_on_the_same_day_hides_amazons_copy(self):
        airmee = parcel(carrier="Airmee", barcode="c443dc28", sender="Amazon", start=at(30), end=at(240))
        mine = self.amazon("1", start=at(60))
        self.assertEqual(self.barcodes([[airmee, mine]]), [["c443dc28"]])
        found = hd.parcel_deliveries(hd.without_amazon_copies([[airmee, mine], []]), NOW)
        self.assertEqual([d.id for d in found], ["airmee:c443dc28"])

    def test_the_carrier_copy_hides_amazon_across_lists(self):
        done = parcel(carrier="Airmee", barcode="c443dc28", sender="Amazon", status="delivered", delivered_at=at(-5))
        mine = self.amazon("1", status="delivered", delivered_at=at(-20))
        self.assertEqual(self.barcodes([[], [done, mine]]), [[], ["c443dc28"]])

    def test_amazons_tracking_id_at_a_carrier_hides_it_whatever_the_sender(self):
        postnord = parcel(barcode="LA508420838NL", sender="Netherlands (the)")
        mine = self.amazon("1", tracking="LA508420838NL", start=at(60 * 24 * 3))
        self.assertEqual(self.barcodes([[postnord, mine]]), [["LA508420838NL"]])

    def test_amazon_stays_when_no_carrier_has_it(self):
        other_day = parcel(carrier="Airmee", barcode="c443dc28", sender="Amazon", start=at(60 * 24), end=at(60 * 25))
        other_shop = parcel(sender="Test Shop", start=at(60))
        mine = self.amazon("1", start=at(60))
        order = f"028-0000001-0000001-{hd.local_day(NOW).isoformat()}"
        self.assertEqual(self.barcodes([[other_day, other_shop, mine]]), [["c443dc28", "TESTPN0001", order]])
        [d] = [d for d in hd.parcel_deliveries(hd.without_amazon_copies([[mine]]), NOW)]
        self.assertEqual((d.id, d.title), (f"amazon:{order}", "Amazon"))

    def test_a_tracking_id_without_an_order_id_is_its_own_parcel(self):
        mine = self.amazon("1", tracking="1Z999AA10123456784", start=at(60))
        self.assertEqual(self.barcodes([[mine]]), [["1Z999AA10123456784"]])

    def test_other_carriers_and_junk_pass_untouched(self):
        lists = [[parcel(), None, parcel(carrier="Airmee", barcode="X1", sender="Amazon")], []]
        self.assertEqual(hd.without_amazon_copies(lists), lists)


class ParcelUpcomingTests(unittest.TestCase):
    def upcoming(self, *parcels, listed=()):
        return hd.upcoming_parcels(list(parcels), set(listed))

    def test_a_parcel_without_a_time_is_upcoming_with_its_own_status_word(self):
        [found] = self.upcoming(parcel(carrier="GLS", barcode="TESTGL0001", status="in_transit"))
        self.assertEqual(found, {"id": "gls:TESTGL0001", "source": "gls", "title": "Test Shop", "status": "in_transit",
                                 "planned_from": None, "planned_to": None})

    def test_planned_times_are_local_iso_each_kept_as_the_carrier_gave_it(self):
        [found] = self.upcoming(parcel(status="registered", start=local_time(12, 14), end=local_time(12, 18)))
        self.assertEqual((found["planned_from"], found["planned_to"]), ("2026-11-12T14:00:00+01:00", "2026-11-12T18:00:00+01:00"))
        [found] = self.upcoming(parcel(status="registered", start=local_time(12, 14)))
        self.assertEqual((found["planned_from"], found["planned_to"]), ("2026-11-12T14:00:00+01:00", None))
        [found] = self.upcoming(parcel(status="registered", planned_from="2026-11-12T14:00:00", planned_to="soon"))
        self.assertEqual((found["planned_from"], found["planned_to"]), (None, None))

    def test_the_title_is_the_sender_else_the_carrier(self):
        found = self.upcoming(parcel(status="in_transit", sender="  Test   Shop "),
                              parcel(barcode="TESTPN0002", status="in_transit", sender=None))
        self.assertEqual([item["title"] for item in found], ["Test Shop", "PostNord"])

    def test_a_parcel_without_a_sender_is_titled_by_its_carriers_brand_and_sourced_by_its_slug(self):
        found = self.upcoming(parcel(carrier="Posten Bring", barcode="TESTBR1", status="in_transit", sender=None),
                              parcel(carrier="Danske Fragtmænd", barcode="TESTDF1", status="in_transit", sender=None))
        self.assertEqual([(item["id"], item["source"], item["title"]) for item in found],
                         [("posten_bring:TESTBR1", "posten_bring", "Bring"),
                          ("danske_fragtmaend:TESTDF1", "danske_fragtmaend", "Danske Fragtmænd")])

    def test_what_the_deliveries_hold_and_what_is_delivered_are_left_out(self):
        found = self.upcoming(parcel(barcode="TESTPN0001", status="out_for_delivery"),
                              parcel(barcode="TESTPN0002", status="in_transit"),
                              parcel(barcode="TESTPN0003", status="delivered"),
                              parcel(barcode="TESTPN0004", status="registered"),
                              listed=["postnord:TESTPN0001"])
        self.assertEqual([item["id"] for item in found], ["postnord:TESTPN0002", "postnord:TESTPN0004"])

    def test_every_other_status_is_listed_as_the_aggregator_says_it(self):
        statuses = ("registered", "in_transit", "out_for_delivery", "at_pickup_point", "returning", "problem", "unknown")
        found = self.upcoming(*[parcel(barcode=f"TESTPN{number}", status=status) for number, status in enumerate(statuses)])
        self.assertEqual([item["status"] for item in found], list(statuses))

    def test_a_status_the_aggregator_does_not_give_reads_unknown(self):
        for status in (None, "", 7, ["in_transit"]):
            with self.subTest(status=status):
                [found] = self.upcoming(parcel(status=status))
                self.assertEqual(found["status"], "unknown")

    def test_soonest_planned_first_then_as_given(self):
        found = self.upcoming(parcel(barcode="TESTPN0001", status="in_transit"),
                              parcel(barcode="TESTPN0002", status="in_transit", start=local_time(13, 10)),
                              parcel(barcode="TESTPN0003", status="in_transit"),
                              parcel(barcode="TESTPN0004", status="in_transit", start=local_time(12, 10), end=local_time(12, 12)),
                              parcel(barcode="TESTPN0005", status="in_transit", start=local_time(13, 10)),
                              parcel(barcode="TESTPN0006", status="in_transit", planned_to=stamp(local_time(12, 20))))
        self.assertEqual([item["id"] for item in found],
                         [f"postnord:TESTPN000{number}" for number in (4, 6, 2, 5, 1, 3)])

    def test_the_same_parcel_twice_is_listed_once_and_unusable_ones_are_skipped(self):
        found = self.upcoming(parcel(status="in_transit"), parcel(status="in_transit", sender="Other"), None, 3, "x", {},
                              parcel(carrier=None), parcel(barcode=""))
        self.assertEqual([(item["id"], item["title"]) for item in found], [("postnord:TESTPN0001", "Test Shop")])

    def test_nothing_incoming_nothing_upcoming(self):
        self.assertEqual(self.upcoming(), [])
        self.assertEqual(hd.upcoming_parcels([], set()), [])


class ParcelPublishedTests(unittest.TestCase):
    def test_a_parcel_entry_carries_the_contract(self):
        d = hd.parcel_delivery(parcel(start=at(60), end=at(120)), NOW)
        entry = hd.snapshot([d], NOW)["attributes"]["deliveries"][0]
        self.assertEqual(entry, {"id": "postnord:TESTPN0001", "source": "postnord", "kind": "parcel", "title": "Test Shop",
                                 "stage": "on_the_way", "eta": None, "window_start": "2026-11-10T18:48:00+01:00",
                                 "window_end": "2026-11-10T19:48:00+01:00", "minutes_left": None, "item_count": None})
        d = hd.parcel_delivery(parcel(start=at(25)), NOW)
        entry = hd.snapshot([d], NOW)["attributes"]["deliveries"][0]
        self.assertEqual((entry["eta"], entry["minutes_left"], entry["window_start"]), ("2026-11-10T18:13:00+01:00", 25, None))

    def test_upcoming_is_published_beside_the_deliveries_and_changes_nothing_else(self):
        out = hd.parcel_delivery(parcel(start=at(10)), NOW)
        incoming = [parcel(start=at(10)), parcel(carrier="GLS", barcode="TESTGL0001", status="in_transit")]
        with_incoming = hd.snapshot([out], NOW, incoming)
        without = hd.snapshot([out], NOW)
        self.assertEqual([item["id"] for item in with_incoming["attributes"]["upcoming"]], ["gls:TESTGL0001"])
        self.assertEqual(without["attributes"]["upcoming"], [])

        def rest(published):
            return {key: value for key, value in published["attributes"].items() if key != "upcoming"}

        self.assertEqual((with_incoming["state"], rest(with_incoming)), (without["state"], rest(without)))
        self.assertEqual(json.loads(json.dumps(with_incoming["attributes"])), with_incoming["attributes"])

    def test_the_carrier_is_not_a_published_key(self):
        entry = hd.snapshot([hd.parcel_delivery(parcel(), NOW)], NOW)["attributes"]["deliveries"][0]
        self.assertNotIn("carrier", entry)

    def test_parcels_count_when_active_and_sort_with_the_other_sources(self):
        wolt_order = delivery(title=KITCHEN, stage="on_the_way", eta=at(20))
        out = hd.parcel_delivery(parcel(start=at(10)), NOW)
        later = hd.parcel_delivery(parcel(barcode="TESTPN0002", status="in_transit", start=at(90), end=at(150)), NOW)
        done = hd.parcel_delivery(parcel(barcode="TESTPN0003", status="delivered", delivered_at=at(-2)), NOW)
        published = hd.snapshot([done, later, wolt_order, out], NOW)
        self.assertEqual(published["state"], 3)
        self.assertEqual([e["id"] for e in published["attributes"]["deliveries"]],
                         ["postnord:TESTPN0001", "wolt:x", "postnord:TESTPN0002", "postnord:TESTPN0003"])
        self.assertEqual(published["attributes"]["next"]["id"], "postnord:TESTPN0001")

    def test_courier_expected_while_a_parcel_is_out_for_delivery_even_without_a_time(self):
        d = hd.parcel_delivery(parcel(), NOW)
        self.assertEqual(hd.courier_expected([d], NOW), (True, "on the way"))
        self.assertEqual(hd.snapshot([d], NOW)["attributes"]["reason"],
                         "1 active, next Test Shop on_the_way; courier expected: on the way")

    def test_a_parcel_window_counts_once_it_opens_until_it_closes(self):
        d = hd.parcel_delivery(parcel(status="in_transit", start=at(60), end=at(180)), NOW)
        self.assertEqual(d.stage, "preparing")
        for minutes, expected in ((-1, False), (59, False), (60, True), (120, True), (180, True), (181, False)):
            with self.subTest(minutes=minutes):
                self.assertEqual(hd.courier_expected([d], at(minutes)), (True, "inside its window") if expected else (False, None))

    def test_a_parcel_eta_counts_from_twenty_minutes_before_to_thirty_after(self):
        d = hd.parcel_delivery(parcel(status="registered", start=at(120)), NOW)
        for offset, expected in ((-1201, False), (-1200, True), (0, True), (1800, True), (1801, False)):
            with self.subTest(offset=offset):
                self.assertEqual(hd.courier_expected([d], at(120) + offset)[0], expected)
        self.assertEqual(hd.courier_expected([d], at(110)), (True, "close to its ETA"))

    def test_a_parcel_later_today_does_not_expect_the_courier_yet(self):
        d = hd.parcel_delivery(parcel(status="in_transit", start=at(240), end=at(300)), NOW)
        self.assertEqual(hd.snapshot([d], NOW)["attributes"]["courier_expected"], False)

    def test_a_delivered_parcel_expects_no_courier(self):
        d = hd.parcel_delivery(parcel(status="delivered", delivered_at=at(-1)), NOW)
        self.assertEqual(hd.snapshot([d], NOW)["attributes"]["courier_expected"], False)


class ParcelSpokenTests(unittest.TestCase):
    def say(self, *deliveries, now=NOW):
        return hd.snapshot(list(deliveries), now)["attributes"]["spoken"]

    def parcel(self, now=NOW, **kw):
        return hd.parcel_delivery(parcel(**kw), now)

    def test_out_for_delivery_with_a_window(self):
        self.assertEqual(self.say(self.parcel(start=local_time(10, 18), end=local_time(10, 20))),
                         "A PostNord parcel is out for delivery, expected between 6 and 8 PM.")
        self.assertEqual(self.say(self.parcel(start=local_time(10, 11, 30), end=local_time(10, 13)), now=local_time(10, 11)),
                         "A PostNord parcel is out for delivery, expected between 11:30 AM and 1 PM.")
        self.assertEqual(self.say(self.parcel(start=local_time(10, 14), end=local_time(10, 16)), now=local_time(10, 14, 30)),
                         "A PostNord parcel is out for delivery, expected between 2 and 4 PM.")

    def test_out_for_delivery_with_an_eta_or_nothing(self):
        self.assertEqual(self.say(self.parcel(start=local_time(10, 18, 30))),
                         "A PostNord parcel is out for delivery, expected around 6:30 PM.")
        self.assertEqual(self.say(self.parcel(start=local_time(10, 19))),
                         "A PostNord parcel is out for delivery, expected around 7 PM.")
        self.assertEqual(self.say(self.parcel()), "A PostNord parcel is out for delivery.")

    def test_a_late_courier_should_be_there_any_moment(self):
        self.assertEqual(self.say(self.parcel(start=at(-6))), "A PostNord parcel should be there any moment now.")
        self.assertEqual(self.say(self.parcel(start=NOW)), "A PostNord parcel should be there any moment now.")
        self.assertEqual(self.say(self.parcel(start=at(-60), end=NOW)), "A PostNord parcel should be there any moment now.")
        self.assertEqual(self.say(self.parcel(start=at(0.5))), "A PostNord parcel is out for delivery, expected around 5:48 PM.")
        self.assertEqual(self.say(self.parcel(start=at(-60), end=at(-6))), "A PostNord parcel should be there any moment now.")
        self.assertEqual(self.say(self.parcel(start=at(-60), end=at(30))), "A PostNord parcel is out for delivery, expected between 4:48 and 6:18 PM.")

    def test_preparing(self):
        self.assertEqual(self.say(self.parcel(status="in_transit", start=local_time(10, 18), end=local_time(10, 20))),
                         "A PostNord parcel is expected between 6 and 8 PM.")
        self.assertEqual(self.say(self.parcel(status="registered", start=local_time(10, 18, 30))),
                         "A PostNord parcel is expected around 6:30 PM.")
        d = delivery(source="postnord", kind="parcel", carrier="PostNord", stage="preparing")
        self.assertEqual(self.say(d), "A PostNord parcel is expected today.")

    def test_the_article_follows_the_carrier_name(self):
        self.assertEqual(self.say(self.parcel(carrier="Airmee")), "An Airmee parcel is out for delivery.")
        self.assertEqual(self.say(self.parcel(carrier="GLS")), "A GLS parcel is out for delivery.")
        self.assertEqual(self.say(self.parcel(carrier="DHL (DE)")), "A DHL (DE) parcel is out for delivery.")
        self.assertEqual(self.say(self.parcel(carrier="InPost")), "An InPost parcel is out for delivery.")
        self.assertEqual(self.say(self.parcel(carrier="Budbee")), "A Budbee parcel is out for delivery.")
        self.assertEqual(self.say(self.parcel(carrier="DAO")), "A DAO parcel is out for delivery.")
        self.assertEqual(self.say(self.parcel(carrier="UPS")), "A UPS parcel is out for delivery.")
        self.assertEqual(self.say(self.parcel(carrier="Danske Fragtmænd")),
                         "A Danske Fragtmænd parcel is out for delivery.")

    def test_posten_bring_is_spoken_as_bring(self):
        self.assertEqual(self.say(self.parcel(carrier="Posten Bring")), "A Bring parcel is out for delivery.")
        self.assertEqual(self.say(self.parcel(carrier="Posten Bring", status="in_transit", start=local_time(10, 18, 30))),
                         "A Bring parcel is expected around 6:30 PM.")

    def test_more_deliveries_are_counted_in_the_same_sentence_after_a_parcel(self):
        first = self.parcel(start=local_time(10, 18, 30))
        second = self.parcel(barcode="TESTPN0002", start=local_time(10, 19))
        third = self.parcel(barcode="TESTPN0003", carrier="Airmee", start=local_time(10, 20))
        self.assertEqual(self.say(first, second), "A PostNord parcel is out for delivery, expected around 6:30 PM, and 1 more.")
        self.assertEqual(self.say(first, second, third), "A PostNord parcel is out for delivery, expected around 6:30 PM, and 2 more.")
        self.assertEqual(self.say(first, delivery(id="nemlig", source="nemlig", kind="groceries", title="Nemlig", eta=at(300))),
                         "A PostNord parcel is out for delivery, expected around 6:30 PM, and 1 more.")

    def test_a_parcel_after_another_source_keeps_the_existing_phrasing(self):
        wolt_order = delivery(eta=at(8))
        out = self.parcel(start=at(30))
        self.assertEqual(self.say(wolt_order, out), "In about 8 minutes, around 5:56 PM. There's one more after that.")

    def test_a_delivered_parcel_is_not_on_the_way(self):
        self.assertEqual(self.say(self.parcel(status="delivered", delivered_at=at(-2))), "There's no delivery on the way.")

    def test_food_is_answered_before_a_delivery_that_is_due_sooner(self):
        out = self.parcel(start=at(10))
        self.assertEqual(self.say(out, delivery(eta=at(30))),
                         "In about 30 minutes, around 6:18 PM. There's one more after that.")
        self.assertEqual(self.say(out, delivery(eta=at(30)), self.parcel(barcode="TESTPN0002", start=at(20))),
                         "In about 30 minutes, around 6:18 PM. There are 2 more after that.")

    def test_without_food_the_next_delivery_is_said_generically(self):
        self.assertEqual(self.say(self.parcel(start=at(10))), "A PostNord parcel is out for delivery, expected around 5:58 PM.")
        self.assertEqual(self.say(delivery(stage="delivered"), self.parcel(start=at(10))),
                         "A PostNord parcel is out for delivery, expected around 5:58 PM.")

    def test_a_preparing_food_order_is_still_the_answer_to_where_is_the_food(self):
        self.assertEqual(self.say(self.parcel(start=at(10)), delivery(stage="preparing", eta=at(40))),
                         f"{KITCHEN} is still making it. Wolt expects it around 6:28 PM. There's one more after that.")

    def test_window_text_can_be_linked_with_and(self):
        self.assertEqual(hd.window_text(local_time(10, 14), local_time(10, 16)), "2 to 4 PM")
        self.assertEqual(hd.window_text(local_time(10, 14), local_time(10, 16), "and"), "2 and 4 PM")
        self.assertEqual(hd.window_text(local_time(10, 11), local_time(10, 13), "and"), "11 AM and 1 PM")
        self.assertIsNone(hd.window_text(None, local_time(10, 16), "and"))


def nemlig_delivery(**kw):
    return delivery(**{"id": "nemlig", "source": "nemlig", "kind": "groceries", "title": "Nemlig", **kw})


class FoodCourierTests(unittest.TestCase):
    def attrs(self, *deliveries, now=NOW):
        return hd.snapshot(list(deliveries), now)["attributes"]

    def test_the_food_flag_only_looks_at_food(self):
        for other in (hd.parcel_delivery(parcel(), NOW), nemlig_delivery()):
            with self.subTest(source=other.source):
                attrs = self.attrs(other)
                self.assertEqual((attrs["courier_expected"], attrs["food_courier_expected"]), (True, False))
        attrs = self.attrs(delivery(), hd.parcel_delivery(parcel(), NOW))
        self.assertEqual((attrs["courier_expected"], attrs["food_courier_expected"]), (True, True))

    def test_the_food_flag_follows_the_same_window_as_the_general_one(self):
        d = delivery(stage="preparing", eta=NOW)
        for offset, expected in ((-1201, False), (-1200, True), (0, True), (1800, True), (1801, False)):
            with self.subTest(offset=offset):
                self.assertEqual(self.attrs(d, now=NOW + offset)["food_courier_expected"], expected)
        self.assertEqual(self.attrs(delivery(stage="preparing"))["food_courier_expected"], False)

    def test_an_order_that_ended_expects_no_courier(self):
        for stage in ("delivered", "cancelled"):
            with self.subTest(stage=stage):
                self.assertEqual(self.attrs(delivery(stage=stage, eta=NOW))["food_courier_expected"], False)

    def test_nothing_on_the_way_has_no_food(self):
        attrs = self.attrs()
        self.assertEqual((attrs["next_food"], attrs["food_courier_expected"], attrs["door_courier_kind"]), (None, False, None))

    def test_next_food_is_the_first_active_food_entry_whatever_arrives_sooner(self):
        sooner = hd.parcel_delivery(parcel(start=at(5)), NOW)
        late = delivery(id="wolt:late", title="Late", stage="preparing", eta=at(40))
        first = delivery(id="wolt:first", title="First", eta=at(15))
        done = delivery(id="wolt:done", title="Done", stage="delivered", eta=at(-10))
        attrs = self.attrs(done, late, sooner, first)
        self.assertEqual(attrs["next"]["id"], "postnord:TESTPN0001")
        self.assertEqual(attrs["next_food"], next(e for e in attrs["deliveries"] if e["id"] == "wolt:first"))
        self.assertEqual(attrs["next_food"]["title"], "First")

    def test_next_food_is_empty_without_an_active_food_order(self):
        self.assertIsNone(self.attrs(hd.parcel_delivery(parcel(), NOW), nemlig_delivery(),
                                     delivery(stage="delivered"))["next_food"])


class DoorCourierKindTests(unittest.TestCase):
    def kind(self, *deliveries, now=NOW):
        return hd.snapshot(list(deliveries), now)["attributes"]["door_courier_kind"]

    def test_food_counts_from_the_moment_its_courier_is_expected(self):
        self.assertEqual(self.kind(delivery()), "food")
        self.assertEqual(self.kind(delivery(stage="preparing", eta=at(15))), "food")

    def test_groceries_and_parcels_must_be_out_for_delivery(self):
        window = {"window_start": at(-10), "window_end": at(50)}
        booked = nemlig_delivery(stage="preparing", **window)
        registered = hd.parcel_delivery(parcel(status="in_transit", start=at(-10), end=at(50)), NOW)
        self.assertEqual(hd.courier_expected([booked], NOW), (True, "inside its window"))
        self.assertEqual(hd.courier_expected([registered], NOW), (True, "inside its window"))
        self.assertIsNone(self.kind(booked))
        self.assertIsNone(self.kind(registered))
        for stage in ("on_the_way", "arriving"):
            with self.subTest(stage=stage):
                self.assertEqual(self.kind(nemlig_delivery(stage=stage)), "groceries")
                self.assertEqual(self.kind(delivery(id="p", source="postnord", kind="parcel", stage=stage)), "parcel")

    def test_food_wins_over_groceries_and_groceries_over_parcels(self):
        parcel_out = hd.parcel_delivery(parcel(), NOW)
        self.assertEqual(self.kind(parcel_out, nemlig_delivery(), delivery()), "food")
        self.assertEqual(self.kind(parcel_out, nemlig_delivery()), "groceries")
        self.assertEqual(self.kind(parcel_out), "parcel")

    def test_door_courier_is_the_entry_of_that_delivery(self):
        parcel_out = hd.parcel_delivery(parcel(), NOW)
        attrs = hd.snapshot([parcel_out, nemlig_delivery()], NOW)["attributes"]
        self.assertEqual((attrs["door_courier"]["source"], attrs["door_courier"]["kind"]), ("nemlig", "groceries"))
        attrs = hd.snapshot([parcel_out], NOW)["attributes"]
        self.assertEqual(attrs["door_courier"]["id"], parcel_out.id)
        self.assertIsNone(hd.snapshot([], NOW)["attributes"]["door_courier"])

    def test_food_still_in_the_kitchen_lets_the_parcel_answer_the_door(self):
        self.assertEqual(self.kind(delivery(stage="preparing"), hd.parcel_delivery(parcel(), NOW)), "parcel")

    def test_ended_deliveries_expect_nothing(self):
        done = hd.parcel_delivery(parcel(status="delivered", delivered_at=at(-1)), NOW)
        self.assertIsNone(self.kind(done, nemlig_delivery(stage="delivered"), delivery(stage="cancelled")))

    def test_the_published_attributes_stay_plain_json(self):
        attrs = hd.snapshot([delivery(eta=at(8)), nemlig_delivery()], NOW)["attributes"]
        self.assertEqual(json.loads(json.dumps(attrs)), attrs)


def carrier_parcel(carrier="Airmee", barcode="TESTAM0001", **kw):
    return hd.parcel_delivery(parcel(carrier=carrier, barcode=barcode, **kw), NOW)


class StreetRingTests(unittest.TestCase):
    def test_each_open_street_door_visit_gives_its_ring_time(self):
        self.assertEqual(hd.street_rings(intercom(("front door", at(0)), ("back door", at(1)))), [at(0), at(1)])

    def test_other_doors_and_damaged_visits_give_nothing(self):
        visits = [{"door": "apartment door", "ring_at": stamp(NOW)}, {"door": "", "ring_at": stamp(NOW)},
                  {"ring_at": stamp(NOW)}, {"door": "front door"}, {"door": "front door", "ring_at": "soon"},
                  {"door": "front door", "ring_at": "2026-11-10T16:48:00"}, "front door", None, 5]
        self.assertEqual(hd.street_rings({"state": "active", "attributes": {"visits": visits}}), [])
        for value in (None, {}, "x", intercom(), {"attributes": {"visits": "x"}}, {"attributes": ["x"]}):
            with self.subTest(value=value):
                self.assertEqual(hd.street_rings(value), [])


class RefreshDelaysTests(unittest.TestCase):
    def test_both_moments_are_counted_from_the_ring(self):
        self.assertEqual(hd.refresh_delays(NOW, NOW), [180, 360])
        self.assertEqual(hd.refresh_delays(NOW, NOW + 60), [120, 300])
        self.assertEqual(hd.refresh_delays(NOW + 2, NOW), [182, 362])

    def test_delays_are_whole_seconds_never_early(self):
        self.assertEqual(hd.refresh_delays(NOW - 0.5, NOW), [180, 360])
        self.assertEqual(hd.refresh_delays(NOW - 0.1, NOW), [180, 360])
        self.assertEqual(hd.refresh_delays(NOW + 0.5, NOW), [181, 361])
        self.assertTrue(all(isinstance(delay, int) for delay in hd.refresh_delays(NOW - 0.5, NOW)))

    def test_a_moment_that_has_come_is_not_made_up(self):
        self.assertEqual(hd.refresh_delays(NOW, NOW + 179), [1, 181])
        self.assertEqual(hd.refresh_delays(NOW, NOW + 180), [180])
        self.assertEqual(hd.refresh_delays(NOW, NOW + 359), [1])
        self.assertEqual(hd.refresh_delays(NOW, NOW + 360), [])


class RefreshSourceTests(unittest.TestCase):
    def source(self, *deliveries, buttons=BUTTONS):
        return hd.refresh_source(list(deliveries), NOW, buttons)

    def test_a_parcel_out_for_delivery_names_its_carrier(self):
        self.assertEqual(self.source(carrier_parcel()), "airmee")
        self.assertEqual(self.source(carrier_parcel("PostNord", "TESTPN0001", start=at(5))), "postnord")

    def test_a_parcel_that_is_not_out_yet_or_already_delivered_names_nobody(self):
        self.assertIsNone(self.source(carrier_parcel(status="in_transit", start=at(30))))
        self.assertIsNone(self.source(carrier_parcel(status="delivered", delivered_at=at(-1))))
        self.assertIsNone(self.source())

    def test_a_carrier_without_a_button_names_nobody(self):
        self.assertIsNone(self.source(carrier_parcel("GLS", "TESTGL0001")))
        self.assertIsNone(self.source(carrier_parcel(), buttons={}))

    def test_only_a_parcel_courier_calls_for_a_refresh_whatever_the_buttons_say(self):
        buttons = {**BUTTONS, "wolt": "button.wolt_test_refresh", "nemlig": "button.nemlig_test_refresh"}
        self.assertIsNone(self.source(delivery(), buttons=buttons))
        self.assertIsNone(self.source(nemlig_delivery(), buttons=buttons))

    def test_a_ring_the_food_or_groceries_courier_more_likely_made_names_nobody(self):
        parcel_out = carrier_parcel()
        self.assertIsNone(self.source(parcel_out, delivery()))
        self.assertIsNone(self.source(parcel_out, nemlig_delivery()))
        self.assertEqual(self.source(parcel_out, delivery(stage="preparing")), "airmee")

    def test_it_is_the_carrier_the_published_door_courier_names_whatever_order_they_come_in(self):
        soon = carrier_parcel("PostNord", "TESTPN0001", start=at(5))
        later = carrier_parcel("Airmee", "TESTAM0001", start=at(40))
        for deliveries in ([later, soon], [soon, later]):
            with self.subTest(order=[d.source for d in deliveries]):
                self.assertEqual(hd.snapshot(deliveries, NOW)["attributes"]["door_courier"]["source"], "postnord")
                self.assertEqual(self.source(*deliveries), "postnord")


class ParcelsUndeliveredTests(unittest.TestCase):
    def test_a_parcel_that_is_not_delivered_keeps_the_carrier_wanting_a_refresh(self):
        for stage in ("preparing", "on_the_way", "arriving"):
            with self.subTest(stage=stage):
                parcel_there = delivery(id="airmee:A", source="airmee", kind="parcel", stage=stage)
                self.assertTrue(hd.parcels_undelivered([parcel_there], "airmee"))

    def test_delivered_gone_and_other_carriers_parcels_do_not(self):
        done = carrier_parcel(status="delivered", delivered_at=at(-1))
        elsewhere = carrier_parcel("PostNord", "TESTPN0001")
        self.assertFalse(hd.parcels_undelivered([done, elsewhere, delivery()], "airmee"))
        self.assertFalse(hd.parcels_undelivered([], "airmee"))
        self.assertTrue(hd.parcels_undelivered([done, elsewhere, carrier_parcel(barcode="TESTAM0002")], "airmee"))


class Harness:
    def __init__(self, states=None, now=NOW, **args):
        self.now = now
        self.states = copy.deepcopy(states or {})
        self.published = []
        self.listened = []
        self.logs = []
        self.queue = []
        self.timers = []
        self.calls = []
        self.apps = {}
        self.handlers = {}
        self.unreadable = None
        self.app = hd.HomeDelivery.__new__(hd.HomeDelivery)
        self.app.args = args
        self.app.log = lambda message, level="INFO": self.logs.append((level, message))
        self.app.get_now = lambda: datetime.fromtimestamp(self.now, timezone.utc)
        self.app.render_template = self.render_template
        self.app.get_state = self.get_state
        self.app.set_state = lambda entity, state=None, attributes=None, replace=False: self.published.append(
            (entity, state, copy.deepcopy(attributes), replace))
        self.app.listen_state = self.listen_state
        self.app.run_every = lambda *a, **kw: "timer"
        self.app.submit_to_executor = lambda func, *a, callback=None, **kw: self.queue.append((func, a, kw, callback))
        self.app.get_app = lambda name: self.apps.get(name)
        self.app.create_task = asyncio.run
        self.app.run_in = lambda callback, delay, **kw: self.timers.append((self.now + delay, callback, kw))
        self.app.call_service = lambda service, **data: self.calls.append((service, data))
        self.app.initialize()

    def listen_state(self, callback, entity, **kw):
        self.listened.append((entity, kw))
        self.handlers.setdefault(entity, []).append(callback)
        return "handle"

    def render_template(self, template):
        if template != hd.WOLT_SENSORS:
            raise AssertionError(template)
        if self.unreadable == "raises":
            raise RuntimeError("Home Assistant did not answer")
        if self.unreadable == "nothing":
            return None
        items = [{"entity_id": key, **copy.deepcopy(value)} for key, value in self.states.items()
                 if key.startswith("sensor.wolt_")]
        return appdaemon_render_result("J" + json.dumps(items))

    def get_state(self, entity_id, attribute=None):
        value = copy.deepcopy(self.states.get(entity_id))
        if attribute == "all":
            return value
        if attribute is None:
            return value.get("state") if isinstance(value, dict) else None
        raise AssertionError(attribute)

    @property
    def last(self):
        entity, state, attributes, replace = self.published[-1]
        return state, attributes

    def tick(self, seconds=0):
        self.now += seconds
        self.app._on_tick({})

    def run_executor(self):
        queue, self.queue = self.queue, []
        for func, args, kw, callback in queue:
            result = func(*args, **kw)
            if callback:
                callback(result=result)

    def change(self, entity, new):
        """Set an entity's state and call the listeners registered for it, as AppDaemon would."""
        old = (self.states.get(entity) or {}).get("state")
        self.states[entity] = {"state": new, "attributes": {}}
        for callback in self.handlers.get(entity, ()):
            callback(entity, "state", old, new, {})

    def ring(self, *rings):
        self.states[INTERCOM_VISIT] = intercom(*rings)
        self.app._on_visit(INTERCOM_VISIT, "all", {}, copy.deepcopy(self.states[INTERCOM_VISIT]), {})

    def advance(self, seconds):
        """Move the clock on, running each timer that falls due on the way."""
        end = self.now + seconds
        for due, callback, kw in sorted(self.timers, key=lambda timer: timer[0]):
            if due <= end:
                self.now = due
                callback(kw)
        self.timers = [timer for timer in self.timers if timer[0] > end]
        self.now = end


class AppTests(unittest.TestCase):
    def test_init_publishes_a_complete_replacing_idle_entity_and_ignores_old_orders(self):
        old = wolt(status="delivered", kind="DELIVERED")
        h = Harness(old)
        entity, state, attrs, replace = h.published[-1]
        self.assertEqual((entity, state, replace), (SENSOR, "0", True))
        self.assertEqual((attrs["deliveries"], attrs["upcoming"]), ([], []))
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

        def wolt_listened():
            return [entity for entity, _ in h.listened if entity.startswith("sensor.wolt_")]

        self.assertEqual(wolt_listened(), ["sensor.wolt_delivery_test_kitchen"])
        self.assertEqual(h.listened[-1][1], {"attribute": "all"})
        h.states.update(wolt(venue="Pizza Test", slug="pizza_test"))
        h.tick(30)
        h.tick(30)
        self.assertEqual(wolt_listened(), ["sensor.wolt_delivery_test_kitchen", "sensor.wolt_delivery_pizza_test"])

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

    def test_unreadable_wolt_sensors_keep_the_last_read_and_warn_once(self):
        h = Harness(wolt(courier=True, eta=at(2000)))
        h.unreadable = "raises"
        for _ in range(30):
            h.tick(30)
        self.assertEqual(h.last[0], "1")
        warnings = [message for level, message in h.logs if level == "WARNING" and message.startswith("Wolt sensors unreadable")]
        self.assertEqual(warnings, ["Wolt sensors unreadable, keeping the last read: RuntimeError"])
        h.unreadable = None
        h.states.update(wolt(status="delivered", kind="DELIVERED"))
        h.tick(30)
        self.assertEqual(h.last[0], "0")
        self.assertEqual([message for _, message in h.logs if message == "Wolt sensors readable again"],
                         ["Wolt sensors readable again"])

    def test_a_template_that_answers_nothing_is_unreadable_too(self):
        h = Harness(wolt(courier=True, eta=at(2000)))
        h.unreadable = "nothing"
        for _ in range(30):
            h.tick(30)
        self.assertEqual(h.last[0], "1")
        self.assertIn(("WARNING", "Wolt sensors unreadable, keeping the last read: ValueError"), h.logs)

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


class ParcelAppTests(unittest.TestCase):
    def test_what_the_aggregator_holds_today_leaves_the_sensor_empty(self):
        week_ago = at(-7 * 24 * 60)
        h = Harness({
            AGGREGATOR_INCOMING: aggregator(parcel(barcode="TESTPN0001", status="registered"),
                                            parcel(barcode="TESTPN0002", status="returning", sender=None),
                                            parcel(barcode="TESTAM0001", carrier="Airmee", status="in_transit")),
            AGGREGATOR_DELIVERED: aggregator(parcel(barcode="TESTPN0003", status="delivered", delivered_at=week_ago),
                                             parcel(barcode="TESTAM0002", carrier="Airmee", status="delivered"),
                                             parcel(barcode="TESTAM0003", carrier="Airmee", status="delivered"))})
        h.tick(30)
        state, attrs = h.last
        self.assertEqual((state, attrs["deliveries"], attrs["courier_expected"]), ("0", [], False))
        self.assertEqual((attrs["spoken"], attrs["reason"]), ("There's no delivery on the way.", "no active delivery"))
        self.assertEqual([(item["source"], item["status"]) for item in attrs["upcoming"]],
                         [("postnord", "registered"), ("postnord", "returning"), ("airmee", "in_transit")])
        self.assertFalse(any("TEST" in message for _, message in h.logs))

    def test_a_parcel_from_booked_to_delivered(self):
        window = (local_time(10, 19), local_time(10, 21))
        h = Harness({AGGREGATOR_INCOMING: aggregator(parcel(status="in_transit", start=window[0], end=window[1]))})
        state, attrs = h.last
        self.assertEqual((state, attrs["next"]["stage"], attrs["next"]["source"], attrs["courier_expected"]), ("1", "preparing", "postnord", False))
        self.assertEqual(attrs["spoken"], "A PostNord parcel is expected between 7 and 9 PM.")

        h.states[AGGREGATOR_INCOMING] = aggregator(parcel(start=window[0], end=window[1]))
        h.tick(30 * 60)
        state, attrs = h.last
        self.assertEqual((state, attrs["next"]["stage"], attrs["courier_expected"]), ("1", "on_the_way", True))
        self.assertEqual(attrs["spoken"], "A PostNord parcel is out for delivery, expected between 7 and 9 PM.")

        h.tick(26 * 60)
        self.assertEqual(h.last[1]["next"]["stage"], "on_the_way")
        h.tick(2 * 60)
        self.assertEqual(h.last[1]["next"]["stage"], "arriving")

        h.states[AGGREGATOR_INCOMING] = aggregator()
        h.states[AGGREGATOR_DELIVERED] = aggregator(parcel(status="delivered", delivered_at=h.now))
        h.tick(60)
        state, attrs = h.last
        self.assertEqual((state, attrs["next"], attrs["courier_expected"], [e["stage"] for e in attrs["deliveries"]]),
                         ("0", None, False, ["delivered"]))
        h.tick(28 * 60)
        self.assertEqual([e["stage"] for e in h.last[1]["deliveries"]], ["delivered"])
        h.tick(3 * 60)
        self.assertEqual(h.last[1]["deliveries"], [])
        self.assertEqual([message for _, message in h.logs if message.startswith("postnord:")],
                         ["postnord:TESTPN0001: new -> preparing", "postnord:TESTPN0001: preparing -> on_the_way",
                          "postnord:TESTPN0001: on_the_way -> arriving", "postnord:TESTPN0001: arriving -> delivered"])

    def test_a_carrier_that_gives_no_delivery_time_is_dated_by_when_it_was_first_seen_delivered(self):
        h = Harness({AGGREGATOR_INCOMING: aggregator(parcel(carrier="Airmee", barcode="TESTAM0001"))})
        self.assertEqual(h.last[1]["spoken"], "An Airmee parcel is out for delivery.")
        h.states[AGGREGATOR_INCOMING] = aggregator()
        h.states[AGGREGATOR_DELIVERED] = aggregator(parcel(carrier="Airmee", barcode="TESTAM0001", status="delivered"))
        h.tick(120)
        self.assertEqual([e["stage"] for e in h.last[1]["deliveries"]], ["delivered"])
        h.tick(28 * 60)
        self.assertEqual(len(h.last[1]["deliveries"]), 1)
        h.tick(120)
        self.assertEqual(h.last[1]["deliveries"], [])

    def test_a_parcel_delivered_before_this_app_looked_is_not_listed(self):
        h = Harness({AGGREGATOR_DELIVERED: aggregator(parcel(status="delivered", delivered_at=at(-31)),
                                                      parcel(barcode="TESTPN0002", status="delivered", delivered_at=at(-29)))})
        self.assertEqual([e["id"] for e in h.last[1]["deliveries"]], ["postnord:TESTPN0002"])

    def test_both_aggregator_sensors_are_listened_to_and_a_change_publishes_at_once(self):
        h = Harness()
        self.assertEqual([kw for entity, kw in h.listened if entity in (AGGREGATOR_INCOMING, AGGREGATOR_DELIVERED)],
                         [{"attribute": "all"}] * 2)
        self.assertEqual({entity for entity, _ in h.listened}, {AGGREGATOR_INCOMING, AGGREGATOR_DELIVERED})
        before = len(h.published)
        h.states[AGGREGATOR_INCOMING] = aggregator(parcel(start=at(30)))
        h.app._on_change(AGGREGATOR_INCOMING, "all", {}, {}, {})
        self.assertEqual(len(h.published), before + 1)
        self.assertEqual((h.last[0], h.last[1]["next"]["stage"]), ("1", "on_the_way"))

    def test_without_the_aggregator_wolt_is_unaffected(self):
        h = Harness(wolt(courier=True, eta=at(10)))
        self.assertEqual((h.last[0], h.last[1]["next"]["source"]), ("1", "wolt"))
        self.assertEqual([m for level, m in h.logs if level == "WARNING" and "Nemlig" not in m], [])

    def test_odd_aggregator_states_are_no_parcels(self):
        for value in (None, {}, {"state": "unavailable", "attributes": None}, {"state": "1", "attributes": []},
                      {"state": "1", "attributes": {"parcels": None}}, {"state": "1", "attributes": {"parcels": "x"}},
                      {"state": "1", "attributes": {"parcels": {"carrier": "PostNord"}}}):
            with self.subTest(value=value):
                h = Harness({AGGREGATOR_INCOMING: value, AGGREGATOR_DELIVERED: value})
                h.tick(30)
                self.assertEqual((h.last[0], h.last[1]["deliveries"], h.last[1]["upcoming"]), ("0", [], []))

    def test_an_aggregator_that_blinks_is_held_not_dropped(self):
        h = Harness({AGGREGATOR_INCOMING: aggregator(parcel(start=at(40)))})
        h.states[AGGREGATOR_INCOMING] = {"state": "unavailable", "attributes": {"friendly_name": "Parcels"}}
        h.tick(60)
        self.assertEqual(h.last[0], "1")
        h.tick(600)
        self.assertEqual(h.last[0], "0")

    def test_parcels_wolt_and_the_rest_share_one_sorted_entity(self):
        h = Harness({**wolt(courier=True, eta=at(30)),
                     AGGREGATOR_INCOMING: aggregator(parcel(start=at(10)), parcel(carrier="Airmee", barcode="TESTAM0001",
                                                                                     status="registered", start=at(300)))})
        h.tick(5)
        state, attrs = h.last
        self.assertEqual(state, "3")
        self.assertEqual([e["source"] for e in attrs["deliveries"]], ["postnord", "wolt", "airmee"])
        self.assertEqual(attrs["spoken"], "In about 30 minutes, around 6:18 PM. There are 2 more after that.")

    def test_two_parcels_from_one_carrier_are_two_deliveries(self):
        h = Harness({AGGREGATOR_INCOMING: aggregator(parcel(), parcel(barcode="TESTPN0002"))})
        self.assertEqual((h.last[0], [e["id"] for e in h.last[1]["deliveries"]]),
                         ("2", ["postnord:TESTPN0001", "postnord:TESTPN0002"]))
        self.assertEqual(h.last[1]["spoken"], "A PostNord parcel is out for delivery, and 1 more.")

    def test_a_parcel_that_is_days_away_is_upcoming_until_its_day_and_then_a_delivery(self):
        h = Harness({AGGREGATOR_INCOMING: aggregator(
            parcel(carrier="GLS", barcode="TESTGL0001", status="in_transit"),
            parcel(status="registered", start=local_time(11, 14), end=local_time(11, 18)))})
        state, attrs = h.last
        self.assertEqual((state, attrs["deliveries"]), ("0", []))
        self.assertEqual([(item["source"], item["status"]) for item in attrs["upcoming"]],
                         [("postnord", "registered"), ("gls", "in_transit")])
        self.assertEqual(attrs["upcoming"][0]["planned_from"], "2026-11-11T14:00:00+01:00")

        h.tick(local_time(11, 9) - h.now)
        state, attrs = h.last
        self.assertEqual((state, [e["source"] for e in attrs["deliveries"]]), ("1", ["postnord"]))
        self.assertEqual([item["source"] for item in attrs["upcoming"]], ["gls"])

    def test_a_parcel_out_for_delivery_is_not_upcoming_and_a_delivered_one_never_is(self):
        h = Harness({AGGREGATOR_INCOMING: aggregator(parcel(), parcel(barcode="TESTPN0002", status="delivered")),
                     AGGREGATOR_DELIVERED: aggregator(parcel(barcode="TESTPN0003", status="delivered", delivered_at=at(-5)))})
        state, attrs = h.last
        self.assertEqual(state, "1")
        self.assertEqual(attrs["upcoming"], [])

    def test_a_parcel_the_ledger_still_holds_is_not_upcoming_as_well(self):
        h = Harness({AGGREGATOR_INCOMING: aggregator(parcel())})
        self.assertEqual((h.last[0], h.last[1]["upcoming"]), ("1", []))
        h.states[AGGREGATOR_INCOMING] = aggregator(parcel(status="at_pickup_point"))
        h.tick(60)
        state, attrs = h.last
        self.assertEqual((state, [e["id"] for e in attrs["deliveries"]], attrs["upcoming"]), ("1", ["postnord:TESTPN0001"], []))
        h.tick(10 * 60)
        state, attrs = h.last
        self.assertEqual((state, attrs["deliveries"], [item["status"] for item in attrs["upcoming"]]),
                         ("0", [], ["at_pickup_point"]))

    def test_upcoming_follows_the_aggregator_and_is_published_empty_when_nothing_is_coming(self):
        h = Harness()
        self.assertEqual(h.last[1]["upcoming"], [])
        h.states[AGGREGATOR_INCOMING] = aggregator(parcel(carrier="GLS", barcode="TESTGL0001", status="in_transit"))
        h.app._on_change(AGGREGATOR_INCOMING, "all", {}, {}, {})
        self.assertEqual([item["id"] for item in h.last[1]["upcoming"]], ["gls:TESTGL0001"])
        h.states[AGGREGATOR_INCOMING] = aggregator()
        h.app._on_change(AGGREGATOR_INCOMING, "all", {}, {}, {})
        self.assertEqual(h.last[1]["upcoming"], [])


class DoorCourierAppTests(unittest.TestCase):
    PARCEL_OUT = {AGGREGATOR_INCOMING: aggregator(parcel(start=at(30)))}

    def make(self, owner="home", **states):
        return Harness({OWNER: {"state": owner, "attributes": {}}, **self.PARCEL_OUT, **states})

    def test_a_parcel_answers_the_door_whoever_is_home(self):
        for owner in ("home", "not_home"):
            with self.subTest(owner=owner):
                h = self.make(owner)
                self.assertEqual((h.last[1]["door_courier_kind"], h.last[1]["courier_expected"]), ("parcel", True))
        self.assertEqual(Harness(self.PARCEL_OUT).last[1]["door_courier_kind"], "parcel")

    def test_food_answers_the_door_whoever_is_home(self):
        for owner in ("home", "not_home"):
            with self.subTest(owner=owner):
                h = self.make(owner, **wolt(courier=True, eta=at(10)))
                self.assertEqual((h.last[1]["door_courier_kind"], h.last[1]["food_courier_expected"]), ("food", True))

    def test_who_is_home_is_not_listened_to(self):
        h = self.make("not_home")
        self.assertNotIn(OWNER, [entity for entity, _ in h.listened])


class CarrierRefreshAppTests(unittest.TestCase):
    AIRMEE_OUT = {AGGREGATOR_INCOMING: aggregator(parcel(carrier="Airmee", barcode="TESTAM0001"))}

    def make(self, states=None, **args):
        return Harness({**self.AIRMEE_OUT, **(states or {})}, **{"refresh_buttons": BUTTONS, **args})

    def pressed(self, h):
        return [data["entity_id"] for service, data in h.calls if service == "button/press"]

    def deliver(self, h, *still_out):
        h.states[AGGREGATOR_INCOMING] = aggregator(*still_out)
        h.states[AGGREGATOR_DELIVERED] = aggregator(
            parcel(carrier="Airmee", barcode="TESTAM0001", status="delivered", delivered_at=h.now))
        h.app._on_change(AGGREGATOR_INCOMING, "all", {}, {}, {})

    def test_a_street_ring_while_a_parcel_is_out_presses_its_carriers_button_at_three_and_six_minutes(self):
        h = self.make()
        h.ring(("front door", NOW))
        h.advance(179)
        self.assertEqual(h.calls, [])
        h.advance(1)
        self.assertEqual(h.calls, [("button/press", {"entity_id": "button.airmee_test_refresh"})])
        h.advance(179)
        self.assertEqual(len(h.calls), 1)
        h.advance(1)
        self.assertEqual(self.pressed(h), ["button.airmee_test_refresh"] * 2)
        h.advance(3600)
        self.assertEqual(len(h.calls), 2)
        self.assertEqual([message for _, message in h.logs if message.startswith("Refreshing")],
                         ["Refreshing airmee after the courier rang"] * 2)

    def test_the_back_door_counts_and_the_apartment_door_does_not(self):
        for door, presses in (("front door", 2), ("back door", 2), ("apartment door", 0), ("", 0)):
            with self.subTest(door=door):
                h = self.make()
                h.ring((door, NOW))
                h.advance(400)
                self.assertEqual(len(self.pressed(h)), presses)

    def test_a_re_ring_of_the_same_visit_schedules_no_more_presses(self):
        h = self.make()
        h.ring(("front door", NOW))
        self.assertEqual(len(h.timers), 2)
        for _ in range(3):
            h.advance(20)
            h.ring(("front door", NOW))
        h.advance(20)
        h.ring()
        self.assertEqual(len(h.timers), 2)
        h.advance(400)
        self.assertEqual(len(self.pressed(h)), 2)

    def test_a_second_visit_is_a_ring_of_its_own(self):
        h = self.make()
        h.ring(("front door", NOW))
        h.advance(30)
        h.ring(("front door", NOW), ("back door", NOW + 30))
        h.advance(400)
        self.assertEqual(len(self.pressed(h)), 4)

    def test_a_ring_that_no_parcel_courier_made_presses_nothing(self):
        cases = {
            "nothing on the way": {},
            "the parcel is only expected later today":
                {AGGREGATOR_INCOMING: aggregator(parcel(carrier="Airmee", status="in_transit", start=at(30)))},
            "a food courier is due": {**wolt(courier=True, eta=at(5)), **self.AIRMEE_OUT},
            "the parcel is delivered": {AGGREGATOR_DELIVERED: aggregator(
                parcel(carrier="Airmee", barcode="TESTAM0001", status="delivered", delivered_at=at(-1)))},
            "the carrier has no button": {AGGREGATOR_INCOMING: aggregator(parcel(carrier="GLS", barcode="TESTGL0001"))},
        }
        for name, states in cases.items():
            with self.subTest(name):
                h = Harness(states, refresh_buttons=BUTTONS)
                h.ring(("front door", NOW))
                self.assertEqual(h.timers, [])
                h.advance(400)
                self.assertEqual(h.calls, [])

    def test_a_press_is_skipped_once_every_parcel_of_the_carrier_is_delivered(self):
        h = self.make()
        h.ring(("front door", NOW))
        h.advance(100)
        self.deliver(h)
        h.advance(400)
        self.assertEqual(h.calls, [])

    def test_the_second_press_is_skipped_when_the_first_brought_the_delivery(self):
        h = self.make()
        h.ring(("front door", NOW))
        h.advance(180)
        self.assertEqual(len(h.calls), 1)
        self.deliver(h)
        h.advance(400)
        self.assertEqual(len(h.calls), 1)

    def test_another_parcel_of_the_carrier_still_out_keeps_the_presses_coming(self):
        second = parcel(carrier="Airmee", barcode="TESTAM0002")
        h = self.make({AGGREGATOR_INCOMING: aggregator(parcel(carrier="Airmee", barcode="TESTAM0001"), second)})
        h.ring(("front door", NOW))
        h.advance(100)
        self.deliver(h, second)
        h.advance(400)
        self.assertEqual(len(self.pressed(h)), 2)

    def test_another_carriers_parcel_does_not_keep_the_presses_coming(self):
        h = self.make({AGGREGATOR_INCOMING: aggregator(parcel(carrier="Airmee", barcode="TESTAM0001"),
                                                       parcel(carrier="PostNord", barcode="TESTPN0001"))})
        h.ring(("front door", NOW))
        h.advance(100)
        self.deliver(h, parcel(carrier="PostNord", barcode="TESTPN0001"))
        h.advance(400)
        self.assertEqual(h.calls, [])

    def test_a_visit_that_rang_a_while_ago_only_gets_the_moments_still_ahead(self):
        h = self.make()
        h.ring(("front door", NOW - 200))
        h.advance(400)
        self.assertEqual(len(self.pressed(h)), 1)
        h = self.make()
        h.ring(("front door", NOW - 400))
        self.assertEqual(h.timers, [])

    def test_a_ring_is_forgotten_once_its_last_moment_has_passed(self):
        h = self.make()
        h.ring(("front door", NOW))
        self.assertEqual(h.app._refreshed_rings, {NOW})
        h.advance(361)
        h.ring()
        self.assertEqual(h.app._refreshed_rings, set())

    def test_the_intercom_is_read_only_when_there_are_buttons(self):
        self.assertNotIn(INTERCOM_VISIT, [entity for entity, _ in Harness(self.AIRMEE_OUT).listened])
        listened = [kw for entity, kw in self.make().listened if entity == INTERCOM_VISIT]
        self.assertEqual(listened, [{"attribute": "all"}])

    def test_the_shipped_buttons_are_keyed_by_the_carrier_slugs_the_parcels_get(self):
        shipped = shipped_refresh_buttons()
        carriers = {"airmee": "Airmee", "postnord": "PostNord", "gls": "GLS", "budbee": "Budbee",
                    "posten_bring": "Posten Bring", "dao": "DAO", "dhl": "DHL", "danske_fragtmaend": "Danske Fragtmænd"}
        self.assertEqual(set(shipped), set(carriers))
        for source, carrier in carriers.items():
            with self.subTest(source=source):
                h = Harness({AGGREGATOR_INCOMING: aggregator(parcel(carrier=carrier))}, refresh_buttons=shipped)
                h.ring(("front door", NOW))
                h.advance(180)
                self.assertEqual(self.pressed(h), [shipped[source]])

    def test_ups_has_no_shipped_button_since_its_refreshes_are_rate_limited(self):
        self.assertNotIn("ups", shipped_refresh_buttons())
        h = Harness({AGGREGATOR_INCOMING: aggregator(parcel(carrier="UPS"))}, refresh_buttons=shipped_refresh_buttons())
        h.ring(("front door", NOW))
        h.advance(400)
        self.assertEqual(self.pressed(h), [])


class CarrierNameTests(unittest.TestCase):
    def test_a_parcel_source_is_named_as_its_carrier_names_itself(self):
        deliveries = [delivery(), carrier_parcel("PostNord", "TESTPN0001"), carrier_parcel("Airmee", "TESTAM0001")]
        self.assertEqual(hd.carrier_name(deliveries, "postnord"), "PostNord")
        self.assertEqual(hd.carrier_name(deliveries, "airmee"), "Airmee")

    def test_posten_bring_is_named_bring_and_danske_fragtmaend_keeps_its_letters(self):
        deliveries = [carrier_parcel("Posten Bring", "TESTBR1"), carrier_parcel("Danske Fragtmænd", "TESTDF1")]
        self.assertEqual(hd.carrier_name(deliveries, "posten_bring"), "Bring")
        self.assertEqual(hd.carrier_name(deliveries, "danske_fragtmaend"), "Danske Fragtmænd")

    def test_a_source_without_a_parcel_is_named_as_it_is(self):
        self.assertEqual(hd.carrier_name([], "postnord"), "postnord")
        self.assertEqual(hd.carrier_name([delivery()], "wolt"), "wolt")


class CourierPlausibleTests(unittest.TestCase):
    def plausible(self, *parcels, now=NOW):
        return hd.courier_plausible(list(parcels), now)

    def test_a_window_reaches_half_an_hour_to_either_side(self):
        window = carrier_parcel(start=at(60), end=at(120))
        for moment, expected in ((at(30) - 1, False), (at(30), True), (at(60), True), (at(90), True), (at(120), True),
                                 (at(150), True), (at(150) + 1, False)):
            with self.subTest(moment=moment - NOW):
                self.assertEqual(self.plausible(window, now=moment), expected)

    def test_any_of_several_windows_will_do(self):
        morning = carrier_parcel(barcode="TESTAM0001", start=at(-300), end=at(-240))
        evening = carrier_parcel(barcode="TESTAM0002", start=at(120), end=at(180))
        self.assertFalse(self.plausible(morning, evening))
        self.assertTrue(self.plausible(morning, evening, now=at(-250)))
        self.assertTrue(self.plausible(morning, evening, now=at(100)))

    def test_an_eta_reaches_three_quarters_of_an_hour_to_either_side(self):
        eta = carrier_parcel(start=at(60))
        for moment, expected in ((at(15) - 1, False), (at(15), True), (at(60), True), (at(105), True),
                                 (at(105) + 1, False)):
            with self.subTest(moment=moment - NOW):
                self.assertEqual(self.plausible(eta, now=moment), expected)

    def test_any_of_several_etas_will_do(self):
        early = carrier_parcel(barcode="TESTAM0001", start=at(-300))
        late = carrier_parcel(barcode="TESTAM0002", start=at(300))
        self.assertFalse(self.plausible(early, late))
        self.assertTrue(self.plausible(early, late, now=at(-290)))
        self.assertTrue(self.plausible(early, late, now=at(290)))

    def test_with_no_times_it_is_the_house_daytime_from_eight_to_six(self):
        no_times = carrier_parcel()
        for hour, minute, expected in ((3, 0, False), (7, 59, False), (8, 0, True), (12, 0, True), (17, 59, True),
                                       (18, 0, False), (18, 30, False), (23, 0, False)):
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(self.plausible(no_times, now=local_time(10, hour, minute)), expected)
        self.assertFalse(self.plausible(no_times, now=local_time(10, 8) - 1))
        self.assertTrue(self.plausible(no_times, now=local_time(10, 18) - 1))

    def test_the_daytime_is_the_local_one_in_summer_too(self):
        no_times = carrier_parcel()

        def summer(hour, minute):
            return datetime(2026, 7, 10, hour, minute, tzinfo=hd.HOUSE_TZ).timestamp()

        self.assertFalse(self.plausible(no_times, now=summer(7, 59)))
        self.assertTrue(self.plausible(no_times, now=summer(8, 0)))
        self.assertTrue(self.plausible(no_times, now=summer(17, 59)))
        self.assertFalse(self.plausible(no_times, now=summer(18, 0)))

    def test_the_more_precise_time_decides_whatever_the_carriers_other_parcels_say(self):
        over = carrier_parcel(barcode="TESTAM0001", start=at(-300), end=at(-240))
        eta_now = carrier_parcel(barcode="TESTAM0002", start=at(10))
        eta_far = carrier_parcel(barcode="TESTAM0003", start=at(300))
        no_times = carrier_parcel(barcode="TESTAM0004")
        self.assertTrue(self.plausible(no_times))
        self.assertFalse(self.plausible(over, no_times))
        self.assertFalse(self.plausible(over, eta_now))
        self.assertFalse(self.plausible(eta_far, no_times))
        self.assertTrue(self.plausible(eta_now, no_times))


class RefreshSourcesTests(unittest.TestCase):
    def sources(self, *deliveries, buttons=BUTTONS):
        return hd.refresh_sources(list(deliveries), NOW, buttons)

    def test_every_carrier_with_a_parcel_out_for_delivery_is_named_in_the_order_of_the_parcels(self):
        soon = carrier_parcel("PostNord", "TESTPN0001", start=at(5))
        later = carrier_parcel("Airmee", "TESTAM0001", start=at(40))
        for deliveries in ([later, soon], [soon, later]):
            with self.subTest(order=[d.source for d in deliveries]):
                self.assertEqual(self.sources(*deliveries), ["postnord", "airmee"])

    def test_a_carrier_is_named_once_however_many_parcels_it_has_out(self):
        self.assertEqual(self.sources(carrier_parcel(), carrier_parcel(barcode="TESTAM0002")), ["airmee"])

    def test_a_carrier_without_a_button_or_without_a_parcel_out_is_not_named(self):
        gls = carrier_parcel("GLS", "TESTGL0001")
        not_out = [carrier_parcel("PostNord", "TESTPN0001", status="in_transit", start=at(30)),
                   carrier_parcel("PostNord", "TESTPN0002", status="delivered", delivered_at=at(-1))]
        self.assertEqual(self.sources(gls, carrier_parcel(), *not_out), ["airmee"])
        self.assertEqual(self.sources(gls, *not_out), [])
        self.assertEqual(self.sources(carrier_parcel(), buttons={}), [])
        self.assertEqual(self.sources(), [])

    def test_nobody_is_named_while_a_food_or_groceries_courier_is_likelier(self):
        parcel_out = carrier_parcel()
        self.assertEqual(self.sources(parcel_out, delivery()), [])
        self.assertEqual(self.sources(parcel_out, nemlig_delivery()), [])
        self.assertEqual(self.sources(parcel_out, delivery(stage="preparing")), ["airmee"])

    def test_a_carrier_whose_courier_is_not_plausible_now_is_not_named(self):
        later = carrier_parcel("PostNord", "TESTPN0001", start=at(120))
        windowed = carrier_parcel("Airmee", "TESTAM0001", start=at(25), end=at(85))
        self.assertEqual(self.sources(later, windowed), ["airmee"])
        self.assertEqual(self.sources(later), [])
        self.assertEqual(hd.refresh_sources([carrier_parcel()], local_time(10, 22), BUTTONS), [])
        self.assertEqual(hd.refresh_sources([carrier_parcel()], local_time(10, 12), BUTTONS), ["airmee"])

    def test_each_carrier_is_judged_by_its_own_parcels(self):
        windowed_later = carrier_parcel("Airmee", "TESTAM0001", start=at(120), end=at(180))
        no_times = carrier_parcel("PostNord", "TESTPN0001")
        self.assertEqual(self.sources(windowed_later, no_times), ["postnord"])


class LandingCouriersTests(unittest.TestCase):
    def couriers(self, *deliveries, used=None, buttons=BUTTONS):
        return hd.landing_couriers(list(deliveries), NOW, buttons, used)

    def test_each_carrier_with_a_parcel_out_is_a_courier_by_its_own_name(self):
        self.assertEqual(self.couriers(carrier_parcel()), [("airmee", "Airmee")])
        both = [carrier_parcel(), carrier_parcel("PostNord", "TESTPN0001", start=at(5))]
        self.assertEqual(self.couriers(*both), [("postnord", "PostNord"), ("airmee", "Airmee")])

    def test_nobody_is_expected_unless_a_parcel_with_a_button_is_out_for_delivery(self):
        cases = {"nothing on the way": [], "only expected later": [carrier_parcel(status="in_transit", start=at(30))],
                 "delivered": [carrier_parcel(status="delivered", delivered_at=at(-1))],
                 "no button": [carrier_parcel("GLS", "TESTGL0001")], "food due": [carrier_parcel(), delivery()],
                 "groceries out": [carrier_parcel(), nemlig_delivery()],
                 "its ETA is hours away": [carrier_parcel(start=at(120))]}
        for name, deliveries in cases.items():
            with self.subTest(name):
                self.assertEqual(self.couriers(*deliveries), [])

    def test_someone_who_has_just_used_the_apartment_door_is_not_taken_for_a_courier(self):
        for used, expected in ((None, True), (NOW - 3600, True), (NOW - 60, True), (NOW - 59, False), (NOW, False)):
            with self.subTest(used=used):
                self.assertEqual(bool(self.couriers(carrier_parcel(), used=used)), expected)


class LandingStepTests(unittest.TestCase):
    AIRMEE = ("airmee", "Airmee")
    POSTNORD = ("postnord", "PostNord")

    def test_an_arrival_watches_each_courier_for_ten_minutes(self):
        watches, left = hd.landing_step({}, "arrived", NOW, [self.AIRMEE, self.POSTNORD])
        self.assertEqual((watches, left),
                         ({"airmee": landing_watch(), "postnord": landing_watch("postnord", "PostNord")}, []))

    def test_an_arrival_without_a_courier_watches_nothing(self):
        self.assertEqual(hd.landing_step({}, "arrived", NOW), ({}, []))

    def test_leaving_ends_every_watch_and_hands_them_back(self):
        watches = {"airmee": landing_watch(), "postnord": landing_watch("postnord", "PostNord", opened=True)}
        self.assertEqual(hd.landing_step(watches, "left", NOW + 90), ({}, list(watches.values())))

    def test_leaving_with_nothing_watched_ends_nothing(self):
        self.assertEqual(hd.landing_step({}, "left", NOW + 90), ({}, []))

    def test_a_visit_must_end_within_ten_minutes(self):
        watches = {"airmee": landing_watch()}
        self.assertEqual(hd.landing_step(watches, "left", NOW + 599), ({}, [landing_watch()]))
        self.assertEqual(hd.landing_step(watches, "left", NOW + 600), ({}, []))

    def test_the_door_opening_marks_every_watch_opened(self):
        watches = {"airmee": landing_watch(), "postnord": landing_watch("postnord", "PostNord")}
        self.assertEqual(hd.landing_step(watches, "opened", NOW + 100),
                         ({"airmee": landing_watch(opened=True),
                           "postnord": landing_watch("postnord", "PostNord", opened=True)}, []))

    def test_a_door_that_opens_with_nothing_watched_changes_nothing(self):
        self.assertEqual(hd.landing_step({}, "opened", NOW), ({}, []))

    def test_a_carrier_that_arrives_again_is_watched_afresh_and_keeps_its_open_door(self):
        watches, left = hd.landing_step({"airmee": landing_watch(opened=True)}, "arrived", NOW + 200, [self.AIRMEE])
        self.assertEqual((watches, left),
                         ({"airmee": landing_watch(until=NOW + 200 + hd.LANDING_WATCH_S, opened=True)}, []))

    def test_another_carrier_arriving_leaves_the_one_already_watched_alone(self):
        watched = landing_watch()
        watches, _ = hd.landing_step({"airmee": watched}, "arrived", NOW + 200, [self.POSTNORD])
        self.assertEqual(watches, {"airmee": watched, "postnord": landing_watch(
            "postnord", "PostNord", until=NOW + 200 + hd.LANDING_WATCH_S)})

    def test_any_event_drops_the_watches_whose_time_is_over(self):
        watches = {"airmee": landing_watch()}
        for event in ("arrived", "left", "opened"):
            with self.subTest(event=event):
                self.assertEqual(hd.landing_step(watches, event, NOW + hd.LANDING_WATCH_S), ({}, []))

    def test_the_watches_given_are_left_alone(self):
        watches = {"airmee": landing_watch()}
        for event in ("arrived", "opened", "left"):
            hd.landing_step(watches, event, NOW + 10, [self.AIRMEE])
        self.assertEqual(watches, {"airmee": landing_watch()})


class ToldRecentlyTests(unittest.TestCase):
    def test_a_carrier_is_told_about_again_after_half_an_hour(self):
        told = {"airmee": NOW}
        self.assertTrue(hd.told_recently(told, "airmee", NOW))
        self.assertTrue(hd.told_recently(told, "airmee", NOW + hd.TELL_AGAIN_AFTER_S - 1))
        self.assertFalse(hd.told_recently(told, "airmee", NOW + hd.TELL_AGAIN_AFTER_S))

    def test_a_carrier_nobody_was_told_about_is_not_recent(self):
        self.assertFalse(hd.told_recently({"airmee": NOW}, "postnord", NOW))
        self.assertFalse(hd.told_recently({}, "airmee", NOW))


class NoticeChannelTests(unittest.TestCase):
    def test_the_speakers_speak_when_he_is_home_and_they_may(self):
        self.assertEqual(hd.notice_channel("home", True), "speakers")

    def test_everything_else_is_a_push(self):
        self.assertEqual(hd.notice_channel("home", False), "push")
        for state in ("not_home", "work", "unknown", "unavailable", "", None):
            for free in (True, False):
                with self.subTest(state=state, free=free):
                    self.assertEqual(hd.notice_channel(state, free), "push")

    def test_the_sentence_says_the_parcel_is_probably_outside_the_door(self):
        self.assertEqual(hd.parcel_outside_sentence("PostNord"), "Your PostNord parcel is probably outside the door.")
        self.assertEqual(hd.parcel_outside_sentence("Airmee or PostNord"),
                         "Your Airmee or PostNord parcel is probably outside the door.")


class CarrierListTests(unittest.TestCase):
    def test_the_names_are_joined_as_speech(self):
        self.assertEqual(hd.carrier_list(["Airmee"]), "Airmee")
        self.assertEqual(hd.carrier_list(["Airmee", "PostNord"]), "Airmee or PostNord")
        self.assertEqual(hd.carrier_list(["Airmee", "PostNord", "GLS"]), "Airmee, PostNord or GLS")


class Speakers:
    """SonosNotifier as HomeDelivery uses it: whether it is quiet, and what is announced."""

    def __init__(self, quiet=False):
        self.quiet = quiet
        self.said = []

    def in_quiet_hours(self):
        return self.quiet

    def notify(self, **kwargs):
        self.said.append(kwargs)


class Phone:
    """MobileNotifier as HomeDelivery uses it."""

    def __init__(self):
        self.pushed = []

    async def notify(self, **kwargs):
        self.pushed.append(kwargs)
        return 1


def airmee_out(**times):
    """The aggregator's incoming sensor listing one Airmee parcel out for delivery, with these planned times."""
    return {AGGREGATOR_INCOMING: aggregator(parcel(carrier="Airmee", barcode="TESTAM0001", **times))}


class LandingWatchAppTests(unittest.TestCase):
    PUSH = {"title": "Parcel delivery", "message": AIRMEE_SENTENCE, "target": "owner_test"}
    NOBODY_EXPECTED = {
        "a food courier is due": {**wolt(courier=True, eta=at(5)), **out_for_delivery(AIRMEE_PARCEL)},
        "nothing on the way": out_for_delivery(),
        "the parcel is only expected later today":
            {AGGREGATOR_INCOMING: aggregator(parcel(carrier="Airmee", status="in_transit", start=at(30)))},
        "the carrier has no button": out_for_delivery(("GLS", "TESTGL0001")),
        "the window is hours away": airmee_out(start=at(120), end=at(180)),
        "the window closed hours ago": airmee_out(start=at(-240), end=at(-180)),
        "the ETA is hours away": airmee_out(start=at(120)),
        "the ETA was hours ago": airmee_out(start=at(-120)),
    }
    SOMEONE_EXPECTED = {
        "inside the window": airmee_out(start=at(-30), end=at(30)),
        "shortly before the window": airmee_out(start=at(25), end=at(85)),
        "shortly after the window": airmee_out(start=at(-80), end=at(-20)),
        "near the ETA": airmee_out(start=at(40)),
        "a little past the ETA": airmee_out(start=at(-30)),
        "no times in the daytime": airmee_out(),
    }

    def make(self, owner="home", quiet=False, parcels=(AIRMEE_PARCEL,), states=None, **args):
        h = Harness({OWNER: {"state": owner, "attributes": {}}, **out_for_delivery(*parcels), **(states or {})},
                    **{"refresh_buttons": BUTTONS, **LANDING_ARGS, **args})
        h.speakers, h.phone = Speakers(quiet), Phone()
        h.apps = {"SonosNotifier": h.speakers, "MobileNotifier": h.phone}
        return h

    def visit(self, h, came=60, stayed=40):
        """Someone comes up `came` seconds on, and the sensor goes off `stayed` seconds later."""
        h.advance(came)
        h.change(LANDING, "on")
        h.advance(stayed)
        h.change(LANDING, "off")

    def visit_touching(self, h, entity, on_the_landing):
        """A visit in which `entity` goes on and off, 10 s after the sensor went on or 60 s after it went off."""
        h.advance(60)
        h.change(LANDING, "on")
        h.advance(10)
        if on_the_landing:
            h.change(entity, "on")
            h.change(entity, "off")
        h.advance(30)
        h.change(LANDING, "off")
        h.advance(60)
        if not on_the_landing:
            h.change(entity, "on")
            h.change(entity, "off")
        h.advance(60)

    def deliver(self, h, *delivered, out=()):
        """The carriers call these (carrier, barcode) parcels delivered; the parcels in `out` are still out."""
        h.states.update(out_for_delivery(*out))
        h.states[AGGREGATOR_DELIVERED] = aggregator(*[
            parcel(carrier=carrier, barcode=barcode, status="delivered", delivered_at=h.now)
            for carrier, barcode in delivered])
        h.app._on_change(AGGREGATOR_INCOMING, "all", {}, {}, {})

    def told(self, h):
        h.run_executor()
        return h.speakers.said, h.phone.pushed

    def test_a_visit_presses_the_carriers_button_at_once_and_after_one_three_and_six_minutes(self):
        h = self.make()
        h.advance(60)
        h.change(LANDING, "on")
        h.advance(40)
        self.assertEqual(presses(h), [])
        self.assertIn(("INFO", "Someone is on the landing while Airmee has a parcel out for delivery"), h.logs)
        h.change(LANDING, "off")
        self.assertEqual(len(presses(h)), 1)
        for waited, total in ((59, 1), (1, 2), (119, 2), (1, 3), (179, 3), (1, 4), (3600, 4)):
            h.advance(waited)
            self.assertEqual(len(presses(h)), total, f"after waiting {waited} s more")
        self.assertEqual(set(presses(h)), {"button.airmee_test_refresh"})
        self.assertIn(("INFO", "Refreshing airmee after someone left the landing"), h.logs)

    def test_the_presses_stop_once_the_carrier_has_nothing_undelivered(self):
        h = self.make()
        self.visit(h)
        h.advance(60)
        self.assertEqual(len(presses(h)), 2)
        self.deliver(h, AIRMEE_PARCEL)
        h.advance(3600)
        self.assertEqual(len(presses(h)), 2)

    def test_another_parcel_of_the_carrier_still_out_keeps_the_presses_coming(self):
        two = [AIRMEE_PARCEL, ("Airmee", "TESTAM0002")]
        h = self.make(parcels=two)
        self.visit(h)
        self.deliver(h, two[0], out=two[1:])
        h.advance(3600)
        self.assertEqual(len(presses(h)), 4)

    def test_he_is_told_at_once_as_the_courier_leaves_the_landing_and_not_before(self):
        h = self.make()
        h.advance(60)
        h.change(LANDING, "on")
        h.advance(40)
        self.assertEqual(self.told(h), ([], []))
        h.change(LANDING, "off")
        self.assertEqual(self.told(h), ([{"message": AIRMEE_SENTENCE}], []))
        self.assertIn(("INFO", "An Airmee parcel is probably outside the door: telling the owner via speakers"),
                      h.logs)

    def test_what_the_carrier_reports_afterwards_is_not_announced(self):
        h = self.make()
        self.visit(h)
        h.advance(300)
        self.deliver(h, AIRMEE_PARCEL)
        self.assertEqual([entry["stage"] for entry in h.last[1]["deliveries"]], ["delivered"])
        h.tick(60)
        h.tick(3600)
        self.assertEqual(self.told(h), ([{"message": AIRMEE_SENTENCE}], []))

    def test_a_visit_to_the_landing_must_end_within_ten_minutes(self):
        for stayed, watched in ((599, True), (600, False)):
            with self.subTest(stayed=stayed):
                h = self.make()
                self.visit(h, 5, stayed)
                self.assertEqual((len(self.told(h)[0]), len(presses(h))), (1, 1) if watched else (0, 0))

    def test_the_apartment_door_opening_during_the_visit_hands_the_parcel_over_and_nothing_is_said(self):
        h = self.make()
        self.visit_touching(h, APARTMENT_DOOR, True)
        self.assertEqual(self.told(h), ([], []))
        self.assertEqual(len(presses(h)), 2)
        h.advance(3600)
        self.assertEqual(len(presses(h)), 4)
        self.assertIn(("INFO", "The apartment door opened while the Airmee courier may have been on the landing: "
                               "handed over, not announced"), h.logs)

    def test_a_door_opened_after_the_courier_left_is_too_late_to_take_back_what_was_said(self):
        h = self.make()
        self.visit_touching(h, APARTMENT_DOOR, False)
        self.assertEqual(self.told(h), ([{"message": AIRMEE_SENTENCE}], []))

    def test_the_doorbell_ringing_without_the_door_opening_does_not_hand_the_parcel_over(self):
        for on_the_landing in (True, False):
            with self.subTest(on_the_landing=on_the_landing):
                h = self.make()
                self.visit_touching(h, APARTMENT_BELL, on_the_landing)
                self.assertEqual(self.told(h), ([{"message": AIRMEE_SENTENCE}], []))
                self.assertEqual(len(presses(h)), 2)

    def test_a_door_opening_before_the_visit_is_no_part_of_it(self):
        h = self.make()
        h.change(APARTMENT_DOOR, "on")
        h.change(APARTMENT_DOOR, "off")
        self.visit(h, came=120)
        self.assertEqual(self.told(h), ([{"message": AIRMEE_SENTENCE}], []))

    def test_someone_who_has_just_gone_out_of_the_flat_is_not_a_courier(self):
        for entity in (APARTMENT_DOOR, APARTMENT_BELL):
            for gap, watched in ((3, False), (59, False), (60, True), (600, True)):
                with self.subTest(entity=entity, gap=gap):
                    h = self.make()
                    h.change(entity, "on")
                    h.change(entity, "off")
                    self.visit(h, came=gap)
                    self.assertEqual((len(self.told(h)[0]), len(presses(h))), (1, 1) if watched else (0, 0))

    def test_a_carrier_told_about_is_not_told_about_again_for_half_an_hour(self):
        h = self.make(now=local_time(10, 10))
        self.visit(h)
        self.assertEqual(len(self.told(h)[0]), 1)
        h.advance(600)
        self.visit(h, came=5, stayed=30)
        self.assertEqual(len(self.told(h)[0]), 1)
        self.assertEqual(len(presses(h)), 5)
        self.assertIn(("INFO", "The Airmee parcel is probably outside the door, not announced again within "
                               "30 minutes"), h.logs)
        h.advance(hd.TELL_AGAIN_AFTER_S)
        self.visit(h, came=5, stayed=30)
        self.assertEqual(len(self.told(h)[0]), 2)

    def test_the_half_hour_is_kept_for_each_carrier_apart(self):
        h = self.make(now=local_time(10, 10))
        self.visit(h)
        h.advance(300)
        h.states.update(out_for_delivery(AIRMEE_PARCEL, POSTNORD_PARCEL))
        h.app._on_change(AGGREGATOR_INCOMING, "all", {}, {}, {})
        self.visit(h, came=5, stayed=30)
        self.assertEqual([said["message"] for said in self.told(h)[0]], [AIRMEE_SENTENCE, POSTNORD_SENTENCE])

    def test_two_carriers_out_are_named_in_one_sentence_and_both_refreshed(self):
        h = self.make(parcels=[AIRMEE_PARCEL, POSTNORD_PARCEL])
        self.visit(h)
        self.assertEqual(self.told(h),
                         ([{"message": "Your Airmee or PostNord parcel is probably outside the door."}], []))
        self.assertEqual(presses(h), ["button.airmee_test_refresh", "button.postnord_test_refresh"])
        h.advance(60)
        self.assertEqual(len(presses(h)), 4)
        self.visit(h, came=5, stayed=30)
        self.assertEqual(len(self.told(h)[0]), 1)

    def test_the_carriers_are_named_in_the_order_of_their_parcels(self):
        h = self.make(states={AGGREGATOR_INCOMING: aggregator(
            parcel(carrier="PostNord", barcode="TESTPN0001", start=at(5)),
            parcel(carrier="Airmee", barcode="TESTAM0001", start=at(40)))})
        self.assertEqual(h.last[1]["door_courier"]["source"], "postnord")
        self.visit(h)
        self.assertEqual([said["message"] for said in self.told(h)[0]],
                         ["Your PostNord or Airmee parcel is probably outside the door."])
        both = ["button.postnord_test_refresh", "button.airmee_test_refresh"]
        self.assertEqual(presses(h), both)
        h.advance(60)
        self.assertEqual(presses(h), both * 2)

    def test_a_carrier_with_two_parcels_out_is_named_once(self):
        h = self.make(parcels=[AIRMEE_PARCEL, ("Airmee", "TESTAM0002")])
        self.visit(h)
        self.assertEqual(self.told(h), ([{"message": AIRMEE_SENTENCE}], []))

    def test_posten_bring_is_named_bring_and_danske_fragtmaend_by_its_own_letters(self):
        buttons = {"posten_bring": "button.bring_test_refresh", "danske_fragtmaend": "button.danske_test_refresh"}
        for carrier, source, name in (("Posten Bring", "posten_bring", "Bring"),
                                      ("Danske Fragtmænd", "danske_fragtmaend", "Danske Fragtmænd")):
            with self.subTest(carrier=carrier):
                h = self.make(parcels=[(carrier, "TESTX0001")], refresh_buttons=buttons)
                self.visit(h)
                self.assertEqual(self.told(h), ([{"message": f"Your {name} parcel is probably outside the door."}], []))
                self.assertEqual(presses(h), [buttons[source]])

    def test_only_a_carrier_with_a_plausible_courier_is_named_and_refreshed(self):
        h = self.make(states={AGGREGATOR_INCOMING: aggregator(
            parcel(carrier="Airmee", barcode="TESTAM0001", start=at(120), end=at(180)),
            parcel(carrier="PostNord", barcode="TESTPN0001"))})
        self.visit(h)
        self.assertEqual(self.told(h), ([{"message": POSTNORD_SENTENCE}], []))
        h.advance(3600)
        self.assertEqual(set(presses(h)), {"button.postnord_test_refresh"})

    def test_in_quiet_hours_or_when_he_is_away_he_gets_a_push_instead(self):
        cases = [("home", True)] + [(owner, quiet) for owner in ("not_home", "work", "unknown", "unavailable")
                                    for quiet in (False, True)]
        for owner, quiet in cases:
            with self.subTest(owner=owner, quiet=quiet):
                h = self.make(owner, quiet)
                self.visit(h)
                self.assertEqual(self.told(h), ([], [self.PUSH]))
                self.assertIn(("INFO", "An Airmee parcel is probably outside the door: telling the owner via push"),
                              h.logs)

    def test_two_carriers_are_pushed_in_one_sentence_too(self):
        h = self.make("not_home", parcels=[AIRMEE_PARCEL, POSTNORD_PARCEL])
        self.visit(h)
        self.assertEqual([push["message"] for push in h.phone.pushed],
                         ["Your Airmee or PostNord parcel is probably outside the door."])

    def test_the_push_goes_to_mikkel_unless_the_arg_says_otherwise(self):
        args = {key: value for key, value in LANDING_ARGS.items() if key != "notify_target"}
        h = Harness({OWNER: {"state": "not_home", "attributes": {}}, **out_for_delivery(AIRMEE_PARCEL)},
                    refresh_buttons=BUTTONS, **args)
        h.apps = {"MobileNotifier": Phone()}
        self.visit(h)
        self.assertEqual([push["target"] for push in h.apps["MobileNotifier"].pushed], ["mikkel"])

    def test_without_speakers_he_is_pushed_even_at_home(self):
        h = self.make()
        del h.apps["SonosNotifier"]
        self.visit(h)
        self.assertEqual(h.phone.pushed, [self.PUSH])

    def test_without_the_phone_notifier_nothing_breaks(self):
        h = self.make("not_home")
        del h.apps["MobileNotifier"]
        self.visit(h)
        self.assertIn(("WARNING", "MobileNotifier is not running: the parcel push is not sent"), h.logs)
        self.assertEqual(len(presses(h)), 1)

    def test_a_failure_telling_him_does_not_stop_the_presses(self):
        h = self.make()

        def broken():
            raise RuntimeError("no helpers")

        h.speakers.in_quiet_hours = broken
        self.visit(h)
        self.assertIn(("WARNING", "Telling the owner about the Airmee parcel failed: no helpers"), h.logs)
        self.assertEqual(len(presses(h)), 1)
        h.advance(3600)
        self.assertEqual(len(presses(h)), 4)

    def test_nothing_is_watched_while_no_courier_is_expected_or_plausible(self):
        for name, states in self.NOBODY_EXPECTED.items():
            with self.subTest(name):
                h = self.make(states=states)
                h.advance(60)
                h.change(LANDING, "on")
                self.assertEqual(h.app._landing_watches, {})
                h.advance(40)
                h.change(LANDING, "off")
                h.advance(3600)
                self.assertEqual((self.told(h), presses(h)), (([], []), []))

    def test_a_courier_that_is_plausible_by_its_window_or_eta_is_watched(self):
        for name, states in self.SOMEONE_EXPECTED.items():
            with self.subTest(name):
                h = self.make(states=states)
                self.visit(h)
                self.assertEqual(self.told(h), ([{"message": AIRMEE_SENTENCE}], []))
                self.assertEqual(len(presses(h)), 1)

    def test_a_carrier_without_times_is_watched_only_in_the_daytime(self):
        cases = ((7, 30, False), (8, 30, True), (12, 0, True), (17, 30, True), (18, 30, False), (23, 0, False))
        for hour, minute, watched in cases:
            with self.subTest(hour=hour, minute=minute):
                h = self.make(now=local_time(10, hour, minute))
                self.visit(h)
                self.assertEqual((len(self.told(h)[0]), len(presses(h))), (1, 1) if watched else (0, 0))

    def test_a_parcel_that_goes_out_after_the_landing_went_on_is_not_watched(self):
        h = self.make(states=out_for_delivery())
        h.advance(60)
        h.change(LANDING, "on")
        h.states.update(out_for_delivery(AIRMEE_PARCEL))
        h.app._on_change(AGGREGATOR_INCOMING, "all", {}, {}, {})
        h.advance(40)
        h.change(LANDING, "off")
        self.assertEqual((self.told(h), presses(h)), (([], []), []))

    def test_a_parcel_delivered_without_a_visit_tells_nothing(self):
        h = self.make()
        h.advance(300)
        self.deliver(h, AIRMEE_PARCEL)
        self.assertEqual((self.told(h), presses(h)), (([], []), []))

    def test_a_landing_that_goes_off_without_having_gone_on_tells_nothing(self):
        h = self.make()
        h.advance(60)
        h.change(LANDING, "off")
        self.assertEqual((self.told(h), presses(h)), (([], []), []))

    def test_the_presses_after_a_ring_stay_beside_the_ones_for_the_visit(self):
        h = self.make()
        h.ring(("front door", NOW))
        self.visit(h)
        h.advance(3600)
        self.assertEqual(presses(h), ["button.airmee_test_refresh"] * 6)
        logged = [message for _, message in h.logs if message.startswith("Refreshing")]
        self.assertEqual((logged.count("Refreshing airmee after the courier rang"),
                          logged.count("Refreshing airmee after someone left the landing")), (2, 4))

    def test_the_sensors_are_listened_to_only_for_a_configured_landing_with_buttons(self):
        sensors = (LANDING, APARTMENT_DOOR, APARTMENT_BELL)
        h = self.make()
        self.assertEqual([(entity, kw) for entity, kw in h.listened if entity in sensors],
                         [(LANDING, {}), (APARTMENT_DOOR, {}), (APARTMENT_BELL, {})])
        self.assertNotIn(OWNER, [entity for entity, _ in h.listened])
        args = {key: value for key, value in LANDING_ARGS.items() if key != "landing_entity"}
        for name, h in {"no landing entity": Harness(out_for_delivery(AIRMEE_PARCEL), refresh_buttons=BUTTONS, **args),
                        "no buttons": Harness(out_for_delivery(AIRMEE_PARCEL), **LANDING_ARGS)}.items():
            with self.subTest(name):
                self.assertEqual([entity for entity, _ in h.listened if entity in sensors], [])

    def test_without_a_landing_entity_a_visit_tells_nothing_and_a_ring_presses_as_before(self):
        h = Harness({OWNER: {"state": "home", "attributes": {}}, **out_for_delivery(AIRMEE_PARCEL)},
                    refresh_buttons=BUTTONS)
        h.speakers, h.phone = Speakers(), Phone()
        h.apps = {"SonosNotifier": h.speakers, "MobileNotifier": h.phone}
        h.ring(("front door", NOW))
        h.change(LANDING, "on")
        h.advance(40)
        h.change(LANDING, "off")
        h.advance(400)
        self.assertEqual(presses(h), ["button.airmee_test_refresh"] * 2)
        self.assertEqual(self.told(h), ([], []))

    def test_the_shipped_entities_are_the_ones_the_house_has(self):
        shipped = {key: shipped_setting(key) for key in LANDING_ARGS}
        self.assertEqual(shipped, {"landing_entity": "binary_sensor.landing_presence_presence",
                                   "apartment_door_entity": "binary_sensor.apartment_door_open",
                                   "apartment_doorbell_entity": "binary_sensor.yale_doorbell_ding",
                                   "owner_entity": "person.mikkel", "notify_target": "mikkel"})
        h = self.make(**shipped)
        h.advance(60)
        h.change(shipped["landing_entity"], "on")
        h.advance(40)
        h.change(shipped["landing_entity"], "off")
        self.assertEqual(self.told(h), ([{"message": AIRMEE_SENTENCE}], []))


if __name__ == "__main__":
    unittest.main()
