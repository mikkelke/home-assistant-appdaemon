from __future__ import annotations

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

import bedroom_solar_shade as bss  # noqa: E402


def make_app(states):
    """BedroomSolarShade with a fake get_state reading from `states`, without
    running AppDaemon's initialize()."""
    app = bss.BedroomSolarShade.__new__(bss.BedroomSolarShade)
    app.person_entities = ["person.mikkel", "person.kristine"]
    app.get_state = lambda entity, **kw: states.get(entity)
    return app


class EveryoneAway(unittest.TestCase):
    """2026-07-15: while everyone's away, skip daylight balancing and close fully.
    Fail-safe is the point of this test class - an unknown/unavailable tracker (dead
    phone, lost GPS fix) must NOT be read as away, or a flaky tracker would blackout
    the room on someone who is actually home."""

    def test_true_when_both_away(self):
        app = make_app({"person.mikkel": "not_home", "person.kristine": "not_home"})
        self.assertTrue(app._everyone_away())

    def test_true_with_a_named_zone_not_home(self):
        app = make_app({"person.mikkel": "work", "person.kristine": "not_home"})
        self.assertTrue(app._everyone_away())

    def test_false_when_one_home(self):
        app = make_app({"person.mikkel": "home", "person.kristine": "not_home"})
        self.assertFalse(app._everyone_away())

    def test_false_when_both_home(self):
        app = make_app({"person.mikkel": "home", "person.kristine": "home"})
        self.assertFalse(app._everyone_away())

    def test_false_when_tracker_unknown(self):
        app = make_app({"person.mikkel": "unknown", "person.kristine": "not_home"})
        self.assertFalse(app._everyone_away())

    def test_false_when_tracker_unavailable(self):
        app = make_app({"person.mikkel": "unavailable", "person.kristine": "not_home"})
        self.assertFalse(app._everyone_away())

    def test_false_when_tracker_missing(self):
        app = make_app({"person.kristine": "not_home"})  # mikkel key absent -> None
        self.assertFalse(app._everyone_away())


if __name__ == "__main__":
    unittest.main()
