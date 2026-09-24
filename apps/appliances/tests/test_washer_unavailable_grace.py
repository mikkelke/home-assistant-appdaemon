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


def make_app(timer_running=False):
    """WasherMonitor with fake get_state/log/run_in/timer_running/cancel_timer, without running
    AppDaemon's initialize()."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.power_sensor = "sensor.washer_plug_power"
    app.state_entity = "sensor.washer_state"
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
    app.push_calls = []
    app.scheduled = []
    app.canceled_timers = []
    app._timer_running = timer_running

    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.timer_running = lambda handle: app._timer_running
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)
    app._transition_to_off = lambda reason, force=False: app.transition_calls.append((reason, force))
    app._push_mobile = lambda message: app.push_calls.append(message)

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    return app


class HaPowerUnavailableNeverChangesState(unittest.TestCase):
    """HA's power entity feeds start detection, the recorder and the dashboard; finish decisions
    read the plug directly. Its outage therefore never changes the washer state (it used to force
    Off after 180 s, wiping a cycle the direct reads were still tracking) - it only arms the
    HA-side dead-plug page."""

    def test_unavailable_arms_the_page_and_nothing_else(self):
        app = make_app()
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        self.assertEqual(app.transition_calls, [])
        self.assertEqual([(cb, d) for cb, d, _ in app.scheduled], [(app._plug_outage_push_timeout, 180)])

    def test_second_unavailable_event_while_the_page_timer_runs_does_not_rearm(self):
        app = make_app()
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        app._timer_running = True
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        self.assertEqual(len(app.scheduled), 1)

    def test_unavailable_power_change_event_does_not_transition(self):
        app = make_app()
        app.states[app.state_entity] = "Running"
        app._power_changed(app.power_sensor, "state", "3.1", "unavailable", {})
        self.assertEqual(app.transition_calls, [])


class HaSidePage(unittest.TestCase):
    def test_lasting_outage_pages_once(self):
        app = make_app()
        app.states[app.power_sensor] = "unavailable"
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        cb, _delay, kw = app.scheduled[-1]
        cb(kw)
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        self.assertEqual(len(app.push_calls), 1)
        self.assertEqual(len(app.scheduled), 1)
        self.assertTrue(app._plug_outage_pushed)

    def test_recovered_before_the_page_does_not_page(self):
        app = make_app()
        app.states[app.power_sensor] = "120.5"
        app._plug_outage_push_timeout({})
        self.assertEqual(app.push_calls, [])

    def test_numeric_reading_cancels_the_pending_page_and_sends_all_clear_after_one(self):
        app = make_app(timer_running=True)
        app._plug_outage_push_timer = "handle-1"
        app._plug_outage_pushed = True
        app.states[app.state_entity] = "Off"
        app._power_changed(app.power_sensor, "state", "unavailable", "0.0", {})
        self.assertIn("handle-1", app.canceled_timers)
        self.assertIsNone(app._plug_outage_push_timer)
        self.assertEqual(len(app.push_calls), 1)
        self.assertFalse(app._plug_outage_pushed)


if __name__ == "__main__":
    unittest.main()
