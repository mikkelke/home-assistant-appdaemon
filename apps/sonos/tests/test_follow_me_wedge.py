# tests/test_follow_me_wedge.py - 2026-07-27 reset-handshake wedge guard (mirrors
# test_state_reset_wedge.py / test_group_manager_wedge.py). SonosFollowMe's
# _reset_in_progress is set True by _on_reset_started (sonos_reset_started listener) and only
# ever cleared by _delayed_reset_sync, which runs reset_resume_delay after
# sonos_reset_completed. If sonos_reset_completed is lost (HA restart mid-reset), the flag
# wedges True forever, permanently pausing follow_me mute/unmute. Same appdaemon stub trick
# as apps/appliances/tests.

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime
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

import follow_me as fm  # noqa: E402


def make_app():
    """SonosFollowMe with fake AppDaemon callables, without running initialize()."""
    app = fm.SonosFollowMe.__new__(fm.SonosFollowMe)
    # Empty all_speakers -> _get_current_coordinator finds nothing -> _should_follow_me_be_active
    # is a clean False -> _trigger_follow_me_sync (called at the end of the normal completion
    # path) is a one-line no-op return, no further mocking needed.
    app.args = {"all_speakers": []}
    app._reset_in_progress = False
    app._reset_resume_handle = None
    app._fm_reset_generation = 0
    app._reset_wedge_timer = None
    app._cached_coordinator = None
    app._cached_coordinator_time = None

    app.log_calls = []      # (level, message)
    app.scheduled = []      # (callback, delay, kwargs, handle)
    app.canceled_timers = []
    app._live_timers = set()

    app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
    app.datetime = lambda: datetime.now()
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
    return app


def wedge_schedule(app):
    return [(cb, delay) for cb, delay, _kw, _handle in app.scheduled if cb == app._on_reset_wedge_timeout]


class ResetStartedArmsWedgeTimer(unittest.TestCase):
    def test_on_reset_started_arms_120s_timer(self):
        app = make_app()
        app._on_reset_started("sonos_reset_started", {}, {})

        self.assertTrue(app._reset_in_progress)
        self.assertIsNotNone(app._reset_wedge_timer)
        self.assertEqual(fm.RESET_WEDGE_TIMEOUT_S, 120)
        self.assertEqual(wedge_schedule(app), [(app._on_reset_wedge_timeout, fm.RESET_WEDGE_TIMEOUT_S)])


class WedgeTimeoutClearsStuckFlag(unittest.TestCase):
    def test_timeout_with_flag_still_set_clears_and_warns(self):
        app = make_app()
        app._reset_in_progress = True
        app._reset_wedge_timer = object()

        app._on_reset_wedge_timeout({})

        self.assertFalse(app._reset_in_progress)
        self.assertIsNone(app._reset_wedge_timer)
        warnings = [(lvl, msg) for lvl, msg in app.log_calls if lvl == "WARNING"]
        self.assertEqual(warnings, [("WARNING", "reset handshake timed out - clearing wedge")])

    def test_timeout_with_flag_already_clear_is_a_noop(self):
        app = make_app()
        app._reset_in_progress = False

        app._on_reset_wedge_timeout({})

        self.assertEqual(app.log_calls, [])


class NormalCompletionCancelsWedgeTimer(unittest.TestCase):
    def test_delayed_reset_sync_cancels_wedge_timer(self):
        app = make_app()
        handle = object()
        app._live_timers.add(handle)
        app._reset_wedge_timer = handle
        app._reset_in_progress = True
        app._fm_reset_generation = 2

        app._delayed_reset_sync({"session_gen": 2})

        self.assertIn(handle, app.canceled_timers)
        self.assertIsNone(app._reset_wedge_timer)
        self.assertFalse(app._reset_in_progress)


if __name__ == "__main__":
    unittest.main()
