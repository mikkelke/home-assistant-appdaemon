# tests/test_presence_stuck_watch.py - PresenceStuckWatch: the no_motion rule, restart seeding,
# and the one-push-per-episode notify contract.
# Same __new__ + monkeypatched-callables harness as the other tests in this directory.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

import presence_stuck_watch as psw  # noqa: E402

PRESENCE = "binary_sensor.bathroom_presence_presence"
MOTION = "sensor.bathroom_presence_motion_state"

NOW0_DT = datetime(2026, 9, 23, 10, 0)
NOW0 = NOW0_DT.timestamp()

BASE_ARGS = {
    "presence": PRESENCE,
    "motion": MOTION,
    "no_motion_min": 30,
    "room": "Bathroom",
}


def _iso(dt):
    return dt.isoformat()


def make_app(states=None, now=None):
    """PresenceStuckWatch with fake AppDaemon callables, without running initialize()."""
    app = psw.PresenceStuckWatch.__new__(psw.PresenceStuckWatch)
    app.presence = PRESENCE
    app.motion = MOTION
    app.no_motion_min = 30.0
    app.room_name = "Bathroom"
    app._notifier = MagicMock()
    app._stuck = False
    app._presence_on_since = None
    app._last_motion_at = None

    app.states = dict(states or {})

    def get_state(entity, attribute=None, **kw):
        return app.states.get((entity, attribute))

    app.get_state = get_state

    clock = {"now": now if now is not None else NOW0}
    app._clock = clock
    app._now = lambda: clock["now"]

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.create_task = MagicMock()
    return app


def make_full_app(states=None, args=None):
    """PresenceStuckWatch with initialize() actually run (AD primitives stubbed only) - used
    to verify arg parsing, seeding, and listener/run_every registration. run_daily/listen_event
    /set_state are deliberately left unstubbed: this app must never call them."""
    app = psw.PresenceStuckWatch.__new__(psw.PresenceStuckWatch)
    app.args = dict(args if args is not None else BASE_ARGS)
    app.states = dict(states or {})
    app.get_state = lambda entity, attribute=None, **kw: app.states.get((entity, attribute))
    app.log = MagicMock()
    app.listen_state = MagicMock()
    app.run_every = MagicMock(return_value="tick-handle")
    app.create_task = MagicMock()
    app._now = lambda: NOW0
    notifier_stub = MagicMock()
    app.get_app = MagicMock(return_value=notifier_stub)
    app.initialize()
    return app


class ArgParsing(unittest.TestCase):
    def test_explicit_values_pass_through(self):
        app = make_full_app()
        self.assertEqual(app.presence, PRESENCE)
        self.assertEqual(app.motion, MOTION)
        self.assertEqual(app.no_motion_min, 30.0)
        self.assertEqual(app.room_name, "Bathroom")

    def test_defaults_when_optional_keys_omitted(self):
        args = {"presence": PRESENCE, "motion": MOTION}
        app = make_full_app(args=args)
        self.assertEqual(app.no_motion_min, 30.0)
        self.assertEqual(app.room_name, "Room")


class SeedAtStartup(unittest.TestCase):
    def test_full_initialize_seeds_from_last_changed(self):
        presence_changed = _iso(NOW0_DT - timedelta(minutes=40))
        motion_changed = _iso(NOW0_DT - timedelta(minutes=10))
        app = make_full_app(states={
            (PRESENCE, None): "on",
            (PRESENCE, "last_changed"): presence_changed,
            (MOTION, None): "small",
            (MOTION, "last_changed"): motion_changed,
        })
        self.assertAlmostEqual(NOW0 - app._presence_on_since, 2400, delta=2)
        self.assertAlmostEqual(NOW0 - app._last_motion_at, 600, delta=2)

    def test_falls_back_to_now_when_last_changed_missing(self):
        app = make_full_app(states={(PRESENCE, None): "on", (MOTION, None): "small"})
        self.assertEqual(app._presence_on_since, NOW0)
        self.assertEqual(app._last_motion_at, NOW0)

    def test_does_not_seed_when_off_and_no_motion(self):
        app = make_full_app(states={(PRESENCE, None): "off", (MOTION, None): "none"})
        self.assertIsNone(app._presence_on_since)
        self.assertIsNone(app._last_motion_at)

    def test_tick_alerts_immediately_when_seed_already_past_threshold(self):
        stale_changed = _iso(NOW0_DT - timedelta(minutes=40))
        app = make_full_app(states={
            (PRESENCE, None): "on",
            (PRESENCE, "last_changed"): stale_changed,
            (MOTION, None): "none",
        })
        app._tick({})
        app.create_task.assert_called_once()


class NoMotionRule(unittest.TestCase):
    def test_fires_at_threshold_not_before(self):
        app = make_app()
        app._presence_on_since = NOW0
        app._last_motion_at = NOW0
        app._evaluate(NOW0 + 29 * 60)
        app.create_task.assert_not_called()
        app._evaluate(NOW0 + 30 * 60)
        app.create_task.assert_called_once()

    def test_recent_motion_blocks_even_past_hold_time(self):
        app = make_app()
        app._presence_on_since = NOW0
        app._last_motion_at = NOW0 + 59 * 60
        app._evaluate(NOW0 + 60 * 60)
        app.create_task.assert_not_called()

    def test_falls_back_to_presence_on_since_when_never_seen(self):
        app = make_app()
        app._presence_on_since = NOW0
        app._last_motion_at = None
        app._evaluate(NOW0 + 30 * 60)
        app.create_task.assert_called_once()

    def test_motion_from_previous_session_does_not_count(self):
        app = make_app(now=NOW0)
        app._last_motion_at = NOW0 - 270 * 60
        app._on_presence_change(PRESENCE, "state", "off", "on", {})
        app._evaluate(NOW0 + 29 * 60)
        app.create_task.assert_not_called()
        app._evaluate(NOW0 + 30 * 60)
        app.create_task.assert_called_once()


class OneAlertPerEpisode(unittest.TestCase):
    def test_repeated_evaluation_only_alerts_once(self):
        app = make_app()
        app._presence_on_since = NOW0
        for minutes in (30, 31, 45, 90):
            app._evaluate(NOW0 + minutes * 60)
        app.create_task.assert_called_once()

    def test_new_episode_after_presence_off_on_rearms(self):
        app = make_app(now=NOW0)
        app._presence_on_since = NOW0
        app._evaluate(NOW0 + 30 * 60)
        app.create_task.assert_called_once()

        app._clock["now"] = NOW0 + 31 * 60
        app._close_episode(app._clock["now"])
        self.assertFalse(app._stuck)

        app._clock["now"] = NOW0 + 40 * 60
        app._on_presence_change(PRESENCE, "state", "off", "on", {})  # fresh on-edge, new session
        app._clock["now"] += 30 * 60
        app._evaluate(app._clock["now"])
        self.assertEqual(app.create_task.call_count, 2)


class EpisodeClearLogging(unittest.TestCase):
    def test_no_push_or_cleared_log_when_closed_before_any_threshold(self):
        app = make_app()
        app._presence_on_since = NOW0
        app._evaluate(NOW0 + 2 * 60)
        app._close_episode(NOW0 + 3 * 60)
        app.create_task.assert_not_called()
        self.assertFalse(app._stuck)
        self.assertEqual([c for c in app.log_calls if "cleared after" in c[0][0]], [])

    def test_logs_cleared_with_duration_when_stuck(self):
        app = make_app()
        app._presence_on_since = NOW0
        app._evaluate(NOW0 + 30 * 60)
        app.create_task.assert_called_once()
        app._close_episode(NOW0 + 35 * 60)
        cleared = [c for c in app.log_calls if "cleared after" in c[0][0]]
        self.assertEqual(len(cleared), 1)
        self.assertIn("35 min", cleared[0][0][0])


class OnPresenceChangeDispatch(unittest.TestCase):
    def test_on_edge_seeds_session_and_resets_stuck(self):
        app = make_app()
        app._stuck = True
        app._on_presence_change(PRESENCE, "state", "off", "on", {})
        self.assertEqual(app._presence_on_since, NOW0)
        self.assertFalse(app._stuck)

    def test_off_edge_closes_session(self):
        app = make_app()
        app._presence_on_since = NOW0 - 600
        app._on_presence_change(PRESENCE, "state", "on", "off", {})
        self.assertIsNone(app._presence_on_since)

    def test_transition_to_unavailable_is_inert(self):
        app = make_app()
        app._presence_on_since = NOW0 - 600
        app._stuck = True
        app._on_presence_change(PRESENCE, "state", "on", "unavailable", {})
        self.assertEqual(app._presence_on_since, NOW0 - 600)
        self.assertTrue(app._stuck)

    def test_on_unavailable_on_rearms(self):
        app = make_app()
        app._presence_on_since = NOW0
        app._stuck = True
        app._on_presence_change(PRESENCE, "state", "on", "unavailable", {})
        app._clock["now"] = NOW0 + 5
        app._on_presence_change(PRESENCE, "state", "unavailable", "on", {})
        self.assertFalse(app._stuck)
        self.assertEqual(app._presence_on_since, NOW0 + 5)


class MotionChangeDispatch(unittest.TestCase):
    def test_active_states_update_last_motion_at_others_dont(self):
        for new, expected in (("small", NOW0), ("large", NOW0), ("none", None)):
            with self.subTest(new=new):
                app = make_app()
                app._on_motion_change(MOTION, "state", "none", new, {})
                self.assertEqual(app._last_motion_at, expected)


class PushCoroutine(unittest.TestCase):
    def test_successful_send_keeps_stuck(self):
        app = make_app()
        app._stuck = True
        app._notifier.notify = AsyncMock(return_value=1)
        asyncio.run(app._push("Bathroom presence stuck?", "msg"))
        app._notifier.notify.assert_awaited_once_with(
            title="Bathroom presence stuck?", message="msg", target="user"
        )
        self.assertTrue(app._stuck)

    def test_zero_sends_rearms_for_retry(self):
        app = make_app()
        app._stuck = True
        app._notifier.notify = AsyncMock(return_value=0)
        asyncio.run(app._push("t", "m"))
        self.assertFalse(app._stuck)
        warnings = [c for c in app.log_calls if c[1].get("level") == "WARNING"]
        self.assertEqual(len(warnings), 1)

    def test_notify_exception_rearms_for_retry(self):
        app = make_app()
        app._stuck = True
        app._notifier.notify = AsyncMock(side_effect=RuntimeError("boom"))
        asyncio.run(app._push("t", "m"))
        self.assertFalse(app._stuck)
        warnings = [c for c in app.log_calls if c[1].get("level") == "WARNING"]
        self.assertEqual(len(warnings), 1)


class InitializeRegistration(unittest.TestCase):
    def setUp(self):
        self.app = make_full_app()

    def _listened_entities(self):
        return {c.args[1] for c in self.app.listen_state.call_args_list if len(c.args) > 1}

    def test_listens_to_presence_and_motion_only(self):
        self.assertEqual(self._listened_entities(), {PRESENCE, MOTION})

    def test_registers_60s_tick(self):
        self.app.run_every.assert_called_once()
        args = self.app.run_every.call_args.args
        self.assertEqual(args[0], self.app._tick)
        self.assertEqual(args[1], "now+60")
        self.assertEqual(args[2], 60)

    def test_looks_up_mobile_notifier_only(self):
        self.app.get_app.assert_called_once_with("MobileNotifier")


if __name__ == "__main__":
    unittest.main()
