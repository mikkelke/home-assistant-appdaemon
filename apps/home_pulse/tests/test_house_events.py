# tests/test_house_events.py - unit tests for HouseEvents' pure builders/validators.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests
# Imports the real module by stubbing the appdaemon package (not installed locally),
# so the code under test is the deployed code, not a duplicate.

import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_opening_the_door_after_a_drum_clean_is_not_an_emptying(self):
        self.assertIsNone(
            house_events.appliance_event("Washer", "Unemptied", "Emptied", {"detected_programme": "drum_clean"})
        )
        kind, _ = house_events.appliance_event("Washer", "Unemptied", "Emptied", {"detected_programme": "bomuld"})
        self.assertEqual(kind, "emptied")


class ApplianceActorTests(unittest.TestCase):
    def test_reliable_method_names_the_resident(self):
        actor = house_events.appliance_actor
        self.assertEqual(actor("finished", {"started_by": "claudia", "started_by_method": "sole_occupant"}), "Claudia")
        self.assertEqual(actor("finished", {"started_by": "mikkel", "started_by_method": "dashboard_action"}), "Mikkel")
        self.assertEqual(actor("finished", {"started_by": "kristine", "started_by_method": "room_elimination"}), "Kristine")
        self.assertEqual(actor("emptied", {"emptied_by": "kristine", "emptied_by_method": "room_elimination"}), "Kristine")

    def test_unreliable_or_empty_method_names_nobody(self):
        actor = house_events.appliance_actor
        for method in ("unknown", "", None):
            self.assertIsNone(actor("finished", {"started_by": "claudia", "started_by_method": method}), method)
        self.assertIsNone(actor("finished", {"started_by": "claudia"}))
        self.assertIsNone(actor("finished", {"started_by": "", "started_by_method": "sole_occupant"}))
        self.assertIsNone(actor("emptied", {"emptied_by": ""}))
        self.assertIsNone(actor("emptied", {}))

    def test_a_name_without_a_method_names_nobody(self):
        actor = house_events.appliance_actor
        self.assertIsNone(actor("emptied", {"emptied_by": "kristine"}))
        self.assertIsNone(actor("finished", {"started_by": "kristine"}))

    def test_non_resident_names_nobody(self):
        actor = house_events.appliance_actor
        self.assertIsNone(actor("finished", {"started_by": "guest", "started_by_method": "sole_occupant"}))
        self.assertIsNone(actor("emptied", {"emptied_by": "Cleaning", "emptied_by_method": "sole_occupant"}))
        self.assertIsNone(actor("emptied", {"emptied_by": 7, "emptied_by_method": "sole_occupant"}))

    def test_emptied_by_method_must_be_reliable(self):
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


NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))


def row(text, age_s, **extra):
    return {"ts": (NOW - timedelta(seconds=age_s)).isoformat(), "icon": "mdi:basket-check", "text": text, **extra}


class EnrichByTests(unittest.TestCase):
    def enrich(self, events, appliance="Washer", by="Kristine"):
        return house_events.enrich_by(events, appliance, by, NOW)

    def test_names_the_newest_emptied_row_of_that_appliance(self):
        events = [row("Dryer finished", 30), row("Washer emptied", 400), row("Washer emptied", 800)]
        self.assertEqual(self.enrich(events)[1].get("by"), "Kristine")
        self.assertNotIn("by", self.enrich(events)[2])
        self.assertEqual(self.enrich(events)[0], events[0])

    def test_fifteen_minutes_is_the_last_moment(self):
        self.assertEqual(self.enrich([row("Washer emptied", 900)])[0].get("by"), "Kristine")
        self.assertNotIn("by", self.enrich([row("Washer emptied", 901)])[0])

    def test_a_row_that_has_a_name_is_left_alone(self):
        events = [row("Washer emptied", 60, by="Mikkel")]
        self.assertEqual(self.enrich(events)[0]["by"], "Mikkel")

    def test_an_older_row_is_never_named_behind_a_newer_one(self):
        named_newer = [row("Washer emptied", 60, by="Mikkel"), row("Washer emptied", 500)]
        self.assertNotIn("by", self.enrich(named_newer)[1])
        too_old_newer = [row("Washer emptied", 1000), row("Washer emptied", 1200)]
        self.assertEqual(self.enrich(too_old_newer), too_old_newer)

    def test_another_appliance_and_other_rows_are_untouched(self):
        events = [row("Dryer emptied", 60), row("Washer finished - used 0.99 kWh", 90), row("Dishwasher emptied", 100)]
        self.assertEqual(self.enrich(events), events)
        self.assertEqual(self.enrich(events, "Dryer")[0]["by"], "Kristine")
        self.assertEqual(self.enrich(events, "Dryer")[1:], events[1:])

    def test_pure(self):
        events = [row("Washer emptied", 60)]
        before = [dict(e) for e in events]
        out = self.enrich(events)
        self.assertEqual(events, before)
        self.assertIsNot(out, events)

    def test_an_undatable_or_future_row_is_left_alone(self):
        for bad in ({"text": "Washer emptied", "ts": "soon"}, {"text": "Washer emptied"}, row("Washer emptied", -30)):
            with self.subTest(bad=bad):
                self.assertEqual(self.enrich([bad]), [bad])
        naive = {"ts": "2026-10-21T11:59:00", "text": "Washer emptied"}
        self.assertEqual(self.enrich([naive]), [naive])

    def test_no_rows_is_no_rows(self):
        self.assertEqual(self.enrich([]), [])


class EmptiedByListenerTests(unittest.TestCase):
    def make_feed(self, events, attrs):
        feed = house_events.HouseEvents()
        feed.appliances = {"sensor.washer_state": "Washer", "sensor.dryer_state": "Dryer"}
        feed.events = events
        feed.get_state = lambda entity, attribute=None: {"attributes": attrs}
        feed.get_now = lambda: NOW
        feed.published = []
        feed._publish = lambda: feed.published.append([dict(e) for e in feed.events])
        feed.written = []
        feed._write_state_file = lambda: feed.written.append(True)
        feed.logged = []
        feed.log = lambda message, **kw: feed.logged.append(message)
        return feed

    def test_a_late_name_lands_on_the_emptied_row_once(self):
        attrs = {"emptied_by": "kristine", "emptied_by_method": "room_elimination"}
        feed = self.make_feed([row("Washer emptied", 400)], attrs)
        feed._on_emptied_by("sensor.washer_state", "emptied_by", "", "kristine", {})
        self.assertEqual(feed.events[0]["by"], "Kristine")
        self.assertEqual(len(feed.published), 1)
        self.assertEqual(len(feed.written), 1)
        feed._on_emptied_by("sensor.washer_state", "emptied_by", "", "kristine", {})
        self.assertEqual(len(feed.published), 1)
        self.assertEqual(len(feed.written), 1)

    def test_an_unreliable_or_missing_name_changes_nothing(self):
        for attrs in (
            {"emptied_by": "kristine", "emptied_by_method": "unknown"},
            {"emptied_by": "kristine"},
            {"emptied_by": "", "emptied_by_method": "unknown"},
        ):
            with self.subTest(attrs=attrs):
                feed = self.make_feed([row("Washer emptied", 400)], attrs)
                feed._on_emptied_by("sensor.washer_state", "emptied_by", "", attrs["emptied_by"], {})
                self.assertNotIn("by", feed.events[0])
                self.assertEqual(feed.published, [])

    def test_only_that_appliances_row_is_named(self):
        attrs = {"emptied_by": "claudia", "emptied_by_method": "sole_occupant"}
        feed = self.make_feed([row("Washer emptied", 100), row("Dryer emptied", 200)], attrs)
        feed._on_emptied_by("sensor.dryer_state", "emptied_by", "", "claudia", {})
        self.assertNotIn("by", feed.events[0])
        self.assertEqual(feed.events[1]["by"], "Claudia")

    def test_every_appliance_is_listened_to_for_its_emptier(self):
        feed = house_events.HouseEvents()
        feed.args = {"state_file": str(Path(tempfile.mkdtemp()) / "house_events_state.json")}
        feed.get_state = lambda entity, attribute=None: None
        feed.set_state = lambda *a, **kw: None
        feed.log = lambda *a, **kw: None
        feed.listen_event = lambda *a, **kw: None
        listened = []
        feed.listen_state = lambda cb, entity, **kw: listened.append((cb.__name__, entity, kw.get("attribute")))
        feed.initialize()
        for entity in ("sensor.washer_state", "sensor.dishwasher_state", "sensor.dryer_state"):
            self.assertIn(("_on_appliance", entity, None), listened)
            self.assertIn(("_on_emptied_by", entity, "emptied_by"), listened)
        self.assertLess(
            listened.index(("_on_appliance", "sensor.washer_state", None)),
            listened.index(("_on_emptied_by", "sensor.washer_state", "emptied_by")),
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
    # Named unlocks and a thumb-turn the door actually opened after are events; locking,
    # a thumb-turn with the door shut, and unattributed unlocks are not.
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

    def test_thumb_turn_with_door_shut_suppressed(self):
        self.assertIsNone(house_events.lock_event("Front door", "locked", "unlocked", "Manual Unlock"))

    def test_thumb_turn_then_door_opened(self):
        icon, text, by = house_events.lock_event("Front door", "locked", "unlocked", "Manual Unlock", True)
        self.assertEqual(icon, "mdi:door-open")
        self.assertEqual(text, "Front door opened from inside")
        self.assertIsNone(by)

    def test_door_opening_never_names_auto_lock(self):
        self.assertIsNone(house_events.lock_event("Front door", "locked", "unlocked", "Auto Lock", True))

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


class DoorOpenedFromInsideTests(unittest.TestCase):
    # Replays 15.11: door contact on at 05:04:50.4, BLE unlock at 05:04:52.4, cloud
    # changed_by "Manual Unlock" at 05:04:54, emit 20 s after the unlock.
    UNLOCK = datetime(2026, 11, 15, 4, 4, 52, 400000, tzinfo=timezone.utc)

    def make_feed(self):
        feed = house_events.HouseEvents()
        feed.locks = {"lock.yale_bt": "Front door"}
        feed.lock_attribution = {"lock.yale_bt": "lock.yale"}
        feed.lock_door = "binary_sensor.apartment_door_open"
        feed._door_opened_at = None
        feed.lock_report_delay_s = 20
        feed.lock_attribution_fresh_s = 180
        feed.events = []
        feed.clock = self.UNLOCK
        feed.get_now = lambda: feed.clock
        cloud_at = (self.UNLOCK + timedelta(seconds=2)).isoformat()
        feed.get_state = lambda entity, attribute=None: {
            "attributes": {"changed_by": "Manual Unlock"},
            "last_updated": cloud_at,
        }
        feed.scheduled = []
        feed.run_in = lambda cb, delay, **kw: feed.scheduled.append((cb, kw))
        feed._publish = lambda: None
        feed._write_state_file = lambda: None
        feed.log = lambda *a, **kw: None
        return feed

    def run_night(self, door_offset_s):
        feed = self.make_feed()
        if door_offset_s is not None:
            feed.clock = self.UNLOCK + timedelta(seconds=door_offset_s)
            feed._on_door_open("binary_sensor.apartment_door_open", "state", "off", "on", {})
        feed.clock = self.UNLOCK
        feed._on_lock("lock.yale_bt", "state", "locked", "unlocked", {})
        feed.clock = self.UNLOCK + timedelta(seconds=20)
        cb, kw = feed.scheduled[0]
        cb(kw)
        return [e["text"] for e in feed.events]

    def test_door_opening_just_before_the_unlock_reported(self):
        self.assertEqual(self.run_night(-2), ["Front door opened from inside"])

    def test_door_opening_after_the_unlock_reported(self):
        self.assertEqual(self.run_night(5), ["Front door opened from inside"])

    def test_door_left_shut_not_reported(self):
        self.assertEqual(self.run_night(None), [])

    def test_an_earlier_opening_not_pinned_on_this_unlock(self):
        self.assertEqual(self.run_night(-7200), [])


class EntryGroupsTests(unittest.TestCase):
    def test_entered_left_and_unknown(self):
        groups = house_events.entry_groups
        self.assertEqual(groups({"entered": ["Claudia"], "left": []}), [("entered", "Claudia")])
        self.assertEqual(groups({"entered": [], "left": ["Mikkel", "Kristine"]}), [("left", "Mikkel and Kristine")])
        self.assertEqual(groups({"entered": ["Claudia"], "left": ["Mikkel"]}), [("entered", "Claudia"), ("left", "Mikkel")])
        self.assertEqual(groups({"entered": [], "left": []}), [])
        self.assertEqual(groups(None), [])
        self.assertEqual(groups({"entered": "Claudia"}), [])


def lock_row(text, at, **extra):
    return {"ts": at.isoformat(), "icon": "mdi:lock-open-variant", "text": text, **extra}


class ApplyEntryTests(unittest.TestCase):
    OPENED = NOW - timedelta(seconds=30)

    def apply(self, events, kind="entered", by="Claudia"):
        return house_events.apply_entry(events, ["Front door"], kind, by, self.OPENED.timestamp())

    def test_an_unlock_row_of_that_opening_takes_the_name_and_keeps_its_text(self):
        events = [lock_row("Front door unlocked", self.OPENED - timedelta(seconds=5), by="One-Touch Lock"),
                  row("Washer emptied", 4000)]
        out, matched = self.apply(events)
        self.assertTrue(matched)
        self.assertEqual(out[0]["text"], "Front door unlocked")
        self.assertEqual((out[0]["by"], out[0]["entry"]), ("Claudia", "entered"))
        self.assertEqual(out[1], events[1])
        self.assertNotIn("entry", events[0])

    def test_going_out_names_the_thumb_turn_row(self):
        events = [lock_row("Front door opened from inside", self.OPENED + timedelta(seconds=22))]
        out, matched = self.apply(events, kind="left", by="Mikkel")
        self.assertTrue(matched)
        self.assertEqual(out[0]["by"], "Mikkel")

    def test_a_guest_let_in_from_inside_is_not_named_on_the_openers_row(self):
        events = [lock_row("Front door opened from inside", self.OPENED + timedelta(seconds=20))]
        out, matched = self.apply(events, kind="entered")
        self.assertFalse(matched)
        self.assertEqual(out, events)

    def test_rows_outside_the_window_or_already_named_are_left(self):
        for events in (
            [lock_row("Front door unlocked", self.OPENED - timedelta(seconds=91))],
            [lock_row("Front door unlocked", self.OPENED + timedelta(seconds=61))],
            [lock_row("Front door unlocked", self.OPENED, by="Kristine", entry="entered")],
            [row("Washer emptied", 30)],
        ):
            with self.subTest(events=events):
                self.assertFalse(self.apply(events)[1])


class DoorEntryListenerTests(unittest.TestCase):
    OPENED = NOW - timedelta(seconds=30)

    def make_feed(self, events, attrs):
        feed = house_events.HouseEvents()
        feed.locks = {"lock.yale_bt": "Front door"}
        feed.events = list(events)
        feed.get_state = lambda entity, attribute=None: {"attributes": attrs}
        feed.get_now = lambda: NOW
        feed._publish = lambda: None
        feed._write_state_file = lambda: None
        feed.log = lambda *a, **kw: None
        return feed

    def attrs(self, entered=(), left=(), decided=NOW):
        return {"entered": list(entered), "left": list(left), "door_opened_at": self.OPENED.isoformat(),
                "decided_at": decided.isoformat()}

    def test_a_named_lock_row_stays_one_row(self):
        feed = self.make_feed([lock_row("Front door unlocked", self.OPENED - timedelta(seconds=3))],
                              self.attrs(entered=["Claudia"]))
        feed._on_door_entry("sensor.door_entry", "decided_at", None, "x", {})
        self.assertEqual(len(feed.events), 1)
        self.assertEqual(feed.events[0]["by"], "Claudia")

    def test_an_opening_with_no_lock_row_gets_its_own(self):
        feed = self.make_feed([], self.attrs(entered=["Mikkel"]))
        feed._on_door_entry("sensor.door_entry", "decided_at", None, "x", {})
        self.assertEqual([(e["text"], e["by"], e["entry"], e["icon"]) for e in feed.events],
                         [("Came home", "Mikkel", "entered", "mdi:door-open")])

    def test_two_people_coming_home_minutes_apart_are_two_rows(self):
        feed = self.make_feed([], self.attrs(entered=["Mikkel"]))
        feed._on_door_entry("sensor.door_entry", "decided_at", None, "x", {})
        feed.get_state = lambda entity, attribute=None: {"attributes": self.attrs(entered=["Kristine"])}
        feed._on_door_entry("sensor.door_entry", "decided_at", None, "y", {})
        self.assertEqual([e["by"] for e in feed.events], ["Kristine", "Mikkel"])

    def test_an_unknown_or_stale_judgement_adds_nothing(self):
        for attrs in (self.attrs(), self.attrs(entered=["Mikkel"], decided=NOW - timedelta(seconds=121))):
            with self.subTest(attrs=attrs):
                feed = self.make_feed([], attrs)
                feed._on_door_entry("sensor.door_entry", "decided_at", None, "x", {})
                self.assertEqual(feed.events, [])


class NightTests(unittest.TestCase):
    def test_noon_is_the_boundary(self):
        tz = house_events.HOUSE_TZ
        self.assertEqual(house_events.night_key(datetime(2026, 11, 16, 1, 30, tzinfo=tz)), "2026-11-15")
        self.assertEqual(house_events.night_key(datetime(2026, 11, 15, 22, 0, tzinfo=tz)), "2026-11-15")
        self.assertEqual(house_events.night_key(datetime(2026, 11, 16, 12, 0, tzinfo=tz)), "2026-11-16")

    def make_feed(self, clock, states=None):
        feed = house_events.HouseEvents()
        feed.events = []
        feed.clock = clock
        feed.get_now = lambda: feed.clock
        feed.sleepers = {"input_boolean.mikkel_sleep_mode": "Mikkel", "input_boolean.claudia_sleep_mode": "Claudia"}
        feed.admin_sleepers = {"Mikkel"}
        feed.get_state = lambda entity, attribute=None: (states or {}).get(entity, "on")
        feed.scheduled = []
        feed.run_in = lambda cb, delay, **kw: feed.scheduled.append((cb, delay, kw))
        feed._publish = lambda: None
        feed._write_state_file = lambda: None
        feed.log = lambda *a, **kw: None
        return feed

    def sleep(self, feed, entity, at):
        feed.clock = at
        feed._on_sleep(entity, "state", "off", "on", {})
        cb, delay, kw = feed.scheduled.pop()
        feed.clock = at + timedelta(seconds=delay)
        cb(kw)

    def test_one_night_row_per_night(self):
        tz = house_events.HOUSE_TZ
        feed = self.make_feed(datetime(2026, 11, 15, 22, 21, tzinfo=tz))
        feed._on_night("input_boolean.house_night_mode", "state", "off", "on", {})
        feed.clock = datetime(2026, 11, 16, 3, 0, tzinfo=tz)
        feed._on_night("input_boolean.house_night_mode", "state", "off", "on", {})
        self.assertEqual([e["text"] for e in feed.events], ["Night mode on"])
        feed.clock = datetime(2026, 11, 16, 22, 32, tzinfo=tz)
        feed._on_night("input_boolean.house_night_mode", "state", "off", "on", {})
        self.assertEqual(len(feed.events), 2)

    def test_the_first_sleeper_is_the_night_row_and_a_later_one_gets_their_own(self):
        tz = house_events.HOUSE_TZ
        feed = self.make_feed(datetime(2026, 11, 15, 22, 21, tzinfo=tz))
        feed._on_night("input_boolean.house_night_mode", "state", "off", "on", {})
        self.sleep(feed, "input_boolean.mikkel_sleep_mode", datetime(2026, 11, 15, 22, 21, 1, tzinfo=tz))
        self.sleep(feed, "input_boolean.claudia_sleep_mode", datetime(2026, 11, 15, 23, 19, tzinfo=tz))
        rows = [(e["text"], e.get("by"), e.get("audience"), e.get("audience_users")) for e in feed.events]
        self.assertEqual(rows, [("Went to bed", "Claudia", None, ["Claudia"]), ("Night mode on", None, None, None)])

    def test_back_to_bed_the_same_night_is_not_a_second_row_and_mikkel_is_admin_only(self):
        tz = house_events.HOUSE_TZ
        feed = self.make_feed(datetime(2026, 11, 18, 23, 31, tzinfo=tz))
        self.sleep(feed, "input_boolean.mikkel_sleep_mode", datetime(2026, 11, 18, 23, 31, tzinfo=tz))
        self.sleep(feed, "input_boolean.mikkel_sleep_mode", datetime(2026, 11, 19, 6, 59, tzinfo=tz))
        self.assertEqual([(e["by"], e.get("audience")) for e in feed.events], [("Mikkel", "admin")])

    def test_a_sleep_mode_off_again_before_the_row_is_written_adds_nothing(self):
        tz = house_events.HOUSE_TZ
        feed = self.make_feed(datetime(2026, 11, 18, 23, 31, tzinfo=tz),
                              states={"input_boolean.claudia_sleep_mode": "off"})
        self.sleep(feed, "input_boolean.claudia_sleep_mode", datetime(2026, 11, 18, 23, 31, tzinfo=tz))
        self.assertEqual(feed.events, [])


if __name__ == "__main__":
    unittest.main()
