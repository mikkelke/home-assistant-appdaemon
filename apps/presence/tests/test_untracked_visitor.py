# tests/test_untracked_visitor.py - UntrackedVisitor: who counts as a visitor, when a visit
# ends, how the cleaner's blinds are asked for and given back, and what survives a restart.
# The pure rules come first; the app is then driven through its real initialize() with a fake
# clock, fake blind owners and a settable state dict. The fake app has no call_service, so a
# direct cover write would fail the test.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import itertools
import json
import os
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

APPS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(APPS / "blinds"))

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

import blind_arbiter  # noqa: E402
import untracked_visitor as uv  # noqa: E402

RESIDENTS = ["Mikkel", "Kristine", "Claudia"]
CLEANERS = ["Liga Cleaning"]
NON_PERSON = ["Auto Lock", "Manual Lock", "Manual Unlock", "One-Touch Lock", "Home Assistant",
              "unknown", "unavailable"]


def classify(name):
    return uv.classify(name, RESIDENTS, CLEANERS, NON_PERSON)


class Classification(unittest.TestCase):
    def test_a_resident_account_name_is_not_a_visitor(self):
        for name in ("Mikkel Example", "Kristine Example Test", "Claudia Example"):
            with self.subTest(name=name):
                self.assertIsNone(classify(name))

    def test_the_cleaner_is_the_cleaner(self):
        self.assertEqual(classify("Liga Cleaning"), uv.CLEANER)

    def test_any_other_person_is_a_guest(self):
        for name in ("Pat Guest", "Sam  Two Spaces", "Gæst Test"):
            with self.subTest(name=name):
                self.assertEqual(classify(name), uv.GUEST)

    def test_lock_labels_are_not_people(self):
        for label in NON_PERSON:
            with self.subTest(label=label):
                self.assertIsNone(classify(label))

    def test_nothing_usable_is_not_a_visitor(self):
        for value in (None, "", "   ", 0, ["Pat Guest"]):
            with self.subTest(value=value):
                self.assertIsNone(classify(value))

    def test_case_and_spacing_do_not_matter(self):
        self.assertIsNone(classify("kristine   EXAMPLE"))
        self.assertIsNone(classify("auto   lock"))
        self.assertEqual(classify("LIGA  cleaning"), uv.CLEANER)

    def test_a_resident_entry_matches_the_start_of_the_name_not_a_part_of_a_word(self):
        self.assertEqual(classify("Mikkelsen Guest"), uv.GUEST)
        self.assertEqual(classify("Guest Mikkel"), uv.GUEST)

    def test_a_label_must_match_the_whole_name(self):
        self.assertEqual(classify("Auto Lock Guest"), uv.GUEST)

    def test_the_cleaner_entry_needs_all_its_words(self):
        self.assertEqual(classify("Liga"), uv.GUEST)
        self.assertEqual(classify("Liga Cleaning Services"), uv.CLEANER)


class EndReason(unittest.TestCase):
    QUIET_S = 15 * 60
    CAP_S = 2 * 3600

    def why(self, now, *, anchor=0, door_open=False, active=False, since=0):
        return uv.end_reason(now, since, anchor, door_open=door_open, active=active,
                             quiet_s=self.QUIET_S, cap_s=self.CAP_S)

    def test_a_closed_door_and_a_quiet_flat_end_the_visit_after_the_quiet_time(self):
        self.assertIsNone(self.why(self.QUIET_S - 1))
        self.assertEqual(self.why(self.QUIET_S), uv.QUIET)

    def test_an_open_door_never_ends_it_quietly(self):
        self.assertIsNone(self.why(self.CAP_S - 1, door_open=True))

    def test_activity_never_ends_it_quietly(self):
        self.assertIsNone(self.why(self.CAP_S - 1, active=True))

    def test_the_quiet_time_counts_from_the_last_disturbance(self):
        self.assertIsNone(self.why(3600 + self.QUIET_S - 1, anchor=3600))
        self.assertEqual(self.why(3600 + self.QUIET_S, anchor=3600), uv.QUIET)

    def test_the_cap_ends_it_whatever_is_going_on(self):
        self.assertIsNone(self.why(self.CAP_S - 1, door_open=True, active=True))
        self.assertEqual(self.why(self.CAP_S, door_open=True, active=True), uv.CAP)

    def test_a_visit_that_is_both_quiet_and_past_the_cap_is_reported_as_quiet(self):
        self.assertEqual(self.why(self.CAP_S + 1), uv.QUIET)

    def test_the_cap_counts_from_the_start_of_the_visit(self):
        self.assertIsNone(self.why(10 * 3600 + self.CAP_S - 1, since=10 * 3600, door_open=True))
        self.assertEqual(self.why(10 * 3600 + self.CAP_S, since=10 * 3600, door_open=True), uv.CAP)

    def test_a_hold_blocks_quiet_and_the_ordinary_cap_until_the_hard_cap(self):
        args = {"door_open": False, "active": False, "quiet_s": self.QUIET_S,
                "cap_s": self.CAP_S, "hold": True, "hard_cap_s": 4 * 3600}
        self.assertIsNone(uv.end_reason(3 * 3600, 0, 0, **args))
        self.assertEqual(uv.end_reason(4 * 3600, 0, 0, **args), uv.HARD_CAP)

    def test_the_hard_cap_is_reported_even_if_the_visit_is_quiet(self):
        self.assertEqual(self.why(4 * 3600), uv.HARD_CAP)


class SmallRules(unittest.TestCase):
    def test_restore_target_prefers_where_the_blind_was(self):
        self.assertEqual(uv.restore_target(77, 38), 77)
        self.assertEqual(uv.restore_target(0, 38), 0)

    def test_restore_target_falls_back_to_the_default(self):
        self.assertEqual(uv.restore_target(None, 38), 38)
        self.assertIsNone(uv.restore_target(None, None))

    def test_sanitize_keeps_a_good_visit(self):
        raw = {"kind": "cleaner", "who": "Liga Cleaning", "since": 100.0, "anchor": 160.0,
               "blinds": {"bedroom": {"state": "lowered", "prior": 38, "asserted": 120.0}}}
        self.assertEqual(uv.sanitize_visit(raw), raw)

    def test_sanitize_drops_a_visit_that_is_not_one(self):
        good = {"kind": "guest", "who": "Pat Guest", "since": 1.0}
        for bad in (None, [], {}, {**good, "kind": "none"}, {**good, "who": " "},
                    {**good, "since": "yesterday"}, {**good, "since": True},
                    {**good, "since": float("nan")}):
            with self.subTest(bad=bad):
                self.assertIsNone(uv.sanitize_visit(bad))

    def test_sanitize_defaults_the_anchor_and_drops_malformed_blinds(self):
        visit = uv.sanitize_visit({"kind": "guest", "who": "Pat Guest", "since": 5,
                                   "blinds": {"a": {"state": "weird"}, "b": "x"}})
        self.assertEqual(visit["anchor"], 5)
        self.assertEqual(visit["blinds"], {})
        for junk in (["bedroom"], "bedroom", 7):
            self.assertEqual(uv.sanitize_visit({"kind": "guest", "who": "Pat Guest", "since": 5,
                                                "blinds": junk})["blinds"], {})

    def test_the_source_is_the_one_the_arbiter_ranks(self):
        self.assertEqual(uv.SOURCE, blind_arbiter.SOURCE_VISITOR)
        self.assertIn(uv.SOURCE, blind_arbiter.PRIORITIES)

    def test_texts(self):
        tz = timezone(timedelta(hours=2))
        t = datetime(2026, 10, 21, 13, 53, tzinfo=tz).timestamp()
        self.assertEqual(uv.started_text("Liga Cleaning", t, tz), "Liga Cleaning unlocked the front door at 13:53")
        self.assertEqual(uv.ended_text(uv.QUIET, t, tz, 15, 2),
                         "Visit ended at 13:53: the front door is closed and the flat has been quiet for 15 min")
        self.assertEqual(uv.ended_text(uv.CAP, t, tz, 15, 2), "Visit ended at 13:53: 2 h limit reached")


# ---------- the app ----------

TZ = timezone(timedelta(hours=2))
T0 = datetime(2026, 10, 21, 13, 53, tzinfo=TZ).timestamp()

ENTITY = "sensor.untracked_visitor"
LOCK = "lock.yale"
DOOR = "binary_sensor.apartment_door_open"
HOLD = "binary_sensor.stick_vacuum_in_use"
ZONES = ("bathroom", "bedroom", "claudias_room", "dining_room", "guest_bathroom", "hallway",
         "kitchen", "kristines_room", "living_room")
ACTIVITY = [f"binary_sensor.{zone}_active" for zone in ZONES]

BLINDS = {
    "bedroom": {"owner_app": "BedroomBlindOwner", "cover": "cover.bedroom_blind",
                "lowered_position": 0, "restore_position": 38},
    "bathroom": {"owner_app": "BathroomBlindOwner", "cover": "cover.bathroom_blind",
                 "lowered_position": 0, "restore_position": 40},
}

BASE_ARGS = {
    "entity": ENTITY, "lock": LOCK, "door": DOOR, "activity": ACTIVITY,
    "residents": RESIDENTS, "cleaners": CLEANERS, "non_person": NON_PERSON,
    "quiet_min": 15, "cap_hours": 2, "blind_reassert_min": 5, "blind_release_ttl_min": 3,
    "blinds": BLINDS,
}

CLEANER_NAME = "Liga Cleaning"
GUEST_NAME = "Pat Guest"
QUIET_S = 15 * 60


class FakeOwner:
    """Records what the visitor asks of a blind owner; answers like BedroomBlindOwner.request."""

    def __init__(self, granted=True, winner="visitor"):
        self.calls = []
        self.granted = granted
        self.winner = winner
        self.fail = False

    def request(self, source, position, *, reason="", ttl_min=None):
        if self.fail:
            raise RuntimeError("owner broke")
        self.calls.append(("request", source, position, reason, ttl_min))
        return {"granted": self.granted, "moved": self.granted, "winner": self.winner,
                "position": position if self.granted else None, "manual_until": None}

    def withdraw(self, source, reason=""):
        self.calls.append(("withdraw", source))

    def requests(self):
        return [c for c in self.calls if c[0] == "request"]

    def positions(self):
        return [c[2] for c in self.requests()]


_STATE_DIR = tempfile.TemporaryDirectory()
_STATE_SEQ = itertools.count()


def new_state_file():
    return os.path.join(_STATE_DIR.name, f"untracked_visitor_{next(_STATE_SEQ)}.json")


def new_owners():
    return {"BedroomBlindOwner": FakeOwner(), "BathroomBlindOwner": FakeOwner()}


def make_app(states=None, attrs=None, args=None, state_file=None, owners=None, now=0):
    """UntrackedVisitor with initialize() actually run and the AppDaemon primitives faked.
    Every app gets its own state file unless one is passed; `now` is seconds after T0."""
    app = uv.UntrackedVisitor.__new__(uv.UntrackedVisitor)
    app.args = {**BASE_ARGS, "state_file": state_file or new_state_file(), **(args or {})}
    app.states = {LOCK: "locked", DOOR: "off", **{entity: "off" for entity in ACTIVITY}}
    app.states.update(states or {})
    app.attrs = {("cover.bedroom_blind", "current_position"): 38,
                 ("cover.bathroom_blind", "current_position"): 40}
    app.attrs.update(attrs or {})
    app.clock = {"now": T0 + now}
    app.logged = []
    app.published = []
    app.owners = owners if owners is not None else new_owners()

    def get_state(entity, attribute=None, **kw):
        return app.attrs.get((entity, attribute)) if attribute else app.states.get(entity)

    app.get_state = get_state
    app.get_now = lambda: datetime.fromtimestamp(app.clock["now"], TZ)
    app.get_app = lambda name: app.owners.get(name)
    app.log = lambda message, level="INFO": app.logged.append((level, message))
    app.set_state = lambda entity, **kw: app.published.append((entity, kw))
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock()
    app.initialize()
    return app


def iso(seconds):
    return datetime.fromtimestamp(T0 + seconds, TZ).isoformat(timespec="seconds")


def at(app, seconds):
    app.clock["now"] = T0 + seconds


def unlock(app, seconds, who, lock_state="unlocked"):
    at(app, seconds)
    app.states[LOCK] = lock_state
    app._on_changed_by(LOCK, "changed_by", "Auto Lock", who, {})


def door(app, seconds, is_open):
    at(app, seconds)
    old, app.states[DOOR] = app.states.get(DOOR), "on" if is_open else "off"
    app._on_door(DOOR, None, old, app.states[DOOR], {})


def zone(app, seconds, name, on):
    at(app, seconds)
    entity = f"binary_sensor.{name}_active"
    old, app.states[entity] = app.states.get(entity), "on" if on else "off"
    app._on_activity(entity, None, old, app.states[entity], {})


def hold(app, seconds, on):
    at(app, seconds)
    old, app.states[HOLD] = app.states.get(HOLD), "on" if on else "off"
    app._on_hold(HOLD, None, old, app.states[HOLD], {})


def tick(app, seconds):
    at(app, seconds)
    app._tick({})


def published(app):
    entity, kw = app.published[-1]
    assert entity == ENTITY
    return kw["state"], kw["attributes"]


def owner_of(app, name):
    return app.owners[name]


def start_cleaner(app, seconds=0):
    """Door opens, the cleaner's code is reported, the door closes behind her."""
    door(app, seconds, True)
    unlock(app, seconds + 2, CLEANER_NAME)
    door(app, seconds + 4, False)


class Publishing(unittest.TestCase):
    def test_starts_at_none_with_empty_who_and_since(self):
        app = make_app()
        state, attrs = published(app)
        self.assertEqual(state, "none")
        self.assertEqual(attrs["who"], "")
        self.assertEqual(attrs["since"], "")
        self.assertTrue(attrs["reason"])

    def test_every_attribute_survives_appdaemons_set_state(self):
        app = make_app()
        for step in (lambda: None, lambda: start_cleaner(app, 10)):
            step()
            _, attrs = published(app)
            for key, value in attrs.items():
                with self.subTest(key=key):
                    self.assertIsInstance(value, str)
            self.assertTrue(app.published[-1][1]["replace"])

    def test_the_entity_is_republished_as_a_keepalive_and_when_the_plugin_restarts(self):
        app = make_app()
        count = len(app.published)
        tick(app, uv.REPUBLISH_S - 1)
        self.assertEqual(len(app.published), count)
        tick(app, uv.REPUBLISH_S)
        self.assertEqual(len(app.published), count + 1)
        app._on_plugin_started("plugin_started", {}, {})
        self.assertEqual(len(app.published), count + 2)

    def test_it_listens_to_what_it_needs(self):
        app = make_app()
        listened = {c.args[1]: c.kwargs for c in app.listen_state.call_args_list}
        self.assertEqual(listened[LOCK], {"attribute": "changed_by"})
        self.assertIn(DOOR, listened)
        for entity in ACTIVITY:
            self.assertIn(entity, listened)
        for person in ("person.mikkel", "person.kristine", "person.claudia"):
            self.assertNotIn(person, listened)


class Start(unittest.TestCase):
    def test_the_cleaners_unlock_starts_a_cleaner_visit(self):
        app = make_app()
        unlock(app, 5, CLEANER_NAME)
        state, attrs = published(app)
        self.assertEqual(state, "cleaner")
        self.assertEqual(attrs["who"], CLEANER_NAME)
        self.assertEqual(attrs["since"], iso(5))
        self.assertIn("13:53", attrs["reason"])

    def test_another_persons_unlock_starts_a_guest_visit_that_touches_no_blind(self):
        app = make_app()
        unlock(app, 5, GUEST_NAME)
        state, attrs = published(app)
        self.assertEqual((state, attrs["who"]), ("guest", GUEST_NAME))
        tick(app, 600)
        for name in ("BedroomBlindOwner", "BathroomBlindOwner"):
            self.assertEqual(owner_of(app, name).calls, [])

    def test_residents_and_lock_labels_never_start_a_visit(self):
        app = make_app()
        count = len(app.published)
        for who in ("Kristine Example", "Mikkel Example", "Claudia Example", "Auto Lock",
                    "Manual Unlock", "One-Touch Lock", "Home Assistant", None, ""):
            unlock(app, 5, who)
        self.assertEqual(published(app)[0], "none")
        self.assertEqual(len(app.published), count)

    def test_a_name_reported_while_the_lock_reads_locked_is_ignored(self):
        app = make_app()
        unlock(app, 5, CLEANER_NAME, lock_state="locked")
        self.assertEqual(published(app)[0], "none")

    def test_the_same_person_again_is_the_same_visit(self):
        app = make_app()
        unlock(app, 0, CLEANER_NAME)
        since = published(app)[1]["since"]
        count = len(app.published)
        unlock(app, 1800, CLEANER_NAME)
        self.assertEqual(len(app.published), count)
        self.assertEqual(published(app)[1]["since"], since)

    def test_the_same_person_again_restarts_the_quiet_clock(self):
        app = make_app()
        unlock(app, 0, CLEANER_NAME)
        unlock(app, 1000, CLEANER_NAME)
        tick(app, 1000 + QUIET_S - 1)
        self.assertEqual(published(app)[0], "cleaner")
        tick(app, 1000 + QUIET_S)
        self.assertEqual(published(app)[0], "none")

    def test_a_second_cleaner_takes_over_the_blinds_without_moving_them_back(self):
        args = {"cleaners": ["Liga Cleaning", "Ona Cleaning"]}
        app = make_app(args=args)
        unlock(app, 0, CLEANER_NAME)
        unlock(app, 600, "Ona Cleaning")
        self.assertEqual(published(app)[1]["who"], "Ona Cleaning")
        self.assertEqual(owner_of(app, "BedroomBlindOwner").positions(), [0, 0])  # claim renewed, not released
        tick(app, 600 + QUIET_S)
        self.assertEqual(owner_of(app, "BedroomBlindOwner").positions(), [0, 0, 38])

    def test_someone_else_replaces_the_visit_and_a_cleaner_visit_gives_its_blinds_back(self):
        app = make_app()
        unlock(app, 0, CLEANER_NAME)
        unlock(app, 600, GUEST_NAME)
        state, attrs = published(app)
        self.assertEqual((state, attrs["who"]), ("guest", GUEST_NAME))
        self.assertEqual(owner_of(app, "BedroomBlindOwner").positions(), [0, 38])
        self.assertEqual(published(app)[1]["since"], iso(600))


class EndRules(unittest.TestCase):
    def test_a_cleaner_using_the_stick_stays_past_the_ordinary_cap(self):
        app = make_app(args={"hold": HOLD})
        start_cleaner(app)
        hold(app, 30, True)
        tick(app, 3 * 3600)
        self.assertEqual(published(app)[0], "cleaner")
        self.assertIn(HOLD, {c.args[1] for c in app.listen_state.call_args_list})

    def test_the_hard_cap_ends_a_cleaner_visit_even_while_using_the_stick(self):
        app = make_app(args={"hold": HOLD, "hard_cap_hours": 4})
        start_cleaner(app)
        hold(app, 30, True)
        tick(app, 2 + 4 * 3600 - 1)
        self.assertEqual(published(app)[0], "cleaner")
        tick(app, 2 + 4 * 3600)
        state, attrs = published(app)
        self.assertEqual((state, attrs["end_reason"]), ("none", "hard_cap"))
        self.assertIn("4 h hard limit", attrs["reason"])

    def test_hold_changes_restart_the_cleaners_quiet_clock(self):
        app = make_app(args={"hold": HOLD})
        start_cleaner(app)
        hold(app, 30, True)
        tick(app, 1800)
        self.assertEqual(published(app)[0], "cleaner")
        hold(app, 1801, False)
        tick(app, 1801 + QUIET_S - 1)
        self.assertEqual(published(app)[0], "cleaner")
        tick(app, 1801 + QUIET_S)
        self.assertEqual(published(app)[1]["end_reason"], "quiet")

    def test_guests_ignore_the_hold_for_quiet_and_the_cap(self):
        for active in (False, True):
            with self.subTest(active=active):
                app = make_app(args={"hold": HOLD}, states={HOLD: "on"})
                unlock(app, 0, GUEST_NAME)
                zone(app, 1, "bedroom", active)
                hold(app, 500, False)
                hold(app, 600, True)
                tick(app, 2 * 3600 if active else 1 + QUIET_S)
                state, attrs = published(app)
                self.assertEqual((state, attrs["end_reason"]), ("none", "cap" if active else "quiet"))

    def test_the_hard_cap_also_ends_a_guest_visit(self):
        app = make_app(args={"hold": HOLD, "cap_hours": 6, "hard_cap_hours": 4}, states={HOLD: "on"})
        unlock(app, 0, GUEST_NAME)
        zone(app, 1, "bedroom", True)
        tick(app, 4 * 3600)
        self.assertEqual(published(app)[1]["end_reason"], "hard_cap")

    def test_the_end_cause_is_empty_during_a_new_visit(self):
        app = make_app()
        start_cleaner(app)
        tick(app, 4 + QUIET_S)
        self.assertEqual(published(app)[1]["end_reason"], "quiet")
        unlock(app, 1800, GUEST_NAME)
        self.assertEqual(published(app)[1]["end_reason"], "")

    def test_unknown_activity_holds_visit_and_blind_claims_until_readable_quiet(self):
        app = make_app()
        start_cleaner(app)
        entity = "binary_sensor.kitchen_active"
        at(app, 30)
        app.states[entity] = "unknown"
        app._on_activity(entity, None, "off", "unknown", {})
        self.assertIsNone(app._active[entity])
        tick(app, 1800)
        self.assertEqual(published(app)[0], "cleaner")
        # No restoration request occurred while activity was unknown.
        self.assertEqual(set(owner_of(app, "BedroomBlindOwner").positions()), {0})
        zone(app, 1801, "kitchen", False)
        tick(app, 1801 + QUIET_S - 1)
        self.assertEqual(published(app)[0], "cleaner")
        tick(app, 1801 + QUIET_S)
        self.assertEqual(published(app)[0], "none")
        self.assertEqual(owner_of(app, "BedroomBlindOwner").positions()[-1], 38)

    def test_unavailable_or_missing_activity_cannot_establish_a_quiet_flat(self):
        for unknown in (None, "unknown", "unavailable"):
            with self.subTest(state=unknown):
                app = make_app(states={"binary_sensor.kitchen_active": unknown})
                start_cleaner(app)
                tick(app, 1800)
                self.assertEqual(published(app)[0], "cleaner")

    def test_the_cap_still_ends_a_visit_with_unknown_activity(self):
        app = make_app(states={"binary_sensor.kitchen_active": "unknown"})
        start_cleaner(app)
        tick(app, 2 + 2 * 3600)
        self.assertEqual(published(app)[0], "none")

    def test_a_closed_door_and_a_quiet_flat_end_the_visit_after_fifteen_minutes(self):
        app = make_app()
        start_cleaner(app)
        zone(app, 30, "hallway", True)
        zone(app, 3000, "hallway", False)
        tick(app, 3000 + QUIET_S - 1)
        self.assertEqual(published(app)[0], "cleaner")
        tick(app, 3000 + QUIET_S)
        state, attrs = published(app)
        self.assertEqual(state, "none")
        self.assertEqual((attrs["who"], attrs["since"]), ("", ""))
        self.assertIn("quiet for 15 min", attrs["reason"])
        self.assertEqual(attrs["end_reason"], "quiet")

    def test_activity_anywhere_keeps_the_visit_on(self):
        app = make_app()
        start_cleaner(app)
        zone(app, 30, "kitchen", True)
        tick(app, 3600)
        self.assertEqual(published(app)[0], "cleaner")

    def test_a_door_edge_restarts_the_quiet_clock(self):
        app = make_app()
        start_cleaner(app)
        door(app, 600, True)
        door(app, 660, False)
        tick(app, 660 + QUIET_S - 1)
        self.assertEqual(published(app)[0], "cleaner")
        tick(app, 660 + QUIET_S)
        self.assertEqual(published(app)[0], "none")

    def test_an_open_door_keeps_the_visit_on_until_the_cap(self):
        app = make_app()
        door(app, 0, True)
        unlock(app, 2, CLEANER_NAME)
        tick(app, 3600)
        self.assertEqual(published(app)[0], "cleaner")

    def test_with_nobody_moving_it_ends_fifteen_minutes_after_the_door_closed(self):
        app = make_app()
        start_cleaner(app)
        tick(app, 4 + QUIET_S - 1)
        self.assertEqual(published(app)[0], "cleaner")
        tick(app, 4 + QUIET_S)
        self.assertEqual(published(app)[0], "none")

    def test_the_cap_ends_a_visit_that_never_goes_quiet(self):
        app = make_app()
        start_cleaner(app)
        zone(app, 30, "bedroom", True)
        tick(app, 2 + 2 * 3600 - 1)
        self.assertEqual(published(app)[0], "cleaner")
        tick(app, 2 + 2 * 3600)
        state, attrs = published(app)
        self.assertEqual(state, "none")
        self.assertIn("2 h limit", attrs["reason"])
        self.assertEqual(attrs["end_reason"], "cap")

    def test_a_guest_visit_ends_by_the_same_rules(self):
        app = make_app()
        unlock(app, 0, GUEST_NAME)
        tick(app, QUIET_S)
        self.assertEqual(published(app)[0], "none")

    def test_after_an_end_a_new_unlock_starts_a_new_visit(self):
        app = make_app()
        unlock(app, 0, CLEANER_NAME)
        tick(app, QUIET_S)
        unlock(app, 7200, CLEANER_NAME)
        state, attrs = published(app)
        self.assertEqual(state, "cleaner")
        self.assertEqual(attrs["since"], iso(7200))


class Blinds(unittest.TestCase):
    def test_the_cleaner_gets_both_blinds_asked_for_through_their_owners(self):
        app = make_app()
        unlock(app, 0, CLEANER_NAME)
        for name in ("BedroomBlindOwner", "BathroomBlindOwner"):
            calls = owner_of(app, name).requests()
            self.assertEqual(len(calls), 1)
            _, source, position, reason, ttl = calls[0]
            self.assertEqual((source, position, ttl), ("visitor", 0, None))
            self.assertIn(CLEANER_NAME, reason)

    def test_the_claim_is_asked_again_every_five_minutes(self):
        app = make_app()
        unlock(app, 0, CLEANER_NAME)
        zone(app, 1, "hallway", True)
        tick(app, 299)
        self.assertEqual(len(owner_of(app, "BedroomBlindOwner").requests()), 1)
        tick(app, 300)
        self.assertEqual(len(owner_of(app, "BedroomBlindOwner").requests()), 2)
        tick(app, 599)
        self.assertEqual(len(owner_of(app, "BedroomBlindOwner").requests()), 2)
        tick(app, 600)
        self.assertEqual(len(owner_of(app, "BedroomBlindOwner").requests()), 3)

    def test_the_blinds_go_back_to_where_they_were_when_the_visit_ends(self):
        app = make_app(attrs={("cover.bedroom_blind", "current_position"): 100,
                              ("cover.bathroom_blind", "current_position"): 40})
        unlock(app, 0, CLEANER_NAME)
        tick(app, QUIET_S)
        self.assertEqual(published(app)[0], "none")
        bedroom = owner_of(app, "BedroomBlindOwner").requests()
        bathroom = owner_of(app, "BathroomBlindOwner").requests()
        self.assertEqual([c[2] for c in bedroom], [0, 100])
        self.assertEqual([c[2] for c in bathroom], [0, 40])
        self.assertEqual((bedroom[-1][1], bedroom[-1][4]), ("visitor", 3))

    def test_an_unreadable_starting_position_falls_back_to_the_configured_one(self):
        app = make_app(attrs={("cover.bedroom_blind", "current_position"): "unavailable",
                              ("cover.bathroom_blind", "current_position"): None})
        unlock(app, 0, CLEANER_NAME)
        tick(app, QUIET_S)
        self.assertEqual(owner_of(app, "BedroomBlindOwner").positions(), [0, 38])
        self.assertEqual(owner_of(app, "BathroomBlindOwner").positions(), [0, 40])

    def test_the_cap_gives_the_blinds_back_too(self):
        app = make_app()
        start_cleaner(app)
        zone(app, 30, "bedroom", True)
        tick(app, 2 + 2 * 3600)
        self.assertEqual(owner_of(app, "BedroomBlindOwner").positions()[-1], 38)

    def test_a_blind_the_owner_refuses_is_left_alone_for_the_whole_visit(self):
        app = make_app()
        bedroom = owner_of(app, "BedroomBlindOwner")
        bedroom.granted, bedroom.winner = False, "manual"
        unlock(app, 0, CLEANER_NAME)
        zone(app, 1, "hallway", True)
        self.assertIn(("withdraw", "visitor"), bedroom.calls)
        self.assertEqual(len(bedroom.requests()), 1)
        tick(app, 600)
        tick(app, 1200)
        zone(app, 1300, "hallway", False)
        tick(app, 1300 + QUIET_S)
        self.assertEqual(len(bedroom.requests()), 1)
        self.assertEqual(owner_of(app, "BathroomBlindOwner").positions(), [0, 0, 0, 40])

    def test_a_missing_owner_costs_the_blind_not_the_visit(self):
        app = make_app(owners={"BathroomBlindOwner": FakeOwner()})
        unlock(app, 0, CLEANER_NAME)
        self.assertEqual(published(app)[0], "cleaner")
        tick(app, 60)
        warnings = [m for lvl, m in app.logged if lvl == "WARNING" and "BedroomBlindOwner" in m]
        self.assertEqual(len(warnings), 1)

    def test_an_owner_that_raises_is_retried_and_warned_about_once(self):
        app = make_app()
        bedroom = owner_of(app, "BedroomBlindOwner")
        bedroom.fail = True
        unlock(app, 0, CLEANER_NAME)
        tick(app, 30)
        tick(app, 60)
        self.assertEqual(len([1 for lvl, _ in app.logged if lvl == "WARNING"]), 1)
        bedroom.fail = False
        tick(app, 90)
        self.assertEqual(bedroom.positions(), [0])
        tick(app, QUIET_S + 90)
        self.assertEqual(bedroom.positions(), [0, 38])

    def test_a_guest_never_asks_for_a_blind_and_ending_it_gives_nothing_back(self):
        app = make_app()
        unlock(app, 0, GUEST_NAME)
        tick(app, QUIET_S)
        self.assertEqual(owner_of(app, "BedroomBlindOwner").calls, [])


class Restart(unittest.TestCase):
    def setUp(self):
        self.file = new_state_file()

    def boot(self, now, **kw):
        """A fresh instance `now` seconds after T0 sharing the state file, like a new AppDaemon."""
        return make_app(state_file=self.file, now=now, **kw)

    def test_the_state_file_records_the_visit_and_the_blinds(self):
        app = self.boot(0)
        unlock(app, 0, CLEANER_NAME)
        visit = json.loads(Path(self.file).read_text())["visit"]
        self.assertEqual(visit["who"], CLEANER_NAME)
        self.assertEqual(visit["blinds"]["bedroom"]["state"], "lowered")
        self.assertEqual(visit["blinds"]["bedroom"]["prior"], 38)

    def test_a_held_cleaner_visit_survives_a_restart_after_the_ordinary_cap(self):
        first = self.boot(0, args={"hold": HOLD})
        start_cleaner(first)
        hold(first, 30, True)
        tick(first, 60)
        self.assertTrue(json.loads(Path(self.file).read_text())["hold"])
        again = self.boot(3 * 3600, args={"hold": HOLD}, states={HOLD: "on"})
        self.assertEqual(published(again)[0], "cleaner")
        tick(again, 2 + 4 * 3600)
        self.assertEqual(published(again)[1]["end_reason"], "hard_cap")

    def test_a_hold_that_changed_while_offline_restarts_the_quiet_clock(self):
        first = self.boot(0, args={"hold": HOLD}, states={HOLD: "on"})
        start_cleaner(first)
        tick(first, 60)
        again = self.boot(1800, args={"hold": HOLD}, states={HOLD: "off"})
        self.assertEqual(published(again)[0], "cleaner")
        tick(again, 1800 + QUIET_S)
        self.assertEqual(published(again)[1]["end_reason"], "quiet")

    def test_the_end_cause_survives_a_restart(self):
        first = self.boot(0)
        start_cleaner(first)
        tick(first, 4 + QUIET_S)
        self.assertEqual(json.loads(Path(self.file).read_text())["end_reason"], "quiet")
        again = self.boot(1800)
        self.assertEqual(published(again)[1]["end_reason"], "quiet")

    def test_the_entity_preserves_the_end_cause_without_a_file(self):
        app = make_app(states={ENTITY: "none"},
                       attrs={(ENTITY, "end_reason"): "cap", (ENTITY, "reason"): "Visit ended"})
        self.assertEqual(published(app)[1]["end_reason"], "cap")
        self.assertEqual(published(app)[1]["reason"], "Visit ended")

    def test_a_restart_during_a_visit_resumes_it_and_keeps_the_blind_claims_alive(self):
        first = self.boot(0)
        start_cleaner(first)
        zone(first, 60, "hallway", True)
        tick(first, 120)
        since = published(first)[1]["since"]

        soon = self.boot(200, states={"binary_sensor.hallway_active": "on"})
        self.assertEqual((published(soon)[0], published(soon)[1]["since"]), ("cleaner", since))
        self.assertEqual(owner_of(soon, "BedroomBlindOwner").calls, [])  # asked a few minutes ago

        later = self.boot(400, states={"binary_sensor.hallway_active": "on"})
        self.assertEqual((published(later)[0], published(later)[1]["since"]), ("cleaner", since))
        self.assertEqual(owner_of(later, "BedroomBlindOwner").positions(), [0])
        self.assertEqual(owner_of(later, "BathroomBlindOwner").positions(), [0])

    def test_the_restored_blinds_remember_where_they_were_before_the_visit(self):
        first = make_app(state_file=self.file, attrs={("cover.bedroom_blind", "current_position"): 100})
        unlock(first, 0, CLEANER_NAME)
        again = self.boot(60, attrs={("cover.bedroom_blind", "current_position"): 0})
        tick(again, QUIET_S)
        self.assertEqual(owner_of(again, "BedroomBlindOwner").positions(), [100])

    def test_a_restart_after_the_visit_went_quiet_ends_it_and_gives_the_blinds_back(self):
        first = self.boot(0)
        start_cleaner(first)
        zone(first, 60, "hallway", True)
        zone(first, 600, "hallway", False)
        tick(first, 630)

        owners = new_owners()
        again = self.boot(40 * 60, owners=owners)
        state, attrs = published(again)
        self.assertEqual(state, "none")
        self.assertIn("quiet", attrs["reason"])
        self.assertEqual(owners["BedroomBlindOwner"].positions(), [38])
        self.assertEqual(owners["BathroomBlindOwner"].positions(), [40])

    def test_a_restart_after_the_cap_ends_it(self):
        first = self.boot(0)
        start_cleaner(first)
        zone(first, 30, "bedroom", True)
        tick(first, 60)

        owners = new_owners()
        again = self.boot(3 * 3600, owners=owners, states={"binary_sensor.bedroom_active": "on"})
        self.assertEqual(published(again)[0], "none")
        self.assertIn("2 h limit", published(again)[1]["reason"])
        self.assertEqual(owners["BedroomBlindOwner"].positions(), [38])

    def test_a_restart_while_someone_is_still_moving_about_keeps_the_visit(self):
        first = self.boot(0)
        start_cleaner(first)
        tick(first, 60)
        again = self.boot(3600, states={"binary_sensor.kitchen_active": "on"})
        self.assertEqual(published(again)[0], "cleaner")

    def test_without_a_file_the_published_entity_is_used(self):
        since = iso(-600)
        app = make_app(states={ENTITY: "cleaner", "binary_sensor.kitchen_active": "on"},
                       attrs={(ENTITY, "who"): CLEANER_NAME, (ENTITY, "since"): since,
                              (ENTITY, "reason"): "from before"})
        state, attrs = published(app)
        self.assertEqual((state, attrs["who"], attrs["since"]), ("cleaner", CLEANER_NAME, since))
        self.assertEqual(attrs["reason"], "from before")
        self.assertEqual(owner_of(app, "BedroomBlindOwner").positions(), [0])

    def test_a_published_none_or_a_broken_entity_means_no_visit(self):
        broken_since = {(ENTITY, "who"): CLEANER_NAME, (ENTITY, "since"): "nope"}
        no_who = {(ENTITY, "since"): "2026-10-21T13:53:00+02:00"}
        for states, attrs in (({ENTITY: "none"}, {}), ({ENTITY: "cleaner"}, broken_since),
                              ({ENTITY: "cleaner"}, no_who)):
            with self.subTest(states=states, attrs=attrs):
                self.assertEqual(published(make_app(states=states, attrs=attrs))[0], "none")

    def test_a_corrupt_file_is_ignored(self):
        Path(self.file).write_text("{not json")
        self.assertEqual(published(self.boot(0))[0], "none")

    def test_the_file_wins_over_the_entity_and_keeps_the_last_reason(self):
        first = self.boot(0)
        unlock(first, 0, CLEANER_NAME)
        tick(first, QUIET_S)
        ended = published(first)[1]["reason"]
        again = self.boot(3600, states={ENTITY: "cleaner"},
                          attrs={(ENTITY, "who"): CLEANER_NAME, (ENTITY, "since"): "2026-10-21T13:53:00+02:00"})
        self.assertEqual(published(again)[0], "none")
        self.assertEqual(published(again)[1]["reason"], ended)


class ConfigStaysInStep(unittest.TestCase):
    def read(self, name):
        return (Path(__file__).resolve().parents[1] / name).read_text()

    def test_every_room_active_zone_has_its_active_entity_in_the_activity_list(self):
        zones = []
        in_zones = False
        for line in self.read("room_active.yaml").splitlines():
            if re.match(r"^  zones:\s*$", line):
                in_zones = True
                continue
            if in_zones:
                m = re.match(r"^    ([a-z_]+):\s*(#.*)?$", line)
                if m:
                    zones.append(m.group(1))
                elif re.match(r"^\S", line) or re.match(r"^  \S", line):
                    break
        listed = re.findall(r"^    - (binary_sensor\.[a-z_]+_active)\s*$", self.read("untracked_visitor.yaml"), re.M)
        self.assertTrue(zones)
        self.assertEqual(sorted(listed), sorted(f"binary_sensor.{z}_active" for z in zones))
        self.assertEqual(sorted(listed), sorted(ACTIVITY))

    def test_the_yaml_does_not_name_a_log_stream(self):
        self.assertNotRegex(self.read("untracked_visitor.yaml"), r"(?m)^\s+log:")


if __name__ == "__main__":
    unittest.main()
