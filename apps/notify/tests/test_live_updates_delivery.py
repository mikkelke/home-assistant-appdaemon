"""Delivery cards read sensor.home_delivery only: one card per source (Wolt, Nemlig, each parcel carrier),
cleared on delivery, one push on a cancel and none for parcels. Only food reaches Kristine. A street ring while the
sensor names a door courier takes that delivery's card away and words the apartment ring that follows it."""

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_live_updates import ENTITIES, HOUSE, NOW, TAGS, Harness, full, visit  # noqa: E402

import live_updates as lu  # noqa: E402

WOLT, NEMLIG = TAGS["wolt"], TAGS["nemlig"]
AIRMEE, POSTNORD, GLS = TAGS["airmee"], TAGS["postnord"], TAGS["gls"]
APARTMENT = TAGS["apartment"]
YALE_DING = "binary_sensor.yale_doorbell_ding"
COURIER = "Probably the Wolt courier"
SHIPPED = Path(__file__).resolve().parents[1] / "live_updates.yaml"
KRISTINE = "notify/mobile_app_kristine_iphone_2"
MIKKEL = "notify/mobile_app_mikkels_ofx9p"
KITCHEN = "Test Kitchen"


def local(at):
    return datetime.fromtimestamp(at, HOUSE).isoformat(timespec="seconds")


def entry(source="wolt", stage="on_the_way", eta=NOW + 480, start=None, end=None, title=KITCHEN, id=None):
    return {"id": id or f"{source}:{title.lower().replace(' ', '_')}", "source": source,
            "kind": "food" if source == "wolt" else "groceries", "title": title if source == "wolt" else "Nemlig",
            "stage": stage, "eta": local(eta) if eta is not None else None,
            "window_start": local(start) if start is not None else None,
            "window_end": local(end) if end is not None else None, "item_count": 1}


def parcel_entry(source="postnord", stage="on_the_way", eta=None, start=None, end=None, barcode="TEST0001",
                 title="Test Shop"):
    return {"id": f"{source}:{barcode}", "source": source, "kind": "parcel", "title": title, "stage": stage,
            "eta": local(eta) if eta is not None else None,
            "window_start": local(start) if start is not None else None,
            "window_end": local(end) if end is not None else None, "item_count": None}


def sensor(*entries):
    active = [e for e in entries if e["stage"] in ("preparing", "on_the_way", "arriving")]
    return full(str(len(active)), {"deliveries": list(entries), "next": active[0] if active else None})


def shipped_audiences():
    """The delivery_*_people lines of the shipped live_updates.yaml, read without PyYAML."""
    found = {}
    for line in SHIPPED.read_text().splitlines():
        match = re.match(r"\s+(delivery_\w+_people):\s*\[([^\]]*)\]", line)
        if match:
            found[match.group(1)] = [name.strip() for name in match.group(2).split(",") if name.strip()]
    return found


def with_courier(state, named):
    """state with HomeDelivery's door_courier set: the entry a ring at the door most likely brings. Without a
    courier the key is absent, as AppDaemon leaves out a None."""
    state["attributes"]["door_courier"] = named
    return state


def expecting(*entries, due=True):
    """sensor(...) as HomeDelivery publishes it while a Wolt courier is due: door_courier is the first active food
    entry."""
    state = sensor(*entries)
    food = [e for e in entries if e["kind"] == "food" and e["stage"] in ("preparing", "on_the_way", "arriving")]
    return with_courier(state, food[0]) if due and food else state


def card(**kw):
    return lu.delivery_card(entry(**kw), NOW)


class WoltCardTests(unittest.TestCase):
    def test_each_active_stage(self):
        for stage, message, icon in (("preparing", "Preparing · 13:08", "mdi:chef-hat"),
                                     ("on_the_way", "On the way · 13:08", "mdi:moped"),
                                     ("arriving", "Arriving · 13:08", "mdi:moped")):
            with self.subTest(stage=stage):
                c = card(stage=stage)
                self.assertEqual((c.tag, c.title, c.message, c.critical_text, c.icon, c.link, c.finish_at),
                                 (WOLT, f"Wolt · {KITCHEN}", message, "8 min", icon, "app://com.wolt.android", NOW + 480))

    def test_a_passed_eta_that_is_still_active_says_arriving_without_a_countdown(self):
        for stage in ("preparing", "on_the_way", "arriving"):
            with self.subTest(stage=stage):
                c = card(stage=stage, eta=NOW - 120)
                self.assertEqual((c.message, c.critical_text, c.icon, c.finish_at), ("Arriving", "Arriving", "mdi:moped", None))
                self.assertNotIn("when", lu.notification_payload(c, NOW)["data"])

    def test_the_eta_instant_itself_counts_as_passed(self):
        self.assertIsNone(card(eta=NOW).finish_at)
        self.assertIsNotNone(card(eta=NOW + 1).finish_at)

    def test_no_eta_shows_the_stage_without_inventing_a_timer(self):
        for stage, label, icon in (("preparing", "Preparing", "mdi:chef-hat"), ("on_the_way", "On the way", "mdi:moped")):
            with self.subTest(stage=stage):
                c = card(stage=stage, eta=None)
                self.assertEqual((c.message, c.critical_text, c.icon, c.finish_at), (label, label, icon, None))

    def test_ended_stages_and_unknown_sources_have_no_card(self):
        for stage in ("delivered", "cancelled", "mystery"):
            self.assertIsNone(card(stage=stage))
        self.assertIsNone(lu.delivery_card(entry(source="mystery"), NOW))
        self.assertIsNone(lu.delivery_card({"stage": "on_the_way"}, NOW))

    def test_a_venue_less_wolt_order_is_titled_wolt(self):
        self.assertEqual(lu.delivery_card({**entry(), "title": None}, NOW).title, "Wolt")

    def test_the_payload_is_the_quiet_live_card_every_other_source_uses(self):
        payload = lu.notification_payload(card(), NOW)
        data = payload["data"]
        self.assertEqual(payload["title"], f"Wolt · {KITCHEN}")
        self.assertEqual((data["tag"], data["channel"], data["importance"], data["critical_text"]),
                         (WOLT, "Live updates", "low", "8 min"))
        self.assertEqual((data["when"], data["chronometer"], data["when_relative"], data["live_update"]), (480, True, True, True))
        self.assertEqual((data["clickAction"], data["url"]), ("app://com.wolt.android",) * 2)
        self.assertNotIn("priority", data)
        self.assertNotIn("progress", data)


class NemligCardTests(unittest.TestCase):
    def hour(self, hours):
        return datetime(2026, 11, 10, hours, 0, tzinfo=HOUSE).timestamp()

    def test_booked_window_without_a_countdown_until_the_last_hour(self):
        c = card(source="nemlig", stage="preparing", eta=None, start=self.hour(16), end=self.hour(19))
        self.assertEqual((c.tag, c.title, c.message, c.critical_text, c.icon, c.link, c.finish_at),
                         (NEMLIG, "Nemlig", "Delivery 16–19", "16–19", "mdi:cart", "app://com.nemlig", None))

    def test_a_window_that_starts_within_the_hour_counts_down_to_it(self):
        for minutes, critical in ((60, "60 min"), (1, "1 min"), (61, "14:01–17:00"), (0, "13–17"), (-30, "12:30–17:00")):
            with self.subTest(minutes=minutes):
                c = card(source="nemlig", stage="preparing", eta=None, start=NOW + minutes * 60, end=NOW + 14400)
                self.assertEqual(c.critical_text, critical)
                self.assertEqual(c.finish_at, NOW + minutes * 60 if minutes in (60, 1) else None)
                self.assertTrue(c.message.startswith("Delivery "))

    def test_half_hour_windows_keep_their_minutes(self):
        start = datetime(2026, 11, 10, 16, 30, tzinfo=HOUSE).timestamp()
        c = card(source="nemlig", stage="preparing", eta=None, start=start, end=start + 5400)
        self.assertEqual(c.message, "Delivery 16:30–18:00")

    def test_no_window_still_gets_a_card(self):
        c = card(source="nemlig", stage="preparing", eta=None)
        self.assertEqual((c.message, c.critical_text, c.finish_at), ("Delivery today", "Today", None))

    def test_on_the_way_counts_down_to_the_eta(self):
        c = card(source="nemlig", eta=NOW + 1500)
        self.assertEqual((c.message, c.critical_text, c.icon, c.finish_at), ("On the way · 13:25", "25 min", "mdi:truck-delivery", NOW + 1500))
        self.assertEqual(lu.delivery_card(entry("nemlig", eta=NOW - 60), NOW).message, "Arriving")
        c = card(source="nemlig", eta=None)
        self.assertEqual((c.message, c.critical_text, c.finish_at), ("On the way", "On the way", None))

    def test_delivered_has_no_card(self):
        self.assertIsNone(card(source="nemlig", stage="delivered"))


def parcel_card(**kw):
    return lu.delivery_card(parcel_entry(**kw), NOW)


class ParcelCardTests(unittest.TestCase):
    def test_each_carrier_has_its_own_tag_title_icon_and_app(self):
        for source, title, link in (("airmee", "Airmee", "app://com.airmee.customer"),
                                    ("postnord", "PostNord", "app://se.postnord.private"),
                                    ("gls", "GLS", "app://dk.gls.consigneeapp")):
            with self.subTest(source=source):
                c = parcel_card(source=source, eta=NOW + 1800)
                self.assertEqual((c.tag, c.title, c.icon, c.link), (TAGS[source], title, "mdi:package-variant-closed", link))

    def test_the_title_is_the_carrier_not_the_sender(self):
        self.assertEqual(parcel_card(title="Some Webshop").title, "PostNord")
        self.assertEqual(parcel_card(title=None).title, "PostNord")

    def test_a_window_is_shown_while_the_courier_is_out(self):
        c = parcel_card(start=NOW + 3600, end=NOW + 10800)
        self.assertEqual((c.message, c.critical_text, c.finish_at), ("Out for delivery · 14–16", "14–16", None))
        c = parcel_card(start=NOW + 5400, end=NOW + 10800)
        self.assertEqual((c.message, c.critical_text, c.finish_at), ("Out for delivery · 14:30–16:00", "14:30–16:00", None))

    def test_an_arriving_parcel_keeps_its_window(self):
        c = parcel_card(stage="arriving", start=NOW + 600, end=NOW + 3600)
        self.assertEqual((c.message, c.critical_text, c.finish_at), ("Arriving · 13:10–14:00", "13:10–14:00", None))
        c = parcel_card(stage="arriving", start=NOW - 600, end=NOW + 600)
        self.assertEqual((c.message, c.finish_at), ("Arriving · 12:50–13:10", None))

    def test_an_exact_eta_is_counted_down_to(self):
        c = parcel_card(eta=NOW + 1800)
        self.assertEqual((c.message, c.critical_text, c.finish_at), ("Out for delivery · 13:30", "30 min", NOW + 1800))
        c = parcel_card(stage="arriving", eta=NOW + 600)
        self.assertEqual((c.message, c.critical_text, c.finish_at), ("Arriving · 13:10", "10 min", NOW + 600))
        self.assertEqual(lu.notification_payload(c, NOW)["data"]["when"], 600)

    def test_a_passed_eta_or_window_says_arriving_without_a_countdown(self):
        for kw in ({"eta": NOW - 60}, {"eta": NOW}, {"start": NOW - 7200, "end": NOW - 60}, {"start": NOW - 7200, "end": NOW},
                   {"stage": "arriving", "eta": NOW - 60}):
            with self.subTest(kw=kw):
                c = parcel_card(**kw)
                self.assertEqual((c.message, c.critical_text, c.finish_at), ("Arriving", "Arriving", None))
                self.assertNotIn("when", lu.notification_payload(c, NOW)["data"])

    def test_no_time_shows_the_stage_without_inventing_a_timer(self):
        c = parcel_card()
        self.assertEqual((c.message, c.critical_text, c.finish_at), ("Out for delivery", "On the way", None))
        c = parcel_card(stage="arriving")
        self.assertEqual((c.message, c.critical_text, c.finish_at), ("Arriving", "Arriving", None))

    def test_there_is_never_a_card_before_the_courier_is_out_or_after_the_hand_over(self):
        for stage in ("preparing", "delivered", "cancelled", "mystery", None):
            with self.subTest(stage=stage):
                self.assertIsNone(parcel_card(stage=stage, eta=NOW + 1800))
                self.assertIsNone(parcel_card(stage=stage, start=NOW + 600, end=NOW + 3600))

    def test_other_carriers_have_no_card(self):
        self.assertIsNone(lu.delivery_card(parcel_entry(source="dao"), NOW))
        self.assertIsNone(lu.delivery_card(parcel_entry(source="dhl_de", eta=NOW + 600), NOW))

    def test_the_payload_is_the_quiet_live_card_and_the_countdown_only_comes_with_an_eta(self):
        payload = lu.notification_payload(parcel_card(eta=NOW + 1800), NOW)
        data = payload["data"]
        self.assertEqual(payload["title"], "PostNord")
        self.assertEqual((data["tag"], data["channel"], data["importance"], data["critical_text"]),
                         (POSTNORD, "Live updates", "low", "30 min"))
        self.assertEqual((data["when"], data["chronometer"], data["when_relative"], data["live_update"]), (1800, True, True, True))
        self.assertEqual((data["clickAction"], data["url"]), ("app://se.postnord.private",) * 2)
        self.assertEqual(data["notification_icon"], "mdi:package-variant-closed")
        self.assertNotIn("when", lu.notification_payload(parcel_card(start=NOW + 600, end=NOW + 3600), NOW)["data"])


class SelectionTests(unittest.TestCase):
    def test_one_card_per_source_for_its_first_active_delivery(self):
        sensor_state = sensor(entry(title="Pizza Test", eta=NOW + 300), entry("nemlig", eta=NOW + 900),
                              entry(title=KITCHEN, eta=NOW + 900), entry(title="Done", stage="delivered", eta=None))
        cards = lu.delivery_cards(sensor_state, NOW)
        self.assertEqual(set(cards), {WOLT, NEMLIG})
        self.assertEqual(cards[WOLT].title, "Wolt · Pizza Test")

    def test_unavailable_or_malformed_sensors_have_no_cards(self):
        for value in (None, full("unavailable"), full("0", {"deliveries": "x"}), full("1", {"deliveries": [None, 3, {}]})):
            with self.subTest(value=value):
                self.assertEqual(lu.delivery_cards(value, NOW), {})
        self.assertEqual(lu.cancelled_deliveries(full("unknown", {"deliveries": [entry(stage="cancelled")]})), [])

    def test_a_carrier_has_one_card_for_its_first_parcel_that_is_out(self):
        state = sensor(parcel_entry(stage="preparing", start=NOW + 600, end=NOW + 3600, barcode="TEST0001"),
                       parcel_entry(eta=NOW + 1200, barcode="TEST0002"), parcel_entry(eta=NOW + 2400, barcode="TEST0003"),
                       parcel_entry("airmee", start=NOW + 3600, end=NOW + 7200), parcel_entry("gls", stage="preparing"),
                       entry(title="Pizza Test", eta=NOW + 300))
        cards = lu.delivery_cards(state, NOW)
        self.assertEqual(set(cards), {WOLT, POSTNORD, AIRMEE})
        self.assertEqual((cards[POSTNORD].message, cards[AIRMEE].message), ("Out for delivery · 13:20", "Out for delivery · 14–15"))

    def test_parcels_are_never_cancelled_announcements(self):
        self.assertEqual(lu.cancelled_deliveries(sensor(parcel_entry(stage="delivered"), parcel_entry("airmee", stage="on_the_way"))), [])

    def test_cancelled_deliveries_are_listed_with_their_card_title(self):
        state = sensor(entry(stage="cancelled"), entry(title="Pizza Test"), entry("nemlig", stage="delivered"))
        self.assertEqual(lu.cancelled_deliveries(state), [("wolt:test_kitchen", f"Wolt · {KITCHEN}", "food")])
        self.assertEqual(lu.cancelled_deliveries(sensor(entry("nemlig", stage="cancelled"))),
                         [("nemlig:test_kitchen", "Nemlig", "groceries")])


class DeliveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"

    async def make(self, **kwargs):
        return await Harness(self.path, **kwargs).initialize()

    async def test_a_wolt_order_gets_its_card_on_mikkels_phone_and_only_there_until_kristine_is_enabled(self):
        h = await self.make(delivery_food_people=["mikkel", "kristine"], states={ENTITIES["delivery"]: sensor(entry())})
        [(service, payload)] = h.ongoing(WOLT)
        self.assertEqual(service, MIKKEL)
        self.assertEqual(payload["data"]["when"], 480)
        self.assertEqual(h.ongoing(NEMLIG), [])

    async def test_an_enabled_kristine_gets_the_same_card_but_only_when_listed(self):
        state = {ENTITIES["delivery"]: sensor(entry())}
        h = await self.make(enabled=["mikkel", "kristine"], delivery_food_people=["mikkel", "kristine"], states=state)
        self.assertEqual({s for s, _ in h.ongoing(WOLT)}, {MIKKEL, KRISTINE})
        h = await Harness(Path(self.temp.name) / "other.json", enabled=["mikkel", "kristine"], states=state).initialize()
        self.assertEqual([s for s, _ in h.ongoing(WOLT)], [MIKKEL])

    async def test_each_source_has_its_own_card(self):
        h = await self.make(states={ENTITIES["delivery"]: sensor(entry(eta=NOW + 300), entry("nemlig", eta=NOW + 1200))})
        self.assertEqual(len(h.ongoing(WOLT)), 1)
        self.assertEqual(len(h.ongoing(NEMLIG)), 1)
        self.assertEqual(h.ongoing(NEMLIG)[0][1]["data"]["clickAction"], "app://com.nemlig")

    async def test_the_card_follows_the_delivery_and_clears_on_delivered_without_a_push(self):
        h = await self.make(states={ENTITIES["delivery"]: sensor(entry(stage="preparing", eta=NOW + 900))})
        self.assertEqual(h.ongoing(WOLT)[0][1]["message"], "Preparing · 13:15")
        h.forget_calls()
        h.states[ENTITIES["delivery"]] = sensor(entry(stage="on_the_way", eta=NOW + 840))
        await h.reconcile(60)
        [(_, payload)] = h.ongoing(WOLT)
        self.assertEqual((payload["message"], payload["data"]["critical_text"]), ("On the way · 13:14", "13 min"))
        h.forget_calls()
        h.states[ENTITIES["delivery"]] = sensor(entry(stage="delivered", eta=None))
        await h.reconcile(60)
        self.assertEqual(len(h.clears(WOLT)), 1)
        self.assertEqual(h.ongoing(WOLT), [])
        h.notify.assert_not_awaited()

    async def test_a_cancelled_order_clears_its_card_and_pushes_once(self):
        h = await self.make(delivery_food_people=["mikkel", "kristine"],
                            states={ENTITIES["delivery"]: sensor(entry(stage="preparing"))})
        h.forget_calls()
        h.states[ENTITIES["delivery"]] = sensor(entry(stage="cancelled", eta=None))
        await h.reconcile(60)
        self.assertEqual(len(h.clears(WOLT)), 1)
        h.notify.assert_awaited_once_with(title=f"Wolt · {KITCHEN}", message="Order cancelled", target=["mikkel"],
                                          category="delivery", data={"tag": "delivery_end"})
        await h.reconcile(60)
        await h.reconcile(600)
        self.assertEqual(h.notify.await_count, 1)

    async def test_the_cancel_push_follows_the_card_audience(self):
        h = await self.make(enabled=["mikkel", "kristine"], delivery_food_people=["kristine"],
                            states={ENTITIES["delivery"]: sensor(entry(stage="cancelled", eta=None))})
        self.assertEqual(h.notify.await_args.kwargs["target"], ["kristine"])
        h = await Harness(Path(self.temp.name) / "none.json", enabled=["mikkel"], delivery_food_people=["kristine"],
                          states={ENTITIES["delivery"]: sensor(entry(stage="cancelled", eta=None))}).initialize()
        h.notify.assert_not_awaited()

    async def test_a_cancel_push_that_reached_nobody_is_retried_once_a_minute(self):
        h = Harness(self.path, states={ENTITIES["delivery"]: sensor(entry(stage="cancelled", eta=None))})
        h.notify.return_value = 0
        await h.initialize()
        self.assertEqual(h.notify.await_count, 1)
        await h.reconcile(30)
        self.assertEqual(h.notify.await_count, 1)
        h.notify.return_value = 1
        await h.reconcile(30)
        self.assertEqual(h.notify.await_count, 2)
        await h.reconcile(120)
        self.assertEqual(h.notify.await_count, 2)

    async def test_a_reload_does_not_repeat_the_cancel_push(self):
        state = {ENTITIES["delivery"]: sensor(entry(stage="cancelled", eta=None))}
        await Harness(self.path, states=state).initialize()
        restored = await Harness(self.path, states=state, now=NOW + 30).initialize()
        restored.notify.assert_not_awaited()

    async def test_another_cancelled_order_pushes_again(self):
        h = await self.make(states={ENTITIES["delivery"]: sensor(entry(stage="cancelled", eta=None))})
        h.states[ENTITIES["delivery"]] = sensor(entry(stage="cancelled", eta=None),
                                                entry(title="Pizza Test", stage="cancelled", eta=None))
        await h.reconcile(5)
        self.assertEqual([c.kwargs["title"] for c in h.notify.await_args_list], [f"Wolt · {KITCHEN}", "Wolt · Pizza Test"])

    async def test_old_cancel_records_are_forgotten(self):
        h = await self.make(states={ENTITIES["delivery"]: sensor(entry(stage="cancelled", eta=None))})
        self.assertIn("wolt:test_kitchen", h.app._state["delivery_ends"])
        h.states[ENTITIES["delivery"]] = sensor()
        await h.reconcile(86401)
        self.assertEqual(h.app._state["delivery_ends"], {})

    async def test_a_parcel_gets_a_card_on_mikkels_phone_and_never_a_push(self):
        h = await self.make(states={ENTITIES["delivery"]: sensor(parcel_entry(eta=NOW + 1800))})
        [(service, payload)] = h.ongoing(POSTNORD)
        self.assertEqual((service, payload["title"], payload["message"]), (MIKKEL, "PostNord", "Out for delivery · 13:30"))
        self.assertEqual(payload["data"]["when"], 1800)
        self.assertEqual((h.ongoing(WOLT), h.ongoing(AIRMEE), h.ongoing(GLS)), ([], [], []))
        h.notify.assert_not_awaited()

    async def test_each_carrier_and_wolt_have_their_own_card(self):
        states = {ENTITIES["delivery"]: sensor(entry(eta=NOW + 300), parcel_entry("airmee", start=NOW + 3600, end=NOW + 7200),
                                               parcel_entry("postnord", eta=NOW + 900), parcel_entry("gls", stage="arriving"))}
        h = await self.make(states=states)
        self.assertEqual([len(h.ongoing(tag)) for tag in (WOLT, AIRMEE, POSTNORD, GLS, NEMLIG)], [1, 1, 1, 1, 0])
        self.assertEqual(h.ongoing(AIRMEE)[0][1]["data"]["clickAction"], "app://com.airmee.customer")
        self.assertEqual(h.ongoing(GLS)[0][1]["message"], "Arriving")

    async def test_a_parcel_that_is_only_expected_today_has_no_card_until_the_courier_is_out(self):
        h = await self.make(states={ENTITIES["delivery"]: sensor(parcel_entry(stage="preparing", start=NOW + 3600, end=NOW + 10800))})
        self.assertEqual(h.ongoing(POSTNORD), [])
        h.states[ENTITIES["delivery"]] = sensor(parcel_entry(stage="on_the_way", start=NOW + 3600, end=NOW + 10800))
        await h.reconcile(60)
        self.assertEqual(h.ongoing(POSTNORD)[0][1]["message"], "Out for delivery · 14–16")

    async def test_the_parcel_card_follows_the_parcel_and_clears_on_delivered_without_a_push(self):
        h = await self.make(states={ENTITIES["delivery"]: sensor(parcel_entry(eta=NOW + 1800))})
        h.forget_calls()
        h.states[ENTITIES["delivery"]] = sensor(parcel_entry(stage="arriving", eta=NOW + 540))
        await h.reconcile(60)
        [(_, payload)] = h.ongoing(POSTNORD)
        self.assertEqual((payload["message"], payload["data"]["critical_text"]), ("Arriving · 13:09", "8 min"))
        h.forget_calls()
        h.states[ENTITIES["delivery"]] = sensor(parcel_entry(stage="delivered", eta=None))
        await h.reconcile(60)
        self.assertEqual(len(h.clears(POSTNORD)), 1)
        self.assertEqual(h.ongoing(POSTNORD), [])
        h.notify.assert_not_awaited()

    async def test_a_reload_clears_every_parcel_tag_on_each_enabled_phone(self):
        h = await self.make()
        for tag in (AIRMEE, POSTNORD, GLS):
            self.assertEqual([service for service, _ in h.clears(tag)], [MIKKEL])

    async def test_an_unavailable_sensor_holds_the_card_then_clears_it(self):
        h = await self.make(stale_limit_s=90, states={ENTITIES["delivery"]: sensor(entry())})
        h.forget_calls()
        h.states[ENTITIES["delivery"]] = full("unavailable")
        await h.reconcile(1)
        await h.reconcile(89)
        self.assertEqual(h.calls, [])
        await h.reconcile(1)
        self.assertEqual(len(h.clears(WOLT)), 1)
        h.notify.assert_not_awaited()

    async def test_the_eta_jittering_inside_a_minute_does_not_resend(self):
        h = await self.make(states={ENTITIES["delivery"]: sensor(entry(eta=NOW + 480))})
        h.forget_calls()
        h.states[ENTITIES["delivery"]] = sensor(entry(eta=NOW + 500))
        await h.reconcile(60)
        self.assertEqual(h.ongoing(WOLT), [])
        h.states[ENTITIES["delivery"]] = sensor(entry(eta=NOW + 720))
        await h.reconcile(60)
        self.assertEqual(len(h.ongoing(WOLT)), 1)


class AudienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def path(self, name):
        return Path(self.temp.name) / f"{name}.json"

    def test_the_shipped_audiences_are_food_to_mikkel_and_kristine_and_the_rest_to_mikkel(self):
        self.assertEqual(shipped_audiences(), {"delivery_food_people": ["mikkel", "kristine"],
                                               "delivery_groceries_people": ["mikkel"],
                                               "delivery_parcel_people": ["mikkel"]})

    async def test_enabling_kristine_gives_her_wolt_cards_but_never_nemlig_or_parcel_cards(self):
        everything = sensor(entry(eta=NOW + 300), entry("nemlig", eta=NOW + 1200), parcel_entry("airmee", eta=NOW + 900),
                            parcel_entry("postnord", eta=NOW + 900), parcel_entry("gls", stage="arriving"))
        h = await Harness(self.path("both"), enabled=["mikkel", "kristine"], states={ENTITIES["delivery"]: everything},
                          **shipped_audiences()).initialize()
        reached = {}
        for service, payload in h.ongoing():
            reached.setdefault(service, set()).add(payload["data"]["tag"])
        self.assertEqual(reached, {MIKKEL: {WOLT, NEMLIG, AIRMEE, POSTNORD, GLS}, KRISTINE: {WOLT}})

    async def test_while_kristine_is_not_enabled_only_mikkel_gets_any_card(self):
        everything = sensor(entry(eta=NOW + 300), entry("nemlig", eta=NOW + 1200), parcel_entry(eta=NOW + 900))
        h = await Harness(self.path("one"), states={ENTITIES["delivery"]: everything}, **shipped_audiences()).initialize()
        self.assertEqual({service for service, _ in h.ongoing()}, {MIKKEL})

    async def test_every_kind_has_its_own_audience(self):
        everything = sensor(entry(eta=NOW + 300), entry("nemlig", eta=NOW + 1200), parcel_entry(eta=NOW + 900))
        h = await Harness(self.path("kinds"), enabled=["mikkel", "kristine"], states={ENTITIES["delivery"]: everything},
                          delivery_food_people=["mikkel"], delivery_groceries_people=["kristine"],
                          delivery_parcel_people=[]).initialize()
        self.assertEqual({tag: {s for s, p in h.ongoing(tag)} for tag in (WOLT, NEMLIG, POSTNORD)},
                         {WOLT: {MIKKEL}, NEMLIG: {KRISTINE}, POSTNORD: set()})

    async def test_the_shipped_cancel_push_reaches_kristine_only_for_wolt(self):
        for order, target in ((entry(stage="cancelled", eta=None), ["mikkel", "kristine"]),
                              (entry("nemlig", stage="cancelled", eta=None), ["mikkel"])):
            with self.subTest(source=order["source"]):
                h = await Harness(self.path(order["source"]), enabled=["mikkel", "kristine"],
                                  states={ENTITIES["delivery"]: sensor(order)}, **shipped_audiences()).initialize()
                self.assertEqual(h.notify.await_args.kwargs["target"], target)


class MatchedRingTests(unittest.TestCase):
    def rings(self, previous=None, delivery=None, doorbell=None, now=NOW):
        return lu.matched_rings(previous, doorbell or visit(), expecting(entry()) if delivery is None else delivery, now, 180)

    def test_a_street_ring_is_matched_to_the_sensors_door_courier(self):
        self.assertEqual(self.rings(), {str(int(NOW)): "wolt:test_kitchen"})
        self.assertEqual(self.rings(doorbell=visit(door="back door")), {str(int(NOW)): "wolt:test_kitchen"})

    def test_a_street_ring_is_matched_to_whatever_delivery_the_sensor_names(self):
        for named in (entry("nemlig"), parcel_entry("gls", barcode="B1")):
            with self.subTest(id=named["id"]):
                state = with_courier(sensor(entry(), named), named)
                self.assertEqual(self.rings(delivery=state), {str(int(NOW)): named["id"]})

    def test_the_apartment_door_never_matches_by_itself(self):
        self.assertEqual(self.rings(doorbell=visit(door="apartment door")), {})

    def test_nothing_matches_without_a_door_courier(self):
        named = entry()
        cases = (sensor(named), expecting(named, due=False), full("unavailable", {"door_courier": named}),
                 full("1", {"door_courier": named["id"]}), full("1", {"door_courier": [named]}),
                 full("1", {"door_courier": {"id": 5}}), full("1", {"door_courier": {"title": "no id"}}),
                 full("1", {"deliveries": [named], "next_food": named, "food_courier_expected": True,
                            "door_courier_kind": "food"}))
        for delivery in cases:
            with self.subTest(delivery=delivery):
                self.assertEqual(self.rings(delivery=delivery), {})
        self.assertEqual(lu.matched_rings({}, visit(), None, NOW, 180), {})

    def test_only_a_fresh_ring_is_matched(self):
        self.assertEqual(self.rings(now=NOW + 179), {str(int(NOW)): "wolt:test_kitchen"})
        self.assertEqual(self.rings(now=NOW + 180), {})

    def test_a_match_stays_with_its_first_order_when_the_sensor_moves_on(self):
        first = self.rings()
        self.assertEqual(self.rings(previous=first, delivery=expecting(entry(title="Pizza Test")), now=NOW + 30), first)

    def test_a_match_is_forgotten_after_an_hour_and_damaged_ones_are_dropped(self):
        old = {str(int(NOW)): "wolt:a"}
        self.assertEqual(self.rings(previous=old, delivery=sensor(), now=NOW + 3599), old)
        self.assertEqual(self.rings(previous=old, delivery=sensor(), now=NOW + 3600), {})
        for bad in (None, [], "x", {"x": "wolt:a"}, {str(int(NOW)): None}, {"nan": "wolt:a"}, {"inf": "wolt:a"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.rings(previous=bad, delivery=sensor()), {})

    def test_an_apartment_ring_matches_up_to_ten_minutes_after_a_matched_street_ring(self):
        rings = {str(int(NOW)): "wolt:a"}
        for offset, matched in ((0, "wolt:a"), (600, "wolt:a"), (601, None), (-1, None)):
            with self.subTest(offset=offset):
                self.assertEqual(lu.apartment_ring_matched(rings, NOW + offset), matched)
        self.assertIsNone(lu.apartment_ring_matched(rings, None))
        later = {str(int(NOW)): "wolt:a", str(int(NOW + 60)): "gls:B"}
        self.assertEqual(lu.apartment_ring_matched(later, NOW + 90), "gls:B")

    def test_the_apartment_card_names_the_courier_of_the_matched_delivery(self):
        ring = visit(door="apartment door")
        [plain] = lu.doorbell_cards(ring, NOW, 180).values()
        for order, text in (("gls:B1", "Probably the GLS courier"), ("nemlig:1", "Probably the Nemlig courier"),
                            ("wolt:a", COURIER), ("", "Probably the courier"), ("x:1", "Probably the courier")):
            with self.subTest(order=order):
                [card] = lu.doorbell_cards(ring, NOW, 180, {str(int(NOW)): order}).values()
                self.assertEqual(card.message, text)
                self.assertEqual((card.tag, card.title, card.critical_text, card.link, card.icon),
                                 (plain.tag, plain.title, plain.critical_text, plain.link, plain.icon))
        self.assertFalse(lu.apartment_ring_matched({}, NOW))


class CourierHandoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"

    async def make(self, **kwargs):
        return await Harness(self.path, **kwargs).initialize()

    async def test_a_street_ring_while_food_is_due_sends_no_card_and_takes_the_wolt_card_away(self):
        for index, door in enumerate(("front door", "back door")):
            with self.subTest(door=door):
                h = await Harness(Path(self.temp.name) / f"street{index}.json", states={
                    ENTITIES["delivery"]: expecting(entry()), ENTITIES["doorbell"]: visit(door=door)}).initialize()
                self.assertEqual(h.ongoing(), [])
                self.assertEqual(h.app._state["courier_rings"], {str(int(NOW)): "wolt:test_kitchen"})

    async def test_a_street_ring_with_no_door_courier_matches_nothing_and_leaves_the_wolt_card(self):
        cases = {"no order": (sensor(), 0), "food not due": (expecting(entry(eta=NOW + 3600), due=False), 1),
                 "sensor names none": (sensor(entry(eta=NOW + 600)), 1), "sensor unavailable": (full("unavailable"), 0)}
        for index, (name, (delivery, wolt_cards)) in enumerate(cases.items()):
            with self.subTest(name):
                h = await Harness(Path(self.temp.name) / f"none{index}.json",
                                  states={ENTITIES["delivery"]: delivery, ENTITIES["doorbell"]: visit()}).initialize()
                self.assertEqual(len(h.ongoing(WOLT)), wolt_cards)
                self.assertEqual(h.app._state["courier_rings"], {})

    async def test_the_match_sticks_after_the_order_is_delivered(self):
        h = await self.make(states={ENTITIES["delivery"]: expecting(entry()), ENTITIES["doorbell"]: visit()})
        h.forget_calls()
        h.states[ENTITIES["delivery"]] = sensor(entry(stage="delivered", eta=None))
        h.states[YALE_DING] = full("on", at=NOW + 90)
        await h.reconcile(90)
        [(_, payload)] = h.ongoing(APARTMENT)
        self.assertEqual(payload["message"], COURIER)

    async def test_a_reload_keeps_the_match_and_the_hand_over(self):
        h = await self.make(states={ENTITIES["delivery"]: expecting(entry(eta=NOW + 480)), ENTITIES["doorbell"]: visit()})
        self.assertEqual(json.loads(self.path.read_text())["courier_rings"], {str(int(NOW)): "wolt:test_kitchen"})
        states = {**h.states, ENTITIES["delivery"]: sensor(entry(eta=NOW + 480))}
        restored = await Harness(self.path, states=states, now=NOW + 30).initialize()
        self.assertEqual(restored.ongoing(), [])
        await restored.reconcile(60)
        self.assertEqual(restored.ongoing(), [])
        restored.states[YALE_DING] = full("on", at=restored.now)
        await restored.reconcile()
        self.assertEqual([payload["message"] for _, payload in restored.ongoing(APARTMENT)], [COURIER])

    async def test_the_first_matched_ring_takes_that_orders_wolt_card_away_for_good(self):
        h = await self.make(states={ENTITIES["delivery"]: sensor(entry(eta=NOW + 480))})
        self.assertEqual(len(h.ongoing(WOLT)), 1)
        h.forget_calls()
        h.states[ENTITIES["delivery"]] = expecting(entry(eta=NOW + 480))
        h.states[ENTITIES["doorbell"]] = visit(at=NOW + 60)
        await h.reconcile(60)
        self.assertEqual(len(h.clears(WOLT)), 1)
        self.assertEqual(h.ongoing(), [])
        h.forget_calls()
        for seconds in (10, 60, 300, 1800):
            await h.reconcile(seconds)
        self.assertEqual((h.ongoing(WOLT), h.clears(WOLT)), ([], []))
        restored = await Harness(self.path, states=h.states, now=h.now).initialize()
        self.assertEqual(restored.ongoing(WOLT), [])

    async def test_another_wolt_order_keeps_the_wolt_card_after_the_hand_over(self):
        pizza, other = entry(title="Pizza Test", eta=NOW + 300), entry(title=KITCHEN, eta=NOW + 1500)
        h = await self.make(states={ENTITIES["delivery"]: sensor(pizza, other)})
        self.assertEqual(h.ongoing(WOLT)[0][1]["title"], "Wolt · Pizza Test")
        h.forget_calls()
        h.states[ENTITIES["delivery"]] = expecting(pizza, other)
        h.states[ENTITIES["doorbell"]] = visit(at=NOW + 60)
        await h.reconcile(60)
        [(_, payload)] = h.ongoing(WOLT)
        self.assertEqual(payload["title"], f"Wolt · {KITCHEN}")
        self.assertEqual(h.clears(WOLT), [])

    async def test_a_ring_while_a_parcel_is_out_takes_its_card_away_for_good(self):
        parcel = parcel_entry("gls", barcode="043312860948", title="ComputerSalg A/S")
        h = await self.make(states={ENTITIES["delivery"]: with_courier(sensor(parcel), parcel)})
        self.assertEqual(h.ongoing(GLS)[0][1]["message"], "Out for delivery")
        h.forget_calls()
        h.states[ENTITIES["doorbell"]] = visit(at=NOW + 60)
        await h.reconcile(60)
        self.assertEqual(len(h.clears(GLS)), 1)
        self.assertEqual(h.ongoing(), [])
        h.forget_calls()
        await h.reconcile(600)
        self.assertEqual(h.ongoing(GLS), [])

    async def test_an_apartment_ring_soon_after_a_matched_parcel_ring_names_the_carrier(self):
        parcel = parcel_entry("gls", barcode="043312860948")
        h = await self.make(states={ENTITIES["delivery"]: with_courier(sensor(parcel), parcel),
                                    ENTITIES["doorbell"]: visit()})
        h.forget_calls()
        h.states[YALE_DING] = full("on", at=NOW + 90)
        await h.reconcile(90)
        [(service, payload)] = h.ongoing(APARTMENT)
        self.assertEqual((service, payload["message"]), (MIKKEL, "Probably the GLS courier"))

    async def test_a_food_ring_leaves_nemlig_and_parcel_cards_alone(self):
        state = expecting(entry(eta=NOW + 300), entry("nemlig", eta=NOW + 1200), parcel_entry(eta=NOW + 900))
        h = await self.make(states={ENTITIES["delivery"]: state, ENTITIES["doorbell"]: visit()})
        self.assertEqual({payload["data"]["tag"] for _, payload in h.ongoing()}, {NEMLIG, POSTNORD})

    async def test_an_apartment_ring_soon_after_a_matched_street_ring_is_the_same_courier(self):
        h = await self.make(states={ENTITIES["delivery"]: expecting(entry()), ENTITIES["doorbell"]: visit()})
        h.forget_calls()
        h.states[YALE_DING] = full("on", at=NOW + 90)
        await h.reconcile(90)
        [(service, payload)] = h.ongoing(APARTMENT)
        self.assertEqual((service, payload["title"], payload["message"]), (MIKKEL, "Doorbell · apartment door", COURIER))

    async def test_an_apartment_ring_with_no_matched_street_ring_before_it_keeps_the_usual_wording(self):
        cases = {"no street ring": (expecting(entry()), None, 0), "street ring unmatched": (sensor(entry()), visit(), 90),
                 "ten minutes and a second later": (expecting(entry()), visit(), 601)}
        for name, (delivery, street, later) in cases.items():
            with self.subTest(name):
                states = {ENTITIES["delivery"]: delivery}
                if street:
                    states[ENTITIES["doorbell"]] = street
                h = await Harness(Path(self.temp.name) / f"{name}.json", states=states).initialize()
                h.states[YALE_DING] = full("on", at=NOW + later)
                await h.reconcile(later)
                [(_, payload)] = h.ongoing(APARTMENT)
                self.assertEqual(payload["message"], "Someone is at the door")

    async def test_damaged_saved_matches_are_dropped_without_a_crash(self):
        self.path.write_text(json.dumps({"version": 1, "courier_rings": {
            "x": "wolt:a", str(int(NOW) - 4000): "wolt:old", str(int(NOW)): 5, str(int(NOW) - 10): "wolt:kept"}}))
        h = await self.make()
        self.assertEqual(h.app._state["courier_rings"], {str(int(NOW) - 10): "wolt:kept"})
        other = Path(self.temp.name) / "other.json"
        other.write_text(json.dumps({"version": 1, "courier_rings": ["x"]}))
        h = await Harness(other).initialize()
        self.assertEqual(h.app._state["courier_rings"], {})


if __name__ == "__main__":
    unittest.main()
