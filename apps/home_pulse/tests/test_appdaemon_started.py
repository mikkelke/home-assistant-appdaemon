"""AppDaemonStarted: one numeric state change per start."""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

import appdaemon_started as ads  # noqa: E402


def start(at):
    app = ads.AppDaemonStarted.__new__(ads.AppDaemonStarted)
    app.set_state = MagicMock()
    with patch.object(ads.time, "time", return_value=at):
        app.initialize()
    return app


class AppDaemonStartedTest(unittest.TestCase):
    def test_sets_start_time_as_epoch_seconds(self):
        app = start(1795000000.7)
        app.set_state.assert_called_once()
        args, kwargs = app.set_state.call_args
        self.assertEqual(args, ("sensor.appdaemon_started",))
        self.assertEqual(kwargs["state"], 1795000000)
        self.assertEqual(kwargs["attributes"]["friendly_name"], "AppDaemon started")

    def test_each_start_gives_a_new_value(self):
        first = start(1795000000.2).set_state.call_args.kwargs["state"]
        second = start(1795000070.2).set_state.call_args.kwargs["state"]
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
