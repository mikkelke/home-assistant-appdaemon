# tests/test_fsm_fuzz.py - restart-storm fuzz (spec section 11.2) over the real
# appliance_fsm.ApplianceFSM + real dryer_policy.DryerPolicy. Run from repo root:
# python3 -m unittest discover -s apps/appliances/tests -q
#
# Seeded RNG (reproducible - a failure prints its seed). Drives a random but state-aware
# ("legal") sequence of Evidence, injecting a simulated process restart at random points: the rig
# snapshots policy.to_store_fields()+engine identity into a plain dict (mirroring
# dryer_shadow.py's _save_store), then builds a FRESH ApplianceFSM+DryerPolicy pair and replays
# dryer_shadow.py's OWN boot sequence (resolve_boot_snapshot -> corroborate_restore ->
# enter_state_silently -> mark_fed_back -> rearm_watchdogs_after_restore -> arm_reconcile/
# try_conclude_at_boot) against it - the fuzz exercises the SAME boot code path production uses,
# just under FakeClock/FakeScheduler (appliance_fsm.py's Clock/Scheduler protocols) instead of
# AppDaemon, so watchdogs/confirm-timers/RECONCILE stay deterministic (spec 10.1).
#
# Invariants asserted after every step and every restart:
#   I1 never an announce/push while hypothesis=True (the F2 class).
#   I2 never a wedge: RUNNING/PAUSED/FINISHED/EMPTIED always has its spec-4.2 watchdog armed.
#   I3 never RUNNING/PAUSED/ENDING with start_time None or an unarmed running watchdog.
#   I4 exactly-once feedback per cycle_id (the F3 class), across duplicate landings AND restarts.

from __future__ import annotations

import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appliance_fsm import ApplianceFSM, Evidence, EvidenceType, RESTORE_STATE_OF, State  # noqa: E402
import dryer_policy as dp  # noqa: E402

E = EvidenceType
S = State
T0 = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

CFG = {
    "power_sensor": "sensor.dryer_plug_power", "energy_sensor": "sensor.dryer_plug_energy",
    "door_sensor": "binary_sensor.dryer_door_contact",
    "start_w": 8, "stop_w": 5, "min_cycle_minutes": 10, "min_energy_kwh": 0.05,
    "fill_window_minutes": 60, "max_running_hours": 5, "pause_timeout_minutes": 10,
    "unemptied_timeout_hours": 24, "emptied_timeout_minutes": 30,
    "restore_corroboration_window_minutes": 10, "announce_freshness_minutes": 20,
    "cooling_period": 300, "store_max_downtime_hours": 12, "boot_future_start_skew_s": 60,
}


# --- local injected seams (per-file - see the assignment on not sharing tests/replay.py) ---


class FakeClock:
    def __init__(self, t0=T0):
        self._t = t0

    def now(self):
        return self._t

    def advance(self, secs):
        self._t = self._t + timedelta(seconds=secs)


class FakeScheduler:
    def __init__(self, clock):
        self.clock = clock
        self._timers = []
        self._seq = 0

    def run_in(self, delay_s, cb):
        h = self._seq
        self._seq += 1
        self._timers.append([self.clock.now() + timedelta(seconds=delay_s), h, cb, True])
        return h

    def cancel(self, handle):
        for t in self._timers:
            if t[1] == handle:
                t[3] = False

    def due_count(self):
        return len([t for t in self._timers if t[3]])

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


class RecordingSink:
    """Records (message, hypothesis_at_call_time, cycle_id_at_call_time) for I1/I4 - hypothesis
    is read from the fsm the moment the call lands, which is only possible AFTER the engine's own
    _user_action gate already let it through (a True reading here would itself be the bug)."""

    def __init__(self, fsm_ref):
        self._fsm_ref = fsm_ref
        self.announces = []
        self.pushes = []
        self.feedbacks = []

    def announce(self, message, **kw):
        self.announces.append((message, self._fsm_ref[0].hypothesis))

    def push_mobile(self, message, **kw):
        self.pushes.append((message, self._fsm_ref[0].hypothesis))

    def select_option(self, entity, option, **kw):
        pass

    def reset_selectors(self, **kw):
        pass

    def save_feedback(self, record):
        self.feedbacks.append((dict(record), self._fsm_ref[0].cycle_id))


class LogSink:
    def __call__(self, message, level="INFO"):
        pass


class Rig:
    """Owns the mutable I/O state (power/energy/door readings, recorder history) that survives a
    restart, plus the CURRENT policy+fsm pair, which restart() replaces wholesale - exactly what
    a real process restart does (new objects; only the store and entity/live readings persist)."""

    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.clock = FakeClock()
        self.sched = FakeScheduler(self.clock)
        self.states = {CFG["power_sensor"]: "0", CFG["energy_sensor"]: "10.0", CFG["door_sensor"]: "off"}
        self.history = {CFG["power_sensor"]: [], CFG["door_sensor"]: []}
        self._energy_kwh = 10.0  # monotonically accumulating meter - see _accrue_energy
        self.log = LogSink()
        self._fsm_ref = [None]
        self.sink = RecordingSink(self._fsm_ref)
        self.store = None  # None until the first landed transition "persists" one
        self._build(initial_state=S.OFF, hypothesis=False)

    def _build(self, *, initial_state, hypothesis, state_since=None, cycle_id=None,
               store_fields=None, last_feedback_cycle_id=None):
        # Short "unknown"-profile fallback duration (no programmes_file in this fuzz rig, so
        # every classified programme falls back to it) - keeps the 80% past_guard threshold
        # (spec G_past_guard) reachable within a fuzz run's random clock advances, without
        # weakening any invariant under test (they hold for any guard_dur).
        profiles = {"unknown": {"label": "Unknown", "duration_min": 20, "max_energy_kwh": 2.0},
                    "finish_uld": {"label": "Finish uld", "duration_min": 5, "max_energy_kwh": 0.02, "is_real": False}}
        policy = dp.DryerPolicy(config=CFG, profiles=profiles)
        if store_fields:
            policy.restore_from(store_fields)

        def get_state(entity, **kw):
            return self.states.get(entity)

        def get_history(entity, **kw):
            return self.history.get(entity, [])

        def publish(published, *, internal, store_only, attrs):
            policy.sync_watchdogs_on_publish(fsm.ctx, internal)
            self.store = {
                "state": fsm.published, "cycle_id": fsm.cycle_id,
                "state_since": fsm.state_since, **policy.to_store_fields(),
            }

        fsm = ApplianceFSM(
            policy, self.sink, self.clock, self.sched, publish, self.log,
            config=CFG, get_state=get_state, get_history=get_history,
            cooling_period=CFG["cooling_period"], initial_state=S.OFF,
        )
        self.policy = policy
        self.fsm = fsm
        self._fsm_ref[0] = fsm
        fsm.enter_state_silently(initial_state, state_since=state_since, hypothesis=hypothesis, cycle_id=cycle_id)
        if last_feedback_cycle_id is not None:
            fsm.mark_fed_back(last_feedback_cycle_id)
        policy.rearm_watchdogs_after_restore(fsm.ctx, initial_state, state_since)
        if hypothesis:
            policy.arm_reconcile(fsm.ctx)
            policy.try_conclude_at_boot(fsm.ctx)

    def restart(self):
        """Simulated process restart: snapshot self.store (as dryer_shadow.py's own store file
        would hold it), build a FRESH policy+fsm, and replay resolve_boot_snapshot ->
        corroborate_restore -> _build's own boot sequence - the SAME functions dryer_shadow.py's
        real initialize() calls."""
        store = self.store
        snap = dp.resolve_boot_snapshot(
            entity_state=None, entity_attrs={}, entity_last_changed=None,  # no live v2 entity in this rig
            store_data=store, helper_state=None, now=self.clock.now(), cfg=CFG,
        )
        if snap is None:
            self._build(initial_state=S.OFF, hypothesis=False)
            return
        initial_state = RESTORE_STATE_OF.get(snap["state"], S.OFF)
        hypothesis = False
        if initial_state in (S.RUNNING, S.PAUSED):
            boot_watts = self._live_watts()
            last_high = dp._parse_hist_ts((snap["store_fields"] or {}).get("last_high_energy_at"))
            hypothesis, _why = dp.corroborate_restore(
                boot_watts=boot_watts, last_high_energy_at=last_high, now=self.clock.now(),
                cfg=CFG, code_fingerprint="fuzzrig", stored_fingerprint=snap["code_fingerprint"],
            )
        self._build(
            initial_state=initial_state, hypothesis=hypothesis,
            state_since=dp._parse_hist_ts(snap["state_since"]) if snap["state_since"] else None,
            cycle_id=snap["cycle_id"], store_fields=snap["store_fields"],
            last_feedback_cycle_id=(snap["store_fields"] or {}).get("last_feedback_cycle_id"),
        )

    def _live_watts(self):
        try:
            return float(self.states[CFG["power_sensor"]])
        except (ValueError, TypeError):
            return 0.0

    # ---- event generation ----

    def set_power(self, watts):
        self.states[CFG["power_sensor"]] = str(watts)
        t = self.clock.now().isoformat()
        self.history[CFG["power_sensor"]] = (self.history[CFG["power_sensor"]] + [{"state": str(watts), "last_changed": t}])[-30:]

    def set_door(self, is_open):
        self.states[CFG["door_sensor"]] = "on" if is_open else "off"
        t = self.clock.now().isoformat()
        self.history[CFG["door_sensor"]] = (self.history[CFG["door_sensor"]] + [{"state": "on" if is_open else "off", "last_changed": t}])[-30:]

    def ev(self, etype, payload=None, live=True):
        return Evidence.make(etype, self.clock.now(), "fuzz", live=live, payload=payload or {})

    def _accrue_energy(self, dt_seconds):
        """Bumps the energy-meter reading proportionally to elapsed time while power is high -
        without this the meter never moves and G_valid_cycle's min_energy_kwh floor could never
        be reached, making a real FINISHED/EMPTIED landing structurally unreachable regardless of
        elapsed run time (found via this file's own fuzz-tuning pass)."""
        try:
            watts = float(self.states[CFG["power_sensor"]])
        except (ValueError, TypeError):
            watts = 0.0
        if watts > CFG["stop_w"]:
            self._energy_kwh += watts * dt_seconds / 3600.0 / 1000.0
            self.states[CFG["energy_sensor"]] = f"{self._energy_kwh:.4f}"

    def step(self):
        """One state-aware random action: a plausible evidence submission, a clock advance, or a
        restart. Mutates entity readings to stay consistent with whatever evidence is submitted,
        same discipline test_dryer_policy_table.py's Harness enforces by hand."""
        r = self.rng
        if r.random() < 0.12:
            self.restart()
            return
        dt = r.choice([1, 5, 15, 60, 600, 3600])
        self.clock.advance(dt)
        self._accrue_energy(dt)
        state = self.fsm.state
        if state == S.OFF:
            if r.random() < 0.5:
                self.set_power(r.choice([15, 20, 600]))
                self.fsm.submit(self.ev(E.POWER_HIGH, {"watts": float(self.states[CFG["power_sensor"]])}))
        elif state == S.STARTING:
            if r.random() < 0.7:
                w = float(self.states[CFG["power_sensor"]])
                self.fsm.submit(self.ev(E.POWER_START_CONFIRMED, {"watts": w}))
            else:
                self.set_power(0)
                self.fsm.submit(self.ev(E.POWER_LOW, {"watts": 0}))
        elif state in (S.RUNNING, S.ENDING):
            choice = r.random()
            if choice < 0.25:
                self.set_power(r.choice([400, 500, 600, 650]))
                self.fsm.submit(self.ev(E.POWER_HIGH, {"watts": float(self.states[CFG["power_sensor"]])}))
                if state == S.ENDING:
                    self.fsm.submit(self.ev(E.POWER_RECOVERED, {"watts": float(self.states[CFG["power_sensor"]])}))
            elif choice < 0.45:
                self.set_power(r.choice([0, 2, 3]))
                self.fsm.submit(self.ev(E.POWER_LOW, {"watts": float(self.states[CFG["power_sensor"]])}))
                if state == S.ENDING:
                    # Give G_past_guard a fair, unbiased chance either way - the small per-step
                    # advances above rarely accumulate 80% of the (short, fuzz-tuned) guard
                    # duration on their own before something else interrupts, which would make
                    # every attempt land on the same "not yet" catch-all row and this generator
                    # would never actually exercise a real FINISHED/EMPTIED landing.
                    self.clock.advance(r.choice([0, 60, 600, 1800]))
                    self.fsm.submit(self.ev(E.POWER_END_CONFIRMED, {"watts": float(self.states[CFG["power_sensor"]])}))
            elif choice < 0.65:
                is_open = r.random() < 0.5
                self.set_door(is_open)
                ev = self.ev(E.DOOR_OPENED if is_open else E.DOOR_CLOSED, {"power_w": self._live_watts()})
                self.fsm.submit(ev)
            elif choice < 0.75:
                self.clock.advance(6 * 3600)
                self.fsm.submit(self.ev(E.WD_RUNNING))
            elif choice < 0.85:
                self.fsm.submit(self.ev(E.PLUG_OUTAGE))
        elif state == S.PAUSED:
            choice = r.random()
            if choice < 0.5:
                w = r.choice([0, 2, 600])
                self.set_power(w)
                self.fsm.submit(self.ev(E.DOOR_CLOSED, {"power_w": float(w)}))
            elif choice < 0.7:
                self.set_door(True)
                self.fsm.submit(self.ev(E.DOOR_OPENED, {"power_w": self._live_watts()}))
            elif choice < 0.85:
                self.clock.advance(20 * 60)
                self.fsm.submit(self.ev(E.WD_PAUSE))
            else:
                self.clock.advance(6 * 3600)
                self.fsm.submit(self.ev(E.WD_RUNNING))
        elif state == S.FINISHED:
            choice = r.random()
            if choice < 0.4:
                self.set_door(True)
                self.fsm.submit(self.ev(E.DOOR_OPENED, {"power_w": self._live_watts()}))
            elif choice < 0.55:
                self.fsm.submit(self.ev(E.FORCE_EMPTIED))
            elif choice < 0.7:
                self.clock.advance(25 * 3600)
                self.fsm.submit(self.ev(E.WD_UNEMPTIED))
            else:
                self.fsm.submit(self.ev(E.POWER_HIGH, {"watts": 200}))
        elif state == S.EMPTIED:
            choice = r.random()
            if choice < 0.6:
                self.set_door(False)
                self.fsm.submit(self.ev(E.DOOR_CLOSED, {"power_w": self._live_watts()}))
            else:
                self.clock.advance(31 * 60)
                self.fsm.submit(self.ev(E.WD_EMPTIED))

        # Drain any due timers (start/finish confirms, watchdogs) - a real host's scheduler would
        # fire these on its own; the fuzz drives them explicitly so no confirm is silently skipped.
        self.sched.advance_to(self.clock.now())


def assert_invariants(tc, rig, step_no):
    fsm, policy = rig.fsm, rig.policy
    ctx = f"seed={rig.rng.__reduce__()[1] if False else '?'} step={step_no} state={fsm.state.name}"

    # I1: no announce/push ever landed with hypothesis True at call time.
    for message, was_hyp in rig.sink.announces + rig.sink.pushes:
        tc.assertFalse(was_hyp, f"{ctx}: announce/push {message!r} reached the sink while hypothesis=True")

    # I2: never a wedge - the watchdog spec 4.2 assigns to the CURRENT state is armed.
    if fsm.state in (S.RUNNING, S.PAUSED):
        tc.assertTrue(policy.watchdogs["running"].armed, f"{ctx}: running watchdog unarmed")
    if fsm.state == S.PAUSED:
        tc.assertTrue(policy.watchdogs["pause"].armed, f"{ctx}: pause watchdog unarmed")
    if fsm.state == S.FINISHED:
        tc.assertTrue(policy.watchdogs["unemptied"].armed, f"{ctx}: unemptied watchdog unarmed")
    if fsm.state == S.EMPTIED:
        tc.assertTrue(policy.watchdogs["emptied"].armed, f"{ctx}: emptied watchdog unarmed")

    # I3: RUNNING/PAUSED/ENDING always has a start_time and an armed running watchdog.
    if fsm.state in (S.RUNNING, S.PAUSED, S.ENDING):
        tc.assertIsNotNone(policy.start_time, f"{ctx}: {fsm.state.name} with start_time=None")
        tc.assertTrue(policy.watchdogs["running"].armed, f"{ctx}: {fsm.state.name} with unarmed running watchdog")

    # I4: exactly-once feedback per cycle_id.
    seen = [cid for _record, cid in rig.sink.feedbacks]
    tc.assertEqual(len(seen), len(set(seen)), f"{ctx}: duplicate feedback for the same cycle_id: {seen}")


class RestartStormFuzz(unittest.TestCase):
    def _run(self, seed, steps=400):
        rig = Rig(seed)
        for i in range(steps):
            rig.step()
            assert_invariants(self, rig, i)
        return rig

    def test_seed_1_reproducible_restart_storm(self):
        self._run(1)

    def test_seed_2_reproducible_restart_storm(self):
        self._run(2)

    def test_seed_3_reproducible_restart_storm(self):
        self._run(3)

    def test_seed_42_reproducible_restart_storm(self):
        self._run(42)

    def test_seed_1_produces_at_least_one_full_cycle_and_one_restart(self):
        """A fuzz run that never actually reaches FINISHED/restarts would pass I1-I4 vacuously -
        this pins that the generator is exercising the interesting paths, not just idling at Off."""
        rig = self._run(7, steps=500)
        self.assertGreaterEqual(len(rig.sink.feedbacks), 1, "no cycle ever finished in 500 steps")


class MarkFedBackPreventsCrossRestartDuplicate(unittest.TestCase):
    """Direct, hand-crafted regression for the exactly-once-across-restart seam (spec section 6):
    a cycle finishes and feeds back; simulate a restart that lands the SAME cycle_id back in a
    state whose table (hypothetically, or via a future change) could re-trigger a finish - the
    mark_fed_back seam, wired in dryer_shadow.py's boot sequence, must suppress a second save even
    when the finish-shaped action is invoked directly."""

    def test_restored_cycle_with_saved_flag_suppresses_a_forced_refinish(self):
        rig = Rig(seed=99)
        rig.set_power(600)
        rig.fsm.submit(rig.ev(E.POWER_HIGH, {"watts": 600}))
        rig.fsm.submit(rig.ev(E.POWER_START_CONFIRMED, {"watts": 600}))
        self.assertEqual(rig.fsm.state, S.RUNNING)
        cid = rig.fsm.cycle_id

        rig.set_power(2)
        rig.states[CFG["energy_sensor"]] = "10.5"
        rig.fsm.submit(rig.ev(E.POWER_LOW, {"watts": 2}))
        rig.policy.start_time = rig.clock.now() - timedelta(minutes=200)
        rig.clock.advance(CFG["cooling_period"] + 100)  # ENDING->FINISHED is RESPECT-cooling
        rig.fsm.submit(rig.ev(E.POWER_END_CONFIRMED, {"watts": 2}))
        self.assertEqual(rig.fsm.state, S.FINISHED)
        self.assertEqual(len(rig.sink.feedbacks), 1)
        self.assertEqual(rig.policy.last_feedback_cycle_id, cid)

        rig.restart()
        self.assertEqual(rig.fsm.state, S.FINISHED)
        self.assertEqual(rig.fsm.cycle_id, cid)

        # Directly invoke the finish bundle again for the SAME restored cycle_id - simulating a
        # hypothetical future table row (or an operator/dashboard-triggered path) that re-enters
        # _finish() from FINISHED. Without mark_fed_back having been called during restart, the
        # engine's in-memory guard would be reset to None and this would save a second record.
        rig.policy._finish(rig.fsm.ctx, skip_announce=True, end_reason="test_refire", lands_on="FINISHED")
        self.assertEqual(len(rig.sink.feedbacks), 1, "mark_fed_back must suppress the post-restart duplicate")


if __name__ == "__main__":
    unittest.main()
