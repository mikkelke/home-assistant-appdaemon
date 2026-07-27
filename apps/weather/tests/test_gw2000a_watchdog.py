"""Unit tests for gw2000a_watchdog's restart-survival fixes (2026-07-27):
two-consecutive-failure paging debounce, persisted notified_down, and the init-time
all-clear for a recovery that happened while AD itself was down."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

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

import gw2000a_watchdog as gw  # noqa: E402

ENTITY = "sensor.gw2000a_wind_speed"
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _fresh_state_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    return path


def make_app(entity_states=None, stale_min=20.0):
    """Gw2000aWatchdog with a fake get_state/get_now, without running initialize().
    entity_states: {entity: (state, last_updated_iso_or_None)}."""
    app = gw.Gw2000aWatchdog.__new__(gw.Gw2000aWatchdog)
    app.entities = [ENTITY]
    app.stale_min = stale_min
    app.notify_target = ["mikkel"]
    app.state_file = _fresh_state_path()

    entity_states = entity_states or {}

    def get_state(entity, attribute=None, **kw):
        state, last_updated = entity_states.get(entity, (None, None))
        if attribute == "last_updated":
            return last_updated
        return state

    app.get_state = get_state
    app.get_now = lambda: NOW
    app.log = lambda *a, **kw: None
    app.notify_calls = []
    app._notify = lambda message: app.notify_calls.append(message)
    app._notified_down = False
    app._consec_fail = 0
    return app


def fresh(now=NOW, minutes_ago=1):
    return (now - timedelta(minutes=minutes_ago)).isoformat()


def stale(now=NOW, minutes_ago=30):
    return (now - timedelta(minutes=minutes_ago)).isoformat()


class IsHealthy(unittest.TestCase):
    def test_true_when_fresh(self):
        app = make_app({ENTITY: ("5.0", fresh())})
        self.assertTrue(app._is_healthy(NOW))

    def test_false_when_unavailable(self):
        app = make_app({ENTITY: ("unavailable", None)})
        self.assertFalse(app._is_healthy(NOW))

    def test_false_when_stale(self):
        app = make_app({ENTITY: ("5.0", stale())})
        self.assertFalse(app._is_healthy(NOW))

    def test_true_when_any_one_of_several_is_fresh(self):
        app = make_app()
        app.entities = ["sensor.a", "sensor.b"]
        states = {"sensor.a": ("unavailable", None), "sensor.b": ("1.0", fresh())}
        app.get_state = lambda entity, attribute=None, **kw: (
            states[entity][1] if attribute == "last_updated" else states[entity][0]
        )
        self.assertTrue(app._is_healthy(NOW))

    def test_fail_open_true_on_unparseable_last_updated(self):
        app = make_app({ENTITY: ("5.0", "not-a-timestamp")})
        self.assertTrue(app._is_healthy(NOW))


class TwoStrikePaging(unittest.TestCase):
    """2026-07-27: an HA restart coinciding with a single check must not page - only
    TWO consecutive failed checks do."""

    def test_single_failed_check_does_not_page(self):
        app = make_app({ENTITY: ("unavailable", None)})
        app._check()
        self.assertFalse(app._notified_down)
        self.assertEqual(app.notify_calls, [])
        self.assertEqual(app._consec_fail, 1)

    def test_second_consecutive_failed_check_pages(self):
        app = make_app({ENTITY: ("unavailable", None)})
        app._check()
        app._check()
        self.assertTrue(app._notified_down)
        self.assertEqual(len(app.notify_calls), 1)
        self.assertIn("stopped reporting", app.notify_calls[0])

    def test_a_healthy_check_in_between_resets_the_streak(self):
        app = make_app({ENTITY: ("unavailable", None)})
        app._check()  # strike 1
        self.assertEqual(app._consec_fail, 1)

        app.get_state = lambda entity, attribute=None, **kw: (
            fresh() if attribute == "last_updated" else "5.0"
        )
        app._check()  # healthy - resets
        self.assertEqual(app._consec_fail, 0)
        self.assertFalse(app._notified_down)

        app.get_state = lambda entity, attribute=None, **kw: (
            None if attribute == "last_updated" else "unavailable"
        )
        app._check()  # strike 1 again, not strike 3 - must not page yet
        self.assertFalse(app._notified_down)
        self.assertEqual(app.notify_calls, [])

    def test_does_not_page_again_while_already_notified(self):
        app = make_app({ENTITY: ("unavailable", None)})
        app._check()
        app._check()
        self.assertEqual(len(app.notify_calls), 1)
        app._check()  # still down - must not re-notify
        app._check()
        self.assertEqual(len(app.notify_calls), 1)


class RecoveryNotification(unittest.TestCase):
    def test_recovery_sends_all_clear_on_the_very_next_healthy_check(self):
        """Recovery is NOT gated by the two-strike debounce - only the down direction is."""
        app = make_app({ENTITY: ("unavailable", None)})
        app._notified_down = True

        app.get_state = lambda entity, attribute=None, **kw: (
            fresh() if attribute == "last_updated" else "5.0"
        )
        app._check()

        self.assertFalse(app._notified_down)
        self.assertEqual(app.notify_calls, ["Weather station is reporting again."])


class StatePersistence(unittest.TestCase):
    """_load_state/_save_state schema: {"notified_down": bool}. Atomic tmp+os.replace
    (house_events pattern), deploy_advisor's path/arg convention."""

    def test_save_then_load_round_trips(self):
        app = make_app({ENTITY: ("unavailable", None)})
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app._notified_down = True
        app._save_state()

        with open(app.state_file) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, {"notified_down": True})

        reloaded = make_app()
        reloaded.state_file = app.state_file
        reloaded._load_state()
        self.assertTrue(reloaded._notified_down)

    def test_missing_file_leaves_default_false(self):
        app = make_app()
        app._load_state()  # must not raise
        self.assertFalse(app._notified_down)

    def test_save_is_atomic_tmp_then_replace(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app._save_state()
        self.assertFalse(os.path.exists(app.state_file + ".tmp"))
        self.assertTrue(os.path.exists(app.state_file))

    def test_an_ad_restart_mid_outage_does_not_re_page(self):
        """Persisted notified_down survives an AD restart, so a fresh instance that is
        still down does not send a second page."""
        down = make_app({ENTITY: ("unavailable", None)})
        self.addCleanup(lambda: os.path.exists(down.state_file) and os.remove(down.state_file))
        down._check()
        down._check()
        self.assertTrue(down._notified_down)

        restarted = make_app({ENTITY: ("unavailable", None)})
        restarted.state_file = down.state_file
        restarted._load_state()
        self.assertTrue(restarted._notified_down)

        restarted._check()  # still down - must stay silent, already notified
        self.assertEqual(restarted.notify_calls, [])


def make_init_app(states=None, args=None):
    """Gw2000aWatchdog built via the REAL initialize() against a faked AD surface, for
    the init-time "recovered while AD was down" path."""
    app = gw.Gw2000aWatchdog.__new__(gw.Gw2000aWatchdog)
    app.args = dict(args or {})

    entity_states = states or {}

    def get_state(entity, attribute=None, **kw):
        state, last_updated = entity_states.get(entity, (None, None))
        if attribute == "last_updated":
            return last_updated
        return state

    app.get_state = get_state
    app.get_now = lambda: NOW
    app.log = lambda *a, **kw: None
    app.run_in_calls = []
    app.run_in = lambda cb, delay, **kw: app.run_in_calls.append((cb, delay))
    app.run_every = MagicMock()
    app.get_app = MagicMock(return_value=MagicMock())
    app.create_task = MagicMock()
    app.initialize()
    return app


class InitTimeRecovery(unittest.TestCase):
    def test_recovered_while_ad_was_down_sends_all_clear_once_and_clears_flag(self):
        path = _fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path, "w") as f:
            json.dump({"notified_down": True}, f)

        app = make_init_app(
            states={ENTITY: ("5.0", fresh())},  # station is healthy again
            args={"entities": [ENTITY], "state_file": path},
        )

        self.assertFalse(app._notified_down)
        with open(path) as f:
            self.assertEqual(json.load(f), {"notified_down": False})

        # Notify is deferred one tick (run_in) like every other create_task-via-_notify
        # call in this codebase - simulate AppDaemon firing it.
        self.assertEqual(len(app.run_in_calls), 1)
        cb, delay = app.run_in_calls[0]
        self.assertEqual(delay, 0)
        cb({})
        app.get_app.assert_called_with("MobileNotifier")
        app.create_task.assert_called_once()

    def test_still_down_at_init_does_not_send_all_clear(self):
        path = _fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path, "w") as f:
            json.dump({"notified_down": True}, f)

        app = make_init_app(
            states={ENTITY: ("unavailable", None)},  # still down
            args={"entities": [ENTITY], "state_file": path},
        )

        self.assertTrue(app._notified_down)
        self.assertEqual(app.run_in_calls, [])

    def test_not_previously_notified_stays_silent_even_if_healthy(self):
        path = _fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        # No persisted state at all (fresh install / never notified).

        app = make_init_app(
            states={ENTITY: ("5.0", fresh())},
            args={"entities": [ENTITY], "state_file": path},
        )

        self.assertFalse(app._notified_down)
        self.assertEqual(app.run_in_calls, [])


if __name__ == "__main__":
    unittest.main()
