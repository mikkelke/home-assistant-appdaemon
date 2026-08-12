# tests/test_dishwasher_dry_tail.py - "finished" means the MACHINE is finished (2026-08-12).
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# The design gap: the finish guards open at ~92-95% of the LEARNED duration, and for ECO -
# whose learned average is itself pulled down by early declarations - that is mid-DRY,
# ~30-40 min before the machine's own countdown reaches zero, on every single ECO run.
# The fix: when the finish conditions (guards + energy idle) are met, the Unemptied
# transition and its announcement wait until the machine's own end, modelled as
# max(guard-open moment, last main activity) + per-programme dry_tail_minutes from
# dishwasher_programmes.yaml (eco: 35; other programmes 0 = unmeasured, unchanged).
#
# Edge cases pinned here: tail already elapsed at guard time (late evaluation -> transition
# now); door-open during the tail (existing immediate door semantics win, tail cancelled);
# zero-tail programmes unchanged; power returning during the tail cancels the pending
# finish; and the feedback/learning record keeps the guard-time run_minutes - the learned
# series must NOT absorb the tail (a learned average grown by the tail pushes next run's
# guard later, which pushes the tail target later: a ratchet).
#
# Per this repo's incident history the real methods run: _confirm_finished,
# _finish_with_dry_tail, _dry_tail_elapsed, _transition_to_unemptied, _transition_to_paused,
# _transition_to_emptied, _handle_door_opened, _power_changed. Only the AppDaemon I/O
# boundary is faked (test_dryer_emptied_exit.py's harness style).

from __future__ import annotations

import json
import os
import sys
import tempfile
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

import dishwasher_monitor as dm  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 8, 12, 11, 38, 17, tzinfo=UTC)
LAST_ACTIVITY = datetime(2026, 8, 12, 13, 15, 20, tzinfo=UTC)
# Learned eco 214 min x 0.92 guard fraction -> guard opens ~14:55:10.
GUARD_OPEN = START + timedelta(minutes=214 * 0.92)
ECO_TAIL_TARGET = GUARD_OPEN + timedelta(minutes=35)

_TEST_PROFILES = {
    "eco": {
        "label": "ECO", "duration_min": 234, "duration_short_min": 74,
        "max_energy_kwh": 0.94, "dry_tail_minutes": 35, "dry_tail_short_minutes": 0,
    },
    "quick": {
        "label": "QuickPowerWash", "duration_min": 58, "duration_short_min": 14,
        "max_energy_kwh": 1.55, "dry_tail_minutes": 0,  # unmeasured -> behaviour unchanged
    },
}


class FakeSonos:
    def __init__(self):
        self.calls = []

    def notify(self, message):
        self.calls.append(message)


def make_running_app(now, *, energy_now="1.65", energy_start=1.0):
    """DishwasherMonitor mid-cycle (Running since START), with fake AD I/O and a movable
    clock. Learned eco = 214 (n=48), guard fraction 0.92 - the incident's numbers."""
    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)

    app.power_sensor = "sensor.dishwasher_plug_power"
    app.energy_sensor = "sensor.dishwasher_plug_energy"
    app.door_sensor = "binary_sensor.dishwasher_door_contact"
    app.state_entity = "sensor.dishwasher_state"
    app.ui_state_select = None  # _sync_ui_select short-circuits; no service plumbing needed
    app.confirmed_programme_entity = None
    app.short_entity = None

    app.start_w = 8.0
    app.stop_w = 2.0
    app.stop_for = 90
    app.min_cycle_minutes = 74
    app.min_energy_kwh = 0.4
    app.fill_window_minutes = 74
    app.pause_timeout_minutes = 5
    app.cooling_period = 300
    app.max_running_hours = 5
    app.unemptied_timeout_hours = 0
    app.emptied_timeout_minutes = 30
    app.min_emptying_seconds = 45
    app.energy_active_watts = 100.0
    app.high_power_threshold = 2
    app.low_power_threshold = 5
    app.pattern_window = 10
    app.start_sustain_seconds_without_door = 120
    app.door_close_fast_start_window_s = 900
    app.finish_guard_fraction = 0.92
    app.finish_guard_use_learned = True
    app.finish_guard_min_learned_n = 5
    app._learned_durations = {"eco": {"n": 48, "avg": 214.0}}

    app.state = "Running"
    app.states = {
        app.state_entity: "Running",
        app.power_sensor: "0.0",
        app.energy_sensor: energy_now,
        app.door_sensor: "off",
    }
    app.attrs = {}
    app.start_time = START
    app.energy_start = energy_start
    app.expected_dur_at_start = None
    app.last_state_change = None
    app.notification_sent = False
    app.detected_programme = "unknown"
    app.detected_short = False
    app.detected_quick_short = False
    app.max_power_w = 1800.0
    app._last_high_power_time = LAST_ACTIVITY
    app.power_readings = []
    app.high_power_counter = 0
    app.low_power_counter = 0
    app._sustain_start_begin = None
    app._strict_start_until_door_or_sustain = False
    app._start_candidate_active = False
    app._start_candidate_source = None
    app._start_candidate_active_samples = 0
    app._start_candidate_max_w = 0.0
    app._start_candidate_began_at = None
    app._start_candidate_energy_at_start = None
    app._start_candidate_window_timer = None
    app._start_candidate_idle_timer = None
    app.pending_start_used_fast_path = False
    app.door_fast_start_armed_until = None
    app.last_door_closed_at = None
    app.door_opened_time = None
    app.door_opened_during_cycle = False
    app.pause_from_low_power = False
    app.program_timer = None
    app.poll_timer = None
    app.classify_timer = None
    app.low_power_timer = None
    app.pause_timer = None
    app.pause_finish_timer = None
    app.running_watchdog_timer = None
    app.unemptied_watchdog_timer = None
    app.emptied_timeout_timer = None
    app.emptied_at = None
    app.power_unavailable_error_timer = None
    app.dry_tail_timer = None
    app._dry_tail_pending = None
    app.notify_target = ["mikkel"]
    app._plug_error_pushed = False

    app.sonos_notifier = FakeSonos()
    # Directory does not exist -> _save_cycle_feedback's write fails safely (caught+logged)
    # unless a test points it at a real temp file (the feedback-pinning test does).
    app.feedback_file = "/nonexistent/dishwasher_feedback_test.json"

    app.now = now
    app._now_utc = lambda: app.now

    def get_state(entity, **kw):
        attr = kw.get("attribute")
        if attr == "all":
            return {"attributes": dict(app.attrs.get(entity, {}))}
        if attr:
            return app.attrs.get(entity, {}).get(attr)
        return app.states.get(entity)

    app.get_state = get_state

    app.set_state_calls = []

    def set_state(entity, **kw):
        app.set_state_calls.append({"entity": entity, **kw})
        if "state" in kw:
            app.states[entity] = kw["state"]
        if "attributes" in kw:
            if kw.get("replace"):
                app.attrs[entity] = dict(kw["attributes"])
            else:
                app.attrs.setdefault(entity, {}).update(kw["attributes"])

    app.set_state = set_state
    app.get_history = lambda **kw: []  # _correct_duration falls back to wall-clock

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.call_service = lambda *a, **kw: None

    app.scheduled = []
    app.canceled_timers = []
    app.timer_running = lambda handle: True
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)

    def run_in(cb, delay, **kw):
        idx = len(app.scheduled)
        handle = f"timer#{idx}:{getattr(cb, '__name__', cb)}"
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    return app


def tail_schedules(app):
    return [d for cb, d, _kw in app.scheduled if getattr(cb, "__name__", "") == "_dry_tail_elapsed"]


class BaseTailTest(unittest.TestCase):
    def setUp(self):
        self._orig_profiles = dm.DishwasherMonitor.PROGRAMME_PROFILES
        dm.DishwasherMonitor.PROGRAMME_PROFILES = {k: dict(v) for k, v in _TEST_PROFILES.items()}

    def tearDown(self):
        dm.DishwasherMonitor.PROGRAMME_PROFILES = self._orig_profiles


class EcoFinishDefersToMachineEnd(BaseTailTest):
    def test_guards_met_holds_running_and_schedules_tail(self):
        """Finish conditions met at ~14:56 -> no Unemptied, no announcement; the transition
        is scheduled for guard-open + 35 min (~15:30), at the machine's own end."""
        t_met = datetime(2026, 8, 12, 14, 56, 41, tzinfo=UTC)
        app = make_running_app(t_met)
        app._confirm_finished({})

        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertEqual(app.sonos_notifier.calls, [])
        self.assertIsNotNone(app.dry_tail_timer)
        self.assertEqual(app._dry_tail_pending["target"], ECO_TAIL_TARGET)
        delays = tail_schedules(app)
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], (ECO_TAIL_TARGET - t_met).total_seconds(), delta=2)

    def test_tail_elapsed_transitions_and_announces(self):
        t_met = datetime(2026, 8, 12, 14, 56, 41, tzinfo=UTC)
        app = make_running_app(t_met)
        app._confirm_finished({})
        app.now = ECO_TAIL_TARGET
        app._dry_tail_elapsed({})

        self.assertEqual(app.states[app.state_entity], "Unemptied")
        self.assertEqual(app.sonos_notifier.calls, ["Dishwasher is ready to be emptied"])
        self.assertIsNone(app.dry_tail_timer)
        self.assertIsNone(app._dry_tail_pending)


class TailAlreadyElapsedAtGuardTime(BaseTailTest):
    def test_late_evaluation_transitions_immediately(self):
        """Guard-open + tail (~15:30) already past when the finish is evaluated (e.g. app
        restart after the machine's own end): transition now, announcement now."""
        late = datetime(2026, 8, 12, 15, 45, 0, tzinfo=UTC)
        app = make_running_app(late)
        app._confirm_finished({})

        self.assertEqual(app.states[app.state_entity], "Unemptied")
        self.assertEqual(app.sonos_notifier.calls, ["Dishwasher is ready to be emptied"])
        self.assertIsNone(app.dry_tail_timer)
        self.assertEqual(tail_schedules(app), [])


class ZeroTailProgrammesUnchanged(BaseTailTest):
    def test_quick_transitions_immediately_as_before(self):
        """dry_tail_minutes: 0 (unmeasured) -> exactly the old immediate behaviour."""
        # QuickPowerWash: 1.5 kWh classifies as quick; guard = nominal 58 * 0.92 = 53.4 min.
        t_met = START + timedelta(minutes=56)
        app = make_running_app(t_met, energy_now="2.50")
        app.min_cycle_minutes = 40  # harness value: quick's 58 min nominal < the eco-tuned 74
        app._last_high_power_time = START + timedelta(minutes=54)
        app._confirm_finished({})

        self.assertEqual(app.states[app.state_entity], "Unemptied")
        self.assertEqual(app.sonos_notifier.calls, ["Dishwasher is ready to be emptied"])
        self.assertIsNone(app.dry_tail_timer)
        self.assertEqual(tail_schedules(app), [])


class DoorOpenDuringTailWinsImmediately(BaseTailTest):
    def _app_with_pending_tail(self):
        t_met = datetime(2026, 8, 12, 14, 56, 41, tzinfo=UTC)
        app = make_running_app(t_met)
        app._confirm_finished({})
        assert app._dry_tail_pending is not None
        return app

    def test_door_open_past_full_guard_empties_now_without_announcement(self):
        """A human opening the door mid-dry is emptying early - their choice. The existing
        door path (Unemptied skip_announce -> Emptied) wins; the pending tail is dropped so
        the deferred callback can never double-fire or double-announce."""
        app = self._app_with_pending_tail()
        tail_handle = app.dry_tail_timer
        app.now = datetime(2026, 8, 12, 15, 15, 0, tzinfo=UTC)  # elapsed 216.7 >= full guard 214
        app.states[app.door_sensor] = "on"
        app._handle_door_opened("Running")

        self.assertEqual(app.states[app.state_entity], "Emptied")
        self.assertEqual(app.sonos_notifier.calls, [])  # door first -> no announcement, as always
        self.assertIsNone(app.dry_tail_timer)
        self.assertIsNone(app._dry_tail_pending)
        self.assertIn(tail_handle, app.canceled_timers)

        # The deferred callback firing later must be a no-op.
        app.now = ECO_TAIL_TARGET
        app._dry_tail_elapsed({})
        self.assertEqual(app.states[app.state_entity], "Emptied")
        self.assertEqual(app.sonos_notifier.calls, [])

    def test_door_open_before_full_guard_pauses_and_cancels_tail(self):
        """Door open mid-dry before the full programme duration keeps its existing Paused
        semantics; a paused machine is not finished, so the pending tail is dropped."""
        app = self._app_with_pending_tail()
        app.now = datetime(2026, 8, 12, 15, 0, 0, tzinfo=UTC)  # elapsed 201.7 < full guard 214
        app.states[app.door_sensor] = "on"
        app._handle_door_opened("Running")

        self.assertEqual(app.states[app.state_entity], "Paused")
        self.assertIsNone(app.dry_tail_timer)
        self.assertIsNone(app._dry_tail_pending)
        self.assertEqual(app.sonos_notifier.calls, [])


class PowerReturnDuringTailCancels(BaseTailTest):
    def test_active_power_drops_the_pending_finish(self):
        """Watts during the tail mean the guards were premature - the machine is still
        working. The pending finish is dropped; live detection starts over."""
        t_met = datetime(2026, 8, 12, 14, 56, 41, tzinfo=UTC)
        app = make_running_app(t_met)
        app._confirm_finished({})
        tail_handle = app.dry_tail_timer

        app.now = datetime(2026, 8, 12, 15, 5, 0, tzinfo=UTC)
        app.states[app.power_sensor] = "300.0"
        app._power_changed(app.power_sensor, "state", "0.0", "300.0", {})

        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertIsNone(app.dry_tail_timer)
        self.assertIsNone(app._dry_tail_pending)
        self.assertIn(tail_handle, app.canceled_timers)
        self.assertEqual(app.sonos_notifier.calls, [])

        # Belt to the suspender: even if the callback somehow fired with power present,
        # it must refuse to declare the machine finished.
        app._dry_tail_pending = {
            "target": ECO_TAIL_TARGET, "classified": "eco", "run_minutes": 198.4,
            "duration_source": None, "idle_min": None, "energy_used": 0.65,
            "end_reason": "low_power_detected",
        }
        app.now = ECO_TAIL_TARGET
        app._dry_tail_elapsed({})
        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertEqual(app.sonos_notifier.calls, [])


class FeedbackDoesNotAbsorbTheTail(BaseTailTest):
    def test_recorded_run_minutes_is_guard_time_not_transition_time(self):
        """The learned-duration series must record the SAME run_minutes it did before the
        tail existed (measured when the guards+idle were met), never transition-start. A
        record grown by the tail would push next run's guard later, which pushes the tail
        target later - a ratchet."""
        t_met = datetime(2026, 8, 12, 14, 56, 41, tzinfo=UTC)
        app = make_running_app(t_met)
        with tempfile.TemporaryDirectory() as tmp:
            app.feedback_file = os.path.join(tmp, "dishwasher_feedback.json")
            app._confirm_finished({})
            app.now = ECO_TAIL_TARGET
            app._dry_tail_elapsed({})

            with open(app.feedback_file) as f:
                cycles = json.load(f)["cycles"]
        expected_run = (t_met - START).total_seconds() / 60  # ~198.4
        tail_absorbing_run = (ECO_TAIL_TARGET - START).total_seconds() / 60  # ~231.9
        self.assertAlmostEqual(cycles[-1]["duration_min"], expected_run, delta=1.0)
        self.assertLess(cycles[-1]["duration_min"], tail_absorbing_run - 20)


class ConfirmFinishedReentryDuringTail(BaseTailTest):
    def test_reentry_is_a_noop(self):
        """The 60s poll keeps re-arming the low-power timer through the passive dry, so
        _confirm_finished re-fires routinely while the tail is pending - it must never
        double-schedule, transition, or re-save."""
        t_met = datetime(2026, 8, 12, 14, 56, 41, tzinfo=UTC)
        app = make_running_app(t_met)
        app._confirm_finished({})
        pending_before = app._dry_tail_pending

        app.now = t_met + timedelta(minutes=3)
        app._confirm_finished({})

        self.assertEqual(len(tail_schedules(app)), 1)
        self.assertIs(app._dry_tail_pending, pending_before)
        self.assertEqual(app.states[app.state_entity], "Running")


if __name__ == "__main__":
    unittest.main()
