# tests/test_actor_attribution.py - unit tests for actor_attribution.py: the seven
# sole-occupancy decision rules, the never-guess presence semantics (zone name vs
# unobservable), backward-only door-edge handling, retention-window edge cases (including
# a pruning off-by-window bug found and fixed during implementation), room elimination for
# 2+ people home (strict window, pending verdicts, room log listener/tick/backfill/prune),
# and the listener/tick/backfill/publish glue. Same __new__ + monkeypatched-callables
# harness as apps/appliances/tests/test_washer_vibration.py in this repo (no running
# AppDaemon required).
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import actor_attribution as aa  # noqa: E402


BASE = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


def make_app(person_entities=None, door_entity="binary_sensor.apartment_door_open",
             stability_seconds=600, door_settle_seconds=300, retention_hours=26,
             door_state="off"):
    """ActorAttribution with fake log/get_state/set_state, without running AppDaemon's
    initialize() - same trick as test_washer_vibration.py's make_app() in
    apps/appliances/tests. _now_utc is the REAL method (no AppDaemon dependency); tests
    override it per-call for deterministic timestamps."""
    app = aa.ActorAttribution.__new__(aa.ActorAttribution)
    app.person_entities = list(person_entities or ["person.mikkel", "person.kristine", "person.claudia"])
    app.door_entity = door_entity
    app.stability_seconds = stability_seconds
    app.door_settle_seconds = door_settle_seconds
    app.retention_hours = retention_hours
    app.publish_sensor = "sensor.household_actor"

    app._person_state = {}
    app._door_state = door_state
    app._snapshot_log = ()
    app._door_events = ()
    app._user_id_to_person = {}
    app.user_id_overrides = {}

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    return app


def snap(ts, home=(), unobs=()):
    return aa._Snapshot(ts, frozenset(home), frozenset(unobs))


def logged_at(app, level):
    return [a for a, kw in app.log_calls if kw.get("level") == level]


ROOMS = {
    "mikkel": "binary_sensor.bedroom_active",
    "kristine": "binary_sensor.kristines_room_active",
    "claudia": "binary_sensor.claudias_room_active",
}


def rows(*pairs):
    """Room-log tuple from (seconds relative to BASE, is_on) pairs: rows((-3600, True),
    (300, False)) is "on since an hour before BASE, off 300 s after it"."""
    return tuple((BASE + timedelta(seconds=s), on) for s, on in pairs)


class Clock:
    """A _now_utc stand-in whose time a test can move between calls."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_room_app(rooms, home=("mikkel", "kristine"), clock=420, **kw):
    """make_app() set up for room elimination at anchor BASE: `home` stable since long
    before it, private_rooms = ROOMS, `rooms` ({person: rows(...)}) as the room log, and
    the clock `clock` seconds after the anchor."""
    app = make_app(**kw)
    app.private_rooms = dict(ROOMS)
    app._room_log = dict(rooms)
    app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=home),)
    app._now_utc = lambda: BASE + timedelta(seconds=clock)
    return app


class SoleOccupantAndMultiOccupantTests(unittest.TestCase):
    """Rules 6 and 7: the only two branches that ever look at "how many are home" once
    presence is known stable/observable/door-quiet."""

    def test_rule7_exactly_one_person_home_is_attributed(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("claudia",)),)

        result = app.attribute("dryer_empty")

        self.assertEqual(result["person"], "claudia")
        self.assertEqual(result["method"], "sole_occupant")
        self.assertIsNone(result["reason"])
        self.assertEqual(result["people_home"], ["claudia"])
        self.assertEqual(result["version"], 1)
        self.assertIn("anchor", result)
        self.assertIn("evaluated_at", result)

    def test_rule6_two_people_home_is_multi_occupant(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel", "kristine")),)

        result = app.attribute("washer_start")

        self.assertIsNone(result["person"])
        self.assertEqual(result["method"], "unknown")
        self.assertEqual(result["reason"], "multi_occupant")
        self.assertEqual(result["people_home"], ["kristine", "mikkel"])  # sorted


class NobodyHomeTests(unittest.TestCase):
    """Rule 5: an empty home-set is logged as a presence-model gap, never used to fall
    back to "whoever we last saw home"."""

    def test_rule5_empty_home_set_is_nobody_home_and_logs_info(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=()),)

        result = app.attribute("washer_start")

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "nobody_home")
        self.assertEqual(result["people_home"], [])
        info_logs = logged_at(app, "INFO")
        self.assertTrue(any("nobody_home" in str(a) for a in info_logs))


class PresenceUnstableTests(unittest.TestCase):
    """Rule 3: the home-set itself must not move anywhere inside the stability window."""

    def test_rule3_home_set_change_inside_window_is_unstable(self):
        app = make_app()  # stability_seconds=600
        app._now_utc = lambda: BASE
        app._snapshot_log = (
            snap(BASE - timedelta(seconds=900), home=("mikkel",)),               # before window
            snap(BASE - timedelta(seconds=200), home=("mikkel", "kristine")),    # inside window
        )

        result = app.attribute("washer_start")

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "presence_unstable")

    def test_single_stable_entry_spanning_the_whole_window_is_not_unstable(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)

        result = app.attribute("washer_start")

        self.assertEqual(result["method"], "sole_occupant")


class PresenceUnobservableTests(unittest.TestCase):
    """Rule 2, and the strict home-semantics it depends on: state == "home" exactly is
    home; a zone friendly name is NOT home (but IS observable); unknown/unavailable/None
    is unobservable and must never be silently treated as away."""

    def test_rule2_unobservable_person_in_window_forces_unknown(self):
        app = make_app()
        app._now_utc = lambda: BASE
        # kristine unobservable while mikkel is (otherwise) alone - home-set itself never
        # changes (only the unobservable set gains kristine), isolating rule 2 from rule 3.
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",), unobs=("kristine",)),)

        result = app.attribute("washer_start")

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "presence_unobservable")

    def test_zone_name_state_counts_as_not_home_not_unobservable(self):
        """A person.* state of e.g. "Work" is a zone friendly name: not "home", but also
        not unknown/unavailable, so it must not block an otherwise-clean sole-occupant
        verdict for someone else - this is the behaviour MobileNotifier.get_people_home()
        gets deliberately wrong for its own (notification) purposes."""
        app = make_app()
        app._now_utc = lambda: BASE
        # kristine's raw state was "Work" at the moment this snapshot was taken; she
        # never appears in home nor unobservable - mikkel is the clean sole occupant.
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)

        result = app.attribute("washer_start")

        self.assertEqual(result["person"], "mikkel")
        self.assertEqual(result["method"], "sole_occupant")


class DoorEventRecentTests(unittest.TestCase):
    """Rule 4, and its explicitly backward-looking-only semantics."""

    def test_rule4_door_open_within_settle_window_before_anchor_blocks_attribution(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)
        app._door_events = (BASE - timedelta(seconds=60),)  # opened 1 min ago, settle=300s

        result = app.attribute("washer_start")

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "door_event_recent")

    def test_door_open_after_anchor_does_not_block_backward_looking_only(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)
        app._door_events = (BASE + timedelta(seconds=10),)  # opens AFTER the anchor

        result = app.attribute("washer_start")

        self.assertEqual(result["person"], "mikkel")
        self.assertEqual(result["method"], "sole_occupant")

    def test_door_open_outside_settle_window_before_anchor_does_not_block(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)
        app._door_events = (BASE - timedelta(seconds=301),)  # 1s outside the 300s settle window

        result = app.attribute("washer_start")

        self.assertEqual(result["method"], "sole_occupant")


class NoHistoryTests(unittest.TestCase):
    """Rule 1, in each of the ways it can fire: an empty log, a log that doesn't reach
    back far enough to cover the stability window, and an anchor older than the
    retention window even when the log would otherwise support an answer."""

    def test_empty_log_is_no_history(self):
        app = make_app()
        app._now_utc = lambda: BASE

        result = app.attribute("washer_start")

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "no_history")

    def test_log_not_reaching_back_to_window_start_is_no_history(self):
        app = make_app()
        app._now_utc = lambda: BASE
        # Only entry is INSIDE the window (100s ago); nothing at or before win_start
        # (600s ago) - there is no carry-in, so the first 500s of the window is a blind
        # spot we cannot vouch for.
        app._snapshot_log = (snap(BASE - timedelta(seconds=100), home=("mikkel",)),)

        result = app.attribute("washer_start")

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "no_history")

    def test_anchor_older_than_retention_window_is_no_history_even_with_supporting_log(self):
        app = make_app(retention_hours=1)
        app._now_utc = lambda: BASE
        old_anchor = BASE - timedelta(hours=2)  # older than retention_hours=1
        app._snapshot_log = (snap(old_anchor - timedelta(seconds=900), home=("mikkel",)),)

        result = app.attribute("washer_start", at=old_anchor)

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "no_history")


class RetentionPruningBoundaryTests(unittest.TestCase):
    """Regression guard for a subtle off-by-window pruning bug found during
    implementation: pruning purely at (now - retention_hours) can discard the one
    carry-in entry a query anchored right at the retention edge needs for its OWN
    stability_seconds lookback (a nearer surviving entry doesn't reach far enough back
    to serve as carry for that exact anchor), producing a false no_history right where
    the retained window is supposed to still work. _prune_log must reach
    stability_seconds further back than the plain retention cutoff (see its docstring)."""

    def test_anchor_at_retention_edge_still_resolves_via_older_carry_entry(self):
        app = make_app(retention_hours=1, stability_seconds=600)
        now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        app._now_utc = lambda: now
        e1 = datetime(2026, 7, 27, 9, 0, 0, tzinfo=timezone.utc)     # far older than any cutoff
        # Newer than (retention cutoff 11:00 - stability_seconds 10min = 10:50), but
        # still older than the plain retention cutoff (11:00) itself.
        e2 = datetime(2026, 7, 27, 10, 55, 0, tzinfo=timezone.utc)
        app._snapshot_log = (snap(e1, home=("mikkel",)), snap(e2, home=("mikkel",)))
        app._prune_log(now)

        anchor = datetime(2026, 7, 27, 11, 0, 0, tzinfo=timezone.utc)  # now - retention_hours
        result = app.attribute("washer_start", at=anchor)

        self.assertEqual(result["person"], "mikkel")
        self.assertEqual(result["method"], "sole_occupant")


class DoorGuardHealthNoteTests(unittest.TestCase):
    """The door-guard-unavailable note: attribution still proceeds (absence of a
    recorded edge is not proof none happened) but the ledger is told the evidence was
    weaker than usual."""

    def test_reason_note_present_when_door_entity_is_unobservable(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)
        app._door_state = "unavailable"

        result = app.attribute("washer_start")

        self.assertEqual(result["person"], "mikkel")  # "still attribute"
        self.assertEqual(result.get("reason_note"), "door_guard_unavailable")

    def test_reason_note_absent_when_door_entity_observable(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)
        app._door_state = "off"

        result = app.attribute("washer_start")

        self.assertNotIn("reason_note", result)


class DashboardActionAttributionTests(unittest.TestCase):
    """_by_dashboard_action: a dashboard-issued action's context.user_id resolves
    straight to a person and BYPASSES the sole-occupancy gate entirely - the whole point
    is that it must work even when the seven-rule table would otherwise refuse
    (multi_occupant here included)."""

    def test_resolves_mapped_id_even_with_two_people_home(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._user_id_to_person = {"abc123": "kristine"}
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel", "kristine")),)

        result = app.attribute("washer_emptied", user_id="abc123")

        self.assertEqual(result["person"], "kristine")
        self.assertEqual(result["method"], "dashboard_action")
        self.assertIsNone(result["reason"])
        self.assertEqual(result["people_home"], ["kristine", "mikkel"])
        self.assertEqual(result["version"], 1)
        self.assertNotIn("reason_note", result)

    def test_dashed_and_undashed_id_resolve_to_the_same_person(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._user_id_to_person = {"abc123def456": "claudia"}
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("claudia",)),)

        dashed = app.attribute("washer_emptied", user_id="ABC123-DEF456")
        undashed = app.attribute("washer_emptied", user_id="abc123def456")

        self.assertEqual(dashed["person"], "claudia")
        self.assertEqual(dashed["method"], "dashboard_action")
        self.assertEqual(undashed["person"], "claudia")
        self.assertEqual(undashed["method"], "dashboard_action")

    def test_unmapped_id_falls_through_to_sole_occupancy(self):
        """An id simply absent from the map (e.g. it belongs to a non-household person
        entity - see UserIdMapSafetyTests for how _refresh_user_map keeps it that way)
        is treated exactly as if no id had been given at all."""
        app = make_app()
        app._now_utc = lambda: BASE
        app._user_id_to_person = {"abc123": "mikkel"}  # non-empty, but not this id
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("kristine",)),)

        result = app.attribute("washer_emptied", user_id="someone-elses-id")

        self.assertEqual(result["person"], "kristine")
        self.assertEqual(result["method"], "sole_occupant")

    def test_empty_map_falls_through_to_sole_occupancy(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._user_id_to_person = {}
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)

        result = app.attribute("washer_emptied", user_id="abc123")

        self.assertEqual(result["person"], "mikkel")
        self.assertEqual(result["method"], "sole_occupant")

    def test_no_user_id_falls_through_to_sole_occupancy(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._user_id_to_person = {"abc123": "mikkel"}
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)

        result = app.attribute("washer_emptied")  # no user_id - e.g. the legacy event path

        self.assertEqual(result["method"], "sole_occupant")

    def test_resolved_actor_not_currently_home_sets_reason_note(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._user_id_to_person = {"abc123": "kristine"}
        # kristine pressed the button, but the home-set at the anchor is mikkel alone.
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)

        result = app.attribute("washer_emptied", user_id="abc123")

        self.assertEqual(result["person"], "kristine")
        self.assertEqual(result.get("reason_note"), "actor_not_home")

    def test_resolved_actor_at_home_has_no_reason_note(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._user_id_to_person = {"abc123": "mikkel"}
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)

        result = app.attribute("washer_emptied", user_id="abc123")

        self.assertNotIn("reason_note", result)

    def test_no_snapshot_coverage_gives_empty_people_home_but_still_resolves(self):
        """Unlike _by_sole_occupancy's rule 1, a dashboard action does not need
        stability_seconds of history - it bypasses that gate entirely - so an anchor with
        zero log coverage still resolves the actor; people_home is just empty (never
        guessed)."""
        app = make_app()
        app._now_utc = lambda: BASE
        app._user_id_to_person = {"abc123": "mikkel"}
        # no _snapshot_log at all

        result = app.attribute("washer_emptied", user_id="abc123")

        self.assertEqual(result["person"], "mikkel")
        self.assertEqual(result["method"], "dashboard_action")
        self.assertEqual(result["people_home"], [])
        self.assertEqual(result.get("reason_note"), "actor_not_home")


class UserIdMapSafetyTests(unittest.TestCase):
    """_refresh_user_map must resolve user_id -> person ONLY through self.person_entities.
    This HA instance also links other, non-household accounts (a wall-tablet kiosk user, a
    Node-RED service account, an AppDaemon service account) to their own person.* entities
    - a naive domain-wide scan would let a tap from any of those get misattributed to a
    fake "person" here."""

    def test_only_configured_person_entities_are_queried(self):
        app = make_app(person_entities=["person.mikkel", "person.kristine", "person.claudia"])
        queried = []
        states = {
            "person.mikkel": {"attributes": {"user_id": "mikkel-uid"}},
            "person.kristine": {"attributes": {"user_id": "kristine-uid"}},
            "person.claudia": {"attributes": {"user_id": "claudia-uid"}},
            # A real entity in this HA instance, deliberately absent from person_entities
            # (e.g. the wall-tablet kiosk account) - must never be looked up at all.
            "person.wall_tablet_kiosk": {"attributes": {"user_id": "kiosk-uid"}},
        }

        def get_state(entity, attribute=None, **kw):
            queried.append((entity, attribute))
            return states.get(entity)

        app.get_state = get_state

        app._refresh_user_map()

        self.assertEqual(sorted(e for e, _a in queried), ["person.claudia", "person.kristine", "person.mikkel"])
        self.assertEqual(
            app._user_id_to_person,
            {"mikkeluid": "mikkel", "kristineuid": "kristine", "claudiauid": "claudia"},
        )
        self.assertNotIn("kioskuid", app._user_id_to_person)

    def test_entity_with_no_user_id_is_skipped(self):
        app = make_app(person_entities=["person.mikkel"])
        app.get_state = lambda entity, attribute=None, **kw: {"attributes": {}}

        app._refresh_user_map()

        self.assertEqual(app._user_id_to_person, {})

    def test_get_state_failure_for_one_entity_does_not_block_the_others(self):
        app = make_app(person_entities=["person.mikkel", "person.kristine"])

        def get_state(entity, attribute=None, **kw):
            if entity == "person.mikkel":
                raise RuntimeError("boom")
            return {"attributes": {"user_id": "kristine-uid"}}

        app.get_state = get_state

        try:
            app._refresh_user_map()
        except Exception as e:  # pragma: no cover
            self.fail(f"_refresh_user_map raised: {e}")

        self.assertEqual(app._user_id_to_person, {"kristineuid": "kristine"})

    def test_user_id_overrides_are_merged_in_after_and_normalized(self):
        app = make_app(person_entities=["person.mikkel"])
        app.get_state = lambda entity, attribute=None, **kw: {"attributes": {"user_id": "mikkel-uid"}}
        app.user_id_overrides = {"KIOSK-UID": "mikkel"}  # dashed/uppercase on purpose

        app._refresh_user_map()

        self.assertEqual(app._user_id_to_person, {"mikkeluid": "mikkel", "kioskuid": "mikkel"})

    def test_refresh_replaces_the_map_wholesale_not_in_place(self):
        """Same thread-safety convention as self._snapshot_log elsewhere in this file: a
        fresh dict object each time, never mutated in place, so a worker thread mid-
        iteration of the old map can never observe a torn write."""
        app = make_app(person_entities=["person.mikkel"])
        app.get_state = lambda entity, attribute=None, **kw: {"attributes": {"user_id": "mikkel-uid"}}
        old_map = app._user_id_to_person

        app._refresh_user_map()

        self.assertIsNot(app._user_id_to_person, old_map)


class NeverRaisesTests(unittest.TestCase):
    """attribute() must never raise, even given a completely bare instance (the
    pre-initialize() case) or internally corrupted state, and even when self.log itself
    doesn't exist yet."""

    def test_bare_uninitialized_instance_returns_no_history_without_raising(self):
        app = aa.ActorAttribution.__new__(aa.ActorAttribution)  # nothing set at all

        result = app.attribute("dishwasher_empty")

        self.assertIsNone(result["person"])
        self.assertEqual(result["method"], "unknown")
        self.assertEqual(result["reason"], "no_history")
        self.assertEqual(result["people_home"], [])
        self.assertEqual(result["version"], 1)
        self.assertIn("anchor", result)
        self.assertIn("evaluated_at", result)

    def test_corrupted_log_and_missing_log_method_falls_back_safely(self):
        app = aa.ActorAttribution.__new__(aa.ActorAttribution)
        app._snapshot_log = ("not-a-snapshot",)  # real entries are _Snapshot namedtuples
        # app.log is deliberately left unset, exercising attribute()'s own
        # try/except-around-self.log fallback (self.log missing entirely is exactly the
        # pre-initialize() shape in production too - see class docstring).

        try:
            result = app.attribute("washer_start")
        except Exception as e:  # pragma: no cover - the assertion below is the real check
            self.fail(f"attribute() raised: {e}")

        self.assertIsNone(result["person"])
        self.assertEqual(result["method"], "unknown")
        self.assertEqual(result["reason"], "no_history")

    def test_non_numeric_config_falls_back_safely(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)
        app.stability_seconds = "not-a-number"  # corrupt config value

        try:
            result = app.attribute("washer_start")
        except Exception as e:  # pragma: no cover
            self.fail(f"attribute() raised: {e}")

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "no_history")
        self.assertTrue(logged_at(app, "ERROR"))


class ProbeTests(unittest.TestCase):
    def test_probe_returns_well_formed_dict_anchored_at_now(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)

        result = app.probe()

        for key in ("person", "method", "reason", "people_home", "anchor", "evaluated_at", "version"):
            self.assertIn(key, result)
        self.assertEqual(result["anchor"], result["evaluated_at"])
        self.assertEqual(result["version"], 1)
        self.assertIsInstance(result["people_home"], list)
        self.assertIn(result["method"], ("sole_occupant", "unknown"))
        self.assertEqual(result["person"], "mikkel")

    def test_probe_ignores_a_populated_user_id_map(self):
        """probe() passes no user_id (module docstring: 'probe() stays unchanged') - a
        populated user-id map must never change its verdict shape or resolution path."""
        app = make_app()
        app._now_utc = lambda: BASE
        app._user_id_to_person = {"abc123": "kristine"}
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel",)),)

        result = app.probe()

        self.assertEqual(result["person"], "mikkel")
        self.assertEqual(result["method"], "sole_occupant")
        for key in ("person", "method", "reason", "people_home", "anchor", "evaluated_at", "version"):
            self.assertIn(key, result)


class FlattenHistoryTests(unittest.TestCase):
    """_flatten_history must normalise every shape AppDaemon's get_history can return -
    same idiom as washer_monitor.py/dryer_monitor.py's own copies."""

    def test_plain_list_passthrough(self):
        app = make_app()
        self.assertEqual(app._flatten_history([{"state": "on"}]), [{"state": "on"}])

    def test_list_of_lists_unwraps_first(self):
        app = make_app()
        self.assertEqual(app._flatten_history([[{"state": "on"}]]), [{"state": "on"}])

    def test_dict_keyed_by_entity(self):
        app = make_app()
        hist = {"person.mikkel": [{"state": "home"}]}
        self.assertEqual(app._flatten_history(hist, "person.mikkel"), [{"state": "home"}])

    def test_empty_or_none_returns_empty_list(self):
        app = make_app()
        self.assertEqual(app._flatten_history(None), [])
        self.assertEqual(app._flatten_history([]), [])


class LiveListenerTests(unittest.TestCase):
    """_on_person_change / _on_door_change: append-on-real-change (never on an
    away<->away or on<->on non-edge), and publish on every invocation."""

    def test_person_change_appends_snapshot_and_publishes(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app.set_state_calls = []
        app.set_state = lambda entity, **kw: app.set_state_calls.append((entity, kw))

        app._on_person_change("person.mikkel", "state", "not_home", "home", {"person": "mikkel"})

        self.assertEqual(len(app._snapshot_log), 1)
        self.assertEqual(app._snapshot_log[-1].home, frozenset({"mikkel"}))
        self.assertEqual(len(app.set_state_calls), 1)
        self.assertEqual(app.set_state_calls[0][0], "sensor.household_actor")

    def test_person_change_between_two_away_states_does_not_grow_log(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app.set_state = lambda *a, **kw: None
        app._person_state = {"mikkel": "not_home"}
        app._snapshot_log = (snap(BASE - timedelta(seconds=10)),)

        # "not_home" -> "Gym": both classify as "away", not a real transition.
        app._on_person_change("person.mikkel", "state", "not_home", "Gym", {"person": "mikkel"})

        self.assertEqual(len(app._snapshot_log), 1)

    def test_door_open_edge_is_recorded_once(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app.set_state = lambda *a, **kw: None

        app._on_door_change(app.door_entity, "state", "off", "on", {})

        self.assertEqual(app._door_events, (BASE,))
        self.assertEqual(app._door_state, "on")

    def test_door_change_between_non_on_states_is_not_an_open_edge(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app.set_state = lambda *a, **kw: None

        app._on_door_change(app.door_entity, "state", "unavailable", "off", {})

        self.assertEqual(app._door_events, ())
        self.assertEqual(app._door_state, "off")

    def test_never_raises_on_garbage_input(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app.set_state = lambda *a, **kw: None
        try:
            app._on_person_change("person.mikkel", "state", None, None, {})
            app._on_door_change(app.door_entity, "state", None, None, {})
        except Exception as e:  # pragma: no cover
            self.fail(f"listener raised: {e}")


class TickReconcileTests(unittest.TestCase):
    """run_every(self._tick, ...): a fresh get_state disagreeing with the cached view is
    applied as a new transition stamped now and logged at WARNING (defends against
    AppDaemon 4.5.13 serving a stale get_state even after the matching listen_state
    callback already fired)."""

    def test_tick_detects_person_drift_and_logs_warning(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._person_state = {"mikkel": "not_home", "kristine": "not_home", "claudia": "not_home"}
        states = {
            "person.mikkel": "home",  # drifted: cache says not_home, fresh read says home
            "person.kristine": "not_home",
            "person.claudia": "not_home",
            "binary_sensor.apartment_door_open": "off",
        }
        app.get_state = lambda entity, **kw: states.get(entity)
        app.set_state = lambda *a, **kw: None

        app._tick({})

        self.assertEqual(app._person_state["mikkel"], "home")
        warnings = logged_at(app, "WARNING")
        self.assertTrue(any("person.mikkel" in str(a) for a in warnings))
        self.assertEqual(len(app._snapshot_log), 1)
        self.assertEqual(app._snapshot_log[-1].home, frozenset({"mikkel"}))

    def test_tick_with_no_disagreement_does_not_grow_log_or_warn(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._person_state = {"mikkel": "home", "kristine": "not_home", "claudia": "not_home"}
        app._snapshot_log = (snap(BASE - timedelta(seconds=10), home=("mikkel",)),)
        states = {
            "person.mikkel": "home",
            "person.kristine": "not_home",
            "person.claudia": "not_home",
            "binary_sensor.apartment_door_open": "off",
        }
        app.get_state = lambda entity, **kw: states.get(entity)
        app.set_state = lambda *a, **kw: None

        app._tick({})

        self.assertEqual(len(app._snapshot_log), 1)
        self.assertEqual(logged_at(app, "WARNING"), [])

    def test_tick_treats_a_freshly_discovered_open_door_as_an_edge(self):
        """Fail-safe direction: if the cache never saw the door open (a missed live
        event), the tick must still count it as an edge rather than silently ignore
        it - a stale-store blip may cost recall, but must never cost precision."""
        app = make_app()
        app._now_utc = lambda: BASE
        app._person_state = {"mikkel": "home", "kristine": "not_home", "claudia": "not_home"}
        app._door_state = "off"
        states = {
            "person.mikkel": "home",
            "person.kristine": "not_home",
            "person.claudia": "not_home",
            "binary_sensor.apartment_door_open": "on",
        }
        app.get_state = lambda entity, **kw: states.get(entity)
        app.set_state = lambda *a, **kw: None

        app._tick({})

        self.assertEqual(app._door_events, (BASE,))

    def test_tick_get_state_failure_for_one_entity_does_not_block_the_others(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._person_state = {"mikkel": "not_home", "kristine": "not_home", "claudia": "not_home"}

        def get_state(entity, **kw):
            if entity == "person.mikkel":
                raise RuntimeError("boom")
            if entity == "person.kristine":
                return "home"
            return "not_home"

        app.get_state = get_state
        app.set_state = lambda *a, **kw: None

        try:
            app._tick({})
        except Exception as e:  # pragma: no cover
            self.fail(f"_tick raised: {e}")

        self.assertEqual(app._person_state["kristine"], "home")
        self.assertEqual(app._person_state["mikkel"], "not_home")  # untouched by the failure


class BackfillTests(unittest.TestCase):
    """_backfill: merges per-person history into the log, clamps the first row of
    already-known state up to window_start, and defaults a person with NO history at
    all to unobservable rather than silently omitting them."""

    def test_merges_multi_person_history_and_defaults_missing_person_to_unobservable(self):
        app = make_app(person_entities=["person.mikkel", "person.kristine"])
        app._now_utc = lambda: BASE
        app.set_state = lambda *a, **kw: None
        window_start = BASE - timedelta(hours=app.retention_hours)

        history = {
            "person.mikkel": [
                {"last_changed": (window_start - timedelta(hours=2)).isoformat(), "state": "home"},
            ],
            "person.kristine": [],  # no data at all
            "binary_sensor.apartment_door_open": [
                {"last_changed": (BASE - timedelta(minutes=30)).isoformat(), "state": "off"},
                {"last_changed": (BASE - timedelta(minutes=10)).isoformat(), "state": "on"},
            ],
        }
        app.get_history = lambda entity_id=None, start_time=None, end_time=None: history.get(entity_id, [])

        app._backfill({})

        self.assertTrue(app._snapshot_log)
        self.assertIn("mikkel", app._snapshot_log[-1].home)
        self.assertIn("kristine", app._snapshot_log[-1].unobservable)
        self.assertEqual(app._snapshot_log[0].ts, window_start)  # clamped up to the window start
        self.assertEqual(len(app._door_events), 1)
        self.assertEqual(app._door_state, "on")
        self.assertTrue(logged_at(app, "WARNING"))  # kristine's missing history is flagged

    def test_get_history_exception_for_everyone_starts_from_an_empty_log(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app.set_state = lambda *a, **kw: None

        def get_history(entity_id=None, start_time=None, end_time=None):
            raise RuntimeError("recorder unavailable")

        app.get_history = get_history

        try:
            app._backfill({})
        except Exception as e:  # pragma: no cover
            self.fail(f"_backfill raised: {e}")

        self.assertEqual(app._snapshot_log, ())
        self.assertEqual(app._door_events, ())
        result = app.attribute("washer_start")
        self.assertEqual(result["reason"], "no_history")

    def test_first_row_newer_than_window_start_is_not_pulled_earlier(self):
        """If an entity's very first known change happens to land INSIDE the window
        (nothing recorded before it), backfill must not assume that state also held for
        the earlier, unknown portion of the window."""
        app = make_app(person_entities=["person.mikkel"])
        app._now_utc = lambda: BASE
        app.set_state = lambda *a, **kw: None
        window_start = BASE - timedelta(hours=app.retention_hours)
        first_seen = window_start + timedelta(hours=1)  # entity's real first-ever change

        history = {
            "person.mikkel": [{"last_changed": first_seen.isoformat(), "state": "home"}],
            "binary_sensor.apartment_door_open": [],
        }
        app.get_history = lambda entity_id=None, start_time=None, end_time=None: history.get(entity_id, [])

        app._backfill({})

        self.assertEqual(app._snapshot_log[0].ts, first_seen)  # NOT clamped down to window_start


class PublishTests(unittest.TestCase):
    """_publish: sensor state is always a non-empty word (person key or a short unknown
    code), and falsy-prone attributes use "" rather than None (AppDaemon 4.5.13 silently
    drops None/False/0 attributes before the /api/states POST - see smart_cooling.py)."""

    def test_publish_uses_empty_string_sentinels_when_nothing_recorded_yet(self):
        app = make_app()
        app._now_utc = lambda: BASE
        calls = []
        app.set_state = lambda entity, **kw: calls.append((entity, kw))

        app._publish()

        self.assertEqual(len(calls), 1)
        entity, kw = calls[0]
        self.assertEqual(entity, "sensor.household_actor")
        self.assertEqual(kw["state"], "no_history")
        attrs = kw["attributes"]
        self.assertEqual(attrs["stable_since"], "")
        self.assertEqual(attrs["last_door_open"], "")
        self.assertIn("person.mikkel", attrs["source_entities"])
        self.assertIn(app.door_entity, attrs["source_entities"])
        self.assertNotEqual(attrs["computed_at"], "")
        self.assertNotIn(None, attrs.values())
        self.assertNotIn(False, [v for v in attrs.values() if isinstance(v, bool)])

    def test_publish_state_is_the_persons_key_when_attributed(self):
        app = make_app()
        app._now_utc = lambda: BASE
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("kristine",)),)
        calls = []
        app.set_state = lambda entity, **kw: calls.append((entity, kw))

        app._publish()

        self.assertEqual(calls[0][1]["state"], "kristine")
        self.assertEqual(calls[0][1]["attributes"]["people_home"], ["kristine"])

    def test_publish_state_for_each_unknown_reason_is_never_empty(self):
        app = make_app()
        app._now_utc = lambda: BASE
        calls = []
        app.set_state = lambda entity, **kw: calls.append((entity, kw))
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel", "kristine")),)

        app._publish()

        state = calls[-1][1]["state"]
        self.assertTrue(state)
        self.assertEqual(state, "multi")

    def test_publish_stays_multi_while_room_elimination_is_pending(self):
        app = make_room_app({"mikkel": rows((-3600, True))}, clock=0)  # probe anchors at now
        calls = []
        app.set_state = lambda entity, **kw: calls.append((entity, kw))

        app._publish()

        self.assertEqual(calls[0][1]["state"], "multi")

    def test_publish_state_for_room_contradiction_is_its_own_word(self):
        app = make_app()
        app._now_utc = lambda: BASE
        calls = []
        app.set_state = lambda entity, **kw: calls.append((entity, kw))
        app.probe = lambda: {"person": None, "reason": "room_contradiction", "people_home": ["kristine", "mikkel"]}

        app._publish()

        self.assertEqual(calls[0][1]["state"], "contradiction")


class RoomEliminationTests(unittest.TestCase):
    """Phase 3, past every hard stop with 2+ people home: a resident whose private room was
    occupied from anchor - 60 s through anchor + 360 s is crossed off, and exactly one
    left is named. Anchor is BASE; the default clock is 420 s after it, past the 390 s
    (hold + margin) before which nobody may be named."""

    def ask(self, rooms, clock=420, **kw):
        return make_room_app(rooms, clock=clock, **kw).attribute("washer_emptied", at=BASE)

    def test_names_the_one_remaining_resident(self):
        result = self.ask({"mikkel": rows((-3600, True))})

        self.assertEqual(result["person"], "kristine")
        self.assertEqual(result["method"], "room_elimination")
        self.assertIsNone(result["reason"])
        self.assertEqual(result["people_home"], ["kristine", "mikkel"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["anchor"], aa.ActorAttribution._iso(BASE))
        self.assertNotIn("pending_until", result)

    def test_two_of_three_crossed_off_names_the_third(self):
        result = self.ask(
            {"mikkel": rows((-3600, True)), "claudia": rows((-3600, True))},
            home=("mikkel", "kristine", "claudia"),
        )

        self.assertEqual(result["person"], "kristine")
        self.assertEqual(result["method"], "room_elimination")

    def test_one_of_three_crossed_off_leaves_two_candidates(self):
        result = self.ask({"mikkel": rows((-3600, True))}, home=("mikkel", "kristine", "claudia"))

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "multi_occupant")

    def test_a_resident_without_a_private_room_is_never_crossed_off(self):
        app = make_room_app({"kristine": rows((-3600, True))})
        app.private_rooms = {"kristine": ROOMS["kristine"]}  # mikkel has no room entry

        result = app.attribute("washer_emptied", at=BASE)

        self.assertEqual(result["person"], "mikkel")
        self.assertEqual(result["method"], "room_elimination")

    def test_a_room_of_someone_not_home_is_irrelevant(self):
        result = self.ask({"claudia": rows((-3600, True))})  # claudia is not in the home-set

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "multi_occupant")

    def test_strict_window_does_not_cross_off(self):
        cases = {
            "room on only from 30 s before the event": rows((-3600, False), (-30, True)),
            "room on only from 59 s before the event": rows((-59, True)),
            "off row inside the window": rows((-3600, True), (300, False)),
            "off row right after the event": rows((-3600, True), (1, False)),
            "off row exactly at the end of the hold": rows((-3600, True), (360, False)),
            "off and back on inside the window": rows((-3600, True), (100, False), (130, True)),
            "off the whole time": rows((-3600, False)),
            "no room history at all": (),
        }
        for name, room_rows in cases.items():
            with self.subTest(name):
                result = self.ask({"mikkel": room_rows})

                self.assertIsNone(result["person"])
                self.assertEqual(result["reason"], "multi_occupant")

    def test_window_edges_that_still_cross_off(self):
        cases = {
            "room on from exactly 60 s before the event": rows((-60, True)),
            "off row just past the hold": rows((-3600, True), (361, False)),
            "off and back on before the lead instant": rows((-3600, True), (-200, False), (-100, True)),
        }
        for name, room_rows in cases.items():
            with self.subTest(name):
                result = self.ask({"mikkel": room_rows})

                self.assertEqual(result["person"], "kristine")
                self.assertEqual(result["method"], "room_elimination")

    def test_unavailable_and_unknown_room_states_count_as_off(self):
        for bad in ("unavailable", "unknown"):
            with self.subTest(bad):
                app = make_room_app({})
                clock = Clock(BASE - timedelta(hours=1))
                app._now_utc = clock
                kwargs = {"person": "mikkel"}
                app._on_room_change(ROOMS["mikkel"], "state", "off", "on", kwargs)
                clock.t = BASE + timedelta(seconds=100)
                app._on_room_change(ROOMS["mikkel"], "state", "on", bad, kwargs)
                clock.t = BASE + timedelta(seconds=130)
                app._on_room_change(ROOMS["mikkel"], "state", bad, "on", kwargs)
                clock.t = BASE + timedelta(seconds=420)

                result = app.attribute("washer_emptied", at=BASE)

                self.assertIsNone(result["person"])
                self.assertEqual(result["reason"], "multi_occupant")

    def test_everyone_crossed_off_is_a_contradiction_not_a_guess(self):
        result = self.ask({"mikkel": rows((-3600, True)), "kristine": rows((-3600, True))})

        self.assertIsNone(result["person"])
        self.assertEqual(result["method"], "unknown")
        self.assertEqual(result["reason"], "room_contradiction")
        self.assertEqual(result["people_home"], ["kristine", "mikkel"])
        self.assertNotIn("pending_until", result)

    def test_nobody_crossed_off_is_multi_occupant(self):
        result = self.ask({})

        self.assertIsNone(result["person"])
        self.assertEqual(result["method"], "unknown")
        self.assertEqual(result["reason"], "multi_occupant")
        self.assertNotIn("pending_until", result)


class RoomEliminationPendingTests(unittest.TestCase):
    """Before anchor + 360 s hold + 30 s margin nobody is named, because a cross-off can
    still be revoked by an off row not recorded yet: at most one candidate -> multi_occupant
    plus pending_until (a name is still possible); two or more -> already final."""

    PENDING_UNTIL = "2026-07-27T12:06:30Z"  # BASE + 390 s

    def ask(self, rooms, clock, **kw):
        return make_room_app(rooms, clock=clock, **kw).attribute("washer_emptied", at=BASE)

    def test_one_candidate_before_the_margin_is_pending_not_named(self):
        for clock in (0, 1, 389):
            with self.subTest(clock=clock):
                result = self.ask({"mikkel": rows((-3600, True))}, clock)

                self.assertIsNone(result["person"])
                self.assertEqual(result["method"], "unknown")
                self.assertEqual(result["reason"], "multi_occupant")
                self.assertEqual(result["people_home"], ["kristine", "mikkel"])
                self.assertEqual(result["pending_until"], self.PENDING_UNTIL)

    def test_nobody_left_before_the_margin_is_pending_too(self):
        result = self.ask({"mikkel": rows((-3600, True)), "kristine": rows((-3600, True))}, 1)

        self.assertEqual(result["reason"], "multi_occupant")
        self.assertEqual(result["pending_until"], self.PENDING_UNTIL)

    def test_two_candidates_are_final_straight_away(self):
        result = self.ask({}, 1)

        self.assertEqual(result["reason"], "multi_occupant")
        self.assertNotIn("pending_until", result)

    def test_names_from_exactly_the_margin_on(self):
        rooms = {"mikkel": rows((-3600, True))}

        before = self.ask(rooms, 389)
        at_margin = self.ask(rooms, 390)

        self.assertIsNone(before["person"])
        self.assertEqual(at_margin["person"], "kristine")
        self.assertEqual(at_margin["method"], "room_elimination")

    def test_an_off_row_recorded_later_turns_pending_into_final_multi_occupant(self):
        app = make_room_app({"mikkel": rows((-3600, True), (200, False))}, clock=1)

        first = app.attribute("washer_emptied", at=BASE)
        app._now_utc = lambda: BASE + timedelta(seconds=250)
        mid = app.attribute("washer_emptied", at=BASE)
        app._now_utc = lambda: BASE + timedelta(seconds=420)
        last = app.attribute("washer_emptied", at=BASE)

        self.assertEqual(first["pending_until"], self.PENDING_UNTIL)  # the off row is not recorded yet
        for later in (mid, last):
            self.assertIsNone(later["person"])
            self.assertEqual(later["reason"], "multi_occupant")
            self.assertNotIn("pending_until", later)

    def test_the_window_is_cut_at_the_evaluation_time(self):
        """An off row stamped after the clock is invisible: cut at clock 100 the room still
        reads occupied (pending), once the clock passes the row it is a final multi."""
        rooms = {"mikkel": rows((-3600, True), (300, False))}

        early = self.ask(rooms, 100)
        later = self.ask(rooms, 310)

        self.assertEqual(early["pending_until"], self.PENDING_UNTIL)
        self.assertNotIn("pending_until", later)
        self.assertIsNone(later["person"])

    def test_pending_until_is_rounded_up_so_waiting_for_it_resolves(self):
        anchor = BASE + timedelta(milliseconds=500)  # real boundary: 12:06:30.5
        app = make_room_app({"mikkel": rows((-3600, True))})
        app._now_utc = lambda: anchor + timedelta(seconds=1)

        pending = app.attribute("washer_emptied", at=anchor)
        app._now_utc = lambda: aa._parse_utc("2026-07-27T12:06:30Z")  # the truncated instant
        truncated = app.attribute("washer_emptied", at=anchor)
        app._now_utc = lambda: aa._parse_utc(pending["pending_until"])
        waited = app.attribute("washer_emptied", at=anchor)

        self.assertEqual(pending["pending_until"], "2026-07-27T12:06:31Z")
        self.assertIn("pending_until", truncated)
        self.assertEqual(waited["person"], "kristine")
        self.assertEqual(waited["method"], "room_elimination")

    def test_probe_is_never_named_by_elimination(self):
        """probe() anchors at now, always inside the pending span."""
        app = make_room_app({"mikkel": rows((-3600, True))}, clock=0)

        result = app.probe()

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "multi_occupant")
        self.assertIn("pending_until", result)


class RoomEliminationChainTests(unittest.TestCase):
    """Where elimination sits in the chain: after the dashboard tap and after every hard
    stop of _by_sole_occupancy, and only ever for multi_occupant."""

    ROOM_ON = {"mikkel": rows((-3600, True))}  # alone, this would name kristine

    def test_each_hard_stop_wins_over_elimination(self):
        two_home = ("mikkel", "kristine")
        cases = {
            "presence_unstable": lambda app: setattr(app, "_snapshot_log", (
                snap(BASE - timedelta(seconds=900), home=("mikkel",)),
                snap(BASE - timedelta(seconds=200), home=two_home),
            )),
            "presence_unobservable": lambda app: setattr(app, "_snapshot_log", (
                snap(BASE - timedelta(seconds=900), home=two_home, unobs=("claudia",)),
            )),
            "door_event_recent": lambda app: setattr(app, "_door_events", (BASE - timedelta(seconds=60),)),
            "no_history": lambda app: setattr(app, "_snapshot_log", ()),
            "nobody_home": lambda app: setattr(app, "_snapshot_log", (snap(BASE - timedelta(seconds=900)),)),
        }
        for reason, break_it in cases.items():
            for clock in (1, 420):
                with self.subTest(reason=reason, clock=clock):
                    app = make_room_app(self.ROOM_ON, clock=clock)
                    break_it(app)

                    result = app.attribute("washer_emptied", at=BASE)

                    self.assertIsNone(result["person"])
                    self.assertEqual(result["method"], "unknown")
                    self.assertEqual(result["reason"], reason)
                    self.assertNotIn("pending_until", result)

    def test_dashboard_tap_wins_over_elimination(self):
        app = make_room_app(self.ROOM_ON)
        app._user_id_to_person = {"abc123": "mikkel"}

        result = app.attribute("washer_emptied", at=BASE, user_id="abc123")

        self.assertEqual(result["person"], "mikkel")
        self.assertEqual(result["method"], "dashboard_action")

    def test_sole_occupant_is_untouched_by_configured_rooms(self):
        app = make_room_app({"mikkel": rows((-3600, True))}, home=("kristine",))

        result = app.attribute("washer_emptied", at=BASE)

        self.assertEqual(result["person"], "kristine")
        self.assertEqual(result["method"], "sole_occupant")
        self.assertNotIn("pending_until", result)

    def test_inert_without_rooms(self):
        self.assertEqual(aa.ActorAttribution.private_rooms, {})
        self.assertEqual(aa.ActorAttribution._room_log, {})
        for clock in (1, 420):
            with self.subTest(clock=clock):
                app = make_app()  # no room attribute set at all: the class defaults apply
                app._now_utc = lambda: BASE + timedelta(seconds=clock)
                app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel", "kristine")),)

                result = app.attribute("washer_emptied", at=BASE)

                self.assertIsNone(result["person"])
                self.assertEqual(result["reason"], "multi_occupant")
                self.assertNotIn("pending_until", result)

    def test_door_guard_note_still_rides_on_a_named_verdict(self):
        app = make_room_app(self.ROOM_ON)
        app._door_state = "unavailable"

        result = app.attribute("washer_emptied", at=BASE)

        self.assertEqual(result["method"], "room_elimination")
        self.assertEqual(result.get("reason_note"), "door_guard_unavailable")

    def test_corrupt_room_state_falls_back_safely(self):
        for what, corrupt in (
            ("room log rows", lambda app: setattr(app, "_room_log", {"mikkel": "garbage"})),
            ("hold seconds", lambda app: setattr(app, "room_hold_seconds", "soon")),
        ):
            with self.subTest(what):
                app = make_room_app(self.ROOM_ON)
                corrupt(app)

                try:
                    result = app.attribute("washer_emptied", at=BASE)
                except Exception as e:  # pragma: no cover
                    self.fail(f"attribute() raised: {e}")

                self.assertIsNone(result["person"])
                self.assertEqual(result["reason"], "no_history")
                self.assertTrue(logged_at(app, "ERROR"))


class RoomLogListenerTests(unittest.TestCase):
    """_on_room_change: one row per real on/off transition, stamped with the clock;
    anything but "on" is off."""

    def test_rows_are_change_only_and_anything_but_on_is_off(self):
        app = make_room_app({})
        clock = Clock(BASE)
        app._now_utc = clock
        kwargs = {"person": "mikkel"}
        steps = [
            (0, "on", True),
            (10, "on", False),           # repeat: no new row
            (20, "unavailable", True),   # on -> off
            (30, "unknown", False),      # off -> off: no new row
            (40, "on", True),
            (50, None, True),            # garbage new state reads as off
        ]
        for dt, new, grows in steps:
            clock.t = BASE + timedelta(seconds=dt)
            before = len(app._room_log.get("mikkel", ()))
            app._on_room_change(ROOMS["mikkel"], "state", "x", new, kwargs)
            self.assertEqual(len(app._room_log["mikkel"]) - before, 1 if grows else 0, (dt, new))

        self.assertEqual(app._room_log["mikkel"], rows((0, True), (20, False), (40, True), (50, False)))

    def test_log_is_replaced_wholesale_never_mutated(self):
        app = make_room_app({"kristine": rows((-60, True)), "mikkel": rows((-30, True))})
        app._now_utc = lambda: BASE
        old_log = app._room_log
        old_mikkel = old_log["mikkel"]

        app._on_room_change(ROOMS["mikkel"], "state", "on", "off", {"person": "mikkel"})

        self.assertIsNot(app._room_log, old_log)
        self.assertEqual(old_log["mikkel"], old_mikkel)
        self.assertEqual(app._room_log["mikkel"], old_mikkel + ((BASE, False),))
        self.assertIs(app._room_log["kristine"], old_log["kristine"])

    def test_does_not_publish(self):
        app = make_room_app({})
        published = []
        app.set_state = lambda *a, **kw: published.append((a, kw))

        app._on_room_change(ROOMS["mikkel"], "state", "off", "on", {"person": "mikkel"})

        self.assertEqual(published, [])

    def test_never_raises_on_garbage_input(self):
        app = make_room_app({})
        try:
            app._on_room_change(ROOMS["mikkel"], "state", None, None, {})  # no person kwarg
        except Exception as e:  # pragma: no cover
            self.fail(f"room listener raised: {e}")
        self.assertTrue(logged_at(app, "ERROR"))


class InitializeRoomTests(unittest.TestCase):
    """initialize() reads the room config and arms one listen_state per room."""

    def make_init_app(self, args):
        app = make_app()
        app.args = args
        app.listen_calls = []
        app.listen_state = lambda cb, entity, **kw: app.listen_calls.append((cb, entity, kw))
        app.listen_event = lambda *a, **kw: None
        app.run_in = lambda *a, **kw: None
        app.run_every = lambda *a, **kw: None
        return app

    def test_one_listener_per_room_carrying_its_person(self):
        app = self.make_init_app({
            "private_rooms": dict(ROOMS), "room_lead_seconds": 45, "room_hold_seconds": 300,
        })

        app.initialize()

        self.assertEqual(app.private_rooms, ROOMS)
        self.assertEqual(app.room_lead_seconds, 45)
        self.assertEqual(app.room_hold_seconds, 300)
        room_listeners = [(entity, kw) for cb, entity, kw in app.listen_calls if cb == app._on_room_change]
        self.assertEqual(
            sorted(room_listeners, key=lambda r: r[0]),
            sorted(((entity, {"person": person}) for person, entity in ROOMS.items()), key=lambda r: r[0]),
        )
        self.assertEqual(len(app.listen_calls), 3 + 1 + 3)  # persons + door + rooms

    def test_no_rooms_configured_means_no_room_listeners_and_default_timings(self):
        app = self.make_init_app({})

        app.initialize()

        self.assertEqual(app.private_rooms, {})
        self.assertEqual((app.room_lead_seconds, app.room_hold_seconds), (60, 360))
        self.assertFalse([c for c in app.listen_calls if c[0] == app._on_room_change])
        self.assertEqual(len(app.listen_calls), 3 + 1)


class RoomTickReconcileTests(unittest.TestCase):
    """_tick on rooms: a room the log holds occupied but a fresh read says is not gets an
    off row stamped now. Never the other way: a fresh "on" cannot invent time in the room."""

    def make_tick_app(self, room_log, fresh):
        app = make_app()
        app._now_utc = lambda: BASE
        app.private_rooms = dict(ROOMS)
        app._room_log = dict(room_log)
        app._person_state = {"mikkel": "home", "kristine": "home", "claudia": "not_home"}
        app._snapshot_log = (snap(BASE - timedelta(seconds=900), home=("mikkel", "kristine")),)
        states = {
            "person.mikkel": "home", "person.kristine": "home", "person.claudia": "not_home",
            "binary_sensor.apartment_door_open": "off",
        }
        states.update({ROOMS[p]: s for p, s in fresh.items()})

        def get_state(entity, **kw):
            if isinstance(states.get(entity), Exception):
                raise states[entity]
            return states.get(entity)

        app.get_state = get_state
        app.set_state = lambda *a, **kw: None
        return app

    def test_occupied_room_reading_not_on_gets_an_off_row_and_a_warning(self):
        for fresh in ("off", "unavailable", "unknown", None):
            with self.subTest(fresh=fresh):
                app = self.make_tick_app({"mikkel": rows((-3600, True))}, {"mikkel": fresh})

                app._tick({})

                self.assertEqual(app._room_log["mikkel"], rows((-3600, True), (0, False)))
                self.assertTrue(any(ROOMS["mikkel"] in str(a) for a in logged_at(app, "WARNING")))

    def test_fresh_on_never_adds_time_in_the_room(self):
        cases = {
            "logged off": {"mikkel": rows((-3600, False))},
            "no rows yet": {},
        }
        for name, room_log in cases.items():
            with self.subTest(name):
                app = self.make_tick_app(room_log, {"mikkel": "on"})

                app._tick({})

                self.assertEqual(app._room_log, room_log)
                self.assertEqual(logged_at(app, "WARNING"), [])

    def test_agreeing_read_changes_nothing(self):
        app = self.make_tick_app({"mikkel": rows((-3600, True))}, {"mikkel": "on"})

        app._tick({})

        self.assertEqual(app._room_log["mikkel"], rows((-3600, True)))
        self.assertEqual(logged_at(app, "WARNING"), [])

    def test_get_state_failure_for_one_room_does_not_block_the_others(self):
        app = self.make_tick_app(
            {"mikkel": rows((-3600, True)), "kristine": rows((-3600, True))},
            {"mikkel": RuntimeError("boom"), "kristine": "off"},
        )

        try:
            app._tick({})
        except Exception as e:  # pragma: no cover
            self.fail(f"_tick raised: {e}")

        self.assertEqual(app._room_log["mikkel"], rows((-3600, True)))  # untouched by the failure
        self.assertEqual(app._room_log["kristine"], rows((-3600, True), (0, False)))

    def test_reconcile_applied_before_elimination_ends_the_cross_off(self):
        """The point of the off row: a dropped listener callback must not keep a resident
        crossed off for good."""
        app = self.make_tick_app({"mikkel": rows((-3600, True))}, {"mikkel": "off"})
        app._now_utc = lambda: BASE + timedelta(seconds=100)

        app._tick({})
        app._now_utc = lambda: BASE + timedelta(seconds=420)
        result = app.attribute("washer_emptied", at=BASE)

        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "multi_occupant")


class RoomBackfillTests(unittest.TestCase):
    """_backfill seeds the room log over the same window as the presence log, with the
    same first-row clamp; a room with no history stays out (its resident never crossed off)."""

    def make_backfill_app(self, history, failing=()):
        app = make_app(person_entities=["person.mikkel", "person.kristine"])
        app.private_rooms = {"mikkel": ROOMS["mikkel"], "kristine": ROOMS["kristine"]}
        app._now_utc = lambda: BASE
        app.set_state = lambda *a, **kw: None
        app.get_state = lambda *a, **kw: None
        app.history_calls = []

        def get_history(entity_id=None, start_time=None, end_time=None):
            app.history_calls.append((entity_id, start_time, end_time))
            if entity_id in failing:
                raise RuntimeError("recorder unavailable")
            return history.get(entity_id, [])

        app.get_history = get_history
        return app

    def presence_history(self, window_start):
        long_ago = (window_start - timedelta(hours=2)).isoformat()
        return {
            "person.mikkel": [{"last_changed": long_ago, "state": "home"}],
            "person.kristine": [{"last_changed": long_ago, "state": "home"}],
        }

    def test_seeds_rooms_with_window_clamp_change_only_and_off_for_unavailable(self):
        window_start = BASE - timedelta(hours=26)
        history = self.presence_history(window_start)
        history[ROOMS["mikkel"]] = [
            {"last_changed": (window_start - timedelta(hours=5)).isoformat(), "state": "on"},
            {"last_changed": (BASE - timedelta(minutes=100)).isoformat(), "state": "unavailable"},
            {"last_changed": (BASE - timedelta(minutes=99)).isoformat(), "state": "unknown"},
            {"last_changed": (BASE - timedelta(minutes=50)).isoformat(), "state": "on"},
            {"last_updated": (BASE - timedelta(minutes=40)).isoformat(), "state": "on"},
        ]
        history[ROOMS["kristine"]] = [{"last_changed": (BASE - timedelta(minutes=5)).isoformat(), "state": "off"}]
        app = self.make_backfill_app(history)

        app._backfill({})

        self.assertEqual(app._room_log["mikkel"], (
            (window_start, True),
            (BASE - timedelta(minutes=100), False),
            (BASE - timedelta(minutes=50), True),
        ))
        self.assertEqual(app._room_log["kristine"][-1], (BASE - timedelta(minutes=5), False))
        room_calls = [c for c in app.history_calls if c[0] in ROOMS.values()]
        self.assertEqual(
            sorted(room_calls),
            sorted((entity, window_start, BASE) for entity in (ROOMS["mikkel"], ROOMS["kristine"])),
        )

    def test_first_row_inside_the_window_is_not_pulled_earlier(self):
        window_start = BASE - timedelta(hours=26)
        first_seen = window_start + timedelta(hours=1)
        history = self.presence_history(window_start)
        history[ROOMS["mikkel"]] = [{"last_changed": first_seen.isoformat(), "state": "on"}]
        app = self.make_backfill_app(history)

        app._backfill({})

        self.assertEqual(app._room_log["mikkel"], ((first_seen, True),))

    def test_room_without_history_or_with_a_failing_fetch_is_left_out(self):
        window_start = BASE - timedelta(hours=26)
        history = self.presence_history(window_start)
        history[ROOMS["mikkel"]] = [{"last_changed": (BASE - timedelta(hours=2)).isoformat(), "state": "on"}]
        app = self.make_backfill_app(history, failing=(ROOMS["kristine"],))

        app._backfill({})

        self.assertEqual(sorted(app._room_log), ["mikkel"])
        self.assertTrue(app._snapshot_log)  # the presence side is unaffected
        self.assertTrue(any(ROOMS["kristine"] in str(a) for a in logged_at(app, "WARNING")))

        history[ROOMS["kristine"]] = []  # fetch works now but comes back empty
        app = self.make_backfill_app(history)

        app._backfill({})

        self.assertEqual(sorted(app._room_log), ["mikkel"])
        self.assertTrue(any(ROOMS["kristine"] in str(a) for a in logged_at(app, "WARNING")))

    def test_backfilled_rooms_feed_the_verdict(self):
        window_start = BASE + timedelta(seconds=420) - timedelta(hours=26)
        history = self.presence_history(window_start)
        history[ROOMS["mikkel"]] = [{"last_changed": (BASE - timedelta(hours=3)).isoformat(), "state": "on"}]
        history[ROOMS["kristine"]] = [{"last_changed": (BASE - timedelta(hours=3)).isoformat(), "state": "off"}]
        app = self.make_backfill_app(history)
        app._now_utc = lambda: BASE + timedelta(seconds=420)

        app._backfill({})
        result = app.attribute("washer_emptied", at=BASE)

        self.assertEqual(result["person"], "kristine")
        self.assertEqual(result["method"], "room_elimination")


class RoomLogPruneTests(unittest.TestCase):
    """_prune_room_log: like _prune_log, one carry-in row from before the cutoff plus
    everything after it, the cutoff reaching room_lead_seconds beyond plain retention."""

    NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)

    def at(self, hh, mm, ss=0):
        return datetime(2026, 7, 27, hh, mm, ss, tzinfo=timezone.utc)

    def test_keeps_one_carry_in_row_and_everything_after_the_cutoff(self):
        app = make_app(retention_hours=1)  # cutoff 10:59:00 = now - 1 h - 60 s lead
        app._room_log = {"mikkel": (
            (self.at(9, 0), False), (self.at(10, 0), True), (self.at(10, 30), False), (self.at(11, 30), True),
        )}

        app._prune_room_log(self.NOW)

        self.assertEqual(app._room_log["mikkel"], ((self.at(10, 30), False), (self.at(11, 30), True)))

    def test_cutoff_reaches_the_lead_beyond_plain_retention(self):
        app = make_app(retention_hours=1)
        # 10:59:30 is older than plain retention (11:00:00) but inside the lead's reach, so
        # the row in effect before it must survive as well.
        app._room_log = {"mikkel": ((self.at(10, 0), True), (self.at(10, 59, 30), False))}

        app._prune_room_log(self.NOW)

        self.assertEqual(app._room_log["mikkel"], ((self.at(10, 0), True), (self.at(10, 59, 30), False)))

    def test_replaces_the_dict_and_tuples_wholesale(self):
        app = make_app(retention_hours=1)
        old_rows = ((self.at(9, 0), False), (self.at(10, 30), True))
        old_log = {"mikkel": old_rows}
        app._room_log = old_log

        app._prune_room_log(self.NOW)

        self.assertIsNot(app._room_log, old_log)
        self.assertEqual(old_log["mikkel"], old_rows)
        self.assertEqual(app._room_log["mikkel"], ((self.at(10, 30), True),))

    def test_anchor_at_the_retention_edge_still_resolves_via_the_carry_in_row(self):
        app = make_app(retention_hours=1)
        app.private_rooms = dict(ROOMS)
        app._now_utc = lambda: self.NOW
        app._snapshot_log = (snap(self.at(9, 0), home=("mikkel", "kristine")),)
        app._room_log = {"mikkel": ((self.at(9, 0), False), (self.at(10, 45), True))}
        app._prune_room_log(self.NOW)
        anchor = self.at(11, 0)  # now - retention_hours; its lead instant 10:59 needs the 10:45 row

        result = app.attribute("washer_emptied", at=anchor)

        self.assertEqual(result["person"], "kristine")
        self.assertEqual(result["method"], "room_elimination")

    def test_tick_prunes_the_room_log(self):
        app = make_app(retention_hours=1)
        app._now_utc = lambda: self.NOW
        app.private_rooms = {"mikkel": ROOMS["mikkel"]}
        app._room_log = {"mikkel": ((self.at(9, 0), False), (self.at(10, 0), True), (self.at(10, 30), False))}
        app._person_state = {"mikkel": "home", "kristine": "not_home", "claudia": "not_home"}
        app.get_state = lambda entity, **kw: {"person.mikkel": "home"}.get(entity, "not_home")
        app.set_state = lambda *a, **kw: None

        app._tick({})

        self.assertEqual(app._room_log["mikkel"], ((self.at(10, 30), False),))


class NoAppDaemonInternalShadowing(unittest.TestCase):
    """Regression guard for the 2026-07-27 failed deploy: an instance attribute named
    `_log` shadowed AppDaemon's own ADAPI._log method (which ADAPI.log calls internally),
    so every self.log() in the app raised "TypeError: 'tuple' object is not callable" -
    including the ones inside except blocks, making the app unstartable.

    The unit suite could not catch it because every test monkeypatches self.log. This
    test works structurally instead: no class-level attribute may collide with a
    non-callable shadow of an ADAPI member."""

    # ADAPI internals that apps must never shadow with data attributes. Kept explicit
    # rather than introspected, so this test stays meaningful without AppDaemon installed.
    RESERVED = {"_log", "log", "error", "args", "name", "AD", "logger", "set_state",
                "get_state", "listen_state", "listen_event", "run_in", "run_every",
                "get_history", "create_task", "call_service", "get_app"}

    def test_no_class_attribute_shadows_an_adapi_member(self):
        offenders = []
        for name, value in vars(aa.ActorAttribution).items():
            if name in self.RESERVED and not callable(value):
                offenders.append(name)
        self.assertEqual(offenders, [], f"class attributes shadow AppDaemon internals: {offenders}")

    def test_no_instance_attribute_assigned_in_source_shadows_an_adapi_member(self):
        # Catches `self._log = ...` anywhere in the module, not just class-level defaults.
        import ast
        src = Path(aa.__file__).read_text()
        assigned = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                if isinstance(node.value, ast.Name) and node.value.id == "self":
                    assigned.add(node.attr)
        clashes = sorted(assigned & self.RESERVED)
        self.assertEqual(clashes, [], f"self.<attr> assignments shadow AppDaemon internals: {clashes}")


if __name__ == "__main__":
    unittest.main()
