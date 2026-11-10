"""Delivery cards read sensor.home_delivery only: one card per source, cleared on delivery, one push on a cancel."""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_live_updates import ENTITIES, HOUSE, NOW, TAGS, Harness, full  # noqa: E402

import live_updates as lu  # noqa: E402

WOLT, NEMLIG = TAGS["wolt"], TAGS["nemlig"]
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


def sensor(*entries):
    active = [e for e in entries if e["stage"] in ("preparing", "on_the_way", "arriving")]
    return full(str(len(active)), {"deliveries": list(entries), "next": active[0] if active else None})


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
        self.assertIsNone(lu.delivery_card(entry(source="airmee"), NOW))
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

    def test_cancelled_deliveries_are_listed_with_their_card_title(self):
        state = sensor(entry(stage="cancelled"), entry(title="Pizza Test"), entry("nemlig", stage="delivered"))
        self.assertEqual(lu.cancelled_deliveries(state), [("wolt:test_kitchen", f"Wolt · {KITCHEN}")])


class DeliveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"

    async def make(self, **kwargs):
        return await Harness(self.path, **kwargs).initialize()

    async def test_a_wolt_order_gets_its_card_on_mikkels_phone_and_only_there_until_kristine_is_enabled(self):
        h = await self.make(delivery_people=["mikkel", "kristine"], states={ENTITIES["delivery"]: sensor(entry())})
        [(service, payload)] = h.ongoing(WOLT)
        self.assertEqual(service, MIKKEL)
        self.assertEqual(payload["data"]["when"], 480)
        self.assertEqual(h.ongoing(NEMLIG), [])

    async def test_an_enabled_kristine_gets_the_same_card_but_only_when_listed(self):
        state = {ENTITIES["delivery"]: sensor(entry())}
        h = await self.make(enabled=["mikkel", "kristine"], delivery_people=["mikkel", "kristine"], states=state)
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
        h = await self.make(delivery_people=["mikkel", "kristine"],
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
        h = await self.make(enabled=["mikkel", "kristine"], delivery_people=["kristine"],
                            states={ENTITIES["delivery"]: sensor(entry(stage="cancelled", eta=None))})
        self.assertEqual(h.notify.await_args.kwargs["target"], ["kristine"])
        h = await Harness(Path(self.temp.name) / "none.json", enabled=["mikkel"], delivery_people=["kristine"],
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


if __name__ == "__main__":
    unittest.main()
