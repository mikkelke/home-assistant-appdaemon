# tests/test_mobile_notifier_payload.py - the notify service payload MobileNotifier builds from a
# caller's data: flat keys go under "data" (HA rejects any other top-level key), nested keys pass
# through untouched. The sender-side shapes are driven for real in apps/intercom/tests and
# apps/safety/tests.
# Run from repo root: python3 -m unittest discover -s apps/notify/tests -q

import sys
import types
import unittest
from pathlib import Path

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

import mobile_notifier as mn  # noqa: E402

OPEN_REJECT = [
    {"action": "ABB_RING_OPEN_7", "title": "Open"},
    {"action": "ABB_RING_REJECT_7", "title": "Reject"},
]


def _new_app():
    app = mn.MobileNotifier.__new__(mn.MobileNotifier)
    app.device_mapping = {
        "mikkel": ["notify.mobile_app_mikkels_ofx9p"],
        "kristine": ["notify.mobile_app_kristine_iphone_2"],
    }
    app.category_audience = {}
    app.platform_map = {}
    app.user_notification_service = "notify.mobile_app_mikkels_ofx9p"
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.calls = []

    async def _call_service(service, **kwargs):
        app.calls.append((service, kwargs))

    app.call_service = _call_service
    return app


def _sent(app, needle="mikkels_ofx9p"):
    for service, kwargs in app.calls:
        if needle in service:
            return kwargs
    raise AssertionError(f"No call to {needle!r} in {[s for s, _ in app.calls]}")


class FlatKeysNestedUnderData(unittest.IsolatedAsyncioTestCase):
    async def test_flat_keys_move_under_data(self):
        app = _new_app()
        await app.notify(title="Intercom auto-opened", message="Someone rang.", target=["mikkel"],
                         data={"tag": "abb_autoopen_front", "image": "/api/image_proxy/image.abb?token=t"})
        self.assertEqual(_sent(app), {
            "title": "Intercom auto-opened",
            "message": "Someone rang.",
            "data": {"tag": "abb_autoopen_front", "image": "/api/image_proxy/image.abb?token=t"},
        })

    async def test_flat_actions_arrive_unchanged_under_data(self):
        app = _new_app()
        await app.notify(title="Someone is ringing the front door", message="Auto-open is off - open for them?",
                         target=["mikkel"], data={"tag": "abb_ring_front", "actions": OPEN_REJECT})
        self.assertEqual(_sent(app), {
            "title": "Someone is ringing the front door",
            "message": "Auto-open is off - open for them?",
            "data": {"tag": "abb_ring_front", "actions": OPEN_REJECT},
        })

    async def test_flat_clear_command_keeps_message_and_nests_tag(self):
        app = _new_app()
        await app.notify(title="clear", message="clear_notification", target=["mikkel"],
                         data={"tag": "abb_ring_front"})
        self.assertEqual(_sent(app), {
            "title": "clear",
            "message": "clear_notification",
            "data": {"tag": "abb_ring_front"},
        })

    async def test_title_message_and_target_stay_top_level(self):
        app = _new_app()
        await app.notify(title="t", message="m", target=["mikkel"],
                         data={"target": ["device-token"], "tag": "x"})
        self.assertEqual(_sent(app), {"title": "t", "message": "m", "target": ["device-token"],
                                      "data": {"tag": "x"}})

    async def test_caller_dict_is_not_mutated(self):
        app = _new_app()
        data = {"tag": "x", "data": {"group": "g"}}
        await app.notify(title="t", message="m", target=["mikkel"], data=data)
        self.assertEqual(data, {"tag": "x", "data": {"group": "g"}})


class CallerNestedDataPreserved(unittest.IsolatedAsyncioTestCase):
    async def test_nested_payload_passes_through_untouched(self):
        nested = {
            "actions": [{"action": "WAKEUP_ENABLE", "title": "Enable", "uri": "/dashboard-rooftop/0#room=bedroom"}],
            "clickAction": "/dashboard-rooftop/0#room=bedroom",
            "url": "/dashboard-rooftop/0#room=bedroom",
        }
        app = _new_app()
        await app.notify(title="Wake-up", message="Alarm tomorrow?", target=["mikkel"], data={"data": nested})
        self.assertEqual(_sent(app), {"title": "Wake-up", "message": "Alarm tomorrow?", "data": nested})

    async def test_nested_key_wins_over_the_same_flat_key(self):
        app = _new_app()
        await app.notify(title="t", message="m", target=["mikkel"],
                         data={"tag": "flat", "image": "/img", "data": {"tag": "nested", "group": "g"}})
        self.assertEqual(_sent(app)["data"], {"tag": "nested", "image": "/img", "group": "g"})

    async def test_no_data_sends_no_data_key(self):
        app = _new_app()
        await app.notify(title="t", message="m", target=["mikkel"], data=None)
        self.assertEqual(_sent(app), {"title": "t", "message": "m"})


class FlatKeysWithPerServiceLayers(unittest.IsolatedAsyncioTestCase):
    async def test_flat_keys_survive_critical_defaults(self):
        app = _new_app()
        await app.notify(title="T", message="M", target=["mikkel", "kristine"], critical=True,
                         data={"tag": "fire_alarm"})
        android = _sent(app, "mikkels_ofx9p")["data"]
        ios = _sent(app, "kristine_iphone")["data"]
        self.assertEqual(android["tag"], "fire_alarm")
        self.assertEqual(android["channel"], "Fire alarm")
        self.assertEqual(ios["tag"], "fire_alarm")
        self.assertEqual(ios["push"]["interruption-level"], "critical")

    async def test_per_person_actions_replace_flat_actions_per_service(self):
        app = _new_app()
        await app.notify(title="T", message="M", target=["mikkel", "kristine"],
                         data={"tag": "x", "actions": [{"action": "SHARED"}]},
                         per_person_actions=lambda person: [{"action": f"ACK_{person.upper()}"}])
        self.assertEqual(_sent(app, "mikkels_ofx9p")["data"], {"tag": "x", "actions": [{"action": "ACK_MIKKEL"}]})
        self.assertEqual(_sent(app, "kristine_iphone")["data"], {"tag": "x", "actions": [{"action": "ACK_KRISTINE"}]})


LINK = "/dashboard-rooftop/home#now=washer-1760345730"


def _warnings(app):
    return [str(a[0]) for a, kw in app.log_calls if kw.get("level") == "WARNING"]


class DashboardLink(unittest.IsolatedAsyncioTestCase):
    async def test_link_sets_url_and_click_action(self):
        app = _new_app()
        await app.notify(title="t", message="m", target=["mikkel"], link=LINK)
        self.assertEqual(_sent(app), {"title": "t", "message": "m", "data": {"url": LINK, "clickAction": LINK}})
        self.assertEqual(_warnings(app), [])

    async def test_link_joins_caller_data(self):
        actions = [{"action": "URI", "title": "Other...", "uri": "/dashboard-rooftop/home#room=guest_bathroom"}]
        app = _new_app()
        await app.notify(title="t", message="m", target=["mikkel"],
                         data={"data": {"actions": actions, "tag": "washer_confirm"}}, link=LINK)
        self.assertEqual(_sent(app)["data"], {"actions": actions, "tag": "washer_confirm",
                                              "url": LINK, "clickAction": LINK})

    async def test_caller_url_and_click_action_win(self):
        app = _new_app()
        await app.notify(title="t", message="m", target=["mikkel"],
                         data={"data": {"url": "/dashboard-rooftop/0#room=bedroom"}}, link=LINK)
        self.assertEqual(_sent(app)["data"], {"url": "/dashboard-rooftop/0#room=bedroom", "clickAction": LINK})

        app = _new_app()
        await app.notify(title="t", message="m", target=["mikkel"],
                         data={"clickAction": "/lovelace/0"}, link=LINK)
        self.assertEqual(_sent(app)["data"], {"clickAction": "/lovelace/0", "url": LINK})

    async def test_invalid_link_is_dropped_with_a_warning_and_the_push_still_goes(self):
        for bad in ("/local/ha-dashboard/index.html", "/dashboard-rooftop", "/dashboard-rooftop/home",
                    "/dashboard-rooftop/home?now=washer-1", "/dashboard-rooftop/0#now=washer-1",
                    "https://ha.me2go.dk/dashboard-rooftop/home#now", "", 42):
            with self.subTest(link=bad):
                app = _new_app()
                sent = await app.notify(title="t", message="m", target=["mikkel"], data={"tag": "x"}, link=bad)
                self.assertEqual(sent, 1)
                self.assertEqual(_sent(app), {"title": "t", "message": "m", "data": {"tag": "x"}})
                self.assertEqual(len(_warnings(app)), 1)
                self.assertIn(repr(bad), _warnings(app)[0])

    async def test_link_reaches_every_service_through_the_per_service_layers(self):
        app = _new_app()
        await app.notify(title="T", message="M", target=["mikkel", "kristine"], critical=True,
                         per_person_actions=lambda person: [{"action": f"ACK_{person.upper()}"}], link=LINK)
        for needle in ("mikkels_ofx9p", "kristine_iphone"):
            data = _sent(app, needle)["data"]
            self.assertEqual((data["url"], data["clickAction"]), (LINK, LINK))


if __name__ == "__main__":
    unittest.main()
