# tests/test_washer_audit_2026_09.py - regression tests for six flaws confirmed by the
# 2026-09-07 appliance logic audit that were still live in washer_monitor.py at HEAD 46a81a7.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# One test class per flaw. Each was verified to reproduce against the pre-fix code before its
# corresponding washer_monitor.py change was made. Reuses make_full_init_app
# (test_washer_restart_survival.py) and make_finish_app (test_washer_door_aware_finish.py)
# rather than re-deriving their harnesses - this repo's incident history says stubbing the real
# state-machine delegates hides the exact bug under test; only the AppDaemon surface is faked,
# per both suites' own CRITICAL rules (never stub _restore_running_state, _set_state_entity,
# _transition_to_unemptied, etc.).
#
#   FLAW 1  Unemptied -> Running false recovery via the power-PUSH route (_power_changed): the
#           Unemptied branch's high_power_counter had no time dimension, so a single Miele
#           anti-crease tumble (a few seconds of 40-80W) tripped the same 3-sample threshold a
#           genuine resume needs. Fixed by requiring the streak to span a real window, mirroring
#           _unemptied_door_recheck's own 60s-apart hardening (588a879).
#   FLAW 2  _check_energy_finish had mid-function returns that skipped the tick's own reschedule
#           (the anti-crease "stay in Running" branch, and the past-expected "announce
#           immediately" branch when the transition was refused) - the dead energy_check_timer
#           handle then also fooled _confirm_finished into skipping its power fallback ("energy
#           detection is active"). _try_finish_via_standby also reported success even when its
#           own _transition_to_unemptied() call was refused.
#   FLAW 3  An AddLoad pause's resume (_transition_to_running_from_pause) only ever re-armed
#           poll_timer, never the energy tick - unlike every other Running entry point - so a
#           single door-open-during-addload event permanently killed energy-based finish
#           detection for the rest of that cycle.
#   FLAW 4  _pending_end_reason leaked past a refused _transition_to_unemptied (the power gate,
#           and the _should_change_state refusal) and across cycle boundaries
#           (_reset_cycle_tracking, _begin_running_cycle), so a stale
#           tail_to_standby/tail_pattern_break/standby_backstop value could skip the mid-cycle
#           rinse safety gate on an unrelated, later transition.
#   FLAW 5  The learning store double/inconsistent-counted an unconfirmed cycle: save-time
#           applied it once (gated only on valid_for_learning), the push-confirm handler applied
#           it again, and washer_feedback.aggregate_cycles' reload only ever counts it once
#           (requires BOTH valid_for_learning AND user confirmation) - n=1 -> n=2 -> n=1.
#   FLAW 6  Boot restore could arm two concurrent energy-tick loops: the power-history
#           start-gap-correction branch and the unconditional restore call right after it both
#           call _restore_energy_state_from_history(), which armed energy_check_timer without
#           cancelling any handle already running.

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from datetime import timedelta
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

import washer_monitor as wm  # noqa: E402
import cycle_store as cystore  # noqa: E402
import washer_feedback as wfb  # noqa: E402

import test_washer_restart_survival as trs  # noqa: E402
import test_washer_door_aware_finish as dwf  # noqa: E402


# =============================================================================
# FLAW 1 - Unemptied -> Running false recovery via the power-push route
# =============================================================================

class Flaw1UnemptiedFalseRecoveryRequiresSustainedPower(unittest.TestCase):
    """_power_changed's Unemptied branch must require the same sustained-time bar
    _unemptied_door_recheck already got (588a879), not just a raw 3-sample count."""

    def test_brief_anti_crease_tumble_does_not_recover(self):
        app = trs.make_full_init_app(sensor_state=None, helper_state="Unemptied", power_watts=0)
        # A real Unemptied entity always has run_time_minutes on it - without this,
        # _recover_from_false_unemptied aborts for an unrelated reason (no data to recover
        # with) and the test would pass for the wrong reason even on the buggy code.
        app.attrs_store.setdefault(app.state_entity, {})["run_time_minutes"] = 120
        app.notification_sent = True

        # Quick succession (a few seconds total) - a Miele anti-crease tumble shape.
        for watts in (3.1, 22, 47, 55, 41, 19, 4):
            app.now = app.now + timedelta(seconds=1)
            app._power_changed(app.power_sensor, "state", None, str(watts), {})

        self.assertEqual(app.state, "Unemptied")
        self.assertTrue(app.notification_sent)

    def test_sustained_high_power_still_recovers(self):
        """Regression guard: the fix must not disable real false-Unemptied recovery - only
        gate it on a genuine sustained window (samples minutes apart, not seconds)."""
        app = trs.make_full_init_app(sensor_state=None, helper_state="Unemptied", power_watts=0)
        app.attrs_store.setdefault(app.state_entity, {})["run_time_minutes"] = 120

        for watts in (22, 47, 55, 60, 58):
            app.now = app.now + timedelta(seconds=45)
            app._power_changed(app.power_sensor, "state", None, str(watts), {})

        self.assertEqual(app.state, "Running")


# =============================================================================
# FLAW 2 - _check_energy_finish must re-arm on every Running exit
# =============================================================================

class Flaw2EnergyTickRearmsOnEveryRunningExit(unittest.TestCase):
    """The anti-crease 'stay in Running until standby detected' branch is a mid-function
    return, not the tick's bottom-of-function reschedule - it must arm the next tick itself."""

    def test_anti_crease_stay_running_branch_rearms_the_tick(self):
        NOW = trs.NOW
        start = NOW - timedelta(minutes=115)
        app = trs.make_full_init_app(
            sensor_state=None,
            helper_state=None,
            power_watts=0,
            now=NOW,
            extra_args={"confirm_entity": "input_select.washer_confirmed_programme"},
        )
        # User-confirmed programme makes guard_dur deterministic (strygelet = 119 min),
        # independent of the live classifier/guard-bar evolution.
        app.states[app.confirm_entity] = "Strygelet"
        app.programme_confirmed_by_user = True
        app.state = "Running"
        app.states[app.state_entity] = "Running"
        app.start_time = start
        app.notification_sent = False
        app.last_door_closed_trusted = False
        app.energy_start = 0.0
        app.states[app.energy_sensor] = 0.35  # keeps classification off the no-anti-crease "uld" branch

        # ~115/119 min: near (but strictly before) expected end - reaches the anti-crease
        # elif but not the "past expected end, announce immediately" branch.
        points = []
        t = NOW - timedelta(minutes=10)
        watt_cycle = [2.0, 2.0, 45.0, 3.0, 2.0, 45.0, 3.0, 2.0, 45.0, 3.0]
        i = 0
        while t < NOW - timedelta(seconds=5):
            points.append({"state": str(watt_cycle[i % len(watt_cycle)]), "last_changed": trs._iso(t)})
            t += timedelta(seconds=60)
            i += 1
        points.append({"state": "45.0", "last_changed": trs._iso(NOW - timedelta(seconds=5))})
        app._history[app.power_sensor] = points
        app.states[app.power_sensor] = "45.0"

        before = len(app.scheduled)
        app._check_energy_finish({})

        self.assertEqual(app.state, "Running")
        self.assertTrue(app.in_finishing_tail)
        self.assertIsNotNone(app.energy_check_timer)
        new_ticks = [c for c in app.scheduled[before:] if c[0] == app._check_energy_finish]
        self.assertEqual(len(new_ticks), 1, app.scheduled[before:])


# =============================================================================
# FLAW 3 - AddLoad pause resume must re-arm energy-based finish detection
# =============================================================================

class Flaw3AddloadPauseResumeRearmsEnergyDetection(unittest.TestCase):
    """_transition_to_running_from_pause must revive the energy tick, not just poll_timer -
    matching _begin_running_cycle / _restore_running_state's own re-arm on every other entry."""

    def test_energy_tick_survives_an_addload_pause_cycle(self):
        NOW = trs.NOW
        start = NOW - timedelta(minutes=3)
        payload = trs.make_store_payload(
            state="Running", start_time=start, cycle_id="cycle-addload", energy_at_start=0.05,
        )
        app = trs.make_full_init_app(
            sensor_state=None, helper_state=None, power_watts=0, store_payload=payload, now=NOW,
        )
        # Establish a live baseline: energy detection was active before the door opened.
        app.states[app.energy_sensor] = 0.05
        app._start_energy_detection()
        self.assertIsNotNone(app.energy_check_timer)

        # Door opens within the AddLoad window -> Paused.
        app.states[app.power_sensor] = 0
        app.states[app.door_sensor] = "on"
        app._handle_door_opened(app.state)
        self.assertEqual(app.get_state(app.state_entity), "Paused")

        # The energy tick that was pending before the pause fires now; the top-of-tick
        # non-Running guard nulls the handle.
        app._check_energy_finish({})
        self.assertIsNone(app.energy_check_timer)

        # Door closes with power above start_w -> resumes Running.
        before = len(app.scheduled)
        app.states[app.power_sensor] = 60
        app.states[app.door_sensor] = "off"
        app._handle_door_closed("Paused")

        self.assertEqual(app.get_state(app.state_entity), "Running")
        self.assertIsNotNone(app.energy_check_timer)
        new_ticks = [c for c in app.scheduled[before:] if c[0] == app._check_energy_finish]
        self.assertEqual(len(new_ticks), 1, app.scheduled[before:])


# =============================================================================
# FLAW 4 - _pending_end_reason must never leak past a refused transition or a cycle boundary
# =============================================================================

class Flaw4PendingEndReasonDoesNotLeak(unittest.TestCase):
    def test_power_gate_refusal_clears_it(self):
        """A mid-cycle-rinse-shaped power history fails _power_looks_like_cycle_end() - the
        transition is refused and must not leave anti_crease_pattern behind for later."""
        app = dwf.make_finish_app(
            state="Running",
            pending_end_reason="anti_crease_pattern",
            power_history=[
                {"state": str(w), "last_changed": dwf._iso(dwf.NOW - timedelta(minutes=m))}
                for m, w in [(7, 60), (6, 200), (5, 65), (4, 190), (3, 55), (2, 210), (1, 70)]
            ],
        )
        app.finish_power_gate_max_mean_w = 45.0
        app.finish_power_gate_max_peak_w = 120.0
        app.finish_power_gate_off_max_mean_w = 12.0
        app.finish_power_gate_off_max_peak_w = 25.0

        app._transition_to_unemptied()

        self.assertEqual(app.state, "Running")
        self.assertIsNone(app._pending_end_reason)

    def test_cooling_period_refusal_clears_it(self):
        """tail_to_standby skips the power gate (stronger evidence already), but
        _should_change_state can still refuse (cooling period) - must still clear it."""
        app = dwf.make_finish_app(state="Running", pending_end_reason="tail_to_standby")
        app.last_state_change = dwf.NOW - timedelta(seconds=60)  # inside cooling_period (300s)

        app._transition_to_unemptied()

        self.assertEqual(app.state, "Running")
        self.assertIsNone(app._pending_end_reason)

    def test_reset_cycle_tracking_clears_it(self):
        app = trs.make_full_init_app(sensor_state=None, helper_state=None, power_watts=0)
        app._pending_end_reason = "tail_pattern_break"

        app._reset_cycle_tracking()

        self.assertIsNone(app._pending_end_reason)

    def test_begin_running_cycle_clears_it(self):
        app = trs.make_full_init_app(sensor_state=None, helper_state=None, power_watts=0)
        app._pending_end_reason = "standby_backstop"

        app._begin_running_cycle("test begin")

        self.assertIsNone(app._pending_end_reason)


# =============================================================================
# FLAW 5 - learned durations must count each cycle exactly once
# =============================================================================

class Flaw5LearnedDurationsCountEachCycleExactlyOnce(unittest.TestCase):
    def _app_with_feedback_file(self):
        tmpdir = tempfile.mkdtemp(prefix="washer_audit_feedback_")
        feedback_file = os.path.join(tmpdir, "washer_feedback.json")
        return trs.make_full_init_app(
            sensor_state=None, helper_state=None, power_watts=0,
            extra_args={"feedback_file": feedback_file},
        )

    def test_unconfirmed_cycle_counts_once_via_the_push_confirm_not_the_save(self):
        app = self._app_with_feedback_file()

        record = app._save_cycle_feedback(
            predicted="eco", predicted_temperature=None,
            confirmed="eco", confirmed_temperature=None,
            duration_min=180.0, energy_kwh=0.7,
            heating_bursts=1, max_power_w=2000.0,
            user_confirmed=False,
            end_reason="low_power_detected",
            completion_class="completed", valid_for_learning=True, validation_flags=[],
        )
        self.assertIsNotNone(record)
        # Matches aggregate_cycles' own reload-time gate: an unconfirmed save must not apply
        # yet - the push-confirm path below is the single point of application.
        self.assertNotIn("eco", app._learned_durations)

        action = wfb.encode_confirm_action(record["ts"], "eco", None)
        app._on_confirm_push_action("mobile_app_notification_action", {"action": action}, {})
        self.assertEqual(app._learned_durations["eco"]["n"], 1)

        app._load_and_apply_feedback()
        self.assertEqual(app._learned_durations["eco"]["n"], 1)

    def test_confirmed_before_save_counts_once_and_push_never_applies_again(self):
        app = self._app_with_feedback_file()

        record = app._save_cycle_feedback(
            predicted="eco", predicted_temperature=None,
            confirmed="eco", confirmed_temperature=None,
            duration_min=150.0, energy_kwh=0.7,
            heating_bursts=1, max_power_w=2000.0,
            user_confirmed=True,
            completion_class="completed", valid_for_learning=True, validation_flags=[],
        )
        self.assertEqual(app._learned_durations["eco"]["n"], 1)

        # should_send_confirm_push would never fire a push for an already-confirmed record in
        # production; _on_confirm_push_action's own "already confirmed" guard is the backstop.
        action = wfb.encode_confirm_action(record["ts"], "eco", None)
        app._on_confirm_push_action("mobile_app_notification_action", {"action": action}, {})
        self.assertEqual(app._learned_durations["eco"]["n"], 1)


# =============================================================================
# FLAW 6 - boot restore must arm exactly one live energy-tick loop
# =============================================================================

def _make_tracked_boot_app(store_payload, power_history, power_watts, energy_history, now):
    """A trimmed make_full_init_app (test_washer_restart_survival.py) with one difference:
    run_in/cancel_timer/timer_running actually track which handles are still live, instead of
    make_full_init_app's dumb stand-ins (timer_running always False, cancel_timer a no-op) -
    those cannot distinguish "cancelled the stale handle before arming a new one" from "never
    cancelled anything", which is exactly the behaviour FLAW 6 is about. initialize() still
    runs for real end to end, same as make_full_init_app - only the timer bookkeeping differs.
    """
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.now = now
    app._now_utc = lambda: app.now

    tmpdir = tempfile.mkdtemp(prefix="washer_audit_flaw6_")
    state_file = os.path.join(tmpdir, "washer_cycle_state.json")
    cystore.CycleStore(state_file, "washer").save(store_payload)

    args = {
        "power_sensor": "sensor.washer_plug_power",
        "energy_sensor": "sensor.washer_plug_energy",
        "door_sensor": "binary_sensor.washer_door_contact",
        "state_entity": "sensor.washer_state",
        "ui_state_entity": "input_select.washer_state",
        "start_w": 18,
        "stop_w": 3.0,
        "feedback_file": "/nonexistent/washer_feedback_test.json",
        "state_file": state_file,
    }
    app.args = args
    app.AD = None
    app.states = {args["power_sensor"]: power_watts}
    app.attrs_store = {}
    app.last_changed_store = {}

    def get_state(entity, attribute=None, **kw):
        if attribute is None:
            return app.states.get(entity)
        if attribute == "all":
            if entity not in app.states and entity not in app.attrs_store:
                return None
            return {
                "state": app.states.get(entity),
                "attributes": dict(app.attrs_store.get(entity, {})),
                "last_changed": app.last_changed_store.get(entity),
                "last_updated": app.last_changed_store.get(entity),
            }
        return (app.attrs_store.get(entity, {}) or {}).get(attribute)
    app.get_state = get_state

    app.set_state_calls = []

    def set_state(entity, state=None, attributes=None, replace=False, **kw):
        app.set_state_calls.append(
            {"entity": entity, "state": state, "attributes": attributes, "replace": replace}
        )
        if state is not None:
            app.states[entity] = state
        if attributes is not None:
            if replace:
                app.attrs_store[entity] = dict(attributes)
            else:
                app.attrs_store.setdefault(entity, {}).update(attributes)
        app.last_changed_store[entity] = trs._iso(app._now_utc())
    app.set_state = set_state

    real_set_state_entity = wm.WasherMonitor._set_state_entity
    app._set_state_entity = lambda **kwargs: real_set_state_entity(app, **kwargs)

    app._history = {
        args["power_sensor"]: power_history or [],
        args["energy_sensor"]: energy_history or [],
    }
    app.get_history = lambda entity_id=None, start_time=None, end_time=None, **kw: [
        list(app._history.get(entity_id, []))
    ]

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

    # Timer trio that actually tracks liveness, unlike make_full_init_app's stand-ins.
    app.scheduled = []
    app._live_handles = set()

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((handle, cb, delay, kw))
        app._live_handles.add(handle)
        return handle

    def cancel_timer(handle):
        app._live_handles.discard(handle)

    def timer_running(handle):
        return handle in app._live_handles

    app.run_in = run_in
    app.cancel_timer = cancel_timer
    app.timer_running = timer_running
    app.listen_state = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None
    app.call_service = lambda *a, **kw: None
    app.get_app = lambda name: None

    app.initialize()
    return app


class Flaw6BootRestoreArmsOnlyOneEnergyTickLoop(unittest.TestCase):
    """Reproduces the exact trigger: a restored Running cycle whose naive start_time predates a
    real idle gap in power history, so the start-gap correction branch fires and calls
    _restore_energy_state_from_history() once - then the unconditional restore call right after
    it calls the same method again. Both must not leave a live handle behind."""

    def test_start_gap_correction_leaves_exactly_one_live_handle(self):
        NOW = trs.NOW
        stored_start = NOW - timedelta(minutes=180)
        gap_start = stored_start + timedelta(minutes=5)
        resumed = gap_start + timedelta(minutes=90)
        power_history = [
            {"state": "85", "last_changed": trs._iso(stored_start - timedelta(minutes=2))},
            {"state": "2", "last_changed": trs._iso(gap_start)},
            {"state": "1.5", "last_changed": trs._iso(gap_start + timedelta(minutes=45))},
            {"state": "90", "last_changed": trs._iso(resumed)},
            {"state": "92", "last_changed": trs._iso(resumed + timedelta(minutes=2))},
        ]
        energy_history = [
            {"state": str(0.10 + 0.01 * i), "last_changed": trs._iso(stored_start + timedelta(minutes=10 * i))}
            for i in range(10)
        ]
        payload = trs.make_store_payload(
            state="Running", start_time=stored_start, cycle_id="cycle-contaminated",
            notification_sent=True,
        )

        app = _make_tracked_boot_app(
            payload, power_history, power_watts=90, energy_history=energy_history, now=NOW,
        )

        # Sanity: this is genuinely the start-gap-correction path FLAW 6 is about.
        self.assertEqual(app.state, "Running")
        self.assertEqual(app._start_time_source, "power_history")

        tick_handles = [h for h, cb, delay, kw in app.scheduled if cb == app._check_energy_finish]
        self.assertGreaterEqual(
            len(tick_handles), 2,
            "test no longer exercises the double-call path this flaw is about",
        )
        live_tick_handles = [h for h in tick_handles if h in app._live_handles]
        self.assertEqual(len(live_tick_handles), 1, app.scheduled)
        self.assertIn(app.energy_check_timer, app._live_handles)


if __name__ == "__main__":
    unittest.main()
