# tests/test_house_events.py - unit tests for HouseEvents' pure event-text builders.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests
# Imports the real module by stubbing the appdaemon package (not installed locally),
# so the builders under test are the deployed code, not a duplicate.

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
    def test_running_transition(self):
        self.assertEqual(house_events.appliance_event("Washer", "Off", "Running", {}), ("started", "Washer started"))

    def test_finished_with_energy(self):
        kind, text = house_events.appliance_event("Dishwasher", "Running", "Unemptied", {"energy_used": 0.718})
        self.assertEqual(kind, "finished")
        self.assertEqual(text, "Dishwasher finished - used 0.72 kWh")

    def test_finished_without_energy(self):
        kind, text = house_events.appliance_event("Dryer", "Running", "Unemptied", {})
        self.assertEqual(text, "Dryer finished")

    def test_restart_replay_suppressed(self):
        self.assertIsNone(house_events.appliance_event("Washer", None, "Running", {}))
        self.assertIsNone(house_events.appliance_event("Washer", "unavailable", "Running", {}))

    def test_emptied(self):
        self.assertEqual(house_events.appliance_event("Dishwasher", "Unemptied", "Emptied", {}), ("emptied", "Dishwasher emptied"))


class AcEventTests(unittest.TestCase):
    def test_start_with_smart_cooling_reason(self):
        self.assertEqual(house_events.ac_event("off", "cool", "precool"), "AC started - precool")

    def test_start_idle_status_gives_no_reason(self):
        self.assertEqual(house_events.ac_event("off", "cool", "idle"), "AC started")

    def test_stop(self):
        self.assertEqual(house_events.ac_event("cool", "off", "precool"), "AC stopped")

    def test_mode_change_is_not_an_event(self):
        self.assertIsNone(house_events.ac_event("cool", "dry", "precool"))

    def test_unavailable_suppressed(self):
        self.assertIsNone(house_events.ac_event("unavailable", "off", None))
        self.assertIsNone(house_events.ac_event("cool", "unavailable", None))


class OtherSourceTests(unittest.TestCase):
    def test_blind(self):
        self.assertEqual(house_events.blind_event("open", "closed"), "Bedroom blind closed")
        self.assertEqual(house_events.blind_event("closing", "open"), "Bedroom blind opened")
        self.assertIsNone(house_events.blind_event("open", "opening"))

    def test_lock(self):
        self.assertEqual(house_events.lock_event("unlocked", "locked"), "Front door locked")
        self.assertEqual(house_events.lock_event("locked", "unlocked"), "Front door unlocked")

    def test_vacuum(self):
        self.assertEqual(house_events.vacuum_event("docked", "cleaning"), "Rober2 started cleaning")
        self.assertEqual(house_events.vacuum_event("returning", "docked"), "Rober2 docked")
        self.assertIsNone(house_events.vacuum_event("docked", "idle"))


if __name__ == "__main__":
    unittest.main()
