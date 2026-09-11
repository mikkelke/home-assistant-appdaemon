# tests/test_mobile_notifier_critical_push.py - Unit tests for MobileNotifier's fire-safety
# additions (2026-09-11): platform-scoped critical payloads, per_person_actions, and
# test_audience narrowing, plus a byte-identical guarantee for pre-existing callers.
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


def _new_app(device_mapping=None, category_audience=None, platform_map=None):
    app = mn.MobileNotifier.__new__(mn.MobileNotifier)
    app.device_mapping = device_mapping if device_mapping is not None else {
        "mikkel": ["notify.mobile_app_mikkels_ofx9p"],
        "kristine": ["notify.mobile_app_kristine_iphone_2"],
        "claudia": ["notify.mobile_app_claudias_iphone"],
    }
    app.category_audience = category_audience or {}
    app.platform_map = platform_map or {}
    app.user_notification_service = "notify.mobile_app_mikkels_ofx9p"

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

    app.calls = []

    async def _call_service(service, **kwargs):
        app.calls.append((service, kwargs))

    app.call_service = _call_service
    return app


def _find_call(app, needle):
    for service, kwargs in app.calls:
        if needle in service:
            return service, kwargs
    raise AssertionError(f"No call found containing {needle!r} in {[s for s, _ in app.calls]}")


class CriticalPayloadPlatformNesting(unittest.IsolatedAsyncioTestCase):
    """critical=True must nest an iOS push dict for iPhone services and Android
    channel/importance fields for the Android service - never both on one service."""

    async def test_ios_gets_push_not_channel(self):
        app = _new_app()
        await app.notify(title="FIRE", message="Evacuate now", target=["mikkel", "kristine"], critical=True)
        _, kwargs = _find_call(app, "kristine_iphone")
        self.assertEqual(
            kwargs["data"]["push"],
            {"interruption-level": "critical", "sound": {"name": "default", "critical": 1, "volume": 1.0}},
        )
        self.assertNotIn("channel", kwargs["data"])

    async def test_android_gets_channel_not_push(self):
        app = _new_app()
        await app.notify(title="FIRE", message="Evacuate now", target=["mikkel", "kristine"], critical=True)
        _, kwargs = _find_call(app, "mikkels_ofx9p")
        self.assertEqual(kwargs["data"]["channel"], "Fire alarm")
        self.assertEqual(kwargs["data"]["importance"], "max")
        self.assertEqual(kwargs["data"]["priority"], "high")
        self.assertEqual(kwargs["data"]["ttl"], 0)
        self.assertTrue(kwargs["data"]["persistent"])
        self.assertTrue(kwargs["data"]["sticky"])
        self.assertNotIn("push", kwargs["data"])

    async def test_channel_kwarg_overrides_default_name(self):
        app = _new_app()
        await app.notify(title="T", message="M", target=["mikkel"], critical=True, channel="Health check")
        _, kwargs = _find_call(app, "mikkels_ofx9p")
        self.assertEqual(kwargs["data"]["channel"], "Health check")


class CallerKeysWinOverCriticalDefaults(unittest.IsolatedAsyncioTestCase):
    async def test_caller_ios_push_fully_replaces_default(self):
        app = _new_app()
        await app.notify(
            title="T", message="M", target=["kristine"], critical=True,
            data={"data": {"push": {"interruption-level": "time-sensitive"}}},
        )
        _, kwargs = _find_call(app, "kristine_iphone")
        self.assertEqual(kwargs["data"]["push"], {"interruption-level": "time-sensitive"})

    async def test_caller_android_key_wins_others_still_defaulted(self):
        app = _new_app()
        await app.notify(
            title="T", message="M", target=["mikkel"], critical=True,
            data={"data": {"importance": "high"}},
        )
        _, kwargs = _find_call(app, "mikkels_ofx9p")
        self.assertEqual(kwargs["data"]["importance"], "high")
        self.assertEqual(kwargs["data"]["channel"], "Fire alarm")


class PerPersonActions(unittest.IsolatedAsyncioTestCase):
    async def test_actions_are_scoped_per_service(self):
        app = _new_app()
        await app.notify(
            title="T", message="M", target=["mikkel", "kristine"],
            per_person_actions=lambda person: [{"action": f"ACK_{person.upper()}"}],
        )
        _, mikkel_kwargs = _find_call(app, "mikkels_ofx9p")
        _, kristine_kwargs = _find_call(app, "kristine_iphone")
        self.assertEqual(mikkel_kwargs["data"]["actions"], [{"action": "ACK_MIKKEL"}])
        self.assertEqual(kristine_kwargs["data"]["actions"], [{"action": "ACK_KRISTINE"}])


class TestAudienceNarrowing(unittest.IsolatedAsyncioTestCase):
    async def test_only_test_audience_receives_the_push(self):
        app = _new_app()
        await app.notify(title="T", message="M", target="all", test_audience=["mikkel"])
        self.assertEqual(len(app.calls), 1)
        service, _ = app.calls[0]
        self.assertIn("mikkels_ofx9p", service)

    async def test_narrowing_is_logged(self):
        app = _new_app()
        await app.notify(title="T", message="M", target="all", test_audience=["mikkel"])
        self.assertTrue(any("test_audience" in str(a) for a, kw in app.log_calls))

    async def test_no_narrowing_when_audience_already_matches(self):
        app = _new_app()
        await app.notify(title="T", message="M", target=["mikkel"], test_audience=["mikkel"])
        self.assertEqual(len(app.calls), 1)
        self.assertFalse(any("test_audience" in str(a) for a, kw in app.log_calls))


class LegacyCallsUnchanged(unittest.IsolatedAsyncioTestCase):
    """Without the new kwargs, notify() must keep sending exactly the same payload
    shape it did before this feature - no stray "data" key, no per-service copying."""

    async def test_bare_call_payload_has_no_new_keys(self):
        app = _new_app()
        await app.notify(title="Washer finished", message="All done", target=["mikkel"])
        _, kwargs = app.calls[0]
        self.assertEqual(kwargs, {"title": "Washer finished", "message": "All done"})

    async def test_existing_nested_data_call_untouched(self):
        app = _new_app()
        await app.notify(
            title="Confirm",
            message="Was it...?",
            target=["mikkel"],
            data={"data": {"actions": [{"action": "X", "title": "Y"}], "tag": "washer_confirm"}},
        )
        _, kwargs = app.calls[0]
        self.assertEqual(
            kwargs,
            {
                "title": "Confirm",
                "message": "Was it...?",
                "data": {"actions": [{"action": "X", "title": "Y"}], "tag": "washer_confirm"},
            },
        )


if __name__ == "__main__":
    unittest.main()
