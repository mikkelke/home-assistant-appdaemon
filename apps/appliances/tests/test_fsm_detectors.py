# tests/test_fsm_detectors.py - evidence-emission-timing tests for appliance_detectors.py.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# Each detector is exercised against a local FakeCtx (this file's own stand-in for
# appliance_fsm.Ctx) plus the same FakeClock/FakeScheduler shape used in test_fsm_engine.py,
# defined locally here too per the assignment (no shared tests/replay.py import). A detector's
# only externally observable behavior is WHAT it emits and WHEN - so every test asserts on
# emitted Evidence (type/payload) and/or scheduled timers, never on private state.

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appliance_fsm import Evidence, EvidenceType, State  # noqa: E402
from appliance_detectors import (  # noqa: E402
    BootRestoreProvider,
    DoorEdgeDetector,
    PlugOutageDetector,
    PowerEndDetector,
    PowerStartDetector,
    WatchdogTimer,
)

E = EvidenceType
S = State
T0 = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


# --- local injected seams (same shape as test_fsm_engine.py's, duplicated per-file on purpose) ---


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


class FakeCtx:
    """Stand-in for appliance_fsm.Ctx exposing exactly the surface Detector subclasses use:
    now/schedule/cancel/get_state/subscribe/emit/config/state/push_mobile. `states` is a plain
    dict a test pre-seeds (e.g. the power sensor's current reading) for get_state() to answer
    at confirm-timer fire time."""

    def __init__(self, clock, scheduler, config=None, state=S.OFF):
        self._clock = clock
        self._scheduler = scheduler
        self.config = dict(config or {})
        self.state = state
        self.states = {}
        self.emitted = []
        self.pushed = []
        self.subscriptions = []

    def now(self):
        return self._clock.now()

    def schedule(self, delay_s, cb):
        return self._scheduler.run_in(delay_s, cb)

    def cancel(self, handle):
        if handle is not None:
            self._scheduler.cancel(handle)

    def get_state(self, entity, **kw):
        return self.states.get(entity)

    def subscribe(self, entity, handler):
        self.subscriptions.append((entity, handler))

    def emit(self, evidence):
        self.emitted.append(evidence)
        return evidence

    def push_mobile(self, message, **kw):
        self.pushed.append(message)

    def types(self):
        return [e.type for e in self.emitted]


DRYER_CFG = {
    "power_sensor": "sensor.dryer_plug_power",
    "door_sensor": "binary_sensor.dryer_door_contact",
    "start_w": 10,
    "stop_w": 5,
    "run_for": 90,
    "stop_for": 120,
    "door_close_fast_confirm_s": 15,
    "door_close_fast_start_window_s": 300,
}


# --- PowerStartDetector ---


class PowerStartDetectorTests(unittest.TestCase):
    def _make(self, state=S.OFF):
        clock = FakeClock()
        sched = FakeScheduler(clock)
        ctx = FakeCtx(clock, sched, config=DRYER_CFG, state=state)
        det = PowerStartDetector()
        det.wire(ctx)
        return det, ctx, clock, sched

    def test_wire_subscribes_power_and_door(self):
        det, ctx, clock, sched = self._make()
        subscribed = [e for e, _h in ctx.subscriptions]
        self.assertIn(DRYER_CFG["power_sensor"], subscribed)
        self.assertIn(DRYER_CFG["door_sensor"], subscribed)

    def test_high_sample_emits_power_high_and_arms_normal_confirm(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "0", "620", ctx)
        self.assertEqual(ctx.types(), [E.POWER_HIGH])
        self.assertEqual(ctx.emitted[0].payload["watts"], 620.0)
        self.assertEqual(len(sched.pending()), 1)
        self.assertEqual(sched.pending()[0][0], clock.now() + timedelta(seconds=DRYER_CFG["run_for"]))

    def test_confirm_fires_past_run_for_with_power_still_high(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "0", "620", ctx)
        ctx.states[DRYER_CFG["power_sensor"]] = "620"
        sched.advance(DRYER_CFG["run_for"])
        self.assertEqual(ctx.types(), [E.POWER_HIGH, E.POWER_START_CONFIRMED])
        self.assertEqual(ctx.emitted[-1].payload["watts"], 620.0)

    def test_drop_below_start_w_before_confirm_cancels_pending_start(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "0", "620", ctx)
        self.assertEqual(len(sched.pending()), 1)
        det.on_state(DRYER_CFG["power_sensor"], "620", "0", ctx)
        self.assertEqual(len(sched.pending()), 0)
        sched.advance(DRYER_CFG["run_for"] + 1)
        self.assertEqual(ctx.types(), [E.POWER_HIGH])  # no POWER_START_CONFIRMED ever emitted

    def test_confirm_fires_but_live_power_now_stale_below_start_w_no_emit(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "0", "620", ctx)
        ctx.states[DRYER_CFG["power_sensor"]] = "0"  # stale/noise by confirm time
        sched.advance(DRYER_CFG["run_for"])
        self.assertEqual(ctx.types(), [E.POWER_HIGH])  # no confirm emitted

    def test_confirm_fires_during_plug_dropout_no_emit(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "0", "620", ctx)
        ctx.states[DRYER_CFG["power_sensor"]] = "unavailable"
        sched.advance(DRYER_CFG["run_for"])
        self.assertEqual(ctx.types(), [E.POWER_HIGH])

    def test_door_close_in_off_arms_fast_start_window_then_fast_confirm(self):
        det, ctx, clock, sched = self._make(state=S.OFF)
        det.on_door(DRYER_CFG["door_sensor"], "on", "off", ctx)  # door closes while Off
        det.on_state(DRYER_CFG["power_sensor"], "0", "620", ctx)
        self.assertEqual(len(sched.pending()), 1)
        self.assertEqual(
            sched.pending()[0][0], clock.now() + timedelta(seconds=DRYER_CFG["door_close_fast_confirm_s"])
        )

    def test_door_open_clears_fast_start_arm(self):
        det, ctx, clock, sched = self._make(state=S.OFF)
        det.on_door(DRYER_CFG["door_sensor"], "on", "off", ctx)  # arm fast window
        det.on_door(DRYER_CFG["door_sensor"], "off", "on", ctx)  # then door opens again - clears it
        det.on_state(DRYER_CFG["power_sensor"], "0", "620", ctx)
        self.assertEqual(sched.pending()[0][0], clock.now() + timedelta(seconds=DRYER_CFG["run_for"]))

    def test_fast_start_window_expires(self):
        det, ctx, clock, sched = self._make(state=S.OFF)
        det.on_door(DRYER_CFG["door_sensor"], "on", "off", ctx)
        clock.advance(DRYER_CFG["door_close_fast_start_window_s"] + 1)
        det.on_state(DRYER_CFG["power_sensor"], "0", "620", ctx)
        self.assertEqual(sched.pending()[0][0], clock.now() + timedelta(seconds=DRYER_CFG["run_for"]))

    def test_starting_state_reaming_after_confirm_cleared_still_arms(self):
        # ctx.state is already STARTING (the engine moved it synchronously on POWER_HIGH); a later
        # high sample after a previous confirm slot cleared should still be able to arm again.
        det, ctx, clock, sched = self._make(state=S.STARTING)
        det.on_state(DRYER_CFG["power_sensor"], "0", "620", ctx)
        self.assertEqual(len(sched.pending()), 1)


# --- PowerEndDetector ---


class PowerEndDetectorTests(unittest.TestCase):
    def _make(self, state=S.RUNNING):
        clock = FakeClock()
        sched = FakeScheduler(clock)
        ctx = FakeCtx(clock, sched, config=DRYER_CFG, state=state)
        det = PowerEndDetector()
        det.wire(ctx)
        return det, ctx, clock, sched

    def test_low_sample_while_running_emits_power_low_and_arms_stop_for(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "600", "2", ctx)
        self.assertEqual(ctx.types(), [E.POWER_LOW])
        self.assertEqual(len(sched.pending()), 1)
        self.assertEqual(sched.pending()[0][0], clock.now() + timedelta(seconds=DRYER_CFG["stop_for"]))

    def test_low_sample_while_ending_also_arms(self):
        det, ctx, clock, sched = self._make(state=S.ENDING)
        det.on_state(DRYER_CFG["power_sensor"], "600", "2", ctx)
        self.assertEqual(ctx.types(), [E.POWER_LOW])

    def test_recovered_spike_cancels_and_emits_recovered(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "600", "2", ctx)
        det.on_state(DRYER_CFG["power_sensor"], "2", "400", ctx)
        self.assertEqual(ctx.types(), [E.POWER_LOW, E.POWER_RECOVERED])
        self.assertEqual(len(sched.pending()), 0)

    def test_confirm_fires_past_stop_for_still_low_emits_end_confirmed(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "600", "2", ctx)
        ctx.states[DRYER_CFG["power_sensor"]] = "2"
        sched.advance(DRYER_CFG["stop_for"])
        self.assertEqual(ctx.types(), [E.POWER_LOW, E.POWER_END_CONFIRMED])

    def test_confirm_fires_but_power_recovered_meanwhile_no_emit(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "600", "2", ctx)
        ctx.states[DRYER_CFG["power_sensor"]] = "400"  # recovered without an on_state callback
        sched.advance(DRYER_CFG["stop_for"])
        self.assertEqual(ctx.types(), [E.POWER_LOW])  # no END_CONFIRMED

    def test_confirm_fires_during_plug_dropout_no_emit(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "600", "2", ctx)
        ctx.states[DRYER_CFG["power_sensor"]] = "unknown"
        sched.advance(DRYER_CFG["stop_for"])
        self.assertEqual(ctx.types(), [E.POWER_LOW])

    def test_outside_running_ending_drops_stale_confirm_no_emit(self):
        det, ctx, clock, sched = self._make(state=S.RUNNING)
        det.on_state(DRYER_CFG["power_sensor"], "600", "2", ctx)
        self.assertEqual(len(sched.pending()), 1)
        ctx.state = S.PAUSED
        det.on_state(DRYER_CFG["power_sensor"], "2", "3", ctx)  # any sample while Paused
        self.assertEqual(len(sched.pending()), 0)


# --- DoorEdgeDetector ---


class DoorEdgeDetectorTests(unittest.TestCase):
    def _make(self, inverted=False):
        clock = FakeClock()
        sched = FakeScheduler(clock)
        cfg = dict(DRYER_CFG)
        if inverted:
            cfg["door_sensor_inverted"] = True
        ctx = FakeCtx(clock, sched, config=cfg, state=S.RUNNING)
        det = DoorEdgeDetector()
        det.wire(ctx)
        return det, ctx, clock, sched

    def test_standard_polarity_open_and_close_with_power_snapshot(self):
        det, ctx, clock, sched = self._make()
        ctx.states[DRYER_CFG["power_sensor"]] = "480"
        det.on_state(DRYER_CFG["door_sensor"], "off", "on", ctx)
        self.assertEqual(ctx.types(), [E.DOOR_OPENED])
        self.assertEqual(ctx.emitted[0].payload["power_w"], 480.0)

        ctx.states[DRYER_CFG["power_sensor"]] = "3"
        det.on_state(DRYER_CFG["door_sensor"], "on", "off", ctx)
        self.assertEqual(ctx.types(), [E.DOOR_OPENED, E.DOOR_CLOSED])
        self.assertEqual(ctx.emitted[1].payload["power_w"], 3.0)

    def test_inverted_polarity_flips_open_close(self):
        det, ctx, clock, sched = self._make(inverted=True)
        det.on_state(DRYER_CFG["door_sensor"], "on", "off", ctx)  # off means OPEN when inverted
        self.assertEqual(ctx.types(), [E.DOOR_OPENED])
        det.on_state(DRYER_CFG["door_sensor"], "off", "on", ctx)  # on means CLOSED when inverted
        self.assertEqual(ctx.types(), [E.DOOR_OPENED, E.DOOR_CLOSED])

    def test_unknown_door_value_emits_nothing(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["door_sensor"], "off", "unavailable", ctx)
        self.assertEqual(ctx.emitted, [])

    def test_missing_power_reading_defaults_snapshot_to_zero(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["door_sensor"], "off", "on", ctx)  # power sensor never seeded
        self.assertEqual(ctx.emitted[0].payload["power_w"], 0.0)


# --- WatchdogTimer ---


class WatchdogTimerTests(unittest.TestCase):
    def _ctx(self):
        clock = FakeClock()
        sched = FakeScheduler(clock)
        ctx = FakeCtx(clock, sched, config={}, state=S.RUNNING)
        return ctx, clock, sched

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            WatchdogTimer("bogus", 60)

    def test_each_kind_fires_its_own_evidence_type(self):
        for kind, expect in (
            ("running", E.WD_RUNNING),
            ("pause", E.WD_PAUSE),
            ("unemptied", E.WD_UNEMPTIED),
            ("emptied", E.WD_EMPTIED),
        ):
            ctx, clock, sched = self._ctx()
            wd = WatchdogTimer(kind, 300)
            wd.arm(ctx)
            self.assertTrue(wd.armed)
            sched.advance(300)
            self.assertEqual(ctx.types(), [expect])
            self.assertFalse(wd.armed)

    def test_cancel_prevents_firing(self):
        ctx, clock, sched = self._ctx()
        wd = WatchdogTimer("running", 300)
        wd.arm(ctx)
        wd.cancel(ctx)
        self.assertFalse(wd.armed)
        sched.advance(400)
        self.assertEqual(ctx.emitted, [])

    def test_rearm_cancels_previous_pending(self):
        ctx, clock, sched = self._ctx()
        wd = WatchdogTimer("running", 300)
        wd.arm(ctx)
        first_handle_count = len(sched.pending())
        wd.arm(ctx, duration_s=500)  # re-arm before the first fires
        self.assertEqual(len(sched.pending()), first_handle_count)  # old cancelled, one new
        sched.advance(300)
        self.assertEqual(ctx.emitted, [])  # the 300s-out timer never fires - it was cancelled
        sched.advance(200)
        self.assertEqual(ctx.types(), [E.WD_RUNNING])

    def test_arm_remaining_schedules_the_leftover_time(self):
        ctx, clock, sched = self._ctx()
        wd = WatchdogTimer("unemptied", 3600)
        since = clock.now() - timedelta(seconds=600)  # 600s already elapsed at restore time
        wd.arm_remaining(ctx, since, floor_s=60)
        self.assertEqual(sched.pending()[0][0], clock.now() + timedelta(seconds=3000))

    def test_arm_remaining_floors_at_60s_when_period_already_elapsed(self):
        ctx, clock, sched = self._ctx()
        wd = WatchdogTimer("emptied", 300)
        since = clock.now() - timedelta(seconds=10_000)  # long past the 300s period
        wd.arm_remaining(ctx, since, floor_s=60)
        self.assertEqual(sched.pending()[0][0], clock.now() + timedelta(seconds=60))
        sched.advance(60)
        self.assertEqual(ctx.types(), [E.WD_EMPTIED])

    def test_arm_remaining_floor_is_never_zero_or_negative(self):
        ctx, clock, sched = self._ctx()
        wd = WatchdogTimer("pause", 60)
        since = clock.now() - timedelta(seconds=1_000_000)  # absurdly stale restore
        wd.arm_remaining(ctx, since, floor_s=60)
        delay = (sched.pending()[0][0] - clock.now()).total_seconds()
        self.assertGreaterEqual(delay, 60)


# --- PlugOutageDetector ---


class PlugOutageDetectorTests(unittest.TestCase):
    def _make(self):
        clock = FakeClock()
        sched = FakeScheduler(clock)
        cfg = dict(DRYER_CFG, power_unavailable_grace_s=180, appliance_label="dryer")
        ctx = FakeCtx(clock, sched, config=cfg, state=S.RUNNING)
        det = PlugOutageDetector()
        det.wire(ctx)
        return det, ctx, clock, sched

    def test_short_dropout_recovered_before_grace_no_outage(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "600", "unavailable", ctx)
        sched.advance(90)  # inside the 180s grace
        det.on_state(DRYER_CFG["power_sensor"], "unavailable", "600", ctx)
        sched.advance(200)
        self.assertEqual(ctx.emitted, [])
        self.assertEqual(ctx.pushed, [])

    def test_sustained_dropout_past_grace_emits_outage_and_pages_once(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "600", "unavailable", ctx)
        ctx.states[DRYER_CFG["power_sensor"]] = "unavailable"
        sched.advance(180)
        self.assertEqual(ctx.types(), [E.PLUG_OUTAGE])
        self.assertEqual(len(ctx.pushed), 1)
        self.assertIn("dryer", ctx.pushed[0])
        self.assertIn("180s", ctx.pushed[0])

    def test_repeated_dropout_samples_during_grace_do_not_double_arm(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "600", "unavailable", ctx)
        det.on_state(DRYER_CFG["power_sensor"], "unavailable", "unknown", ctx)
        det.on_state(DRYER_CFG["power_sensor"], "unknown", None, ctx)
        self.assertEqual(len(sched.pending()), 1)

    def test_recovery_after_outage_pages_recovery_message(self):
        det, ctx, clock, sched = self._make()
        det.on_state(DRYER_CFG["power_sensor"], "600", "unavailable", ctx)
        ctx.states[DRYER_CFG["power_sensor"]] = "unavailable"
        sched.advance(180)
        self.assertEqual(len(ctx.pushed), 1)
        det.on_state(DRYER_CFG["power_sensor"], "unavailable", "600", ctx)
        self.assertEqual(len(ctx.pushed), 2)
        self.assertIn("resumed", ctx.pushed[1])


# --- BootRestoreProvider ---


class BootRestoreProviderTests(unittest.TestCase):
    def _ctx(self):
        clock = FakeClock()
        return FakeCtx(clock, FakeScheduler(clock), config={}, state=S.OFF)

    def test_none_snapshot_restores_nothing(self):
        provider = BootRestoreProvider(None)
        self.assertIsNone(provider.restore(self._ctx()))

    def test_empty_dict_snapshot_restores_nothing(self):
        provider = BootRestoreProvider({})
        self.assertIsNone(provider.restore(self._ctx()))

    def test_snapshot_produces_exactly_one_boot_restore_evidence(self):
        snap = {"state": "Running", "cycle_id": "cid-1", "start_time": T0}
        provider = BootRestoreProvider(snap, source="store")
        ctx = self._ctx()
        evidence = provider.restore(ctx)
        self.assertIsInstance(evidence, Evidence)
        self.assertEqual(evidence.type, E.BOOT_RESTORE)
        self.assertFalse(evidence.live)
        self.assertEqual(evidence.payload["state"], "Running")
        self.assertEqual(evidence.payload["cycle_id"], "cid-1")
        self.assertEqual(evidence.payload["source"], "store")

    def test_snapshot_source_defaults_when_not_overridden(self):
        provider = BootRestoreProvider({"state": "Off"})
        evidence = provider.restore(self._ctx())
        self.assertEqual(evidence.payload["source"], "unknown")

    def test_set_snapshot_replaces_and_can_override_source(self):
        provider = BootRestoreProvider({"state": "Off"}, source="entity")
        provider.set_snapshot({"state": "Emptied"}, source="helper")
        evidence = provider.restore(self._ctx())
        self.assertEqual(evidence.payload["state"], "Emptied")
        self.assertEqual(evidence.payload["source"], "helper")

    def test_provider_never_reads_ha_only_the_handed_snapshot(self):
        # FakeCtx.get_state would raise if ever called with an unexpected shape; restore() must not
        # touch it at all - only ctx.now() for the evidence timestamp.
        provider = BootRestoreProvider({"state": "Paused"})
        ctx = self._ctx()
        calls = []
        ctx.get_state = lambda *a, **kw: calls.append((a, kw)) or None
        provider.restore(ctx)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
