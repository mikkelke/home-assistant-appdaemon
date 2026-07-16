# tests/test_house_events.py - unit tests for HouseEvents' pure builders/validators.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests
# Imports the real module by stubbing the appdaemon package (not installed locally),
# so the code under test is the deployed code, not a duplicate.

import sys
import types
import unittest
from pathlib import Path

# Stub appdaemon.plugins.hass.hassapi before importing the app module.
_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
_hassapi.Hass = object
for name, mod in (
    ("appdaemon", types.ModuleType("appdaemon")),
    ("appdaemon.plugins", types.ModuleType("appdaemon.plugins")),
    ("appdaemon.plugins.hass", types.ModuleType("appdaemon.plugins.hass")),
    ("appdaemon.plugins.hass.hassapi", _hassapi),
):
    sys.modules.setdefault(name, mod)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import house_events  # noqa: E402


class ApplianceEventTests(unittest.TestCase):
    def test_started_is_deliberately_not_an_event(self):
        self.assertIsNone(house_events.appliance_event("Washer", "Off", "Running", {}))

    def test_finished_with_energy(self):
        kind, text = house_events.appliance_event("Dishwasher", "Running", "Unemptied", {"energy_used": 0.718})
        self.assertEqual(kind, "finished")
        self.assertEqual(text, "Dishwasher finished - used 0.72 kWh")

    def test_finished_without_energy(self):
        kind, text = house_events.appliance_event("Dryer", "Running", "Unemptied", {})
        self.assertEqual(text, "Dryer finished")

    def test_restart_replay_suppressed(self):
        self.assertIsNone(house_events.appliance_event("Washer", None, "Unemptied", {}))
        self.assertIsNone(house_events.appliance_event("Washer", "unavailable", "Unemptied", {}))

    def test_emptied(self):
        self.assertEqual(house_events.appliance_event("Dishwasher", "Unemptied", "Emptied", {}), ("emptied", "Dishwasher emptied"))


class ReportEventTests(unittest.TestCase):
    def test_valid_report(self):
        event = house_events.build_report_event(
            {"cause": "Bedroom TV turned on", "effect": "TV lift going down", "icon": "mdi:television"}
        )
        self.assertEqual(event["text"], "Bedroom TV turned on -> TV lift going down")
        self.assertEqual(event["cause"], "Bedroom TV turned on")
        self.assertEqual(event["effect"], "TV lift going down")
        self.assertEqual(event["icon"], "mdi:television")

    def test_missing_or_blank_cause_effect_rejected(self):
        self.assertIsNone(house_events.build_report_event({"effect": "TV lift going down"}))
        self.assertIsNone(house_events.build_report_event({"cause": "  ", "effect": "x"}))
        self.assertIsNone(house_events.build_report_event({"cause": "x", "effect": ""}))
        self.assertIsNone(house_events.build_report_event("not a dict"))

    def test_bad_icon_falls_back(self):
        event = house_events.build_report_event({"cause": "a", "effect": "b", "icon": "javascript:alert(1)"})
        self.assertEqual(event["icon"], "mdi:auto-fix")

    def test_length_capped(self):
        event = house_events.build_report_event({"cause": "c" * 500, "effect": "e" * 500})
        self.assertEqual(len(event["cause"]), house_events.MAX_TEXT_LEN)
        self.assertEqual(len(event["effect"]), house_events.MAX_TEXT_LEN)


if __name__ == "__main__":
    unittest.main()
