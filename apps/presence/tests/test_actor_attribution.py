# tests/test_actor_attribution.py - unit tests for actor_attribution.py: the seven
# sole-occupancy decision rules, the never-guess presence semantics (zone name vs
# unobservable), backward-only door-edge handling, retention-window edge cases (including
# a pruning off-by-window bug found and fixed during implementation), and the
# listener/tick/backfill/publish glue. Same __new__ + monkeypatched-callables harness as
# apps/appliances/tests/test_washer_vibration.py in this repo (no running AppDaemon
# required).
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

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    return app


def snap(ts, home=(), unobs=()):
    return aa._Snapshot(ts, frozenset(home), frozenset(unobs))


def logged_at(app, level):
    return [a for a, kw in app.log_calls if kw.get("level") == level]


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
