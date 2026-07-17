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

import living_room_tv_control as lrc  # noqa: E402


FIXED_NOW = datetime(2026, 7, 17, 20, 0, 0)


def make_app(elapsed_seconds, confirmed=False, tv_state="on", max_wait_seconds=12.0, recheck_seconds=1.0):
    """LivingRoomTvControl with fake get_state/log/run_in/datetime, without running AppDaemon's
    initialize(). _power_on_confirm_tick's reschedule call was mis-indented inside the timeout
    branch after its `return` until 2026-07-17 - unreachable, so the poll loop could never
    actually loop (every observed power-on had confirmed on the first tick, 60/60 since April,
    so it never mattered in practice)."""
    app = lrc.LivingRoomTvControl.__new__(lrc.LivingRoomTvControl)
    app.tv_entity = "media_player.living_room_tv"
    app.power_on_confirm_recheck_seconds = recheck_seconds
    app.power_on_confirm_max_wait_seconds = max_wait_seconds
    app.power_off_threshold_w = 5.0
    app._power_on_confirm_handle = "prev-handle"
    app._power_on_confirm_wait_start = FIXED_NOW - timedelta(seconds=elapsed_seconds)

    app.datetime = lambda: FIXED_NOW
    app.log = lambda *a, **kw: None

    app.states = {app.tv_entity: tv_state}
    app.get_state = lambda entity, **kw: app.states.get(entity)

    app._tv_power_on_confirmed_for_speaker_reset = lambda: confirmed
    app._read_tv_power_w = lambda: 42.0

    app.execute_calls = []
    app._execute_tv_power_on_actions = lambda: app.execute_calls.append(True)

    app.cancel_calls = []
    app._cancel_power_on_confirm = lambda reason=None: app.cancel_calls.append(reason)

    app.scheduled = []

    def run_in(cb, delay, **kw):
        app.scheduled.append((cb, delay, kw))
        return object()

    app.run_in = run_in
    return app


def scheduled_callbacks(app):
    return [cb for cb, _delay, _kw in app.scheduled]


class PowerOnConfirmTickReschedules(unittest.TestCase):
    """2026-07-17: the reschedule at the end of _power_on_confirm_tick sat inside the timeout
    branch, after its `return` - dead code, so the poll loop never actually polled a second time."""

    def test_unconfirmed_before_max_wait_reschedules_itself(self):
        app = make_app(elapsed_seconds=5, confirmed=False, tv_state="on")
        app._power_on_confirm_tick()
        self.assertIn(app._power_on_confirm_tick, scheduled_callbacks(app))
        cb, delay, _kw = app.scheduled[0]
        self.assertEqual(delay, app.power_on_confirm_recheck_seconds)
        self.assertEqual(app.execute_calls, [])
        self.assertEqual(app.cancel_calls, [])

    def test_unconfirmed_at_or_past_max_wait_does_not_reschedule(self):
        app = make_app(elapsed_seconds=15, confirmed=False, tv_state="on")
        app._power_on_confirm_tick()
        self.assertEqual(app.scheduled, [])
        self.assertIsNone(app._power_on_confirm_wait_start)
        self.assertEqual(app.execute_calls, [])
        self.assertEqual(app.cancel_calls, [])

    def test_confirmed_runs_power_on_actions_and_does_not_reschedule(self):
        app = make_app(elapsed_seconds=5, confirmed=True, tv_state="on")
        app._power_on_confirm_tick()
        self.assertEqual(app.execute_calls, [True])
        self.assertEqual(app.scheduled, [])
        self.assertIsNone(app._power_on_confirm_wait_start)

    def test_tv_reported_off_cancels_instead_of_rescheduling(self):
        app = make_app(elapsed_seconds=5, confirmed=False, tv_state="off")
        app._power_on_confirm_tick()
        self.assertEqual(
            app.cancel_calls,
            ["Power-on confirm canceled: TV returned to off"],
        )
        self.assertEqual(app.execute_calls, [])
        self.assertEqual(app.scheduled, [])


if __name__ == "__main__":
    unittest.main()
