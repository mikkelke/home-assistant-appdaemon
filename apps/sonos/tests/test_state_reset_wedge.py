# tests/test_state_reset_wedge.py - 2026-07-27 reset-handshake wedge guard.
#
# The TV-triggered Sonos reset handshake (sonos_reset_requested -> sonos_reset_ready ->
# sonos_reset_completed) can wedge _reset_in_progress True forever if a handshake event is
# lost (e.g. an HA restart mid-reset) - that permanently skips auto/manual resets and disables
# the immediate-unmute safety nets. Same stub trick as apps/appliances/tests -
# appdaemon isn't installed in the test env, so appdaemon.plugins.hass.hassapi is stubbed
# before import.

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

import state_reset as sr  # noqa: E402


def make_app():
    """SonosStateReset with fake AppDaemon callables, without running initialize()."""
    app = sr.SonosStateReset.__new__(sr.SonosStateReset)
    app.speaker_volumes = {}
    app.default_volume = 0.18
    app.inactivity_sec = 600
    app._reset_in_progress = False
    app._reset_wedge_timer = None
    app._active_reset_ctx = None

    app.log_calls = []      # (level, message)
    app.scheduled = []      # (callback, delay, kwargs, handle)
    app.canceled_timers = []
    app.fired_events = []   # (event, kwargs)
    app._live_timers = set()

    app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))

    # get_state always returns None (unknown speaker) - _needs_reset then fails open to
    # "needs reset" (volume_level is None), so _do_reset always proceeds past the
    # already-at-defaults filter without extra per-speaker state setup.
    app.get_state = lambda entity, attribute=None, **kw: None

    def run_in(cb, delay, **kw):
        handle = object()
        app._live_timers.add(handle)
        app.scheduled.append((cb, delay, kw, handle))
        return handle

    app.run_in = run_in
    app.timer_running = lambda handle: handle in app._live_timers

    def cancel_timer(handle):
        app._live_timers.discard(handle)
        app.canceled_timers.append(handle)

    app.cancel_timer = cancel_timer

    def fire_event(event, **kw):
        app.fired_events.append((event, kw))

    app.fire_event = fire_event
    return app


def wedge_schedule(app):
    return [(cb, delay) for cb, delay, _kw, _handle in app.scheduled if cb == app._on_reset_wedge_timeout]


class DoResetArmsWedgeTimer(unittest.TestCase):
    def test_do_reset_arms_120s_wedge_timer_and_fires_requested(self):
        app = make_app()
        app._do_reset(["media_player.test"], trigger="manual_trigger")

        self.assertTrue(app._reset_in_progress)
        self.assertIsNotNone(app._reset_wedge_timer)
        self.assertEqual(sr.RESET_WEDGE_TIMEOUT_S, 120)
        self.assertEqual(wedge_schedule(app), [(app._on_reset_wedge_timeout, sr.RESET_WEDGE_TIMEOUT_S)])
        self.assertEqual([e for e, _kw in app.fired_events], ["sonos_reset_requested"])

    def test_fire_event_failure_clears_wedge_and_flag_immediately(self):
        app = make_app()

        def boom(event, **kw):
            raise RuntimeError("HA connection lost")

        app.fire_event = boom

        app._do_reset(["media_player.test"], trigger="manual_trigger")

        # The wedge timer armed just before the failed fire_event must be canceled, not left
        # dangling for the full 120s.
        self.assertEqual(len(app.scheduled), 1)
        armed_handle = app.scheduled[0][3]
        self.assertIn(armed_handle, app.canceled_timers)
        self.assertIsNone(app._reset_wedge_timer)
        self.assertFalse(app._reset_in_progress)
        self.assertIsNone(app._active_reset_ctx)
        errors = [msg for lvl, msg in app.log_calls if lvl == "ERROR"]
        self.assertTrue(any("failed to fire sonos_reset_requested" in msg for msg in errors))


class WedgeTimeoutClearsStuckFlag(unittest.TestCase):
    def test_timeout_with_flag_still_set_clears_and_warns(self):
        app = make_app()
        app._reset_in_progress = True
        app._active_reset_ctx = {"trigger": "manual_trigger"}
        app._reset_wedge_timer = object()

        app._on_reset_wedge_timeout({})

        self.assertFalse(app._reset_in_progress)
        self.assertIsNone(app._active_reset_ctx)
        self.assertIsNone(app._reset_wedge_timer)
        warnings = [(lvl, msg) for lvl, msg in app.log_calls if lvl == "WARNING"]
        self.assertEqual(warnings, [("WARNING", "reset handshake timed out - clearing wedge")])

    def test_timeout_with_flag_already_clear_is_a_noop(self):
        app = make_app()
        app._reset_in_progress = False

        app._on_reset_wedge_timeout({})

        self.assertEqual(app.log_calls, [])


class NormalCompletionCancelsWedgeTimer(unittest.TestCase):
    def test_finish_reset_cancels_wedge_timer(self):
        app = make_app()
        handle = object()
        app._live_timers.add(handle)
        app._reset_wedge_timer = handle
        app._reset_in_progress = True
        app._active_reset_ctx = {"trigger": "manual_trigger"}

        app._finish_reset(["media_player.test"], "manual_trigger", None)

        self.assertIn(handle, app.canceled_timers)
        self.assertIsNone(app._reset_wedge_timer)
        self.assertFalse(app._reset_in_progress)
        self.assertIsNone(app._active_reset_ctx)

    def test_on_reset_ready_no_targets_cancels_wedge_timer(self):
        """Group Manager answering with no targets is also a normal (if degenerate) end of
        the handshake - it must not leave the wedge timer armed for 120s."""
        app = make_app()
        handle = object()
        app._live_timers.add(handle)
        app._reset_wedge_timer = handle
        app._reset_in_progress = True

        app._on_reset_ready("sonos_reset_ready", {}, {})

        self.assertIn(handle, app.canceled_timers)
        self.assertIsNone(app._reset_wedge_timer)
        self.assertFalse(app._reset_in_progress)


if __name__ == "__main__":
    unittest.main()
