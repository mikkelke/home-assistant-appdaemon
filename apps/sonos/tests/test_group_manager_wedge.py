# tests/test_group_manager_wedge.py - 2026-07-27 reset-handshake wedge guard (mirrors
# test_state_reset_wedge.py). GroupManager's _reset_in_progress is set True by both
# _on_reset_requested and _on_reset_started (sonos_reset_requested/_started listeners) and
# only ever cleared by _resume_after_reset, which runs reset_resume_delay after
# sonos_reset_completed. If any handshake event is lost (HA restart mid-reset), the flag
# wedges True forever, permanently blocking group operations, family-zone sync, and
# evaluation. Same appdaemon stub trick as apps/appliances/tests.

from __future__ import annotations

import sys
import threading
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

import group_manager as gm  # noqa: E402


def make_app():
    """SonosGroupManager with fake AppDaemon callables, without running initialize()."""
    app = gm.SonosGroupManager.__new__(gm.SonosGroupManager)
    app.speakers = ["media_player.a", "media_player.b"]
    app.reset_resume_delay = 2.5
    app.throttle = 2.0
    app.settle_delay = 2.0
    app._last_trigger = 0.0

    app._reset_in_progress = False
    app._pending_reset = None
    app._reset_resume_handle = None
    app._reset_generation = 0
    app._reset_wedge_timer = None
    app._pending_evaluate_handle = None
    app._pending_group_update = None
    app._group_update_generation = 0
    app._group_operation_lock = threading.Lock()
    app._group_operation_queue = []
    app._group_changes = []
    app._family_zone_sync_in_progress = False
    app._family_zone_sync_start_time = None

    app.log_calls = []      # (level, message)
    app.scheduled = []      # (callback, delay, kwargs, handle)
    app.canceled_timers = []
    app.fired_events = []   # (event, kwargs)
    app._live_timers = set()

    app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
    app.datetime = lambda: datetime.now()

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


class ResetRequestedArmsWedgeTimer(unittest.TestCase):
    def test_on_reset_requested_arms_120s_timer(self):
        app = make_app()
        app._on_reset_requested(
            "sonos_reset_requested",
            {"targets": ["media_player.a"], "trigger": "manual_trigger"},
            {},
        )

        self.assertTrue(app._reset_in_progress)
        self.assertIsNotNone(app._reset_wedge_timer)
        self.assertEqual(gm.RESET_WEDGE_TIMEOUT_S, 120)
        self.assertEqual(wedge_schedule(app), [(app._on_reset_wedge_timeout, gm.RESET_WEDGE_TIMEOUT_S)])
        self.assertEqual([e for e, _kw in app.fired_events], ["sonos_reset_ready"])


class ResetStartedArmsWedgeTimer(unittest.TestCase):
    def test_on_reset_started_arms_120s_timer(self):
        app = make_app()
        app._on_reset_started("sonos_reset_started", {}, {})

        self.assertTrue(app._reset_in_progress)
        self.assertEqual(len(wedge_schedule(app)), 1)

    def test_on_reset_started_after_requested_rearms_a_single_live_timer(self):
        """Both listeners set the flag for the same handshake; arming must be idempotent
        (cancel-then-reschedule), never leaving two live wedge timers."""
        app = make_app()
        app._on_reset_requested(
            "sonos_reset_requested",
            {"targets": ["media_player.a"], "trigger": "manual_trigger"},
            {},
        )
        first_handle = app._reset_wedge_timer

        app._on_reset_started("sonos_reset_started", {}, {})

        self.assertIn(first_handle, app.canceled_timers)
        self.assertNotEqual(app._reset_wedge_timer, first_handle)
        self.assertEqual(len(app._live_timers & {h for _cb, _d, _kw, h in app.scheduled}), 1)


class WedgeTimeoutClearsStuckFlag(unittest.TestCase):
    def test_timeout_with_flag_still_set_clears_and_warns(self):
        app = make_app()
        app._reset_in_progress = True
        app._pending_reset = {"targets": ["media_player.a"]}
        app._reset_wedge_timer = object()

        app._on_reset_wedge_timeout({})

        self.assertFalse(app._reset_in_progress)
        self.assertIsNone(app._pending_reset)
        self.assertIsNone(app._reset_wedge_timer)
        warnings = [(lvl, msg) for lvl, msg in app.log_calls if lvl == "WARNING"]
        self.assertEqual(warnings, [("WARNING", "reset handshake timed out - clearing wedge")])

    def test_timeout_with_flag_already_clear_is_a_noop(self):
        app = make_app()
        app._reset_in_progress = False

        app._on_reset_wedge_timeout({})

        self.assertEqual(app.log_calls, [])


class NormalCompletionCancelsWedgeTimer(unittest.TestCase):
    def test_resume_after_reset_cancels_wedge_timer(self):
        app = make_app()
        handle = object()
        app._live_timers.add(handle)
        app._reset_wedge_timer = handle
        app._reset_in_progress = True
        app._reset_generation = 3

        app._resume_after_reset({"session_gen": 3})

        self.assertIn(handle, app.canceled_timers)
        self.assertIsNone(app._reset_wedge_timer)
        self.assertFalse(app._reset_in_progress)


if __name__ == "__main__":
    unittest.main()
