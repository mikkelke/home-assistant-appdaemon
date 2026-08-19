# tests/test_replay_harness.py - Unit tests for tests/replay.py (build spec 10-11): the
# FakeClock/FakeScheduler determinism seam, tape-loader validation, and schema validation of
# every tape fixture under tests/tapes/. Run from repo root:
#   python3 -m unittest discover -s apps/appliances/tests -q
#
# MUST NOT import appliance_fsm.py/appliance_detectors.py - replay.py is built in parallel
# with the engine those own; this file only ever imports replay.py itself, plus (for the
# end-to-end dispatch checks) a small hand-rolled engine double defined right here, never the
# real thing. See replay.py's module docstring for the two integration seams this pins around.

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay as rp  # noqa: E402

TAPES_DIR = Path(__file__).resolve().parent / "tapes"

EXPECTED_TAPE_FILES = {
    "real_01_powerdrop.json", "real_02_powerdrop.json", "real_03_restartstorm.json",
    "t1_cooling_swallow.json", "t2_stale_restore_announce.json", "t3_feedback_once.json",
    "t4_dst_fallback.json", "t5_reclassify_shorter.json", "t6_helper_no_clock.json",
}


def _dt(seconds=0):
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def _minimal_tape(**overrides):
    tape = {
        "meta": {"appliance": "dryer", "source": "synthetic", "captured_at": "2026-01-01T00:00:00+00:00"},
        "args": {},
        "initial": {"entity_state": "Off", "entity_attrs": {}, "helper_state": None, "store": None},
        "events": [{"t": 0, "kind": "power", "watts": 0}],
        "expect": [{"t_approx": 0, "published": "Off"}],
    }
    tape.update(overrides)
    return tape


class FakeClockBasics(unittest.TestCase):
    def test_now_returns_start_and_is_aware_utc(self):
        clock = rp.FakeClock(_dt())
        self.assertEqual(clock.now(), _dt())
        self.assertIsNotNone(clock.now().tzinfo)

    def test_set_rejects_naive_datetime(self):
        clock = rp.FakeClock(_dt())
        with self.assertRaises(ValueError):
            clock.set(datetime(2026, 1, 1))

    def test_advance_moves_forward_by_seconds(self):
        clock = rp.FakeClock(_dt())
        clock.advance(30)
        self.assertEqual(clock.now(), _dt(30))


class FakeSchedulerOrderingSemantics(unittest.TestCase):
    """spec 10.1: 'fires due callbacks in time order, advancing FakeClock to each fire time
    first' - the whole harness's determinism rests on this."""

    def test_due_callbacks_fire_in_fire_at_order_not_arm_order(self):
        clock = rp.FakeClock(_dt())
        sched = rp.FakeScheduler(clock)
        fired = []
        sched.run_in(lambda kw: fired.append("late"), 20)
        sched.run_in(lambda kw: fired.append("early"), 5)
        sched.advance_to(_dt(30))
        self.assertEqual(fired, ["early", "late"])

    def test_ties_break_by_arm_order(self):
        clock = rp.FakeClock(_dt())
        sched = rp.FakeScheduler(clock)
        fired = []
        sched.run_in(lambda kw: fired.append("first-armed"), 10)
        sched.run_in(lambda kw: fired.append("second-armed"), 10)
        sched.advance_to(_dt(10))
        self.assertEqual(fired, ["first-armed", "second-armed"])

    def test_clock_is_advanced_to_each_fire_time_before_invocation(self):
        clock = rp.FakeClock(_dt())
        sched = rp.FakeScheduler(clock)
        seen = []
        sched.run_in(lambda kw: seen.append(clock.now()), 5)
        sched.run_in(lambda kw: seen.append(clock.now()), 15)
        sched.advance_to(_dt(30))
        self.assertEqual(seen, [_dt(5), _dt(15)])
        self.assertEqual(clock.now(), _dt(30))  # still lands on target once the queue drains

    def test_cancel_prevents_firing_and_reports_whether_it_removed_anything(self):
        clock = rp.FakeClock(_dt())
        sched = rp.FakeScheduler(clock)
        fired = []
        handle = sched.run_in(lambda kw: fired.append("boom"), 5)
        self.assertTrue(sched.cancel(handle))
        self.assertFalse(sched.cancel(handle))  # already gone - nothing left to cancel
        sched.advance_to(_dt(30))
        self.assertEqual(fired, [])

    def test_callback_scheduling_new_due_work_is_picked_up_in_the_same_pass(self):
        clock = rp.FakeClock(_dt())
        sched = rp.FakeScheduler(clock)
        fired = []

        def first(kw):
            fired.append("first")
            sched.run_in(lambda kw2: fired.append("chained"), 1)  # due within this same advance

        sched.run_in(first, 5)
        sched.advance_to(_dt(10))
        self.assertEqual(fired, ["first", "chained"])

    def test_advance_to_cannot_move_backward(self):
        clock = rp.FakeClock(_dt(50))
        sched = rp.FakeScheduler(clock)
        with self.assertRaises(ValueError):
            sched.advance_to(_dt(10))

    def test_advance_to_with_nothing_pending_still_reaches_target(self):
        clock = rp.FakeClock(_dt())
        sched = rp.FakeScheduler(clock)
        sched.advance_to(_dt(100))
        self.assertEqual(clock.now(), _dt(100))


class TapeLoaderValidation(unittest.TestCase):
    def _write(self, tmpdir, tape):
        path = Path(tmpdir) / "tape.json"
        path.write_text(json.dumps(tape))
        return path

    def test_valid_tape_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, _minimal_tape())
            loaded = rp.load_tape(path)
            self.assertEqual(loaded["expect"][0]["published"], "Off")

    def test_event_missing_kind_is_rejected_and_names_the_event(self):
        tape = _minimal_tape(events=[{"t": 0}])
        with self.assertRaises(rp.TapeValidationError) as ctx:
            rp.validate_tape(tape, name="bad.json")
        self.assertIn("event[0]", str(ctx.exception))
        self.assertIn("bad.json", str(ctx.exception))

    def test_event_unknown_kind_is_rejected(self):
        tape = _minimal_tape(events=[{"t": 0, "kind": "spin_cycle"}])
        with self.assertRaises(rp.TapeValidationError) as ctx:
            rp.validate_tape(tape)
        self.assertIn("event[0]", str(ctx.exception))
        self.assertIn("spin_cycle", str(ctx.exception))

    def test_power_event_missing_watts_is_rejected(self):
        tape = _minimal_tape(events=[{"t": 0, "kind": "power"}])
        with self.assertRaises(rp.TapeValidationError) as ctx:
            rp.validate_tape(tape)
        self.assertIn("watts", str(ctx.exception))

    def test_door_event_bad_state_is_rejected(self):
        tape = _minimal_tape(events=[{"t": 0, "kind": "door", "state": "ajar"}])
        with self.assertRaises(rp.TapeValidationError):
            rp.validate_tape(tape)

    def test_non_monotonic_event_t_is_rejected_and_names_the_offender(self):
        tape = _minimal_tape(events=[
            {"t": 10, "kind": "tick"},
            {"t": 5, "kind": "tick"},
        ])
        with self.assertRaises(rp.TapeValidationError) as ctx:
            rp.validate_tape(tape)
        self.assertIn("event[1]", str(ctx.exception))

    def test_expect_bad_published_value_is_rejected(self):
        tape = _minimal_tape(expect=[{"t_approx": 0, "published": "Spinning"}])
        with self.assertRaises(rp.TapeValidationError) as ctx:
            rp.validate_tape(tape)
        self.assertIn("expect[0]", str(ctx.exception))

    def test_expect_flag_wrong_type_is_rejected(self):
        tape = _minimal_tape(expect=[{"t_approx": 0, "published": "Off", "hypothesis": "yes"}])
        with self.assertRaises(rp.TapeValidationError):
            rp.validate_tape(tape)

    def test_non_monotonic_expect_t_approx_is_rejected(self):
        tape = _minimal_tape(expect=[
            {"t_approx": 10, "published": "Off"},
            {"t_approx": 5, "published": "Running"},
        ])
        with self.assertRaises(rp.TapeValidationError) as ctx:
            rp.validate_tape(tape)
        self.assertIn("expect[1]", str(ctx.exception))

    def test_bad_meta_source_is_rejected(self):
        tape = _minimal_tape(meta={"appliance": "dryer", "source": "vibes"})
        with self.assertRaises(rp.TapeValidationError):
            rp.validate_tape(tape)

    def test_events_not_a_list_is_rejected(self):
        tape = _minimal_tape(events={"t": 0})
        with self.assertRaises(rp.TapeValidationError):
            rp.validate_tape(tape)


class AllTapeFixturesValidate(unittest.TestCase):
    """Schema validation of every checked-in tape fixture - loadable, monotonic t, expect
    entries well-formed. Does not require or exercise a real engine."""

    def test_exactly_the_expected_fixture_files_are_present(self):
        actual = {p.name for p in TAPES_DIR.glob("*.json")}
        self.assertEqual(actual, EXPECTED_TAPE_FILES)

    def test_every_fixture_loads_and_validates(self):
        for name in sorted(EXPECTED_TAPE_FILES):
            with self.subTest(tape=name):
                tape = rp.load_tape(TAPES_DIR / name)
                self.assertIsInstance(tape["events"], list)
                self.assertGreater(len(tape["events"]), 0)
                self.assertIsInstance(tape["expect"], list)
                self.assertGreater(len(tape["expect"]), 0)

    def test_every_fixture_events_are_t_monotonic(self):
        for name in sorted(EXPECTED_TAPE_FILES):
            with self.subTest(tape=name):
                tape = rp.load_tape(TAPES_DIR / name)
                ts = [e["t"] for e in tape["events"]]
                self.assertEqual(ts, sorted(ts), f"{name}: events['t'] is not monotonic")

    def test_every_fixture_expect_entries_are_well_formed(self):
        for name in sorted(EXPECTED_TAPE_FILES):
            with self.subTest(tape=name):
                tape = rp.load_tape(TAPES_DIR / name)
                t_approx = [e["t_approx"] for e in tape["expect"]]
                self.assertEqual(t_approx, sorted(t_approx), f"{name}: expect t_approx is not monotonic")
                for entry in tape["expect"]:
                    self.assertIn(entry["published"], rp.HA_PUBLISHED_VALUES)
                    for flag in ("announced", "pushed", "hypothesis"):
                        if flag in entry:
                            self.assertIsInstance(entry[flag], bool)

    def test_every_fixture_has_provenance_notes(self):
        # Not schema-required (validate_tape does not demand it), but every fixture this
        # harness ships pins a specific incident/finding - authoring diligence, not the loader.
        for name in sorted(EXPECTED_TAPE_FILES):
            with self.subTest(tape=name):
                tape = rp.load_tape(TAPES_DIR / name)
                notes = tape.get("meta", {}).get("notes", "")
                self.assertGreater(len(notes), 40, f"{name}: meta.notes missing/too short")


class _ToyEngine:
    """Hand-rolled engine double - NOT appliance_fsm.ApplianceFSM - satisfying only the
    surface Replay.run() touches (submit()/initialize()/published). Exists purely to prove
    Replay's own dispatch loop (event->Evidence translation, restart handling, clock-driven
    tick firing) works end-to-end without importing the real engine."""

    def __init__(self):
        self.published = "Off"
        self.submitted = []
        self.initialized = 0

    def submit(self, evidence):
        self.submitted.append(evidence)
        t = evidence.type
        if t is rp.EvidenceType.POWER_HIGH and self.published == "Off":
            self.published = "Running"
        elif t is rp.EvidenceType.POWER_LOW and self.published == "Running":
            self.published = "Off"
        elif t is rp.EvidenceType.DOOR_OPENED and self.published == "Running":
            self.published = "Paused"
        elif t is rp.EvidenceType.DOOR_CLOSED and self.published == "Paused":
            self.published = "Running"

    def initialize(self):
        self.initialized += 1
        self.published = "Off"


class ReplayDrivesADispatchLoopEndToEnd(unittest.TestCase):
    """Not one of the three explicitly required categories, but cheap and directly valuable:
    proves replay.py's own event-dispatch loop (not just its data structures in isolation)
    behaves as documented, using a toy engine double instead of the real one."""

    def test_power_door_and_restart_events_drive_the_toy_engine_and_build_a_trace(self):
        tape = _minimal_tape(events=[
            {"t": 0, "kind": "power", "watts": 300},
            {"t": 10, "kind": "door", "state": "on"},
            {"t": 20, "kind": "door", "state": "off"},
            {"t": 30, "kind": "restart"},
        ], expect=[{"t_approx": 0, "published": "Off"}])
        replay = rp.Replay(tape)
        engine = _ToyEngine()

        result = replay.run(engine)

        self.assertEqual(
            [e["published"] for e in result.trace],
            ["Running", "Paused", "Running", "Off"],
        )
        self.assertEqual(engine.initialized, 1)
        self.assertEqual(len(engine.submitted), 3)  # power, door-open, door-close (restart is not submit())

    def test_power_dead_zone_sample_updates_sensor_but_submits_nothing(self):
        tape = _minimal_tape(events=[{"t": 0, "kind": "power", "watts": 6.5}])  # between stop_w=5, start_w=8
        replay = rp.Replay(tape)
        engine = _ToyEngine()

        replay.run(engine)

        self.assertEqual(engine.submitted, [])
        self.assertEqual(replay.entities["power_w"], 6.5)

    def test_tick_advances_clock_without_touching_the_engine(self):
        tape = _minimal_tape(events=[{"t": 50, "kind": "tick"}])
        replay = rp.Replay(tape)
        engine = _ToyEngine()

        replay.run(engine)

        self.assertEqual(replay.clock.now(), replay.anchor + timedelta(seconds=50))
        self.assertEqual(engine.submitted, [])
        self.assertEqual(engine.initialized, 0)

    def test_evidence_fields_match_spec_3_1_shape(self):
        tape = _minimal_tape(events=[{"t": 0, "kind": "power", "watts": 300}])
        replay = rp.Replay(tape)
        engine = _ToyEngine()

        replay.run(engine)

        evidence = engine.submitted[0]
        self.assertEqual(evidence.type, rp.EvidenceType.POWER_HIGH)
        self.assertEqual(evidence.event_class, rp.EventClass.POWER)
        self.assertTrue(evidence.live)
        self.assertEqual(evidence.payload, {"watts": 300.0})
        self.assertEqual(evidence.ts, replay.anchor)


class BuildEngineAdapterIsAnExplicitPlaceholder(unittest.TestCase):
    def test_raises_not_implemented_with_a_todo_integration_marker(self):
        with self.assertRaises(NotImplementedError) as ctx:
            rp.build_engine_adapter({}, rp.FakeClock(_dt()), None)
        self.assertIn("TODO(integration)", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
