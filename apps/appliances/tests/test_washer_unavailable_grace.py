from __future__ import annotations

import sys
import types
import unittest
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


def make_app(power_unavailable_off_after_seconds=180, timer_running=False):
    """WasherMonitor with fake get_state/log/run_in/timer_running/cancel_timer, without running
    AppDaemon's initialize(). Ported from dishwasher_monitor.py's power_unavailable_error_after_seconds
    guard (2026-07-17 log investigation)."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.power_sensor = "sensor.washer_plug_power"
    app.state_entity = "sensor.washer_state"
    app.power_unavailable_off_after_seconds = power_unavailable_off_after_seconds
    app.power_unavailable_off_timer = None
    # Dead-plug phone-page watchdog (25c95ee) rides the same unavailable path; the grace
    # and the page are separate timers with separate thresholds.
    app.plug_outage_push_after_seconds = 180
    app._plug_outage_push_timer = None
    app._plug_outage_pushed = False
    # Attributes _power_changed reads unconditionally before any Running-only branch.
    app.significant_w = 30.0
    app.start_w = 18.0
    app.high_power_counter = 0

    app.states = {}
    app.log_calls = []
    app.transition_calls = []
    app.scheduled = []
    app.canceled_timers = []
    app._timer_running = timer_running

    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.timer_running = lambda handle: app._timer_running
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)
    app._transition_to_off = lambda reason, force=False: app.transition_calls.append((reason, force))

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    return app


def scheduled_callbacks(app):
    return [cb for cb, _delay, _kw in app.scheduled]


class UnavailableStartsGraceInsteadOfForcingOff(unittest.TestCase):
    """2026-07-17 log investigation: washer/dishwasher power plugs go unavailable almost
    exclusively during HA restarts or ESPHome OTA flashes. The dishwasher monitor already
    absorbs these with a 180s grace period (zero false transitions ever); the washer used to
    force_transition_to_off() the instant the plug dropped, destroying cycle tracking/learning
    data. It now waits power_unavailable_off_after_seconds like the dishwasher."""

    def test_unavailable_schedules_grace_timer_without_forcing_off(self):
        app = make_app()
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        self.assertIsNotNone(app.power_unavailable_off_timer)
        self.assertEqual(app.transition_calls, [])
        # Two timers arm on a power-sensor dropout: the forced-Off grace and the
        # dead-plug page watchdog. Neither transitions anything yet.
        grace = [(cb, d) for cb, d, _kw in app.scheduled if cb == app._power_unavailable_off_timeout]
        self.assertEqual(grace, [(app._power_unavailable_off_timeout, app.power_unavailable_off_after_seconds)])
        self.assertIn(app._plug_outage_push_timeout, scheduled_callbacks(app))

    def test_second_unavailable_event_while_timer_running_does_not_reschedule(self):
        """Also covers the double-invocation dedup noted in the investigation: the 'unavailable'
        listen_state on state_entity/power_sensor and the unavailable branch of _power_changed
        used to both call _handle_unavailable and log twice 11 ms apart."""
        app = make_app()
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        before = len(app.scheduled)

        app._timer_running = True  # simulate the grace timer still running
        app._handle_unavailable(app.state_entity, None, None, "unavailable", {})
        self.assertEqual(len(app.scheduled), before)


class UnavailableGraceTimeout(unittest.TestCase):
    """Timeout re-checks the power sensor before force-wiping the cycle, so a recovery that
    lands right at the deadline is not mistaken for a real outage."""

    def test_still_unavailable_forces_off(self):
        app = make_app(power_unavailable_off_after_seconds=180)
        app.power_unavailable_off_timer = "handle-1"
        app.states[app.power_sensor] = "unavailable"
        app._power_unavailable_off_timeout({})
        self.assertEqual(
            app.transition_calls,
            [("Power sensor unavailable >= 180s", True)],
        )
        self.assertIsNone(app.power_unavailable_off_timer)

    def test_recovered_before_timeout_does_not_force_off(self):
        app = make_app()
        app.power_unavailable_off_timer = "handle-1"
        app.states[app.power_sensor] = "120.5"
        app._power_unavailable_off_timeout({})
        self.assertEqual(app.transition_calls, [])
        self.assertIsNone(app.power_unavailable_off_timer)


class PowerChangedCancelsGrace(unittest.TestCase):
    """A numeric power reading proves the plug is back, so the pending forced-Off must be
    cancelled - mirrors dishwasher_monitor.py's _power_changed calling
    _cancel_power_unavailable_grace right after the successful float(new) parse."""

    def test_numeric_reading_cancels_pending_grace_timer(self):
        app = make_app(timer_running=True)
        app.power_unavailable_off_timer = "handle-1"
        app.states[app.state_entity] = "Off"
        app._power_changed(app.power_sensor, "state", "unavailable", "0.0", {})
        self.assertIsNone(app.power_unavailable_off_timer)
        self.assertIn("handle-1", app.canceled_timers)


if __name__ == "__main__":
    unittest.main()
