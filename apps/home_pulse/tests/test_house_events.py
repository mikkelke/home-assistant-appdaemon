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


class ApplianceActorTests(unittest.TestCase):
    def test_reliable_method_names_the_resident(self):
        actor = house_events.appliance_actor
        self.assertEqual(actor("finished", {"started_by": "claudia", "started_by_method": "sole_occupant"}), "Claudia")
        self.assertEqual(actor("finished", {"started_by": "mikkel", "started_by_method": "dashboard_action"}), "Mikkel")
        self.assertEqual(actor("emptied", {"emptied_by": "kristine"}), "Kristine")

    def test_unreliable_or_empty_method_names_nobody(self):
        actor = house_events.appliance_actor
        for method in ("unknown", "", None):
            self.assertIsNone(actor("finished", {"started_by": "claudia", "started_by_method": method}), method)
        self.assertIsNone(actor("finished", {"started_by": "claudia"}))
        self.assertIsNone(actor("finished", {"started_by": "", "started_by_method": "sole_occupant"}))
        self.assertIsNone(actor("emptied", {"emptied_by": ""}))
        self.assertIsNone(actor("emptied", {}))

    def test_non_resident_names_nobody(self):
        actor = house_events.appliance_actor
        self.assertIsNone(actor("finished", {"started_by": "guest", "started_by_method": "sole_occupant"}))
        self.assertIsNone(actor("emptied", {"emptied_by": "Cleaning"}))
        self.assertIsNone(actor("emptied", {"emptied_by": 7}))

    def test_emptied_by_method_when_present_must_be_reliable(self):
        actor = house_events.appliance_actor
        self.assertEqual(actor("emptied", {"emptied_by": "kristine", "emptied_by_method": "dashboard_action"}), "Kristine")
        self.assertEqual(actor("emptied", {"emptied_by": "claudia", "emptied_by_method": "sole_occupant"}), "Claudia")
        for method in ("unknown", "", None, "restored_from_entity"):
            self.assertIsNone(actor("emptied", {"emptied_by": "kristine", "emptied_by_method": method}), method)
        self.assertIsNone(actor("emptied", {"emptied_by": "", "emptied_by_method": "sole_occupant"}))

    def test_starter_and_emptier_are_never_swapped(self):
        actor = house_events.appliance_actor
        started = {"started_by": "claudia", "started_by_method": "sole_occupant"}
        self.assertIsNone(actor("emptied", started))
        self.assertIsNone(actor("finished", {"emptied_by": "claudia"}))

    def test_finished_row_carries_the_name(self):
        feed = house_events.HouseEvents()
        feed.appliances = {"sensor.washer_state": "Washer"}
        attrs = {"energy_used": 0.987, "started_by": "claudia", "started_by_method": "sole_occupant"}
        feed.get_state = lambda entity, attribute=None: {"attributes": attrs}
        added = []
        feed._add = lambda icon, text, **kwargs: added.append((icon, text, kwargs.get("by")))
        feed._on_appliance("sensor.washer_state", "state", "Running", "Unemptied", {})
        attrs["started_by_method"] = "unknown"
        feed._on_appliance("sensor.washer_state", "state", "Running", "Unemptied", {})
        self.assertEqual(
            added,
            [
                ("mdi:washing-machine", "Washer finished - used 0.99 kWh", "Claudia"),
                ("mdi:washing-machine", "Washer finished - used 0.99 kWh", None),
            ],
        )


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

    def test_text_only_report(self):
        event = house_events.build_report_event(
            {"text": " Kristine started Spotify in Kitchen ", "icon": "mdi:spotify", "by": "Kristine"}
        )
        self.assertEqual(event["text"], "Kristine started Spotify in Kitchen")
        self.assertIsNone(event["cause"])
        self.assertIsNone(event["effect"])
        self.assertEqual(event["icon"], "mdi:spotify")
        self.assertEqual(event["by"], "Kristine")

    def test_text_only_needs_text(self):
        self.assertIsNone(house_events.build_report_event({"text": "   "}))
        self.assertIsNone(house_events.build_report_event({"text": 5}))
        self.assertIsNone(house_events.build_report_event({"icon": "mdi:music"}))

    def test_cause_effect_win_over_text(self):
        event = house_events.build_report_event({"cause": "c", "effect": "e", "text": "t"})
        self.assertEqual(event["text"], "c -> e")
        half = house_events.build_report_event({"cause": "c", "text": "t"})
        self.assertEqual(half["text"], "t")
        self.assertIsNone(half["cause"])

    def test_text_length_capped(self):
        event = house_events.build_report_event({"text": "t" * 500})
        self.assertEqual(len(event["text"]), house_events.MAX_TEXT_LEN)


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
    # v6: only NAMED unlocks are events - locking (manual or auto), thumb-turn "Manual
    # Unlock", and unattributed unlocks are the door simply being used, never feed-worthy
    # (2026-07-24: 16 routine lock lines in one day drowned the 40-entry feed).
    def test_unlocked_with_name(self):
        icon, text, by = house_events.lock_event("Front door", "locked", "unlocked", "Mikkel")
        self.assertEqual(icon, "mdi:lock-open-variant")
        self.assertEqual(text, "Front door unlocked")
        self.assertEqual(by, "Mikkel")

    def test_resident_full_name_trimmed_to_first_name(self):
        _, _, by = house_events.lock_event("Front door", "locked", "unlocked", "Mikkel Eskildsen")
        self.assertEqual(by, "Mikkel")

    def test_non_resident_names_never_clipped(self):
        # Lock code users keep their full wording - "Cleaning" stays valuable and intact.
        _, _, by = house_events.lock_event("Front door", "locked", "unlocked", "Cleaning")
        self.assertEqual(by, "Cleaning")
        _, _, by = house_events.lock_event("Front door", "locked", "unlocked", "Keypad Code 3")
        self.assertEqual(by, "Keypad Code 3")

    def test_locking_is_never_an_event(self):
        # The steady state the lock always returns to - auto, manual, or named.
        self.assertIsNone(house_events.lock_event("Front door", "unlocked", "locked", "Auto Lock"))
        self.assertIsNone(house_events.lock_event("Front door", "unlocked", "locked", "Mikkel"))
        self.assertIsNone(house_events.lock_event("Front door", "unlocked", "locked", None))

    def test_thumb_turn_unlock_suppressed(self):
        # "Manual Unlock" = someone physically at the door - not news to anyone.
        self.assertIsNone(house_events.lock_event("Front door", "locked", "unlocked", "Manual Unlock"))

    def test_unattributed_unlock_suppressed(self):
        self.assertIsNone(house_events.lock_event("Front door", "locked", "unlocked", None))
        self.assertIsNone(house_events.lock_event("Front door", "locked", "unlocked", "   "))

    def test_restart_replay_suppressed(self):
        self.assertIsNone(house_events.lock_event("Front door", None, "unlocked", "Mikkel"))
        self.assertIsNone(house_events.lock_event("Front door", "unavailable", "unlocked", "Mikkel"))

    def test_non_milestone_states_suppressed(self):
        self.assertIsNone(house_events.lock_event("Front door", "locked", "jammed", "Mikkel"))
        self.assertIsNone(house_events.lock_event("Front door", "locked", "locked", "Mikkel"))

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

    def test_report_audience_users(self):
        event = house_events.build_report_event(
            {"cause": "c", "effect": "e", "audience_users": [" Claudia ", "Claudia", "", 7, "Kristine"]}
        )
        self.assertEqual(event["audience_users"], ["Claudia", "Kristine"])  # trimmed, deduped, junk dropped
        # admin outranks: a users list alongside audience="admin" is ignored
        admin = house_events.build_report_event(
            {"cause": "c", "effect": "e", "audience": "admin", "audience_users": ["Claudia"]}
        )
        self.assertIsNone(admin["audience_users"])
        # malformed list degrades to public, never to hidden
        bad = house_events.build_report_event({"cause": "c", "effect": "e", "audience_users": "Claudia"})
        self.assertIsNone(bad["audience_users"])

    def test_report_audience_only_admin_narrows(self):
        admin = house_events.build_report_event({"cause": "c", "effect": "e", "audience": "admin"})
        self.assertEqual(admin["audience"], "admin")
        # Anything but the literal "admin" must stay public - hiding is the privileged direction.
        for bad in (None, "", "Admin", "private", 1, True):
            event = house_events.build_report_event({"cause": "c", "effect": "e", "audience": bad})
            self.assertIsNone(event["audience"], f"audience={bad!r} must not narrow")


if __name__ == "__main__":
    unittest.main()
