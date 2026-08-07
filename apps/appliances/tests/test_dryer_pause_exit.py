# tests/test_dryer_pause_exit.py - Paused-state cooling-period bypass on real door-close events.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# Ports washer_monitor.py's force=True door-close fix (its lines ~2334-2341) and poll-time
# reconciler (its lines ~3719-3730) to the dryer. The incident this addresses: _transition_to_paused
# stamps last_state_change and arms a 600s pause timer; the door closed 14s later, which cancelled
# the pause timer and then asked _should_change_state to leave Paused - refused, because only 14s of
# the 600s cooling_period had elapsed. Result: Paused, no timer, no way out, for 34h. Door-close from
# Paused is an authoritative event and must bypass cooling (force=True); the poll reconciler is a
# belt-and-braces recovery for a missed door-close listen_state edge.
#
# Per this repo's incident history, stubbing out the very function whose interaction with its
# caller IS the bug would hide it again - so every test below calls the real _handle_door_closed,
# real _evaluate_pause_exit, real _should_change_state and real _poll_power. Only the AppDaemon I/O
# boundary (get_state / set_state / run_in / get_history / log / timer_running / cancel_timer) is
# faked, following test_dryer_unavailable_grace.py's make_app harness style.

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta
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

import dryer_monitor as dm  # noqa: E402


def make_app():
    """DryerMonitor with fake get_state/log/run_in/get_history/timer_running/cancel_timer,
    without running AppDaemon's initialize(). Mirrors test_dryer_unavailable_grace.py's
    make_app, extended with the attributes _handle_door_closed / _evaluate_pause_exit /
    _transition_to_off / _transition_to_unemptied / _transition_to_running_from_pause /
    _poll_power touch along their real (non-stubbed) code paths."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)

    app.power_sensor = "sensor.dryer_plug_power"
    app.energy_sensor = "sensor.dryer_plug_energy"
    app.door_sensor = "binary_sensor.dryer_door_contact"
    app.state_entity = "sensor.dryer_state"

    app.start_w = 8.0
    app.stop_w = 5.0
    app.main_cycle_power = 400.0
    app.min_cycle_minutes = 60
    app.min_energy_kwh = 0.05
    app.cooling_period = 600
    app.unemptied_timeout_hours = 24.0

    app.state = "Off"
    app.states = {}
    app.last_state_change = None
    app.start_time = None
    app.energy_start = None
    app.detected_programme = "unknown"
    app.keep_fresh_detected = False
    app.notification_sent = False
    app.max_power_w = 0.0
    app.high_power_counter = 0

    app.poll_timer = None
    app.pause_timer = None
    app.low_power_timer = None
    app.pattern_check_timer = None
    app.classify_timer = None
    app.running_watchdog_timer = None
    app.unemptied_watchdog_timer = None
    app.start_confirm_timer = None
    app.door_opened_time = None
    app.door_fast_start_armed_until = None
    app.power_unavailable_off_timer = None
    app._plug_outage_push_timer = None
    app._plug_outage_pushed = False

    # No programme selectors configured -> _get_confirmed_programme_key() short-circuits to
    # None, and no Sonos/announce plumbing -> _transition_to_unemptied's notify block is skipped.
    app.programme_entity = None
    app.announce_entity = None
    app.sonos_notifier = None

    # Directory does not exist, so _save_cycle_feedback's write fails safely (caught + logged)
    # without touching real repo data - same trick as test_dryer_unavailable_grace.py's
    # make_full_init_app.
    app.feedback_file = "/nonexistent/dryer_feedback_test.json"

    app.log_calls = []
    app.set_state_calls = []
    app.reset_calls = []
    app.scheduled = []
    app.canceled_timers = []
    app._timer_running = False

    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.get_history = lambda **kw: []  # no history -> _correct_duration falls back to wall-clock
    app.timer_running = lambda handle: app._timer_running
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)
    app._reset_programme_selectors_to_unconfirmed = lambda: app.reset_calls.append("selectors")
    app._reset_cycle_tracking = lambda: app.reset_calls.append("cycle_tracking")

    def set_state_entity(**kw):
        app.set_state_calls.append(kw)
        if "state" in kw:
            app.states[app.state_entity] = kw["state"]

    app._set_state_entity = set_state_entity

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    return app


class DoorClosedFromPausedBypassesCoolingPeriod(unittest.TestCase):
    """The original bug: _transition_to_paused stamps last_state_change and arms a 600s pause
    timer; a door-close seconds later must not be refused by _should_change_state's cooling
    check. last_state_change is seeded to "now" in every test here to simulate being deep
    inside that 600s window - exactly the condition that wedged the dryer in Paused for 34h."""

    def _paused_app(self):
        app = make_app()
        app.states[app.state_entity] = "Paused"
        app.state = "Paused"
        app.last_state_change = datetime.now()  # inside the 600s cooling window
        return app

    def test_low_power_invalid_cycle_transitions_to_off_despite_cooling_period(self):
        app = self._paused_app()
        app.states[app.power_sensor] = "2.2"
        app.start_time = app._now_utc() - timedelta(minutes=12)
        app._handle_door_closed("Paused")
        self.assertEqual(app.state, "Off")
        self.assertEqual(app.reset_calls, ["selectors", "cycle_tracking"])

    def test_high_power_transitions_to_running_despite_cooling_period(self):
        app = self._paused_app()
        app.states[app.power_sensor] = "10.0"  # >= start_w
        app._handle_door_closed("Paused")
        self.assertEqual(app.state, "Running")

    def test_low_power_valid_cycle_transitions_to_unemptied_despite_cooling_period(self):
        app = self._paused_app()
        app.states[app.power_sensor] = "2.2"
        app.start_time = app._now_utc() - timedelta(minutes=65)
        app.energy_start = 0.0
        app.states[app.energy_sensor] = "0.10"
        app._handle_door_closed("Paused")
        self.assertEqual(app.state, "Unemptied")


class PollPowerPausedDoorReconciler(unittest.TestCase):
    """_poll_power's belt-and-braces reconciler for a missed/ignored door-close listen_state
    edge (or a rapid close blocked by cooling right after entering Paused) - state says Paused
    but the door is physically closed. `door == "off"` is checked explicitly so that
    unknown/unavailable/None can never be misread as "door closed"."""

    def _paused_app(self, door_state):
        app = make_app()
        app.states[app.state_entity] = "Paused"
        app.state = "Paused"
        app.last_state_change = datetime.now()  # inside the 600s cooling window
        if door_state is not None:
            app.states[app.door_sensor] = door_state
        # Reached only when the reconciler condition is False (falls through to the regular
        # poll tail) - a valid numeric reading keeps that harmless for a Paused state.
        app.states[app.power_sensor] = "3.0"
        return app

    def test_door_closed_triggers_pause_exit_despite_cooling_period(self):
        app = self._paused_app(door_state="off")
        app.states[app.power_sensor] = "10.0"  # >= start_w -> resumes Running
        app._poll_power({})
        self.assertEqual(app.state, "Running")
        info_logs = [a for a, kw in app.log_calls if kw.get("level") == "INFO"]
        self.assertTrue(any("state Paused but door is closed" in str(a[0]) for a in info_logs))

    def test_resuming_running_re_arms_the_poll_loop(self):
        """The reconciler runs from inside a fired poll timer, so its handle is already dead.
        _transition_to_running_from_pause only re-arms when poll_timer is None - leaving the
        stale handle in place would silently kill 60s polling for the rest of the cycle."""
        app = self._paused_app(door_state="off")
        app.states[app.power_sensor] = "10.0"
        app.poll_timer = "stale-handle-of-the-timer-that-just-fired"
        app._poll_power({})
        self.assertEqual(app.state, "Running")
        self.assertIsNotNone(app.poll_timer)
        self.assertNotEqual(app.poll_timer, "stale-handle-of-the-timer-that-just-fired")

    def test_door_open_does_nothing(self):
        app = self._paused_app(door_state="on")
        app._poll_power({})
        self.assertEqual(app.state, "Paused")
        self.assertEqual(app.set_state_calls, [])

    def test_door_unknown_does_nothing(self):
        app = self._paused_app(door_state="unknown")
        app._poll_power({})
        self.assertEqual(app.state, "Paused")
        self.assertEqual(app.set_state_calls, [])

    def test_door_unavailable_does_nothing(self):
        app = self._paused_app(door_state="unavailable")
        app._poll_power({})
        self.assertEqual(app.state, "Paused")
        self.assertEqual(app.set_state_calls, [])

    def test_door_none_does_nothing(self):
        app = self._paused_app(door_state=None)
        app._poll_power({})
        self.assertEqual(app.state, "Paused")
        self.assertEqual(app.set_state_calls, [])

    def test_state_not_paused_does_nothing(self):
        app = make_app()
        app.states[app.state_entity] = "Off"
        app.state = "Off"
        app._poll_power({})
        self.assertEqual(app.state, "Off")
        self.assertEqual(app.set_state_calls, [])


class UnforcedTransitionsStillRefuseInsideCoolingPeriod(unittest.TestCase):
    """Regression guard: force=False must still be the default at every pre-existing call site.
    Calling _transition_to_off / _transition_to_unemptied the OLD way (no force kwarg) must
    still be refused inside the cooling period - only the door-close path and the poll
    reconciler (both force=True) bypass it now."""

    def test_transition_to_off_unforced_is_refused_inside_cooling_period(self):
        app = make_app()
        app.states[app.state_entity] = "Paused"
        app.state = "Paused"
        app.last_state_change = datetime.now()  # inside the 600s cooling window
        app._transition_to_off("some reason")
        self.assertEqual(app.state, "Paused")
        self.assertEqual(app.set_state_calls, [])

    def test_transition_to_unemptied_unforced_is_refused_inside_cooling_period(self):
        app = make_app()
        app.states[app.state_entity] = "Paused"
        app.state = "Paused"
        app.last_state_change = datetime.now()  # inside the 600s cooling window
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Paused")
        self.assertEqual(app.set_state_calls, [])


if __name__ == "__main__":
    unittest.main()
