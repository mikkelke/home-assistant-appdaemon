"""AssistBrief: the published brief is exactly the scripts' responses."""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

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

import assist_brief as ab  # noqa: E402


def envelope(resp):
    return {"success": True, "result": {"context": {}, "response": resp}}


class ScriptResponse(unittest.TestCase):
    def test_unwraps_envelope(self):
        self.assertEqual(ab.script_response(envelope({"home": ["Mikkel"]})), {"home": ["Mikkel"]})

    def test_empty_or_odd_is_none(self):
        self.assertIsNone(ab.script_response(envelope({})))
        self.assertIsNone(ab.script_response(None))
        self.assertIsNone(ab.script_response({"result": {"response": ["x"]}}))

    def test_brief_drops_failed_keeps_order(self):
        brief = ab.build_brief([("b", {"x": 1}), ("a", None), ("c", {"y": 2})])
        self.assertEqual(list(brief), ["b", "c"])


class Refresh(unittest.TestCase):
    def make(self, responses):
        app = ab.AssistBrief.__new__(ab.AssistBrief)
        app.scripts = list(responses)
        app.sensor = "sensor.assist_house_brief"
        app._busy = False
        app.log = lambda *a, **k: None

        async def call_service(service, return_response):
            r = responses[service.split("/", 1)[1]]
            if isinstance(r, Exception):
                raise r
            return envelope(r)

        app.call_service = call_service
        app.set_state = AsyncMock()
        return app

    def test_publishes_every_response(self):
        app = self.make({"who_is_home": {"home": ["Mikkel"]}, "deliveries": {"status": "none"}})
        asyncio.run(app.refresh())
        attrs = app.set_state.await_args.kwargs["attributes"]
        self.assertEqual(attrs["who_is_home"], {"home": ["Mikkel"]})
        self.assertEqual(attrs["deliveries"], {"status": "none"})

    def test_failed_script_is_left_out(self):
        app = self.make({"who_is_home": {"home": ["Mikkel"]}, "deliveries": RuntimeError("down")})
        asyncio.run(app.refresh())
        attrs = app.set_state.await_args.kwargs["attributes"]
        self.assertNotIn("deliveries", attrs)
        self.assertIn("who_is_home", attrs)


if __name__ == "__main__":
    unittest.main()
