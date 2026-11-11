"""Live cards use the published states, fixed audiences and a clock controlled by the tests."""

import copy
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
        sys.modules[name] = types.ModuleType(name)
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import live_updates as lu

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp()
HOUSE = ZoneInfo("Europe/Copenhagen")
MOBILE = {
    "mikkel": ["notify.mobile_app_mikkels_ofx9p"],
    "kristine": ["notify.mobile_app_kristine_iphone_2"],
    "claudia": ["notify.mobile_app_claudias_iphone"],
}
ENTITIES = {
    "washer": "sensor.washer_state",
    "dryer": "sensor.dryer_state",
    "cook": "sensor.cook_state",
    "coffee": "sensor.coffee_state",
    "doorbell": "sensor.intercom_visit",
    "apartment": "binary_sensor.yale_doorbell_ding",
    "delivery": "sensor.home_delivery",
}
TAGS = {
    "washer": "live_washer", "dryer": "live_dryer", "bath": "live_cook_bath",
    "probe": "live_cook_probe", "coffee": "live_coffee", "apartment": "live_doorbell_apartment",
    "wolt": "live_delivery_wolt", "nemlig": "live_delivery_nemlig",
    "airmee": "live_delivery_airmee", "postnord": "live_delivery_postnord", "gls": "live_delivery_gls",
}
STREET_TAGS = ("live_doorbell_front", "live_doorbell_back")


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def full(state, attributes=None, at=NOW):
    return {"state": state, "attributes": dict(attributes or {}), "last_updated": iso(at),
            "last_changed": iso(at)}


def laundry(source="washer", starter="mikkel", finish=NOW + 720, state="Running", start=NOW):
    end = datetime.fromtimestamp(finish, HOUSE).strftime("%H:%M") if source == "washer" else iso(finish)
    return full(state, {"started_by": starter, "cycle_start_time": iso(start),
                        "estimated_end_time": end, "estimated_remaining_min": (finish - NOW) / 60})


def bath(finish=NOW + 720, phase="cooking", temp="57.5"):
    return full("bath", {"bath_phase": phase, "probe_phase": "off", "bath_done_at": iso(finish),
                         "bath_ready_at": iso(finish), "bath_temp": temp, "bath_target": "58"})


def probe(finish=NOW + 720, phase="tracking", core="43.2"):
    return full("probe", {"bath_phase": "off", "probe_phase": phase, "probe_eta": iso(finish),
                          "probe_core": core, "probe_pull": "50", "probe_carve_at": iso(finish)})


def coffee(finish=NOW + 720, phase="brewing"):
    return full(phase, {"brew_id": "c123", "recipe_name": "Morning", "eta": iso(finish),
                        "machine": "working", "pour": "1", "pours_total": "3"})


def visit(at=NOW, door="front door"):
    return full("active", {"visits": [{"door": door, "ring_at": iso(at)}]})


class Harness:
    def __init__(self, path, enabled=None, states=None, mapping=None, now=NOW, **args):
        self.now = now
        self.path = path
        self.mapping = copy.deepcopy(mapping or MOBILE)
        self.states = {
            ENTITIES["washer"]: full("Off"), ENTITIES["dryer"]: full("Off"),
            ENTITIES["cook"]: full("off", {"bath_phase": "off", "probe_phase": "off"}),
            ENTITIES["coffee"]: full("idle"), ENTITIES["doorbell"]: full("idle", {"visits": []}),
            ENTITIES["delivery"]: full("0", {"deliveries": []}),
            ENTITIES["apartment"]: full("off"),
            "person.mikkel": full("home"), "person.kristine": full("home"),
            "person.claudia": full("not_home"),
        }
        self.states.update(copy.deepcopy(states or {}))
        self.calls = []
        self.fail = set()
        self.notify = AsyncMock(return_value=1)
        self.app = lu.LiveUpdates.__new__(lu.LiveUpdates)
        self.app.args = {"state_file": str(path), **args}
        if enabled is not None:
            self.app.args["enabled_people"] = enabled
        self.app.log = lambda *a, **kw: None
        self.app.get_now = AsyncMock(side_effect=lambda: datetime.fromtimestamp(self.now, timezone.utc))
        self.app.get_app = AsyncMock(return_value=types.SimpleNamespace(device_mapping=self.mapping, notify=self.notify))
        self.app.get_state = AsyncMock(side_effect=self.get_state)
        self.app.listen_state = AsyncMock(return_value="listener")
        self.app.run_every = AsyncMock(return_value="timer")
        self.app.call_service = AsyncMock(side_effect=self.call_service)

    async def get_state(self, entity=None, attribute=None, **kwargs):
        value = self.states.get(entity)
        if attribute == "all":
            return copy.deepcopy(value)
        if isinstance(value, dict):
            return value.get("state") if attribute is None else value.get("attributes", {}).get(attribute)
        return value

    async def call_service(self, service, **kwargs):
        self.calls.append((service, copy.deepcopy(kwargs)))
        if service in self.fail:
            raise RuntimeError("temporary delivery failure")

    async def initialize(self):
        await self.app.initialize()
        return self

    async def reconcile(self, seconds=0):
        self.now += seconds
        await self.app._reconcile()

    def ongoing(self, tag=None):
        return [(service, payload) for service, payload in self.calls
                if payload.get("message") != "clear_notification"
                and (tag is None or payload.get("data", {}).get("tag") == tag)]

    def clears(self, tag=None):
        return [(service, payload) for service, payload in self.calls
                if payload.get("message") == "clear_notification"
                and (tag is None or payload.get("data", {}).get("tag") == tag)]

    def forget_calls(self):
        self.calls.clear()


class LiveUpdateIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"

    async def make(self, **kwargs):
        return await Harness(self.path, **kwargs).initialize()

    async def test_start_payload_for_each_source(self):
        cases = (
            ("washer", laundry(), TAGS["washer"], "Washer", "mdi:washing-machine"),
            ("dryer", laundry("dryer"), TAGS["dryer"], "Dryer", "mdi:tumble-dryer"),
            ("cook", bath(), TAGS["bath"], "Sous vide", "mdi:pot-steam"),
            ("cook", probe(), TAGS["probe"], "Roast", "mdi:food-steak"),
            ("coffee", coffee(), TAGS["coffee"], "Coffee", "mdi:coffee-maker"),
        )
        for index, (source, state, tag, title, icon) in enumerate(cases):
            with self.subTest(source=source, tag=tag):
                harness = await Harness(Path(self.temp.name) / f"start{index}.json",
                                        states={ENTITIES[source]: state}).initialize()
                [(service, payload)] = harness.ongoing(tag)
                self.assertEqual(service, "notify/mobile_app_mikkels_ofx9p")
                self.assertEqual(payload["title"], title)
                data = payload["data"]
                self.assertEqual(data["critical_text"], "12 min")
                self.assertEqual(data["notification_icon"], icon)
                self.assertEqual(data["when"], 720)
                self.assertIs(data["chronometer"], True)
                self.assertIs(data["when_relative"], True)
                self.assertIs(data["live_update"], True)
                self.assertIs(data["sticky"], True)
                self.assertIs(data["alert_once"], True)
                self.assertEqual(data["channel"], "Live updates")
                self.assertEqual(data["importance"], "low")
                # The push relay rejects the whole message when priority is "low".
                self.assertNotIn("priority", data)
                self.assertEqual(data["clickAction"], data["url"])
                self.assertTrue(data["clickAction"].startswith("/dashboard-rooftop/home#"))
                self.assertNotIn("progress", data)

    async def test_eta_jitter_and_natural_countdown_do_not_resend(self):
        fixtures = (("washer", laundry, TAGS["washer"]),
                    ("dryer", lambda **kw: laundry("dryer", **kw), TAGS["dryer"]),
                    ("cook", bath, TAGS["bath"]), ("cook", probe, TAGS["probe"]),
                    ("coffee", coffee, TAGS["coffee"]))
        for index, (source, factory, tag) in enumerate(fixtures):
            with self.subTest(source=source, tag=tag):
                h = await Harness(Path(self.temp.name) / f"eta{index}.json",
                                  states={ENTITIES[source]: factory()}).initialize()
                h.forget_calls()
                await h.reconcile(60)
                self.assertEqual(h.ongoing(tag), [])
                h.states[ENTITIES[source]] = factory(finish=NOW + 779)
                await h.reconcile(60)
                self.assertEqual(h.ongoing(tag), [])
                h.states[ENTITIES[source]] = factory(finish=NOW + 780)
                await h.reconcile()
                self.assertEqual(len(h.ongoing(tag)), 1)

    async def test_pending_cook_temperature_update_uses_latest_text_after_rate_limit(self):
        h = await self.make(states={ENTITIES["cook"]: bath()})
        h.forget_calls()
        h.states[ENTITIES["cook"]] = bath(temp="57.6")
        await h.reconcile(30)
        self.assertEqual(h.ongoing(TAGS["bath"]), [])
        h.states[ENTITIES["cook"]] = bath(temp="57.7")
        await h.reconcile(30)
        [(_, payload)] = h.ongoing(TAGS["bath"])
        self.assertIn("57.7", payload["message"])
        self.assertNotIn("57.6", payload["message"])
        self.assertEqual(payload["data"]["when"], 660)

    async def test_separate_tags_do_not_block_each_other(self):
        h = await self.make(states={ENTITIES["washer"]: laundry()})
        h.forget_calls()
        h.states[ENTITIES["coffee"]] = coffee()
        await h.reconcile(1)
        self.assertEqual(len(h.ongoing(TAGS["coffee"])), 1)
        self.assertEqual(h.ongoing(TAGS["washer"]), [])

    async def test_source_audiences_are_intersected_with_enabled_people(self):
        h = await self.make(enabled=["mikkel", "kristine"], cook_people=["kristine", "claudia"],
                            coffee_people=[], doorbell_people=["claudia"],
                            states={ENTITIES["cook"]: bath(), ENTITIES["coffee"]: coffee(),
                                    ENTITIES["apartment"]: full("on")})
        self.assertEqual([s for s, _ in h.ongoing(TAGS["bath"])], ["notify/mobile_app_kristine_iphone_2"])
        self.assertEqual(h.ongoing(TAGS["coffee"]), [])
        self.assertEqual(h.ongoing(TAGS["apartment"]), [])

    async def test_restart_does_not_resend_unchanged_active_cards_for_each_source(self):
        fixtures = (("washer", laundry(), TAGS["washer"]), ("dryer", laundry("dryer"), TAGS["dryer"]),
                    ("cook", bath(), TAGS["bath"]), ("cook", probe(), TAGS["probe"]),
                    ("coffee", coffee(), TAGS["coffee"]), ("apartment", full("on"), TAGS["apartment"]))
        for index, (source, running, tag) in enumerate(fixtures):
            with self.subTest(source=source, tag=tag):
                path = Path(self.temp.name) / f"restart{index}.json"
                h = await Harness(path, states={ENTITIES[source]: running}).initialize()
                restored = await Harness(path, states=h.states, now=NOW + 10).initialize()
                self.assertEqual(restored.ongoing(tag), [])
                self.assertEqual(restored.clears(tag), [])

    async def test_pause_is_rate_limited_and_removes_countdown(self):
        for source, factory in (("washer", lambda state: laundry(state=state)),
                                ("dryer", lambda state: laundry("dryer", state=state)),
                                ("coffee", lambda state: coffee(phase=state.lower()))):
            with self.subTest(source=source):
                h = await Harness(Path(self.temp.name) / f"pause{source}.json",
                                  states={ENTITIES[source]: factory("Running" if source != "coffee" else "Brewing")}).initialize()
                h.forget_calls()
                h.states[ENTITIES[source]] = factory("Paused")
                await h.reconcile(59)
                self.assertEqual(h.ongoing(TAGS[source]), [])
                await h.reconcile(1)
                [(_, payload)] = h.ongoing(TAGS[source])
                self.assertEqual(payload["data"]["critical_text"], "Pause")
                self.assertNotIn("when", payload["data"])
                self.assertNotIn("chronometer", payload["data"])

    async def test_end_clears_each_source_immediately(self):
        cases = (("washer", laundry(), full("Unemptied"), TAGS["washer"]),
                 ("dryer", laundry("dryer"), full("Emptied"), TAGS["dryer"]),
                 ("cook", bath(), bath(phase="done"), TAGS["bath"]),
                 ("cook", probe(), probe(phase="carve"), TAGS["probe"]),
                 ("coffee", coffee(), coffee(phase="ready"), TAGS["coffee"]))
        for index, (source, running, ended, tag) in enumerate(cases):
            with self.subTest(source=source, tag=tag):
                h = await Harness(Path(self.temp.name) / f"end{index}.json",
                                  states={ENTITIES[source]: running}).initialize()
                h.forget_calls()
                h.states[ENTITIES[source]] = ended
                await h.reconcile(1)
                [(_, payload)] = h.clears(tag)
                self.assertEqual(payload, {"message": "clear_notification", "data": {"tag": tag}})
                self.assertEqual(h.ongoing(tag), [])

    async def test_starter_routes_to_iphone_without_presence_requirement(self):
        h = await self.make(enabled=["mikkel", "kristine", "claudia"],
                            states={ENTITIES["washer"]: laundry(starter="claudia")})
        [(service, payload)] = h.ongoing(TAGS["washer"])
        self.assertEqual(service, "notify/mobile_app_claudias_iphone")
        self.assertIs(payload["data"]["live_update"], True)
        self.assertEqual(payload["data"]["when"], 720)

    async def test_unattributed_home_audience_is_fixed_until_late_attribution(self):
        h = await self.make(enabled=["mikkel", "kristine", "claudia"],
                            states={ENTITIES["washer"]: laundry(starter=None)})
        self.assertEqual({s for s, _ in h.ongoing(TAGS["washer"])},
                         {"notify/mobile_app_mikkels_ofx9p", "notify/mobile_app_kristine_iphone_2"})
        h.forget_calls()
        h.states["person.mikkel"] = full("not_home")
        h.states["person.claudia"] = full("home")
        await h.reconcile(60)
        self.assertEqual(h.ongoing(TAGS["washer"]), [])
        h.states[ENTITIES["washer"]] = laundry(starter="claudia")
        await h.reconcile()
        self.assertEqual({s for s, _ in h.clears(TAGS["washer"])},
                         {"notify/mobile_app_mikkels_ofx9p", "notify/mobile_app_kristine_iphone_2"})
        self.assertEqual([s for s, _ in h.ongoing(TAGS["washer"])], ["notify/mobile_app_claudias_iphone"])
        h.forget_calls()
        h.states[ENTITIES["washer"]] = laundry(starter=None)
        await h.reconcile(60)
        self.assertEqual(h.ongoing(TAGS["washer"]), [])
        self.assertEqual(h.clears(TAGS["washer"]), [])

    async def test_unattributed_cycle_with_no_enabled_person_home_reaches_nobody(self):
        empty = {f"person.{name}": full("not_home") for name in MOBILE}
        h = await self.make(enabled=["mikkel", "kristine"],
                            states={**empty, ENTITIES["dryer"]: laundry("dryer", starter="")})
        self.assertEqual(h.ongoing(TAGS["dryer"]), [])
        only_kristine = {**empty, "person.kristine": full("home")}
        h = await Harness(Path(self.temp.name) / "kristine.json", enabled=["mikkel"],
                          states={**only_kristine, ENTITIES["washer"]: laundry(starter="")}).initialize()
        self.assertEqual(h.ongoing(TAGS["washer"]), [])

    async def test_disabled_people_receive_neither_cards_nor_startup_clears(self):
        h = await self.make(states={ENTITIES["washer"]: laundry(starter="kristine"),
                                   ENTITIES["dryer"]: laundry("dryer", starter="claudia")})
        self.assertEqual(h.ongoing(), [])
        self.assertTrue(all("mikkels_ofx9p" in service for service, _ in h.calls))
        h = await Harness(Path(self.temp.name) / "nobody.json", enabled=[],
                          states={ENTITIES["washer"]: laundry(), ENTITIES["coffee"]: coffee(),
                                  ENTITIES["cook"]: bath(), ENTITIES["apartment"]: full("on")}).initialize()
        self.assertEqual(h.calls, [])

    async def test_late_attribution_to_disabled_person_clears_initial_audience(self):
        h = await self.make(states={ENTITIES["washer"]: laundry(starter=None)})
        h.forget_calls()
        h.states[ENTITIES["washer"]] = laundry(starter="kristine")
        await h.reconcile(1)
        self.assertEqual(len(h.clears(TAGS["washer"])), 1)
        self.assertEqual(h.ongoing(), [])
        self.assertTrue(all("mikkels_ofx9p" in service for service, _ in h.calls))

    async def test_late_attribution_before_sixty_seconds_creates_on_new_starters_iphone(self):
        h = await self.make(enabled=["mikkel", "kristine", "claudia"],
                            states={ENTITIES["washer"]: laundry(starter=None),
                                    "person.kristine": full("not_home")})
        h.forget_calls()
        h.states[ENTITIES["washer"]] = laundry(starter="kristine")
        await h.reconcile(1)
        self.assertEqual([s for s, _ in h.clears(TAGS["washer"])], ["notify/mobile_app_mikkels_ofx9p"])
        self.assertEqual([s for s, _ in h.ongoing(TAGS["washer"])], ["notify/mobile_app_kristine_iphone_2"])

    async def test_available_unchanged_source_is_not_stale_from_old_last_updated(self):
        state = laundry()
        state["last_updated"] = iso(NOW - 86400)
        h = await self.make(stale_limit_s=30, states={ENTITIES["washer"]: state})
        h.forget_calls()
        await h.reconcile(120)
        self.assertEqual(h.clears(TAGS["washer"]), [])
        self.assertEqual(h.ongoing(TAGS["washer"]), [])

    async def test_ending_one_cook_lane_preserves_the_other(self):
        state = bath()
        state["state"] = "both"
        state["attributes"].update(probe()["attributes"])
        state["attributes"]["bath_phase"] = "cooking"
        h = await self.make(states={ENTITIES["cook"]: state})
        h.forget_calls()
        state["attributes"]["bath_phase"] = "done"
        h.states[ENTITIES["cook"]] = state
        await h.reconcile(1)
        self.assertEqual(len(h.clears(TAGS["bath"])), 1)
        self.assertEqual(h.clears(TAGS["probe"]), [])
        self.assertEqual(h.ongoing(TAGS["probe"]), [])

    async def test_restart_preserves_audience_and_send_throttle(self):
        h = await self.make(enabled=["mikkel", "kristine", "claudia"],
                            states={ENTITIES["washer"]: laundry(starter="")})
        states = copy.deepcopy(h.states)
        states["person.mikkel"] = full("not_home")
        states["person.kristine"] = full("not_home")
        states["person.claudia"] = full("home")
        states[ENTITIES["washer"]] = laundry(finish=NOW + 900, starter="")
        restored = await Harness(self.path, enabled=["mikkel", "kristine", "claudia"],
                                 states=states, now=NOW + 30).initialize()
        self.assertEqual(restored.ongoing(TAGS["washer"]), [])
        restored.forget_calls()
        await restored.reconcile(30)
        self.assertEqual({s for s, _ in restored.ongoing(TAGS["washer"])},
                         {"notify/mobile_app_mikkels_ofx9p", "notify/mobile_app_kristine_iphone_2"})
        self.assertEqual(restored.clears(TAGS["washer"]), [])

    async def test_startup_reconciles_finished_sources_and_clears_stable_tags(self):
        h = await self.make()
        self.assertEqual(h.ongoing(), [])
        self.assertTrue(set(TAGS.values()).issubset({p["data"]["tag"] for _, p in h.clears()}))
        self.assertTrue(all("mikkels_ofx9p" in service for service, _ in h.clears()))

    async def test_unavailable_holds_then_clears_per_source(self):
        fixtures = (("washer", laundry(), TAGS["washer"]), ("dryer", laundry("dryer"), TAGS["dryer"]),
                    ("cook", bath(), TAGS["bath"]), ("coffee", coffee(), TAGS["coffee"]),
                    ("apartment", full("on"), TAGS["apartment"]))
        for index, (source, running, tag) in enumerate(fixtures):
            with self.subTest(source=source):
                h = await Harness(Path(self.temp.name) / f"stale{index}.json", stale_limit_s=90,
                                  states={ENTITIES[source]: running}).initialize()
                h.forget_calls()
                h.states[ENTITIES[source]] = full("unavailable")
                await h.reconcile(1)
                await h.reconcile(89)
                self.assertEqual(h.calls, [])
                await h.reconcile(1)
                self.assertEqual(len(h.clears(tag)), 1)

    async def test_unavailable_deadline_survives_restart_and_recovery(self):
        h = await self.make(stale_limit_s=90, states={ENTITIES["coffee"]: coffee()})
        h.states[ENTITIES["coffee"]] = full("unknown")
        await h.reconcile(10)
        restored = await Harness(self.path, stale_limit_s=90, states=h.states, now=NOW + 80).initialize()
        self.assertEqual(restored.ongoing(TAGS["coffee"]), [])
        self.assertEqual(restored.clears(TAGS["coffee"]), [])
        restored.forget_calls()
        await restored.reconcile(20)
        self.assertEqual(len(restored.clears(TAGS["coffee"])), 1)
        restored.forget_calls()
        restored.states[ENTITIES["coffee"]] = coffee()
        await restored.reconcile(60)
        self.assertEqual(len(restored.ongoing(TAGS["coffee"])), 1)

    async def test_apartment_doorbell_elapsed_card_deep_link_and_max_age(self):
        h = await self.make(states={ENTITIES["apartment"]: full("on")})
        [(_, payload)] = h.ongoing(TAGS["apartment"])
        self.assertIn("apartment door", (payload["title"] + payload["message"]).lower())
        self.assertNotIn("chronometer", payload["data"])
        self.assertNotIn("when", payload["data"])
        self.assertEqual(payload["data"]["clickAction"], f"/dashboard-rooftop/home#now=ring-{int(NOW)}")
        self.assertNotIn("sound", payload["data"])
        h.forget_calls()
        await h.reconcile(59)
        self.assertEqual(h.ongoing(TAGS["apartment"]), [])
        await h.reconcile(1)
        self.assertEqual(len(h.ongoing(TAGS["apartment"])), 1)
        h.forget_calls()
        await h.reconcile(120)
        self.assertEqual(len(h.clears(TAGS["apartment"])), 1)

    async def test_a_street_door_ring_sends_no_card_of_its_own(self):
        both = visit()
        both["attributes"]["visits"].append({"door": "back door", "ring_at": iso(NOW)})
        h = await self.make(states={ENTITIES["doorbell"]: both})
        for seconds in (0, 60, 120):
            await h.reconcile(seconds)
            for tag in STREET_TAGS:
                self.assertEqual(h.ongoing(tag), [])
        self.assertEqual(h.ongoing(), [])

    async def test_cook_lanes_and_the_apartment_door_have_independent_tags_while_street_doors_have_none(self):
        both = bath()
        both["state"] = "both"
        both["attributes"].update(probe()["attributes"])
        both["attributes"]["bath_phase"] = "cooking"
        doors = visit()
        doors["attributes"]["visits"].append({"door": "back door", "ring_at": iso(NOW)})
        h = await self.make(states={ENTITIES["cook"]: both, ENTITIES["doorbell"]: doors,
                                    ENTITIES["apartment"]: full("on")})
        self.assertEqual({p["data"]["tag"] for _, p in h.ongoing()},
                         {TAGS["bath"], TAGS["probe"], TAGS["apartment"]})

    async def test_apartment_ring_pulse_keeps_card_until_max_and_survives_reload(self):
        entity = "binary_sensor.yale_doorbell_ding"
        h = await self.make(states={entity: full("on")})
        self.assertEqual(len(h.ongoing(TAGS["apartment"])), 1)
        h.states[entity] = full("off", at=NOW + 2)
        await h.reconcile(2)
        restored = await Harness(self.path, states=h.states, now=NOW + 30).initialize()
        self.assertEqual(restored.clears(TAGS["apartment"]), [])
        restored.forget_calls()
        await restored.reconcile(150)
        self.assertEqual(len(restored.clears(TAGS["apartment"])), 1)

    async def test_observed_apartment_on_edge_is_kept_when_state_is_already_off(self):
        entity = "binary_sensor.yale_doorbell_ding"
        h = await self.make()
        h.forget_calls()
        h.now += 1
        h.states[entity] = full("off", at=h.now + 1)
        await h.app._on_state(entity, "all", full("off"), full("on", at=h.now), {})
        self.assertEqual(len(h.ongoing(TAGS["apartment"])), 1)

    async def test_cycle_start_correction_does_not_reselect_unattributed_audience(self):
        h = await self.make(enabled=["mikkel", "kristine", "claudia"],
                            states={ENTITIES["washer"]: laundry(starter=None)})
        h.forget_calls()
        h.states["person.mikkel"] = full("not_home")
        h.states["person.kristine"] = full("not_home")
        h.states["person.claudia"] = full("home")
        h.states[ENTITIES["washer"]] = laundry(starter=None, start=NOW - 300)
        await h.reconcile(60)
        self.assertEqual(h.calls, [])
        h.states[ENTITIES["washer"]] = laundry(starter=None, start=NOW - 300, finish=NOW + 900)
        await h.reconcile(60)
        self.assertEqual({service for service, _ in h.ongoing(TAGS["washer"])},
                         {"notify/mobile_app_mikkels_ofx9p", "notify/mobile_app_kristine_iphone_2"})

    async def test_new_laundry_cycle_reselects_home_audience(self):
        h = await self.make(enabled=["mikkel", "kristine", "claudia"],
                            states={ENTITIES["dryer"]: laundry("dryer", starter="mikkel")})
        h.states[ENTITIES["dryer"]] = full("Off")
        await h.reconcile(60)
        h.forget_calls()
        h.states["person.mikkel"] = full("not_home")
        h.states["person.kristine"] = full("not_home")
        h.states["person.claudia"] = full("home")
        h.states[ENTITIES["dryer"]] = laundry("dryer", starter="", start=h.now, finish=h.now + 900)
        await h.reconcile(1)
        self.assertEqual([s for s, _ in h.ongoing(TAGS["dryer"])], ["notify/mobile_app_claudias_iphone"])

    async def test_no_home_fallback_cannot_bypass_enabled_people(self):
        states = {f"person.{name}": full("unknown") for name in MOBILE}
        states[ENTITIES["washer"]] = laundry(starter="")
        h = await self.make(enabled=["kristine"], states=states)
        self.assertEqual(h.ongoing(), [])
        self.assertTrue(all("kristine_iphone" in service for service, _ in h.calls))

    async def test_each_persons_multiple_services_are_read_from_notifier_and_deduplicated(self):
        mapping = {"mikkel": ["notify.custom_android", "notify.custom_iphone", "notify.custom_android"]}
        h = await self.make(mapping=mapping, states={ENTITIES["washer"]: laundry()})
        self.assertEqual([s for s, _ in h.ongoing(TAGS["washer"])],
                         ["notify/custom_android", "notify/custom_iphone"])

    async def test_failed_delivery_retries_without_flooding_other_devices(self):
        h = await self.make(enabled=["mikkel", "kristine"], states={ENTITIES["washer"]: laundry(starter="")})
        h.forget_calls()
        h.fail.add("notify/mobile_app_kristine_iphone_2")
        h.states[ENTITIES["washer"]] = laundry(starter="", finish=NOW + 900)
        await h.reconcile(60)
        h.fail.clear()
        h.forget_calls()
        await h.reconcile(60)
        self.assertEqual([s for s, _ in h.ongoing(TAGS["washer"])], ["notify/mobile_app_kristine_iphone_2"])

    async def test_malformed_sources_and_corrupt_saved_state_do_not_crash(self):
        self.path.write_text("not json", encoding="utf-8")
        malformed = {ENTITIES["washer"]: None, ENTITIES["dryer"]: full("unknown", []),
                     ENTITIES["cook"]: full("both", {"bath_phase": "heating", "bath_temp": "nan",
                                                      "bath_ready_at": "bad", "probe_phase": "tracking",
                                                      "probe_core": "inf"}),
                     ENTITIES["coffee"]: full("brewing", {"eta": "bad"}),
                     ENTITIES["doorbell"]: full("active", {"visits": [None, {"door": "front door", "ring_at": "bad"}]})}
        h = await self.make(states=malformed)
        await h.reconcile(60)
        for _, payload in h.ongoing():
            self.assertNotIn("nan", str(payload).lower())
            self.assertNotIn("inf", str(payload).lower())


class CardDecisionTests(unittest.TestCase):
    def test_washer_clock_date_uses_publish_time_across_midnight(self):
        at = datetime(2026, 11, 10, 23, 55, tzinfo=HOUSE).timestamp()
        state = full("Running", {"estimated_end_time": "00:15", "estimated_remaining_min": "20"}, at=at)
        card = lu.laundry_card("washer", state, at)
        self.assertEqual(card.finish_at, at + 1200)
        self.assertEqual(card.critical_text, "20 min")

    def test_unknown_eta_shows_stage_without_an_invented_timer(self):
        for card in (lu.laundry_card("washer", full("Running"), NOW),
                     lu.coffee_card(full("grinding"), NOW)):
            with self.subTest(tag=card.tag):
                self.assertIsNone(card.finish_at)
                payload = lu.notification_payload(card, NOW)
                self.assertNotIn("when", payload["data"])
                self.assertTrue(payload["data"]["critical_text"])

    def test_terminal_cook_phases_end_cards(self):
        for phase in ("off", "done", "chilled", "interrupted"):
            with self.subTest(phase=phase):
                self.assertEqual(lu.cook_cards(bath(phase=phase), NOW), {})

    def test_malformed_phase_and_door_attributes_are_ignored(self):
        for value in ([], {}, True, 42):
            with self.subTest(value=value):
                state = full("both", {"bath_phase": value, "probe_phase": value})
                self.assertEqual(lu.cook_cards(state, NOW), {})
                state = full("active", {"visits": [{"door": value, "ring_at": iso(NOW)}]})
                self.assertEqual(lu.doorbell_cards(state, NOW), {})

    def test_missing_and_nonfinite_eta_never_create_countdown(self):
        for value in (None, "", "unknown", "nan", "inf", float("nan"), float("inf"), True, {}, []):
            with self.subTest(value=value):
                state = full("brewing", {"eta": value})
                card = lu.coffee_card(state, NOW)
                self.assertIsNone(card.finish_at)
                self.assertNotIn("when", lu.notification_payload(card, NOW)["data"])


if __name__ == "__main__":
    unittest.main()
