# tests/test_fsm_engine.py - engine-core tests for appliance_fsm.ApplianceFSM.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# These exercise the ENGINE mechanics only (cooling matrix, publish mapping, hypothesis gate,
# exactly-once feedback, table interpreter) against a deliberately physics-free TestPolicy, so a
# failure here is unambiguously an engine bug, never a dryer-policy one. Time is driven exclusively
# through the FakeClock/FakeScheduler stubs defined locally below (the real engine reads now() and
# schedules only via these injected seams), and only the I/O boundary is faked - the transition/
# cooling/feedback logic under test is always the real ApplianceFSM code.

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appliance_fsm import (  # noqa: E402
    ApplianceFSM,
    BYPASS,
    Evidence,
    EvidenceType,
    RESPECT,
    Row,
    State,
    TransitionTable,
)

E = EvidenceType
S = State
T0 = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


# --- local injected seams (spec 10.1): deterministic clock + scheduler ---


class FakeClock:
    def __init__(self, t0=T0):
        self._t = t0

    def now(self):
        return self._t

    def advance(self, secs):
        self._t = self._t + timedelta(seconds=secs)


class FakeScheduler:
    """Stores (fire_at, seq, cb, alive). advance_to fires all due callbacks in time order, moving the
    clock to each fire time first - so confirm timers and watchdogs are deterministic."""

    def __init__(self, clock):
        self.clock = clock
        self._timers = []
        self._seq = 0

    def run_in(self, delay_s, cb):
        handle = self._seq
        self._seq += 1
        self._timers.append([self.clock.now() + timedelta(seconds=delay_s), handle, cb, True])
        return handle

    def cancel(self, handle):
        for t in self._timers:
            if t[1] == handle:
                t[3] = False

    def pending(self):
        return [t for t in self._timers if t[3]]

    def advance_to(self, when):
        while True:
            due = sorted([t for t in self._timers if t[3] and t[0] <= when], key=lambda t: (t[0], t[1]))
            if not due:
                break
            t = due[0]
            t[3] = False
            self.clock._t = t[0]
            t[2]()
        self.clock._t = when

    def advance(self, secs):
        self.advance_to(self.clock.now() + timedelta(seconds=secs))


# --- fakes for the host-supplied surfaces ---


class RecordingSink:
    def __init__(self):
        self.announces = []
        self.pushes = []
        self.selects = []
        self.resets = []
        self.feedbacks = []

    def announce(self, message, **kw):
        self.announces.append(message)

    def push_mobile(self, message, **kw):
        self.pushes.append(message)

    def select_option(self, entity, option, **kw):
        self.selects.append((entity, option))

    def reset_selectors(self, **kw):
        self.resets.append(kw)

    def save_feedback(self, record):
        self.feedbacks.append(record)


class LogSink:
    def __init__(self):
        self.records = []

    def __call__(self, message, level="INFO"):
        self.records.append((level, message))

    def at(self, level):
        return [m for lv, m in self.records if lv == level]

    def has(self, needle):
        return any(needle in m for _lv, m in self.records)


class PublishRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, published, *, internal, store_only, attrs):
        self.calls.append(
            {"published": published, "internal": internal, "store_only": store_only, "attrs": dict(attrs)}
        )

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


class TestPolicy:
    """Physics-free policy: enough named guards/actions and a transition table to exercise every
    engine mechanic. Guards read only ctx.evidence.payload; actions only touch engine primitives."""

    def __init__(self):
        self.ran = []
        self.guards = {
            "g_true": lambda ctx: True,
            "g_false": lambda ctx: False,
            "g_high": lambda ctx: ctx.evidence.payload.get("watts", 0) >= 100,
            "g_low": lambda ctx: ctx.evidence.payload.get("watts", 999) <= 5,
        }
        self.actions = {
            "a_mint": self._a_mint,
            "a_touch": self._a_touch,
            "a_finish": self._a_finish,
            "a_notify": self._a_notify,
        }
        self.table = {
            S.OFF: {
                E.POWER_HIGH: [Row(action="a_touch", target=S.STARTING)],
            },
            S.STARTING: {
                E.POWER_START_CONFIRMED: [Row(guards=("g_high",), action="a_mint", target=S.RUNNING)],
                E.POWER_LOW: [Row(target=S.OFF)],
            },
            S.RUNNING: {
                E.POWER_LOW: [Row(target=S.ENDING)],
                E.POWER_HIGH: [Row(action="a_touch")],
                E.DOOR_OPENED: [Row(target=S.PAUSED)],
                E.POWER_END_CONFIRMED: [Row(action="a_finish", target=S.FINISHED)],
                E.KEEP_FRESH: [
                    Row(guards=("g_false",), action="a_touch", target=S.OFF),
                    Row(guards=("g_true",), target=S.ENDING),
                ],
                E.WD_RUNNING: [Row(target=S.OFF)],
                E.RECONCILE: [Row(target=S.OFF)],
                E.PLUG_OUTAGE: [Row(guards=("g_false",), target=S.OFF)],
                E.FORCE_EMPTIED: [Row(action="a_notify")],
            },
            S.ENDING: {
                E.POWER_END_CONFIRMED: [Row(action="a_finish", target=S.FINISHED)],
                E.POWER_RECOVERED: [Row(target=S.RUNNING)],
            },
            S.PAUSED: {
                E.DOOR_CLOSED: [
                    Row(guards=("g_high",), target=S.RUNNING),
                    Row(guards=("g_low",), target=S.FINISHED),
                ],
            },
            S.FINISHED: {
                E.DOOR_OPENED: [Row(target=S.EMPTIED)],
                E.POWER_HIGH: [Row()],
                E.KEEP_FRESH: [Row(action="a_finish")],
                E.WD_UNEMPTIED: [Row(target=S.OFF)],
            },
            S.EMPTIED: {
                E.DOOR_CLOSED: [Row(target=S.OFF)],
            },
        }

    def _a_mint(self, ctx):
        self.ran.append("a_mint")
        ctx.mint_cycle_id()

    def _a_touch(self, ctx):
        self.ran.append("a_touch")

    def _a_finish(self, ctx):
        self.ran.append("a_finish")
        ctx.request_feedback({"end_reason": "test", "cycle_id": ctx.cycle_id})

    def _a_notify(self, ctx):
        self.ran.append("a_notify")
        ctx.announce("done")
        ctx.push_mobile("done")
        ctx.select_option("input_select.x", "Y")


def make_engine(*, state=S.OFF, cooling_period=0.0, policy=None):
    policy = policy or TestPolicy()
    clock = FakeClock()
    sched = FakeScheduler(clock)
    sink = RecordingSink()
    log = LogSink()
    pub = PublishRecorder()
    fsm = ApplianceFSM(
        policy, sink, clock, sched, pub, log, cooling_period=cooling_period, initial_state=state
    )
    return fsm, policy, clock, sched, sink, log, pub


def ev(etype, clock, watts=None, live=True):
    payload = {} if watts is None else {"watts": watts}
    return Evidence.make(etype, clock.now(), "test", live=live, payload=payload)


def drive_to_running(fsm, clock):
    """OFF -> STARTING -> RUNNING (mints a cycle_id). Leaves last_state_change stamped at now."""
    fsm.submit(ev(E.POWER_HIGH, clock, watts=200))
    fsm.submit(ev(E.POWER_START_CONFIRMED, clock, watts=200))


# --- cooling matrix (spec 3.3) ---


class CoolingMatrixByEventClass(unittest.TestCase):
    def test_respects_by_class_and_row_override(self):
        fsm, *_ = make_engine(cooling_period=600)
        clock = fsm._clock

        def e(t):
            return Evidence.make(t, clock.now(), "t")

        # PHYSICAL + CLOCK + RESTORE bypass; POWER respects; WD only-running respects.
        self.assertFalse(fsm._cooling_respects(Row(), e(E.DOOR_OPENED)))
        self.assertFalse(fsm._cooling_respects(Row(), e(E.FORCE_EMPTIED)))
        self.assertFalse(fsm._cooling_respects(Row(), e(E.RECONCILE)))
        self.assertFalse(fsm._cooling_respects(Row(), e(E.BOOT_RESTORE)))
        self.assertTrue(fsm._cooling_respects(Row(), e(E.POWER_END_CONFIRMED)))
        self.assertTrue(fsm._cooling_respects(Row(), e(E.PLUG_OUTAGE)))
        self.assertTrue(fsm._cooling_respects(Row(), e(E.WD_RUNNING)))
        self.assertFalse(fsm._cooling_respects(Row(), e(E.WD_UNEMPTIED)))
        self.assertFalse(fsm._cooling_respects(Row(), e(E.WD_PAUSE)))
        self.assertFalse(fsm._cooling_respects(Row(), e(E.WD_EMPTIED)))
        # Row override wins over the class default (POWER routed to a physical outcome must bypass).
        self.assertFalse(fsm._cooling_respects(Row(cooling=BYPASS), e(E.POWER_END_CONFIRMED)))
        self.assertTrue(fsm._cooling_respects(Row(cooling=RESPECT), e(E.DOOR_OPENED)))

    def test_power_transition_refused_logs_exact_line(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(cooling_period=600)
        drive_to_running(fsm, clock)  # stamps last_state_change at T0
        fsm.submit(ev(E.POWER_LOW, clock, watts=0))  # RUNNING -> ENDING (store-only, no re-stamp)
        res = fsm.submit(ev(E.POWER_END_CONFIRMED, clock, watts=0))  # elapsed 0 < 600 -> REFUSED
        self.assertTrue(res.refused)
        self.assertEqual(fsm.state, S.ENDING)
        self.assertEqual(fsm.published, "Running")
        self.assertIn(
            "REFUSED ENDING->FINISHED (POWER_END_CONFIRMED/POWER): cooling 0s < 600s", log.at("INFO")
        )
        self.assertEqual(sink.feedbacks, [])
        # Past the window it lands and feeds back exactly once.
        clock.advance(601)
        fsm.submit(ev(E.POWER_END_CONFIRMED, clock, watts=0))
        self.assertEqual(fsm.state, S.FINISHED)
        self.assertEqual(len(sink.feedbacks), 1)

    def test_watchdog_running_respects_others_bypass(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(cooling_period=600)
        drive_to_running(fsm, clock)
        res = fsm.submit(ev(E.WD_RUNNING, clock))  # RUNNING->OFF, running watchdog RESPECTS
        self.assertTrue(res.refused)
        self.assertEqual(fsm.state, S.RUNNING)
        self.assertTrue(log.has("REFUSED RUNNING->OFF (WD_RUNNING/WATCHDOG): cooling 0s < 600s"))

    def test_physical_bypasses_cooling_the_34h_wedge(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(cooling_period=600)
        drive_to_running(fsm, clock)
        fsm.submit(ev(E.POWER_LOW, clock, watts=0))
        clock.advance(601)
        fsm.submit(ev(E.POWER_END_CONFIRMED, clock, watts=0))  # -> FINISHED, stamps clock
        fsm.submit(ev(E.DOOR_OPENED, clock))  # FINISHED->EMPTIED (PHYSICAL bypass), stamps clock
        res = fsm.submit(ev(E.DOOR_CLOSED, clock))  # EMPTIED->OFF at elapsed 0 - must NOT be refused
        self.assertFalse(res.refused)
        self.assertEqual(fsm.state, S.OFF)
        self.assertFalse(log.has("REFUSED EMPTIED->OFF"))

    def test_watchdog_unemptied_and_reconcile_bypass(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(cooling_period=600)
        drive_to_running(fsm, clock)
        # WD_UNEMPTIED bypasses even at elapsed 0.
        fsm.submit(ev(E.POWER_LOW, clock, watts=0))
        clock.advance(601)
        fsm.submit(ev(E.POWER_END_CONFIRMED, clock, watts=0))  # FINISHED, stamps clock
        self.assertFalse(fsm.submit(ev(E.WD_UNEMPTIED, clock)).refused)
        self.assertEqual(fsm.state, S.OFF)
        # RECONCILE (CLOCK) bypasses too.
        fsm2, _p, clock2, *_ = make_engine(cooling_period=600)
        drive_to_running(fsm2, clock2)
        self.assertFalse(fsm2.submit(ev(E.RECONCILE, clock2)).refused)
        self.assertEqual(fsm2.state, S.OFF)


# --- publish mapping + store-only rule (spec section 2) ---


class PublishMapping(unittest.TestCase):
    def test_store_only_transition_does_not_republish_value(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine()
        res = fsm.submit(ev(E.POWER_HIGH, clock, watts=200))  # OFF -> STARTING (Off -> Off)
        self.assertEqual(fsm.state, S.STARTING)
        self.assertEqual(fsm.published, "Off")
        self.assertTrue(res.store_only)
        self.assertFalse(res.published_changed)
        self.assertTrue(pub.last["store_only"])
        self.assertEqual(pub.last["published"], "Off")
        self.assertEqual(pub.last["internal"], S.STARTING)
        # A store-only transition never stamps the cooling clock.
        self.assertIsNone(fsm.last_state_change)

    def test_published_transition_republishes_and_stamps(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine()
        fsm.submit(ev(E.POWER_HIGH, clock, watts=200))
        res = fsm.submit(ev(E.POWER_START_CONFIRMED, clock, watts=200))  # STARTING -> RUNNING
        self.assertEqual(fsm.published, "Running")
        self.assertTrue(res.published_changed)
        self.assertFalse(res.store_only)
        self.assertFalse(pub.last["store_only"])
        self.assertEqual(pub.last["published"], "Running")
        self.assertIsNotNone(fsm.last_state_change)

    def test_running_to_ending_is_store_only_running(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine()
        drive_to_running(fsm, clock)
        stamped = fsm.last_state_change
        res = fsm.submit(ev(E.POWER_LOW, clock, watts=0))  # RUNNING -> ENDING (Running -> Running)
        self.assertEqual(fsm.state, S.ENDING)
        self.assertEqual(fsm.published, "Running")
        self.assertTrue(res.store_only)
        self.assertTrue(pub.last["store_only"])
        self.assertEqual(fsm.last_state_change, stamped)  # store-only did not re-stamp

    def test_engine_attrs_carry_internal_hypothesis_cycle(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine()
        drive_to_running(fsm, clock)
        attrs = pub.last["attrs"]
        self.assertEqual(attrs["internal_state"], "RUNNING")
        self.assertFalse(attrs["hypothesis"])
        self.assertEqual(attrs["cycle_id"], fsm.cycle_id)


# --- hypothesis gate (spec section 8) ---


class HypothesisGate(unittest.TestCase):
    """Isolates the _user_action gate itself (call ctx.announce/push_mobile/select_option
    directly) from evidence routing - since the engine now ALSO auto-clears hypothesis on any
    landed live POWER/PHYSICAL transition (see HypothesisAutoClearOnLiveLanding below), driving
    these through fsm.submit(ev(E.FORCE_EMPTIED, ...)) would clear hypothesis (FORCE_EMPTIED is
    live PHYSICAL evidence, and TestPolicy's RUNNING/FORCE_EMPTIED row lands trivially - no target,
    so no cooling check at all) BEFORE a_notify's own ctx.announce ran, which is the CORRECT new
    behavior, just not what these three tests are meant to isolate."""

    def test_announce_and_push_refused_selectors_not_gated(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING)
        fsm.set_hypothesis(True)
        fsm.ctx.announce("done")
        fsm.ctx.push_mobile("done")
        fsm.ctx.select_option("input_select.x", "Y")
        self.assertEqual(sink.announces, [])
        self.assertEqual(sink.pushes, [])
        self.assertEqual(sink.selects, [("input_select.x", "Y")])  # select_option is NOT gated
        self.assertTrue(log.has("REFUSED announce"))
        self.assertTrue(log.has("REFUSED push_mobile"))

    def test_actions_fire_once_hypothesis_cleared(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING)
        fsm.set_hypothesis(True)
        fsm.set_hypothesis(False)
        fsm.ctx.announce("done")
        fsm.ctx.push_mobile("done")
        self.assertEqual(sink.announces, ["done"])
        self.assertEqual(sink.pushes, ["done"])

    def test_boot_restore_can_set_hypothesis(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine()
        snap = {"state": "Running", "cycle_id": "abc", "state_since": clock.now()}
        boot = Evidence.make(E.BOOT_RESTORE, clock.now(), "boot", live=False, payload=snap)
        fsm.apply_boot_restore(boot, hypothesis=True)
        self.assertEqual(fsm.state, S.RUNNING)
        self.assertEqual(fsm.published, "Running")
        self.assertTrue(fsm.hypothesis)
        self.assertEqual(fsm.cycle_id, "abc")
        # An announce from a hypothesis state is refused.
        fsm.ctx.announce("done")
        self.assertEqual(sink.announces, [])


# --- hypothesis auto-clear on a landed live POWER/PHYSICAL transition (spec section 8, the
# integration-found gap: only a_track_power_high/a_resume_from_pause/a_reconcile explicitly
# cleared hypothesis in dryer_policy.py, so a restored-uncorroborated cycle that tumbles at
# mid-power - above stop_w, below start_w, so PowerStartDetector never emits POWER_HIGH and that
# explicit clear never runs - could ride POWER_LOW -> ENDING -> POWER_END_CONFIRMED all the way to
# FINISHED with hypothesis still True, permanently suppressing that cycle's announce/push: the
# silent-finish class this whole project exists to kill) ---


class _HypothesisPolicy:
    """A second, minimal policy for the tests below, kept separate from TestPolicy above so
    RECONCILE can have a real declining guard (mirrors 'history ambiguous, declines to conclude')
    without touching TestPolicy's own unconditional RUNNING/RECONCILE row that other tests in this
    file already depend on."""

    def __init__(self):
        self.ran = []
        self.reconcilable = False
        self.guards = {"g_reconcilable": lambda ctx: self.reconcilable}
        self.actions = {"a_reconcile_clears": self._a_reconcile_clears, "a_finish": self._a_finish}
        self.table = {
            S.RUNNING: {
                E.POWER_LOW: [Row(target=S.ENDING)],
                E.RECONCILE: [Row(guards=("g_reconcilable",), action="a_reconcile_clears", target=S.FINISHED)],
            },
            S.ENDING: {
                E.POWER_END_CONFIRMED: [Row(action="a_finish", target=S.FINISHED)],
            },
            S.FINISHED: {},
        }

    def _a_reconcile_clears(self, ctx):
        self.ran.append("a_reconcile_clears")
        ctx.clear_hypothesis()
        ctx.request_feedback({"end_reason": "reconcile", "cycle_id": ctx.cycle_id})

    def _a_finish(self, ctx):
        self.ran.append("a_finish")
        ctx.request_feedback({"end_reason": "finish", "cycle_id": ctx.cycle_id})
        ctx.announce("ready")


class HypothesisAutoClearOnLiveLanding(unittest.TestCase):
    def test_mid_power_tumble_clears_hypothesis_before_finish_lands_announce_fires(self):
        """(a) The exact reachable failure: uncorroborated restored Running, RECONCILE declines to
        conclude (guard fails - 'history ambiguous'), the machine tumbles at mid-power (never
        triggers a >= start_w POWER_HIGH, so a_track_power_high's explicit clear never runs), then
        finishes via the normal POWER_LOW -> ENDING -> POWER_END_CONFIRMED route. The engine's own
        rule (not a policy action) must clear hypothesis the moment POWER_LOW lands - well before
        FINISHED - so the finish bundle's announce is never gated."""
        policy = _HypothesisPolicy()
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING, cooling_period=0, policy=policy)
        fsm.set_hypothesis(True)
        fsm.set_cycle_id("mid-power")

        # RECONCILE declines - guard fails, no landing, hypothesis untouched.
        res = fsm.submit(ev(E.RECONCILE, clock, live=False))
        self.assertFalse(res.matched)
        self.assertTrue(fsm.hypothesis)

        # Mid-power tumble: no detector ever emits in this range (above stop_w, below start_w) -
        # nothing submitted here, matching the reachable failure exactly.

        # Genuine finish: POWER_LOW lands RUNNING -> ENDING (store-only) - hypothesis must clear
        # here, not later.
        fsm.submit(ev(E.POWER_LOW, clock, watts=3))
        self.assertEqual(fsm.state, S.ENDING)
        self.assertFalse(fsm.hypothesis, "a landed live POWER transition must clear hypothesis immediately")

        fsm.submit(ev(E.POWER_END_CONFIRMED, clock, watts=3))
        self.assertEqual(fsm.state, S.FINISHED)
        self.assertFalse(fsm.hypothesis)
        self.assertEqual(sink.announces, ["ready"], "announce must fire - hypothesis was already clear")

    def test_reconcile_landing_still_governs_its_own_hypothesis_handling(self):
        """(b) RECONCILE (CLOCK class) is excluded from the new engine rule by design - a landed
        RECONCILE transition clears hypothesis ONLY because its own policy action still calls
        ctx.clear_hypothesis(), exactly as before this fix."""
        policy = _HypothesisPolicy()
        policy.reconcilable = True
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING, cooling_period=0, policy=policy)
        fsm.set_hypothesis(True)
        fsm.set_cycle_id("reconcile-case")

        res = fsm.submit(ev(E.RECONCILE, clock, live=False))
        self.assertTrue(res.matched)
        self.assertEqual(fsm.state, S.FINISHED)
        self.assertFalse(fsm.hypothesis)  # cleared by a_reconcile_clears, not the engine rule
        self.assertIn("a_reconcile_clears", policy.ran)

    def test_clock_class_landing_without_an_explicit_clear_leaves_hypothesis_true(self):
        """(b), the other half: prove the engine rule really excludes CLOCK - using TestPolicy's
        own unconditional RUNNING/RECONCILE row (no action at all), a landed CLOCK-class transition
        must NOT auto-clear hypothesis the way a POWER/PHYSICAL landing would."""
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING)
        fsm.set_hypothesis(True)
        res = fsm.submit(ev(E.RECONCILE, clock, live=False))  # TestPolicy: RUNNING/RECONCILE -> OFF, no action
        self.assertTrue(res.matched)
        self.assertEqual(fsm.state, S.OFF)
        self.assertTrue(fsm.hypothesis, "CLOCK-class landing must not be auto-corroboration")

    def test_refused_live_transition_does_not_clear_hypothesis(self):
        """(c) A live POWER transition that the table WOULD have taken, but the cooling gate
        blocked, is not corroboration - nothing actually landed."""
        policy = _HypothesisPolicy()
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.ENDING, cooling_period=600, policy=policy)
        fsm.set_hypothesis(True)
        fsm.set_cycle_id("refused-case")
        fsm._last_state_change = clock.now()  # simulate a transition that just landed moments ago

        res = fsm.submit(ev(E.POWER_END_CONFIRMED, clock, watts=3))  # POWER, RESPECT, elapsed 0 < 600
        self.assertTrue(res.refused)
        self.assertEqual(fsm.state, S.ENDING)
        self.assertTrue(fsm.hypothesis, "a cooling-refused transition must not clear hypothesis")

    def test_unmatched_live_evidence_does_not_clear_hypothesis(self):
        """Belt: an UNLISTED or guard-unmatched live POWER/PHYSICAL submission (nothing in the
        table for it, or it matched no guard combo) must not clear hypothesis either - only a
        genuine landing (matched AND not refused) counts, matching this file's own TableInterpreter
        matched/refused vocabulary."""
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.OFF)
        fsm.set_hypothesis(True)
        res = fsm.submit(ev(E.DOOR_OPENED, clock))  # OFF has no DOOR_OPENED row in TestPolicy
        self.assertFalse(res.matched)
        self.assertTrue(fsm.hypothesis)

    def test_physical_landing_also_clears_hypothesis(self):
        """The rule covers PHYSICAL, not just POWER - reusing TestPolicy's own RUNNING/FORCE_EMPTIED
        row (a_notify: announce+push+select_option) directly demonstrates why
        HypothesisGate's three tests had to stop routing through this exact evidence: a landed live
        PHYSICAL transition is real corroboration, so the announce/push inside a_notify now fire."""
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING)
        fsm.set_hypothesis(True)
        fsm.submit(ev(E.FORCE_EMPTIED, clock))
        self.assertFalse(fsm.hypothesis)
        self.assertEqual(sink.announces, ["done"])
        self.assertEqual(sink.pushes, ["done"])


# --- exactly-once feedback (spec section 6) ---


class ExactlyOnceFeedback(unittest.TestCase):
    def test_duplicate_finished_landing_saves_once(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine()
        drive_to_running(fsm, clock)
        cid = fsm.cycle_id
        fsm.submit(ev(E.POWER_END_CONFIRMED, clock, watts=0))  # -> FINISHED, save #1
        fsm.submit(ev(E.KEEP_FRESH, clock))  # FINISHED stay, a_finish re-requests -> suppressed
        self.assertEqual(len(sink.feedbacks), 1)
        self.assertEqual(sink.feedbacks[0]["cycle_id"], cid)
        self.assertTrue(log.has("feedback suppressed"))

    def test_new_cycle_resets_the_guard(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine()
        drive_to_running(fsm, clock)
        cid1 = fsm.cycle_id
        fsm.submit(ev(E.POWER_END_CONFIRMED, clock, watts=0))  # save #1 (cid1)
        fsm.submit(ev(E.DOOR_OPENED, clock))  # FINISHED -> EMPTIED
        fsm.submit(ev(E.DOOR_CLOSED, clock))  # EMPTIED -> OFF
        drive_to_running(fsm, clock)  # mints cid2
        cid2 = fsm.cycle_id
        fsm.submit(ev(E.POWER_END_CONFIRMED, clock, watts=0))  # save #2 (cid2)
        self.assertEqual(len(sink.feedbacks), 2)
        self.assertNotEqual(cid1, cid2)
        self.assertEqual([f["cycle_id"] for f in sink.feedbacks], [cid1, cid2])

    def test_restore_into_finished_does_not_resave(self):
        # Engine A finishes and saves once for cid X.
        fsm_a, policy_a, clock_a, _s, sink_a, _l, _p = make_engine()
        drive_to_running(fsm_a, clock_a)
        cid = fsm_a.cycle_id
        fsm_a.submit(ev(E.POWER_END_CONFIRMED, clock_a, watts=0))
        self.assertEqual(len(sink_a.feedbacks), 1)
        # Restart: engine B restores Unemptied silently - a silent restore never saves feedback.
        fsm_b, policy_b, clock_b, _s2, sink_b, _l2, _p2 = make_engine()
        fsm_b.enter_state_silently(S.FINISHED, cycle_id=cid)
        self.assertEqual(sink_b.feedbacks, [])
        self.assertEqual(fsm_b.published, "Unemptied")

    def test_restored_guard_blocks_refinish_after_restart(self):
        # Cross-restart exactly-once needs the guard carried (mark_fed_back): a self-heal re-FINISH of
        # an already-fed-back cycle must NOT save a second record.
        fsm, policy, clock, sched, sink, log, pub = make_engine()
        fsm.enter_state_silently(S.ENDING, cycle_id="X")
        fsm.set_cycle_id("X")
        fsm.mark_fed_back("X")
        fsm.submit(ev(E.POWER_END_CONFIRMED, clock, watts=0))  # ENDING -> FINISHED, a_finish
        self.assertEqual(sink.feedbacks, [])  # suppressed: X already fed back


# --- table interpreter semantics (spec section 4 shape) ---


class TableInterpreter(unittest.TestCase):
    def test_guard_combo_first_matching_row_wins(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING)
        fsm.submit(ev(E.KEEP_FRESH, clock))  # row1 guard g_false skipped, row2 g_true wins -> ENDING
        self.assertEqual(fsm.state, S.ENDING)
        self.assertNotIn("a_touch", policy.ran)  # row1's action never ran

    def test_no_guard_combo_matched_logs_debug_noop(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING)
        res = fsm.submit(ev(E.PLUG_OUTAGE, clock))  # only row has g_false
        self.assertFalse(res.matched)
        self.assertEqual(fsm.state, S.RUNNING)
        self.assertTrue(any("no guard combo matched" in m for m in log.at("DEBUG")))

    def test_unlisted_pair_logs_debug_noop(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.OFF)
        res = fsm.submit(ev(E.DOOR_OPENED, clock))  # OFF has no DOOR_OPENED row
        self.assertFalse(res.matched)
        self.assertEqual(fsm.state, S.OFF)
        self.assertTrue(any("no row in table" in m for m in log.at("DEBUG")))

    def test_explicit_noop_row_logs_debug_and_does_not_publish(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.FINISHED)
        before = len(pub.calls)
        res = fsm.submit(ev(E.POWER_HIGH, clock, watts=500))  # FINISHED/POWER_HIGH -> Row() no-op
        self.assertTrue(res.matched)
        self.assertEqual(fsm.state, S.FINISHED)
        self.assertEqual(fsm.published, "Unemptied")
        self.assertEqual(len(pub.calls), before)  # no publish for an explicit no-op
        self.assertTrue(any("explicit no-op row" in m for m in log.at("DEBUG")))

    def test_same_state_action_runs_without_transition(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING)
        fsm.submit(ev(E.POWER_HIGH, clock, watts=500))  # RUNNING/POWER_HIGH -> a_touch, stay RUNNING
        self.assertEqual(fsm.state, S.RUNNING)
        self.assertIn("a_touch", policy.ran)

    def test_table_validation_rejects_unknown_names(self):
        with self.assertRaises(ValueError):
            TransitionTable({S.OFF: {E.POWER_HIGH: [Row(action="does_not_exist")]}}, [], [])
        with self.assertRaises(ValueError):
            TransitionTable({S.OFF: {E.POWER_HIGH: [Row(guards=("nope",))]}}, [], [])


# --- policy.before_guards: the optional, generic pre-guard hook (dryer_policy.py's keep-fresh
# short-circuit ordering fix is the motivating case, but the mechanism itself is policy-agnostic -
# the engine never references keep_fresh_detected or anything dryer-specific) ---


class _BeforeGuardsPolicy:
    """Deliberately minimal: a guard that only passes once before_guards has run, proving the
    hook fires - and mutates policy state - strictly before _match() evaluates any guard."""

    def __init__(self):
        self.armed = False
        self.calls = []
        self.guards = {"g_armed": lambda ctx: self.armed}
        self.actions = {}
        self.table = {S.RUNNING: {E.POWER_HIGH: [Row(guards=("g_armed",), target=S.OFF)]}}

    def before_guards(self, ctx):
        self.calls.append(ctx.evidence.type)
        if ctx.evidence.type == E.POWER_HIGH:
            self.armed = True


class BeforeGuardsHook(unittest.TestCase):
    def test_hook_fires_before_guard_evaluation(self):
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING, policy=_BeforeGuardsPolicy())
        self.assertFalse(policy.armed)
        res = fsm.submit(ev(E.POWER_HIGH, clock, watts=10))
        self.assertTrue(res.matched)
        self.assertEqual(fsm.state, S.OFF)  # only reachable if g_armed saw armed=True already

    def test_hook_fires_even_when_no_row_matches_or_exists(self):
        """Unconditional, regardless of outcome - matches the KEEP_FRESH motivating case, where
        the flag must set even on a declined (unmatched-guard) submission."""
        policy = _BeforeGuardsPolicy()
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.OFF, policy=policy)  # OFF has no POWER_HIGH row here
        fsm.submit(ev(E.POWER_HIGH, clock, watts=10))
        self.assertIn(E.POWER_HIGH, policy.calls)

    def test_hook_is_optional_and_backward_compatible(self):
        # TestPolicy never defines before_guards - exercised by every other test in this file;
        # this just pins that the engine tolerates its absence rather than erroring.
        fsm, policy, clock, sched, sink, log, pub = make_engine(state=S.RUNNING)
        self.assertIsNone(fsm._before_guards)
        fsm.submit(ev(E.POWER_HIGH, clock, watts=500))  # must not raise


if __name__ == "__main__":
    unittest.main()
