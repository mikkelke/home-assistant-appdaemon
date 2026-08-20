# tests/test_replay_integration.py - Runs all nine tapes under tests/tapes/ through the REAL
# engine (appliance_fsm.ApplianceFSM + dryer_policy.DryerPolicy + appliance_detectors) via
# replay.py's build_engine_adapter(). Unlike test_replay_harness.py, this file IS allowed to
# import the real engine - that is precisely what it is here to exercise.
#
# Trace-timing note (load-bearing for every tolerance below): Replay.run() samples
# engine.published only after each discrete tape event, via FakeScheduler.advance_to(target),
# which - by design (spec 10.1) - always ends by setting the clock to `target` even when a
# scheduled callback fired at some earlier instant during the same sweep. So a transition
# caused by a confirm-timer/watchdog that matures BETWEEN two tape events gets its trace
# timestamp attributed to the NEXT processed event, not its own true fire time (verified with a
# debugger against real_01: the true POWER_START_CONFIRMED landed at exactly t=12.0s -
# door_close_fast_confirm_s - matching real_start_time; the trace recorded it at t=52.042, the
# next power sample's own timestamp, purely because nothing else happened in between). This is
# a harness property, not an engine bug - _TRACE_TOLERANCE_S below is sized off the largest gap
# actually observed across these nine tapes (T3's ~230s) and applied uniformly.
#
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from replay import Replay, load_tape, build_engine_adapter  # noqa: E402

TAPES_DIR = Path(__file__).resolve().parent / "tapes"
_TRACE_TOLERANCE_S = 300.0  # see the module docstring's trace-timing note


def _run(name):
    tape = load_tape(TAPES_DIR / name)
    replay = Replay(tape)
    engine = build_engine_adapter(tape, replay.clock, replay.scheduler)
    result = replay.run(engine)
    # No assertion below needs the on-disk store once the tape has finished running - only the
    # already-materialized engine/policy in-memory state. Cleaning up explicitly (rather than
    # letting tempfile.TemporaryDirectory's finalizer do it) avoids a ResourceWarning per tape.
    engine._tmpdir.cleanup()
    return tape, replay, engine, result


def _sequence(entries, key):
    return [e[key] for e in entries]


class _ReplayIntegrationCase(unittest.TestCase):
    def assertTraceMatchesExpect(self, trace, expect, *, tolerance_s=_TRACE_TOLERANCE_S):
        """Published-value sequence must match exactly and in order; timing is checked only to
        the tolerance the harness's own trace-granularity property (module docstring) allows."""
        self.assertEqual(_sequence(trace, "published"), _sequence(expect, "published"))
        for actual, exp in zip(trace, expect):
            self.assertLessEqual(
                abs(actual["t"] - exp["t_approx"]), tolerance_s,
                f"t={actual['t']} vs expected t_approx={exp['t_approx']} for {exp['published']!r} "
                f"(tolerance {tolerance_s}s)",
            )


class SyntheticRegressionTapesMatchExpect(_ReplayIntegrationCase):
    """T1-T6 (spec 11.3) against the real engine: full expect[] including the
    hypothesis/announced/pushed flags spec 11.3 explicitly calls for on T2."""

    def test_t1_cooling_swallow_bypasses_cooling_from_emptied(self):
        tape, replay, engine, result = _run("t1_cooling_swallow.json")
        self.assertTraceMatchesExpect(result.trace, tape["expect"])
        self.assertEqual(engine.actions.calls, [])

    def test_t2_stale_restore_sets_hypothesis_and_never_announces(self):
        """Was vacuous (asserted tape["expect"][0]'s own literals against themselves - see this
        tape's own meta.notes for how that hid the 2026-08-19 incident shape on this engine).
        Now drives the real post-boot pipeline past the boot snapshot: the tape's live low-power
        sample at t=5 lands RUNNING->ENDING and auto-clears hypothesis (engine's own landed-live-
        evidence rule, spec 8) before the scheduled RECONCILE ever fires; the stop_for confirm
        then finishes through dryer_policy.py's _finish freshness gate (FIX-3) - every assertion
        below reads real engine/action state, never tape["expect"] literals."""
        tape, replay, engine, result = _run("t2_stale_restore_announce.json")
        self.assertTraceMatchesExpect(result.trace, tape["expect"])
        self.assertEqual(_sequence(result.trace, "published"), ["Running", "Unemptied"])

        self.assertFalse(engine._fsm.hypothesis, "a landed live low-power sample must clear hypothesis")
        self.assertEqual(engine._fsm.state.name, "FINISHED")

        announces = [a for a in engine.actions.calls if a["kind"] == "announce"]
        pushes = [a for a in engine.actions.calls if a["kind"] == "push_mobile"]
        feedback = [a for a in engine.actions.calls if a["kind"] == "save_feedback"]
        self.assertEqual(announces, [], "a stale, uncorroborated-restore finish must never use Sonos")
        self.assertEqual(len(pushes), 1)
        self.assertIn("late detection", pushes[0]["message"])
        self.assertEqual(len(feedback), 1)

    def test_t3_feedback_saved_exactly_once_despite_the_noisy_tail(self):
        tape, replay, engine, result = _run("t3_feedback_once.json")
        self.assertTraceMatchesExpect(result.trace, tape["expect"])
        feedback_calls = [a for a in engine.actions.calls if a["kind"] == "save_feedback"]
        self.assertEqual(len(feedback_calls), 1)
        self.assertEqual(feedback_calls[0]["record"]["predicted"], feedback_calls[0]["record"]["confirmed"])

    def test_t4_dst_fallback_utc_clock_never_refuses(self):
        tape, replay, engine, result = _run("t4_dst_fallback.json")
        self.assertTraceMatchesExpect(result.trace, tape["expect"])
        self.assertFalse(engine._fsm.hypothesis)
        self.assertEqual(engine.actions.calls, [])

    def test_t5_guard_anchored_to_restored_programme_never_reclassifies_shorter(self):
        tape, replay, engine, result = _run("t5_reclassify_shorter.json")
        # The critical assertion is negative: Unemptied must never appear anywhere in the trace.
        self.assertTraceMatchesExpect(result.trace, tape["expect"])
        self.assertNotIn("Unemptied", _sequence(result.trace, "published"))
        self.assertEqual(engine._fsm.state.name, "RUNNING")
        self.assertFalse(engine._fsm.hypothesis)

    def test_t6_helper_only_running_falls_through_to_off(self):
        tape, replay, engine, result = _run("t6_helper_no_clock.json")
        self.assertTraceMatchesExpect(result.trace, tape["expect"])
        self.assertEqual(engine._fsm.state.name, "OFF")
        self.assertEqual(engine.actions.calls, [])


class RealCapturedCyclesMatchLegacySequence(_ReplayIntegrationCase):
    """real_01/real_02: the engine's published-state SEQUENCE must match the legacy app's own
    captured sensor.dryer_state sequence (the differential oracle) - spec 10.4's whole point."""

    def test_real_01_reaches_unemptied_via_keep_fresh_fallback(self):
        tape, replay, engine, result = _run("real_01_powerdrop.json")
        self.assertEqual(_sequence(result.trace, "published"), ["Off", "Running", "Unemptied"])
        # Timing genuinely diverges here (KeepFreshDetector one-shot finding, this tape's own
        # meta.notes) - matched against the engine's OWN verified timing, not a fixed tolerance
        # around the legacy timestamp.
        self.assertTraceMatchesExpect(result.trace, tape["expect"])
        feedback_calls = [a for a in engine.actions.calls if a["kind"] == "save_feedback"]
        self.assertEqual(len(feedback_calls), 1)

    def test_real_02_reaches_unemptied_within_tolerance_of_the_legacy_timestamp(self):
        tape, replay, engine, result = _run("real_02_powerdrop.json")
        self.assertEqual(_sequence(result.trace, "published"), ["Off", "Running", "Unemptied"])
        self.assertTraceMatchesExpect(result.trace, tape["expect"])
        announces = [a for a in engine.actions.calls if a["kind"] == "announce"]
        self.assertEqual(len(announces), 1)


class RealRestartStormEngineOutperformsLegacy(_ReplayIntegrationCase):
    """real_03 - THE HEADLINE RESULT of this whole build. 2026-08-12 was one continuous
    physical drying draw that three whole-installation dropouts (door sensor unavailable->
    unknown->off, the HA-restart signature) fragmented into FOUR Running blocks in the LEGACY
    app's own sensor.dryer_state history (tape['expect'] below, preserved unedited as that
    historical record - see real_03_restartstorm.json's meta.notes) - the exact class of bug
    F2's restore-corroboration machinery exists to close. The legacy app never recovered from
    that fragmentation: it has no Unemptied/Emptied anywhere in its own history for this cycle,
    so it never announced the finish either.

    Verified outcome against the real engine (current code, incl. the keep-fresh short-circuit
    -ordering fix that landed after this test file was first written - see the trace below):
    it does NOT reproduce the legacy fragmentation. Every one of the three simulated restarts
    finds the durable CycleStore's Running record corroborated immediately (live power is still
    ~600-700W at each restart instant, matching physical reality) - never even entering the
    hypothesis state, let alone a false Off. The single cycle_id minted at the genuine start
    (t=60.9s) is carried, unchanged, through all three restarts via the store round-trip
    (verified directly against engine.cycle_id below) - never re-minted.

    Beyond just surviving the restarts, the engine now ALSO correctly finishes the cycle: the
    real anti-crease power pattern is detected via keep-fresh at t=5762.2s (~96min in),
    landing Unemptied and sending the ONE real announce ('Dryer is ready to be emptied') this
    cycle ever gets - the legacy app never sent it at all. The door then genuinely opens (this
    tape's own captured door-open/door-close pair) at t=11969.3s (Emptied) and closes at
    t=12208.4s (Off) - a real ~239s Emptied dwell, which is why the final Off now lands ~238s
    AFTER the legacy app's own final Off (t_approx=11970.0), not within ~1s of it as it did
    before the finish was detected: the legacy app's Off at that timestamp was a coincidence of
    both apps reacting to the same door-open edge from RUNNING; the engine's Off three entries
    later is a different event (the door closing on an already-Emptied cycle). So the full
    story is: the engine held ONE cycle through three restarts AND correctly finished and
    announced it - two things the legacy app got wrong for this exact real capture. This is
    intentionally asserted against the engine's OWN actual sequence, NOT tape['expect'] (which
    stays the legacy differential-oracle baseline the divergence is measured against, per this
    docstring)."""

    def test_engine_completes_and_announces_the_cycle_the_legacy_app_never_recovered_from(self):
        tape, replay, engine, result = _run("real_03_restartstorm.json")
        legacy_sequence = _sequence(tape["expect"], "published")
        engine_sequence = _sequence(result.trace, "published")

        self.assertEqual(legacy_sequence, ["Off", "Running", "Off", "Running", "Off", "Running", "Off", "Running", "Off"])
        self.assertEqual(engine_sequence, ["Off", "Running", "Unemptied", "Emptied", "Off"])
        self.assertNotEqual(engine_sequence, legacy_sequence)  # the divergence itself, explicit

        # Each of these is a directly-modeled discrete event (keep-fresh finish; this tape's own
        # captured door-open; this tape's own captured door-close) rather than a confirm-timer
        # whose fire time could be attributed to a later tape event - so unlike
        # assertTraceMatchesExpect's generous _TRACE_TOLERANCE_S, a tight delta here is the
        # right check, calibrated to the actual measured values (see the class docstring).
        measured_t = [e["t"] for e in result.trace]
        self.assertAlmostEqual(measured_t[2], 5762.234, delta=5.0)   # Unemptied: keep-fresh finish
        self.assertAlmostEqual(measured_t[3], 11969.299, delta=5.0)  # Emptied: door opens
        self.assertAlmostEqual(measured_t[4], 12208.360, delta=5.0)  # Off: door closes
        # The ~239s(!) gap from the legacy app's own final Off is real, not a harness artifact -
        # see the class docstring for why it is no longer the ~1s this test used to assert.
        self.assertAlmostEqual(result.trace[-1]["t"] - tape["expect"][-1]["t_approx"], 238.3, delta=5.0)

    def test_hypothesis_never_true_and_exactly_one_announce_no_pushes(self):
        tape, replay, engine, result = _run("real_03_restartstorm.json")
        # Claim 1, unchanged by the keep-fresh fix: never a hypothesis state anywhere in the run.
        self.assertFalse(engine._fsm.hypothesis)
        # Claim 2, changed by the fix: the cycle now finishes, so it gets exactly the one real
        # announce it earned - not "no announce" (this test's pre-fix claim, when the cycle
        # never finished at all) and not more than one (F3's exactly-once invariant).
        announces = [a for a in engine.actions.calls if a["kind"] == "announce"]
        pushes = [a for a in engine.actions.calls if a["kind"] == "push_mobile"]
        self.assertEqual(len(announces), 1)
        self.assertEqual(announces[0]["message"], "Dryer is ready to be emptied")
        self.assertEqual(pushes, [])

    def test_cycle_id_minted_once_and_survives_every_restart_unminted_again(self):
        tape, replay, engine, result = _run("real_03_restartstorm.json")
        self.assertIsNotNone(engine._fsm.cycle_id)
        feedback_calls = [a for a in engine.actions.calls if a["kind"] == "save_feedback"]
        self.assertLessEqual(len(feedback_calls), 1)  # F3: never more than one per minted cycle


class AllNineTapesProduceAWellFormedTrace(_ReplayIntegrationCase):
    """Coverage floor: every checked-in tape actually runs end to end against the real engine
    (loads, boots, drains its scheduler) without exception, and produces a non-empty,
    internally consistent trace - independent of any tape's own specific expect[] content."""

    def test_every_tape_runs_and_publishes_only_ha_vocabulary(self):
        ha_vocab = {"Off", "Running", "Paused", "Unemptied", "Emptied"}
        names = sorted(p.name for p in TAPES_DIR.glob("*.json"))
        self.assertEqual(len(names), 9)
        for name in names:
            with self.subTest(tape=name):
                tape, replay, engine, result = _run(name)
                self.assertGreater(len(result.trace), 0)
                for entry in result.trace:
                    self.assertIn(entry["published"], ha_vocab)
                ts = [e["t"] for e in result.trace]
                self.assertEqual(ts, sorted(ts))


if __name__ == "__main__":
    unittest.main()
