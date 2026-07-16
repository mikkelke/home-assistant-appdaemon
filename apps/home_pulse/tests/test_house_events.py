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


class SanitizeFeedTests(unittest.TestCase):
    def test_filters_and_caps(self):
        good = {"ts": "2026-07-16T10:00:00+00:00", "text": "x"}
        raw = [good] * (house_events.MAX_EVENTS + 10) + [{"ts": ""}, {"text": "no ts"}, "junk", None]
        out = house_events.sanitize_feed(raw)
        self.assertEqual(len(out), house_events.MAX_EVENTS)
        self.assertTrue(all(e is good for e in out))

    def test_non_list_is_empty(self):
        self.assertEqual(house_events.sanitize_feed(None), [])
        self.assertEqual(house_events.sanitize_feed({"events": []}), [])


class LockEventTests(unittest.TestCase):
    # v4: lock_event returns (icon, text, by) - the person travels in `by`, never in text.
    def test_unlocked_with_name(self):
        icon, text, by = house_events.lock_event("Front door", "locked", "unlocked", "Mikkel")
        self.assertEqual(icon, "mdi:lock-open-variant")
        self.assertEqual(text, "Front door unlocked")
        self.assertEqual(by, "Mikkel")

    def test_auto_lock_reads_automatically(self):
        icon, text, by = house_events.lock_event("Front door", "unlocked", "locked", "Auto Lock")
        self.assertEqual(icon, "mdi:lock")
        self.assertEqual(text, "Front door locked automatically")
        self.assertIsNone(by)

    def test_no_attribution_states_the_fact(self):
        _, text, by = house_events.lock_event("Front door", "locked", "unlocked", None)
        self.assertEqual(text, "Front door unlocked")
        self.assertIsNone(by)

    def test_blank_attribution_ignored(self):
        _, text, by = house_events.lock_event("Front door", "locked", "unlocked", "   ")
        self.assertEqual(text, "Front door unlocked")
        self.assertIsNone(by)

    def test_restart_replay_suppressed(self):
        self.assertIsNone(house_events.lock_event("Front door", None, "locked", None))
        self.assertIsNone(house_events.lock_event("Front door", "unavailable", "locked", None))

    def test_non_milestone_states_suppressed(self):
        self.assertIsNone(house_events.lock_event("Front door", "locked", "jammed", None))
        self.assertIsNone(house_events.lock_event("Front door", "locked", "locked", None))

    def test_attribution_length_capped(self):
        _, _, by = house_events.lock_event("Front door", "locked", "unlocked", "x" * 500)
        self.assertLessEqual(len(by), house_events.MAX_TEXT_LEN)

    def test_report_by_passthrough(self):
        event = house_events.build_report_event(
            {"cause": "Kitchen lights switched to manual", "effect": "Automation paused", "by": " Mikkel "}
        )
        self.assertEqual(event["by"], "Mikkel")
        no_by = house_events.build_report_event({"cause": "c", "effect": "e"})
        self.assertIsNone(no_by["by"])


if __name__ == "__main__":
    unittest.main()
