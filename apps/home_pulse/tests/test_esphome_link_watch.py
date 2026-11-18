"""EsphomeLinkWatch: reload a hung ESPHome connection, a few times per episode."""

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import esphome_link_watch as elw  # noqa: E402

PROBE = "sensor.bathroom_bathroom_display_loop_time"
T0 = datetime(2026, 11, 18, 8, 13, 57, tzinfo=timezone.utc)


def make_app():
    app = elw.EsphomeLinkWatch.__new__(elw.EsphomeLinkWatch)
    app.args = {"probes": [PROBE]}
    app.state = {"state": "unavailable", "last_changed": T0.isoformat()}
    app.get_state = lambda entity, attribute=None: app.state
    app.call_service = MagicMock()
    app.run_every = MagicMock()
    app.log = MagicMock()
    app.initialize()
    return app


def check_at(app, seconds):
    with patch.object(elw, "datetime", wraps=datetime) as dt:
        dt.now.return_value = T0 + timedelta(seconds=seconds)
        app._check({})


def reloads(app):
    return app.call_service.call_count


class LinkWatch(unittest.TestCase):
    def test_short_outage_is_left_alone(self):
        app = make_app()
        check_at(app, 60)
        self.assertEqual(reloads(app), 0)

    def test_hung_link_reloads_once_then_waits_for_the_cooldown(self):
        app = make_app()
        check_at(app, 121)
        app.call_service.assert_called_once_with("homeassistant/reload_config_entry", entity_id=PROBE)
        check_at(app, 300)
        self.assertEqual(reloads(app), 1)
        check_at(app, 121 + 600)
        self.assertEqual(reloads(app), 2)

    def test_gives_up_after_max_reloads_until_the_probe_is_back(self):
        app = make_app()
        for t in range(121, 121 + 10 * 600, 600):
            check_at(app, t)
        self.assertEqual(reloads(app), 3)
        app.state = {"state": "21.0", "last_changed": T0.isoformat()}
        check_at(app, 7000)
        app.state = {"state": "unavailable", "last_changed": (T0 + timedelta(seconds=7100)).isoformat()}
        check_at(app, 7100 + 121)
        self.assertEqual(reloads(app), 4)

    def test_available_probe_never_reloads(self):
        app = make_app()
        app.state = {"state": "21.0", "last_changed": T0.isoformat()}
        check_at(app, 5000)
        self.assertEqual(reloads(app), 0)


if __name__ == "__main__":
    unittest.main()
