# tests/test_actor_followup.py - names that settle ~6.5 min after the event: ActorFollowupMixin on
# its own, then the washer, dryer and dishwasher monitors driven through the real
# ActorAttribution (apps/presence) with a room log, so the first answer is really "pending" and the
# second really "room_elimination", "multi_occupant" or "room_contradiction".
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "presence"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "home_pulse"))

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
import actor_followup as af  # noqa: E402
import cycle_store as cs  # noqa: E402
import house_events  # noqa: E402
import test_dishwasher_attribution as tdw  # noqa: E402
import test_dryer_attribution as tda  # noqa: E402
import test_washer_attribution as twa  # noqa: E402
import test_washer_restart_survival as trs  # noqa: E402
import test_washer_review4 as tr4  # noqa: E402
from test_dryer_attribution import Clock  # noqa: E402

ROOMS = {
    "mikkel": "binary_sensor.bedroom_active",
    "kristine": "binary_sensor.kristines_room_active",
    "claudia": "binary_sensor.claudias_room_active",
}


def room_actor(now_fn, since, rooms, home=("mikkel", "kristine")):
    """Real ActorAttribution: `home` present since `since`, a private-room log per resident."""
    actor = tda.make_actor(now_fn, [(since, set(home))])
    actor.private_rooms = dict(ROOMS)
    actor._room_log = dict(rooms)
    return actor


def on_since(*people, since):
    """Room log in which each of `people` sits in their room from `since` for good."""
    return {person: ((since, True),) for person in people}


def named(attrs, prefix):
    return attrs.get(f"{prefix}_by"), attrs.get(f"{prefix}_by_method")


def callbacks_named(timers, name):
    return [(cb, delay, kw) for cb, delay, kw in timers if getattr(cb, "__name__", "") == name]


def records_in(path):
    return json.loads(Path(path).read_text())["cycles"] if Path(path).exists() else []


# ----------------------------------------------------------------------------------------------
# The mixin on its own
# ----------------------------------------------------------------------------------------------

T0 = datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc)
ANCHOR = "2026-10-15T16:00:00Z"
PENDING = "2026-10-15T16:06:30Z"


def verdict(person=None, method="unknown", reason="multi_occupant", anchor=ANCHOR, pending_until=None):
    result = {
        "person": person,
        "method": method if person else "unknown",
        "reason": None if person else reason,
        "people_home": ["kristine", "mikkel"],
        "anchor": anchor,
        "evaluated_at": "2026-10-15T16:06:35Z",
        "version": 1,
    }
    if pending_until:
        result["pending_until"] = pending_until
    return result


class Host(af.ActorFollowupMixin):
    """The seams a monitor gives the mixin, with canned answers and a record of what was done."""

    def __init__(self, tmpdir, now=T0 + timedelta(seconds=20)):
        self.now = now
        self.state = "Running"
        self.cycle = "cycle-1"
        self._cycle_actor = verdict(pending_until=PENDING)
        self.feedback_file = os.path.join(tmpdir, "feedback.json")
        self.answers = []
        self.asked = []
        self.timers = []
        self.published = []
        self.logs = []
        self.fail_run_in = False
        self.fail_publish = False

    def _now_utc(self):
        return self.now

    def _cycle_store_cycle_id(self):
        return self.cycle

    def _attribute(self, event, at=None, user_id=None):
        self.asked.append((event, at, user_id))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def run_in(self, callback, delay, **kwargs):
        if self.fail_run_in:
            raise RuntimeError("scheduler down")
        self.timers.append((callback, delay, kwargs))

    def _set_state_entity(self, **kwargs):
        if self.fail_publish:
            raise RuntimeError("HA down")
        self.published.append(kwargs)

    def log(self, message, level="INFO", **kw):
        self.logs.append((level, message))


def write_records(host, records):
    Path(host.feedback_file).write_text(json.dumps({"version": 2, "cycles": records}))


def emptied_record(anchor=ANCHOR, **extra):
    record = {
        "ts": "2026-10-15T18:00:00+02:00",
        "started_by": "mikkel",
        "emptied_by": None,
        "emptied_ts": "2026-10-15T18:30:00+02:00",
        "attribution": {"start": verdict("mikkel", "sole_occupant"),
                        "empty": verdict(anchor=anchor, pending_until=PENDING)},
    }
    record.update(extra)
    return record


class Scheduling(unittest.TestCase):

    def test_a_verdict_with_nothing_pending_schedules_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            for v in (verdict("kristine", "sole_occupant"), verdict(), {}, None):
                host._follow_up_actor("start", v, "washer_start")
            self.assertEqual(host.timers, [])
            self.assertEqual(host.logs, [])

    def test_one_callback_for_the_pending_instant_plus_five_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)  # 20 s after the anchor, pending until 390 s after it

            host._follow_up_actor("start", host._cycle_actor, "dryer_start")

            (callback, delay, kwargs), = host.timers
            self.assertEqual(callback.__func__, af.ActorFollowupMixin._actor_followup_start)
            self.assertEqual(delay, 375)
            self.assertEqual(kwargs, {"actor_event": "dryer_start", "actor_anchor": ANCHOR, "actor_cycle_id": "cycle-1"})

    def test_empty_uses_its_own_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            host._follow_up_actor("empty", verdict(pending_until=PENDING), "dryer_emptied")
            self.assertEqual(host.timers[0][0].__func__, af.ActorFollowupMixin._actor_followup_empty)

    def test_a_fraction_of_a_second_rounds_the_wait_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp, now=T0 + timedelta(seconds=20, milliseconds=400))
            host._follow_up_actor("start", host._cycle_actor, "dryer_start")
            self.assertEqual(host.timers[0][1], 375)

    def test_an_instant_already_past_waits_the_minimum(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp, now=T0 + timedelta(seconds=400))
            host._follow_up_actor("start", host._cycle_actor, "dryer_start")
            self.assertEqual(host.timers[0][1], 1)

    def test_an_unknown_kind_or_a_verdict_without_an_anchor_schedules_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            host._follow_up_actor("finish", host._cycle_actor, "x")
            host._follow_up_actor("start", dict(host._cycle_actor, anchor=""), "x")
            self.assertEqual(host.timers, [])

    def test_a_scheduler_that_fails_is_logged_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            host.fail_run_in = True
            host._follow_up_actor("start", host._cycle_actor, "dryer_start")
            self.assertEqual([level for level, _ in host.logs], ["WARNING"])


class StartCallback(unittest.TestCase):
    KW = {"actor_event": "dryer_start", "actor_anchor": ANCHOR, "actor_cycle_id": "cycle-1"}

    def test_a_name_replaces_the_verdict_and_is_published_as_a_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            host.answers = [verdict("kristine", "room_elimination")]

            host._actor_followup_start(self.KW)

            self.assertEqual(host.asked, [("dryer_start", T0, None)])
            self.assertEqual(host._cycle_actor["method"], "room_elimination")
            self.assertEqual(host.published, [{
                "state": "Running",
                "attributes": {"started_by": "kristine", "started_by_method": "room_elimination"},
            }])

    def test_an_unknown_answer_is_kept_as_the_final_reason_and_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            host.answers = [verdict(reason="room_contradiction")]

            host._actor_followup_start(self.KW)

            self.assertEqual(host._cycle_actor["reason"], "room_contradiction")
            self.assertNotIn("pending_until", host._cycle_actor)
            self.assertEqual(host.published, [])

    def test_a_paused_cycle_is_still_the_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            host.state = "Paused"
            host.answers = [verdict("kristine", "room_elimination")]
            host._actor_followup_start(self.KW)
            self.assertEqual(host.published[0]["state"], "Paused")

    def test_another_cycle_or_a_cycle_that_is_over_is_ignored(self):
        pending = verdict(pending_until=PENDING)
        cases = {
            "newer cycle": ("cycle-2", "Running"),
            "no cycle": (None, "Running"),
            "unemptied": ("cycle-1", "Unemptied"),
            "emptied": ("cycle-1", "Emptied"),
            "off": ("cycle-1", "Off"),
        }
        for name, (cycle, state) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                host = Host(tmp)
                host.cycle, host.state = cycle, state
                host.answers = [verdict("kristine", "room_elimination")]

                host._actor_followup_start(self.KW)

                self.assertEqual(host.asked, [])
                self.assertEqual(host._cycle_actor, pending)
                self.assertEqual(host.published, [])

    def test_failures_are_logged_never_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            host.answers = [RuntimeError("attribution blew up")]
            host._actor_followup_start(self.KW)
            self.assertEqual(host._cycle_actor["pending_until"], PENDING)

            host.answers = [verdict("kristine", "room_elimination")]
            host.fail_publish = True
            host._actor_followup_start(self.KW)
            self.assertEqual(host._cycle_actor["person"], "kristine")
            self.assertEqual([level for level, _ in host.logs].count("WARNING"), 2)

            host._actor_followup_start({})
            host._actor_followup_start({"actor_cycle_id": "cycle-1", "actor_anchor": "not a time"})


class EmptyCallback(unittest.TestCase):
    KW = {"actor_event": "dryer_emptied", "actor_anchor": ANCHOR, "actor_cycle_id": "cycle-1"}

    def run_empty(self, tmp, records, answer, state="Emptied"):
        host = Host(tmp)
        host.state = state
        write_records(host, records)
        host.answers = [answer]
        host._actor_followup_empty(self.KW)
        return host

    def test_a_name_lands_on_the_newest_record_without_touching_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            older = emptied_record(ts="2026-10-14T18:00:00+02:00")
            host = self.run_empty(tmp, [older, emptied_record()], verdict("kristine", "room_elimination"))

            self.assertEqual(host.asked, [("dryer_emptied", T0, None)])
            first, second = records_in(host.feedback_file)
            self.assertEqual(first, older)
            self.assertEqual(second["emptied_by"], "kristine")
            self.assertEqual(second["emptied_ts"], "2026-10-15T18:30:00+02:00")
            self.assertEqual(second["attribution"]["empty"]["method"], "room_elimination")
            self.assertNotIn("pending_until", second["attribution"]["empty"])
            self.assertEqual(second["attribution"]["start"]["method"], "sole_occupant")
            self.assertEqual(host.published, [{
                "state": "Emptied",
                "attributes": {"emptied_by": "kristine", "emptied_by_method": "room_elimination"},
            }])

    def test_an_unknown_answer_records_the_final_reason_and_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self.run_empty(tmp, [emptied_record()], verdict(reason="multi_occupant"))

            record, = records_in(host.feedback_file)
            self.assertIsNone(record["emptied_by"])
            self.assertEqual(record["attribution"]["empty"]["reason"], "multi_occupant")
            self.assertNotIn("pending_until", record["attribution"]["empty"])
            self.assertEqual(host.published, [])

    def test_a_record_that_is_not_this_emptying_is_left_alone_and_nothing_is_published(self):
        named_already = emptied_record(emptied_by="mikkel")
        cases = {
            "other anchor": [emptied_record(anchor="2026-10-15T16:20:00Z")],
            "already named": [named_already],
            "peeked away": [{"ts": "x", "attribution": {"start": verdict("mikkel", "sole_occupant")}}],
            "no attribution": [{"ts": "x", "emptied_by": None}],
            "attribution not a dict": [{"ts": "x", "attribution": "?"}],
            "empty not a dict": [{"ts": "x", "attribution": {"empty": None}}],
            "only an older record matches": [emptied_record(), {"ts": "newer", "attribution": {"start": None}}],
            "no records": [],
        }
        for name, records in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                host = self.run_empty(tmp, records, verdict("kristine", "room_elimination"))

                self.assertEqual(records_in(host.feedback_file), records)
                self.assertEqual(host.published, [])

    def test_a_missing_or_unreadable_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            host.answers = [verdict("kristine", "room_elimination")]
            host._actor_followup_empty(self.KW)
            self.assertEqual(host.published, [])

            for content in ("{broken", "[]", '{"cycles": "x"}'):
                Path(host.feedback_file).write_text(content)
                host.answers = [verdict("kristine", "room_elimination")]
                host._actor_followup_empty(self.KW)
                self.assertEqual(Path(host.feedback_file).read_text(), content)
            self.assertEqual(host.published, [])

    def test_the_name_is_published_whatever_the_state_has_become(self):
        for state in ("Off", "Running", "Unemptied"):
            with self.subTest(state), tempfile.TemporaryDirectory() as tmp:
                host = self.run_empty(tmp, [emptied_record()], verdict("kristine", "room_elimination"), state=state)
                self.assertEqual(host.published[0]["state"], state)
                self.assertEqual(host.published[0]["attributes"]["emptied_by"], "kristine")

    def test_a_write_that_fails_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            write_records(host, [emptied_record()])
            host.answers = [verdict("kristine", "room_elimination")]
            before = Path(host.feedback_file).read_bytes()
            original = af.feedback_io.write_json

            def broken(path, data):
                raise OSError("disk full")

            af.feedback_io.write_json = broken
            try:
                host._actor_followup_empty(self.KW)
            finally:
                af.feedback_io.write_json = original

            self.assertEqual(Path(host.feedback_file).read_bytes(), before)
            self.assertEqual(host.published, [])
            self.assertIn("WARNING", [level for level, _ in host.logs])

    def test_failures_are_logged_never_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(tmp)
            write_records(host, [emptied_record()])
            host.answers = [RuntimeError("attribution blew up")]
            host._actor_followup_empty(self.KW)
            host.fail_publish = True
            host.answers = [verdict("kristine", "room_elimination")]
            host._actor_followup_empty(self.KW)
            host._actor_followup_empty({})
            self.assertEqual(records_in(host.feedback_file)[0]["emptied_by"], "kristine")


class NoShadowingOfAppDaemonMembers(unittest.TestCase):

    def test_the_mixin_assigns_no_log_attribute(self):
        assigned = set()
        for node in ast.walk(ast.parse(Path(af.__file__).read_text())):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                if isinstance(node.value, ast.Name) and node.value.id == "self":
                    assigned.add(node.attr)
        self.assertNotIn("_log", assigned)
        self.assertNotIn("log", assigned)


# ----------------------------------------------------------------------------------------------
# The dryer
# ----------------------------------------------------------------------------------------------

def record_timers(app):
    app.timers = []

    def run_in(cb, delay, **kw):
        app.timers.append((cb, delay, kw))
        return f"timer:{len(app.timers)}"

    app.run_in = run_in


def record_publishes(app):
    app.publishes = []
    real = app.set_state

    def set_state(entity_id, **kw):
        app.publishes.append((entity_id, kw))
        return real(entity_id, **kw)

    app.set_state = set_state


def fire(app, clock, callback, delay, kwargs):
    clock.now += timedelta(seconds=delay)
    callback(kwargs)


class DryerFollowUps(unittest.TestCase):
    ROOM_LOG = on_since("mikkel", since=tda.T0 - timedelta(hours=1))  # Kristine is the one left

    def _started(self, tmp, rooms=None):
        clock = Clock(tda.T0)
        actor = room_actor(clock, tda.T0 - timedelta(hours=2), self.ROOM_LOG if rooms is None else rooms)
        app = tda.make_app(tmp, clock, actor)
        record_timers(app)
        record_publishes(app)
        app.initialize()
        tda.load_and_start(app, clock, close_at=tda.T0)
        return app, clock, actor

    def _unemptied(self, tmp, rooms=None):
        app, clock, actor = self._started(tmp, rooms)
        (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")
        fire(app, clock, cb, delay, kw)  # the start settles first, as in the house
        tda.finish(app, clock)
        clock.now += timedelta(minutes=30)
        return app, clock, actor

    # -- start ------------------------------------------------------------------------------

    def test_the_start_is_pending_first_and_named_when_the_follow_up_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._started(tmp)

            self.assertEqual(named(tda.published(app), "started"), ("", "unknown"))
            self.assertEqual(app._cycle_actor["pending_until"], PENDING)
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")
            self.assertEqual(delay, 375)
            self.assertEqual(kw, {"actor_event": "dryer_start", "actor_anchor": ANCHOR, "actor_cycle_id": app.cycle_id})

            fire(app, clock, cb, delay, kw)

            self.assertEqual(actor.calls[-1], ("dryer_start", tda.T0, None))
            self.assertEqual(named(tda.published(app), "started"), ("kristine", "room_elimination"))
            self.assertEqual(app._cycle_actor["method"], "room_elimination")
            self.assertEqual(app.state, "Running")

    def test_the_name_reaches_the_store_and_the_record_and_the_feed_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, _actor = self._started(tmp)
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")
            self.assertEqual(tda_stored(tmp)["started_by"], None)
            clock.now += timedelta(seconds=delay - 5)
            app._update_running_attributes()  # the store was just written: only a changed name forces the next write

            fire(app, clock, cb, 5, kw)

            self.assertEqual((tda_stored(tmp)["started_by"], tda_stored(tmp)["started_by_method"]), ("kristine", "room_elimination"))
            tda.finish(app, clock)
            attrs = tda.published(app)
            self.assertEqual(house_events.appliance_actor("finished", attrs), "Kristine")
            record = tda.feedback_cycles(app)[-1]
            self.assertEqual(record["started_by"], "kristine")
            self.assertEqual(record["attribution"]["start"]["method"], "room_elimination")
            self.assertNotIn("pending_until", record["attribution"]["start"])

    def test_the_publish_is_a_merge_of_the_names_only_and_leaves_the_state_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, _actor = self._started(tmp)
            last_change, since = app.last_state_change, app.state_since
            publishes = len(app.publishes)
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")

            fire(app, clock, cb, delay, kw)

            (entity, call), = app.publishes[publishes:]
            self.assertEqual(entity, tda.STATE_ENTITY)
            self.assertEqual(call["state"], "Running")
            self.assertFalse(call.get("replace"))
            self.assertEqual(call["attributes"], {"started_by": "kristine", "started_by_method": "room_elimination"})
            self.assertEqual((app.last_state_change, app.state_since), (last_change, since))

    def test_an_answer_that_stays_unknown_leaves_the_names_empty_and_records_why(self):
        # Mikkel left his room 200 s after the load: an off row the first answer could not see yet.
        rooms = {"mikkel": ((tda.T0 - timedelta(hours=1), True), (tda.T0 + timedelta(seconds=200), False))}
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._started(tmp, rooms)
            publishes = len(app.publishes)
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")

            fire(app, clock, cb, delay, kw)

            self.assertEqual(len(actor.calls), 2)
            self.assertEqual(app._cycle_actor["reason"], "multi_occupant")
            self.assertNotIn("pending_until", app._cycle_actor)
            self.assertEqual(len(app.publishes), publishes)
            self.assertEqual(named(tda.published(app), "started"), ("", "unknown"))
            tda.finish(app, clock)
            record = tda.feedback_cycles(app)[-1]
            self.assertIsNone(record["started_by"])
            self.assertEqual(record["attribution"]["start"]["reason"], "multi_occupant")
            self.assertNotIn("pending_until", record["attribution"]["start"])

    def test_a_contradiction_is_the_recorded_reason(self):
        rooms = on_since("mikkel", "kristine", since=tda.T0 - timedelta(hours=1))
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, _actor = self._started(tmp, rooms)
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")

            fire(app, clock, cb, delay, kw)

            self.assertEqual(app._cycle_actor["reason"], "room_contradiction")
            self.assertEqual(named(tda.published(app), "started"), ("", "unknown"))

    def test_a_follow_up_of_an_earlier_cycle_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._started(tmp)
            (stale_cb, _delay, stale_kw), = callbacks_named(app.timers, "_actor_followup_start")
            app._transition_to_off("test", force=True)
            clock.now += timedelta(minutes=30)
            app.last_state_change = clock.now - timedelta(seconds=700)
            tda.load_and_start(app, clock)
            asked = len(actor.calls)
            publishes = len(app.publishes)
            actor_now = app._cycle_actor

            fire(app, clock, stale_cb, 1, stale_kw)

            self.assertEqual(len(actor.calls), asked)
            self.assertIs(app._cycle_actor, actor_now)
            self.assertEqual(len(app.publishes), publishes)
            self.assertNotEqual(stale_kw["actor_cycle_id"], app.cycle_id)

    def test_a_follow_up_after_the_cycle_ended_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._started(tmp)
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")
            tda.finish(app, clock)
            asked = len(actor.calls)

            fire(app, clock, cb, delay, kw)

            self.assertEqual(len(actor.calls), asked)
            self.assertEqual(named(tda.published(app), "started"), ("", "unknown"))

    def test_a_restart_during_the_wait_loses_the_name_and_raises_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, clock, actor = self._started(tmp)
            self.assertEqual(len(callbacks_named(first.timers, "_actor_followup_start")), 1)
            clock.now += timedelta(minutes=2)

            second = tda.make_app(tmp, clock, actor, sensor_state="Running",
                                  sensor_attrs=tda.published(first), power_w="900", energy_kwh="10.0")
            record_timers(second)
            second.initialize()

            self.assertEqual(callbacks_named(second.timers, "_actor_followup_start"), [])
            self.assertEqual(named(tda.published(second), "started"), ("", "unknown"))
            tda.finish(second, clock)
            record = tda.feedback_cycles(second)[-1]
            self.assertIsNone(record["started_by"])
            self.assertEqual(record["attribution"]["start"]["reason"], "restored_from_entity")

    # -- empty ------------------------------------------------------------------------------

    def test_the_emptier_is_pending_first_and_named_on_the_record_and_the_entity(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._unemptied(tmp)

            app._door_state_changed(tda.DOOR_SENSOR, None, "off", "on", {})

            self.assertEqual(app.state, "Emptied")
            self.assertEqual(named(tda.published(app), "emptied"), ("", "unknown"))
            record = tda.feedback_cycles(app)[-1]
            self.assertIsNone(record["emptied_by"])
            self.assertEqual(record["attribution"]["empty"]["pending_until"], aa.ActorAttribution._iso_up(clock.now + timedelta(seconds=390)))
            emptied_ts = record["emptied_ts"]
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_empty")
            self.assertEqual(kw["actor_event"], "dryer_emptied")
            self.assertEqual(kw["actor_anchor"], record["attribution"]["empty"]["anchor"])
            publishes = len(app.publishes)

            fire(app, clock, cb, delay, kw)

            attrs = tda.published(app)
            self.assertEqual(named(attrs, "emptied"), ("kristine", "room_elimination"))
            self.assertEqual(house_events.appliance_actor("emptied", attrs), "Kristine")
            record = tda.feedback_cycles(app)[-1]
            self.assertEqual(record["emptied_by"], "kristine")
            self.assertEqual(record["emptied_ts"], emptied_ts)
            self.assertEqual(record["attribution"]["empty"]["method"], "room_elimination")
            self.assertEqual(record["attribution"]["start"]["method"], "room_elimination")
            self.assertEqual(app.state, "Emptied")
            (entity, call), = app.publishes[publishes:]
            self.assertFalse(call.get("replace"))
            self.assertEqual(set(call["attributes"]), {"emptied_by", "emptied_by_method"})

    def test_a_name_that_arrives_after_the_door_shut_still_reaches_the_record_and_the_entity(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, _actor = self._unemptied(tmp)
            app._door_state_changed(tda.DOOR_SENSOR, None, "off", "on", {})
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_empty")
            clock.now += timedelta(minutes=2)
            app._door_state_changed(tda.DOOR_SENSOR, None, "on", "off", {})
            self.assertEqual(app.state, "Off")

            fire(app, clock, cb, delay - 120, kw)

            self.assertEqual(app.entities[tda.STATE_ENTITY]["state"], "Off")
            self.assertEqual(named(tda.published(app), "emptied"), ("kristine", "room_elimination"))
            self.assertEqual(tda.feedback_cycles(app)[-1]["emptied_by"], "kristine")

    def test_an_emptier_that_stays_unknown_records_the_final_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._unemptied(tmp)
            app._door_state_changed(tda.DOOR_SENSOR, None, "off", "on", {})
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_empty")
            actor._room_log = {"mikkel": ((clock.now - timedelta(hours=1), True), (clock.now + timedelta(seconds=200), False))}
            publishes = len(app.publishes)

            fire(app, clock, cb, delay, kw)

            record = tda.feedback_cycles(app)[-1]
            self.assertIsNone(record["emptied_by"])
            self.assertEqual(record["attribution"]["empty"]["reason"], "multi_occupant")
            self.assertNotIn("pending_until", record["attribution"]["empty"])
            self.assertEqual(len(app.publishes), publishes)
            self.assertEqual(named(tda.published(app), "emptied"), ("", "unknown"))

    def test_a_record_that_is_no_longer_this_emptying_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._unemptied(tmp)
            app._door_state_changed(tda.DOOR_SENSOR, None, "off", "on", {})
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_empty")
            for name, change in (
                ("another anchor", lambda r: r["attribution"]["empty"].update(anchor="2026-10-15T01:00:00Z")),
                ("already named", lambda r: r.update(emptied_by="mikkel")),
            ):
                with self.subTest(name):
                    path = Path(app.feedback_file)
                    data = json.loads(path.read_text())
                    saved = json.dumps(data)
                    change(data["cycles"][-1])
                    path.write_text(json.dumps(data))
                    before = path.read_text()
                    publishes = len(app.publishes)

                    fire(app, clock, cb, delay, kw)

                    self.assertEqual(path.read_text(), before)
                    self.assertEqual(len(app.publishes), publishes)
                    path.write_text(saved)

    def test_a_restart_during_the_wait_keeps_the_pending_marker_and_raises_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, clock, actor = self._unemptied(tmp)
            first._door_state_changed(tda.DOOR_SENSOR, None, "off", "on", {})
            before = tda.feedback_cycles(first)[-1]
            clock.now += timedelta(minutes=1)

            second = tda.make_app(tmp, clock, actor, sensor_state="Emptied", sensor_attrs=tda.published(first))
            record_timers(second)
            second.initialize()
            clock.now += timedelta(minutes=1)
            second._door_state_changed(tda.DOOR_SENSOR, None, "on", "off", {})

            self.assertEqual(callbacks_named(second.timers, "_actor_followup_empty"), [])
            record = tda.feedback_cycles(second)[-1]
            self.assertEqual(record, before)
            self.assertIsNone(record["emptied_by"])
            self.assertIn("pending_until", record["attribution"]["empty"])


def tda_stored(tmp):
    return cs.CycleStore(Path(tmp) / "dryer_cycle_state.json", "dryer").load()


# ----------------------------------------------------------------------------------------------
# The washer
# ----------------------------------------------------------------------------------------------

class WasherFollowUps(unittest.TestCase):
    NOW = datetime(2026, 7, 27, 20, 0, 0, tzinfo=timezone.utc)

    def _begin(self, rooms):
        clock = Clock(self.NOW)
        actor = room_actor(clock, self.NOW - timedelta(hours=5), rooms)
        close = self.NOW - timedelta(seconds=30)
        app = twa._make_begin_cycle_app(
            self.NOW, last_door_closed_at=close, last_door_closed_trusted=True,
            last_off_at_str=(self.NOW - timedelta(hours=3)).isoformat(), actor=actor,
        )
        app._now_utc = clock
        app._begin_running_cycle()
        return app, clock, actor

    def test_the_start_is_named_when_the_follow_up_runs(self):
        app, clock, actor = self._begin(on_since("mikkel", since=self.NOW - timedelta(hours=1)))
        self.assertEqual(named(app.set_state_calls[-1]["attributes"], "started"), ("", "unknown"))
        (cb, delay, kw), = callbacks_named(app.scheduled, "_actor_followup_start")
        self.assertEqual(delay, 365)  # pending until 390 s after the close, 30 s ago, plus 5
        self.assertEqual(kw, {
            "actor_event": "washer_start",
            "actor_anchor": aa.ActorAttribution._iso(self.NOW - timedelta(seconds=30)),
            "actor_cycle_id": app._cycle_id,
        })
        published_before = len(app.set_state_calls)

        fire(app, clock, cb, delay, kw)

        self.assertEqual(app._cycle_actor["method"], "room_elimination")
        self.assertEqual(app.set_state_calls[published_before:], [{
            "state": "Running",
            "attributes": {"started_by": "kristine", "started_by_method": "room_elimination"},
        }])

    def test_an_answer_that_stays_unknown_publishes_nothing(self):
        rooms = on_since("mikkel", "kristine", since=self.NOW - timedelta(hours=1))
        app, clock, _actor = self._begin(rooms)
        (cb, delay, kw), = callbacks_named(app.scheduled, "_actor_followup_start")
        published_before = len(app.set_state_calls)

        fire(app, clock, cb, delay, kw)

        self.assertEqual(app._cycle_actor["reason"], "room_contradiction")
        self.assertEqual(len(app.set_state_calls), published_before)

    def test_a_follow_up_of_an_earlier_start_is_ignored(self):
        app, clock, actor = self._begin(on_since("mikkel", since=self.NOW - timedelta(hours=1)))
        (cb, delay, kw), = callbacks_named(app.scheduled, "_actor_followup_start")
        app._begin_running_cycle()  # a new identity: the washer began the cycle over
        asked = len(actor.calls)
        current = app._cycle_actor

        fire(app, clock, cb, delay, kw)

        self.assertEqual(len(actor.calls), asked)
        self.assertIs(app._cycle_actor, current)

    def test_the_late_start_name_is_written_to_the_cycle_store_at_once(self):
        app, clock = tr4.running_app(minutes=1)
        actor = room_actor(lambda: app.now, trs.NOW - timedelta(hours=5),
                           on_since("mikkel", since=trs.NOW - timedelta(hours=1)))
        app.get_app = lambda name: actor if name == "ActorAttribution" else None
        app.last_door_closed_at = app.now - timedelta(seconds=30)
        app.last_door_closed_trusted = True

        app._begin_running_cycle()

        self.assertEqual(app._cycle_store.load()["started_by_method"], "unknown")
        (cb, delay, kw), = callbacks_named(app.scheduled, "_actor_followup_start")
        app.now += timedelta(seconds=delay - 5)
        app._set_state_entity(state="Running", attributes={})  # the store was just written
        app.now += timedelta(seconds=5)
        cb(kw)

        stored = app._cycle_store.load()
        self.assertEqual((stored["started_by"], stored["started_by_method"]), ("kristine", "room_elimination"))
        self.assertEqual(named(app.attrs_store["sensor.washer_state"], "started"), ("kristine", "room_elimination"))

    # -- the emptying, through the full washer ---------------------------------------------

    def _finished(self, rooms, minutes=90, direct=False):
        app, clock = tr4.running_app(minutes=minutes)
        actor = room_actor(lambda: app.now, trs.NOW - timedelta(hours=5), rooms)
        app.get_app = lambda name: actor if name == "ActorAttribution" else None
        app._cycle_actor = {"person": "mikkel", "method": "sole_occupant", "reason": None,
                            "people_home": ["mikkel"], "anchor": "x", "evaluated_at": "y", "version": 1}
        if direct:
            app._correct_duration = lambda wall, log_prefix=None: (80.0, "power_history")
        else:
            tr4.feed(app, [2.8] * 95, clock)
            app._check_energy_finish({})
            assert app.state == "Unemptied", app.state
        return app, actor

    def _entity(self, app):
        return app.attrs_store["sensor.washer_state"]

    def test_the_unemptied_publish_carries_the_starters_method_too(self):
        app, _actor = self._finished({})
        attrs = self._entity(app)
        self.assertEqual(named(attrs, "started"), ("mikkel", "sole_occupant"))
        self.assertEqual(house_events.appliance_actor("finished", attrs), "Mikkel")

    def test_the_emptier_is_named_on_the_record_and_the_entity_after_the_unemptied_save(self):
        app, actor = self._finished(on_since("mikkel", since=trs.NOW - timedelta(hours=1)))

        app._transition_to_emptied("Door opened - emptying")

        self.assertEqual(named(self._entity(app), "emptied"), ("", "unknown"))
        record = tr4.records(app)[-1]
        self.assertIsNone(record.get("emptied_by"))
        self.assertIn("pending_until", record["attribution"]["empty"])
        emptied_ts = record["emptied_ts"]
        (cb, delay, kw), = callbacks_named(app.scheduled, "_actor_followup_empty")
        self.assertEqual(kw["actor_event"], "washer_emptied")
        published_before = len(app.set_state_calls)

        app.now += timedelta(seconds=delay)
        cb(kw)

        attrs = self._entity(app)
        self.assertEqual(named(attrs, "emptied"), ("kristine", "room_elimination"))
        self.assertEqual(house_events.appliance_actor("emptied", attrs), "Kristine")
        record = tr4.records(app)[-1]
        self.assertEqual((record["emptied_by"], record["emptied_ts"]), ("kristine", emptied_ts))
        self.assertEqual(record["attribution"]["start"]["method"], "sole_occupant")
        self.assertEqual(app.state, "Emptied")
        (call,) = app.set_state_calls[published_before:]
        self.assertEqual((call["state"], call["replace"]), ("Emptied", False))
        self.assertEqual(call["attributes"]["emptied_by"], "kristine")
        self.assertEqual(set(call["attributes"]) - {"last_drum_clean"}, {"emptied_by", "emptied_by_method"})

    def test_a_direct_running_to_emptied_record_is_named_the_same_way(self):
        app, actor = self._finished(on_since("mikkel", since=trs.NOW - timedelta(hours=1)), minutes=100, direct=True)

        app._transition_to_emptied("door_opened_first")

        record = tr4.records(app)[-1]
        self.assertIsNone(record["emptied_by"])
        self.assertIn("pending_until", record["attribution"]["empty"])
        (cb, delay, kw), = callbacks_named(app.scheduled, "_actor_followup_empty")
        app.now += timedelta(seconds=delay)
        cb(kw)
        self.assertEqual(tr4.records(app)[-1]["emptied_by"], "kristine")
        self.assertEqual(named(self._entity(app), "emptied"), ("kristine", "room_elimination"))

    def test_an_emptier_that_stays_unknown_records_the_final_reason(self):
        app, actor = self._finished(on_since("mikkel", since=trs.NOW - timedelta(hours=1)))
        app._transition_to_emptied("Door opened - emptying")
        (cb, delay, kw), = callbacks_named(app.scheduled, "_actor_followup_empty")
        actor._room_log = {"mikkel": ((trs.NOW - timedelta(hours=1), True), (trs.NOW + timedelta(seconds=200), False))}
        published_before = len(app.set_state_calls)

        app.now += timedelta(seconds=delay)
        cb(kw)

        record = tr4.records(app)[-1]
        self.assertIsNone(record["emptied_by"])
        self.assertEqual(record["attribution"]["empty"]["reason"], "multi_occupant")
        self.assertNotIn("pending_until", record["attribution"]["empty"])
        self.assertEqual(len(app.set_state_calls), published_before)

    def test_a_record_that_is_no_longer_this_emptying_is_left_alone(self):
        app, actor = self._finished(on_since("mikkel", since=trs.NOW - timedelta(hours=1)))
        app._transition_to_emptied("Door opened - emptying")
        (cb, delay, kw), = callbacks_named(app.scheduled, "_actor_followup_empty")
        data = json.loads(Path(app.feedback_path).read_text())
        data["cycles"].append({"ts": "2026-08-12T16:00:00+02:00", "attribution": {"start": None}})
        Path(app.feedback_path).write_text(json.dumps(data))
        before = Path(app.feedback_path).read_text()
        published_before = len(app.set_state_calls)

        app.now += timedelta(seconds=delay)
        cb(kw)

        self.assertEqual(Path(app.feedback_path).read_text(), before)
        self.assertEqual(len(app.set_state_calls), published_before)

    def test_a_restart_during_the_wait_keeps_the_pending_marker_and_raises_nothing(self):
        app, actor = self._finished(on_since("mikkel", since=trs.NOW - timedelta(hours=1)))
        app._transition_to_emptied("Door opened - emptying")
        before = tr4.records(app)[-1]

        second = trs.make_full_init_app(
            sensor_state="Emptied", sensor_attrs=dict(self._entity(app)), power_watts=0.0,
            extra_args={"feedback_file": app.feedback_path},
        )

        self.assertEqual(callbacks_named(second.scheduled, "_actor_followup_empty"), [])
        record = json.loads(Path(app.feedback_path).read_text())["cycles"][-1]
        self.assertEqual(record["attribution"], before["attribution"])
        self.assertEqual(record["emptied_ts"], before["emptied_ts"])
        self.assertIsNone(record.get("emptied_by"))
        self.assertIn("pending_until", record["attribution"]["empty"])


# ----------------------------------------------------------------------------------------------
# The dishwasher
# ----------------------------------------------------------------------------------------------

class DishwasherFollowUps(unittest.TestCase):
    ROOM_LOG = on_since("mikkel", since=tdw.T0 - timedelta(hours=1))

    def _started(self, tmp, rooms=None):
        clock = Clock(tdw.T0)
        actor = room_actor(clock, tdw.T0 - timedelta(hours=2), self.ROOM_LOG if rooms is None else rooms)
        app = tdw.make_app(tmp, clock, actor)
        record_publishes(app)
        app.initialize()
        tdw.load_and_start(app, clock, close_at=tdw.T0)
        return app, clock, actor

    def _unemptied(self, tmp):
        app, clock, actor = self._started(tmp)
        (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")
        fire(app, clock, cb, delay, kw)
        tdw.finish(app, clock)
        clock.now += timedelta(minutes=30)
        return app, clock, actor

    def test_the_start_is_named_when_the_follow_up_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._started(tmp)
            self.assertEqual(named(tdw.published(app), "started"), ("", "unknown"))
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")
            self.assertEqual(delay, 375)
            self.assertEqual(kw, {"actor_event": "dishwasher_start", "actor_anchor": aa.ActorAttribution._iso(tdw.T0),
                                  "actor_cycle_id": app.cycle_id})
            clock.now += timedelta(seconds=delay - 5)
            app._update_running_attributes()  # the store was just written: only a changed name forces the next write
            publishes = len(app.publishes)

            fire(app, clock, cb, 5, kw)

            self.assertEqual(named(tdw.published(app), "started"), ("kristine", "room_elimination"))
            self.assertEqual((tdw.stored(tmp)["started_by"], tdw.stored(tmp)["started_by_method"]),
                             ("kristine", "room_elimination"))
            (entity, call), = app.publishes[publishes:]
            self.assertEqual((call["state"], call.get("replace")), ("Running", None))
            self.assertEqual(app.state, "Running")
            tdw.finish(app, clock)
            record = tdw.feedback_cycles(app)[-1]
            self.assertEqual(record["started_by"], "kristine")
            self.assertEqual(record["attribution"]["start"]["method"], "room_elimination")

    def test_a_start_that_stays_unknown_leaves_the_names_empty_and_records_why(self):
        # Mikkel left his room 200 s after the load: an off row the first answer could not see yet.
        rooms = {"mikkel": ((tdw.T0 - timedelta(hours=1), True), (tdw.T0 + timedelta(seconds=200), False))}
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, _actor = self._started(tmp, rooms)
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")
            publishes = len(app.publishes)

            fire(app, clock, cb, delay, kw)

            self.assertEqual(app._cycle_actor["reason"], "multi_occupant")
            self.assertEqual(len(app.publishes), publishes)
            self.assertEqual(named(tdw.published(app), "started"), ("", "unknown"))
            tdw.finish(app, clock)
            record = tdw.feedback_cycles(app)[-1]
            self.assertIsNone(record["started_by"])
            self.assertEqual(record["attribution"]["start"]["reason"], "multi_occupant")
            self.assertNotIn("pending_until", record["attribution"]["start"])

    def test_a_follow_up_of_an_earlier_run_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._started(tmp)
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_start")
            app._transition_to_off("test", force=True)
            clock.now += timedelta(minutes=30)
            app.last_state_change = clock.now - timedelta(seconds=700)
            tdw.load_and_start(app, clock, close_at=clock.now)
            asked = len(actor.calls)

            fire(app, clock, cb, 1, kw)

            self.assertEqual(len(actor.calls), asked)
            self.assertNotEqual(kw["actor_cycle_id"], app.cycle_id)

    def test_the_emptier_is_named_on_the_record_and_the_entity(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, _actor = self._unemptied(tmp)
            tdw.open_door(app)
            self.assertEqual(named(tdw.published(app), "emptied"), ("", "unknown"))
            record = tdw.feedback_cycles(app)[-1]
            self.assertIsNone(record["emptied_by"])
            emptied_ts = record["emptied_ts"]
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_empty")
            self.assertEqual(kw["actor_event"], "dishwasher_emptied")

            fire(app, clock, cb, delay, kw)

            attrs = tdw.published(app)
            self.assertEqual(named(attrs, "emptied"), ("kristine", "room_elimination"))
            self.assertEqual(house_events.appliance_actor("emptied", attrs), "Kristine")
            record = tdw.feedback_cycles(app)[-1]
            self.assertEqual((record["emptied_by"], record["emptied_ts"]), ("kristine", emptied_ts))
            self.assertEqual(record["attribution"]["empty"]["method"], "room_elimination")
            self.assertEqual(app.state, "Emptied")

    def test_an_emptier_that_stays_unknown_records_the_final_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._unemptied(tmp)
            tdw.open_door(app)
            (cb, delay, kw), = callbacks_named(app.timers, "_actor_followup_empty")
            actor._room_log = {"mikkel": ((clock.now - timedelta(hours=1), True), (clock.now + timedelta(seconds=200), False))}
            publishes = len(app.publishes)

            fire(app, clock, cb, delay, kw)

            record = tdw.feedback_cycles(app)[-1]
            self.assertIsNone(record["emptied_by"])
            self.assertEqual(record["attribution"]["empty"]["reason"], "multi_occupant")
            self.assertNotIn("pending_until", record["attribution"]["empty"])
            self.assertEqual(len(app.publishes), publishes)
            self.assertEqual(named(tdw.published(app), "emptied"), ("", "unknown"))

    def test_a_peeked_emptying_is_not_named_afterwards_but_the_next_real_one_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._unemptied(tmp)
            tdw.open_door(app)
            (peek_cb, peek_delay, peek_kw), = callbacks_named(app.timers, "_actor_followup_empty")
            clock.now += timedelta(seconds=10)
            tdw.close_door(app)
            self.assertEqual(app.state, "Unemptied")
            reverted = tdw.feedback_cycles(app)[-1]
            publishes = len(app.publishes)

            fire(app, clock, peek_cb, peek_delay, peek_kw)

            self.assertEqual(tdw.feedback_cycles(app)[-1], reverted)
            self.assertEqual(len(app.publishes), publishes)
            self.assertNotIn("emptied_by", tdw.published(app))

            tdw.open_door(app)
            second = callbacks_named(app.timers, "_actor_followup_empty")[-1]
            self.assertNotEqual(second[2]["actor_anchor"], peek_kw["actor_anchor"])

            fire(app, clock, peek_cb, 1, peek_kw)  # the peek's callback again, now behind a newer emptying
            self.assertIsNone(tdw.feedback_cycles(app)[-1]["emptied_by"])

            fire(app, clock, second[0], second[1], second[2])
            self.assertEqual(tdw.feedback_cycles(app)[-1]["emptied_by"], "kristine")
            self.assertEqual(named(tdw.published(app), "emptied"), ("kristine", "room_elimination"))

    def test_a_restart_during_the_wait_keeps_the_pending_marker_and_raises_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, clock, actor = self._unemptied(tmp)
            tdw.open_door(first)
            before = tdw.feedback_cycles(first)[-1]
            clock.now += timedelta(minutes=1)

            second = tdw.make_app(tmp, clock, actor, sensor_state="Emptied", sensor_attrs=tdw.published(first))
            second.initialize()
            clock.now += timedelta(minutes=1)
            tdw.close_door(second)

            self.assertEqual(callbacks_named(second.timers, "_actor_followup_empty"), [])
            record = tdw.feedback_cycles(second)[-1]
            self.assertEqual(record, before)
            self.assertIsNone(record["emptied_by"])
            self.assertIn("pending_until", record["attribution"]["empty"])


if __name__ == "__main__":
    unittest.main()
