# tests/test_appdaemon_release_watch.py - unit tests for appdaemon_release_watch.py
# (weekly AppDaemon/appdaemon release + issue #2599 tripwire). Run from repo root:
# python3 -m unittest discover -s apps/notify/tests
#
# Same stub-appdaemon-then-import-standalone trick as test_mobile_notifier.py /
# apps/appliances/tests/*: appdaemon isn't installed in the test env, so
# appdaemon.plugins.hass.hassapi is faked just enough (Hass = object) before the
# module under test is imported.
#
# fetch_snapshot() is tested against a mocked requests.get - it is a pure,
# self-free function by design (see the module docstring's "Threading" section),
# so no AppDaemon app instance is needed for that half of the coverage. The
# notify-or-not/seeding logic is tested via a harness app built with __new__
# (bypassing initialize()), mirroring apps/weather/tests/test_gw2000a_watchdog.py.

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

import appdaemon_release_watch as arw  # noqa: E402


def _fresh_state_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    return path


def make_app(state_file=None):
    """AppdaemonReleaseWatch with fakes for logging/notify, without running
    initialize() - mirrors make_app() in test_gw2000a_watchdog.py."""
    app = arw.AppdaemonReleaseWatch.__new__(arw.AppdaemonReleaseWatch)
    app.pinned_version = "4.5.13"
    app.repo = "AppDaemon/appdaemon"
    app.issue_number = 2599
    app.notify_target = "mikkel"
    app.state_file = state_file or _fresh_state_path()

    app.logs = []
    app.log = lambda msg, level="INFO": app.logs.append((level, msg))

    app.notify_calls = []
    app._notify = lambda message: app.notify_calls.append(message)

    app._state = app._load_state()
    return app


def _snapshot(tag="4.5.14", issue_state="open", url="https://github.com/AppDaemon/appdaemon/releases/tag/4.5.14"):
    return {"ok": True, "tag_name": tag, "html_url": url, "published_at": "2026-08-20T00:00:00Z", "issue_state": issue_state}


class FirstRunSeeding(unittest.TestCase):
    """First-ever run (no state file) must never notify - see module docstring
    "Seeding". This holds even when the fetched data already differs from the
    pinned version / is already closed."""

    def test_seeds_state_without_notifying(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        app._handle_snapshot(_snapshot(tag="4.5.13", issue_state="open"))

        self.assertEqual(app.notify_calls, [])
        self.assertEqual(app._state, {"last_seen_tag": "4.5.13", "last_seen_issue_state": "open"})
        with open(app.state_file) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, {"last_seen_tag": "4.5.13", "last_seen_issue_state": "open"})

    def test_seeds_silently_even_if_already_ahead_of_pinned_and_already_closed(self):
        """The tripwire didn't exist before today - a first run that finds a NEWER
        release and a CLOSED issue must still just seed, not fire a backlog of
        "notifications" for facts nobody has been tracking yet."""
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        app._handle_snapshot(_snapshot(tag="4.6.0", issue_state="closed"))

        self.assertEqual(app.notify_calls, [])
        self.assertEqual(app._state["last_seen_tag"], "4.6.0")
        self.assertEqual(app._state["last_seen_issue_state"], "closed")


class NewReleaseNotification(unittest.TestCase):
    def test_new_release_notifies_once_then_stays_silent_on_unchanged_data(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app._handle_snapshot(_snapshot(tag="4.5.13", issue_state="open"))  # seed
        self.assertEqual(app.notify_calls, [])

        app._handle_snapshot(_snapshot(tag="4.5.14", issue_state="open"))
        self.assertEqual(len(app.notify_calls), 1)
        msg = app.notify_calls[0]
        self.assertIn("4.5.14", msg)
        self.assertIn("4.5.13", msg)  # pinned version named
        self.assertIn("https://github.com/AppDaemon/appdaemon/releases/tag/4.5.14", msg)
        self.assertNotIn("#2599", msg)  # issue still open - no revert hint

        # Same tag again on the next run - must not re-notify.
        app._handle_snapshot(_snapshot(tag="4.5.14", issue_state="open"))
        self.assertEqual(len(app.notify_calls), 1)

    def test_new_release_while_issue_already_closed_includes_revert_hint(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app._handle_snapshot(_snapshot(tag="4.5.13", issue_state="closed"))  # seed, already closed
        self.assertEqual(app.notify_calls, [])

        app._handle_snapshot(_snapshot(tag="4.5.14", issue_state="closed"))
        self.assertEqual(len(app.notify_calls), 1)
        msg = app.notify_calls[0]
        self.assertIn("4.5.14", msg)
        self.assertIn("#2599", msg)
        self.assertIn("can potentially be reverted", msg)


class IssueClosedNotification(unittest.TestCase):
    def test_open_to_closed_transition_notifies(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app._handle_snapshot(_snapshot(tag="4.5.13", issue_state="open"))  # seed
        self.assertEqual(app.notify_calls, [])

        app._handle_snapshot(_snapshot(tag="4.5.13", issue_state="closed"))
        self.assertEqual(len(app.notify_calls), 1)
        msg = app.notify_calls[0]
        self.assertIn("#2599", msg)
        self.assertIn("closed", msg)
        self.assertIn("4.5.13", msg)  # pinned version named in the revert hint
        self.assertIn("https://github.com/AppDaemon/appdaemon/issues/2599", msg)
        # No new release this cycle - the tag must not be (mis)claimed as new.
        self.assertNotIn("is out", msg)

    def test_staying_closed_does_not_re_notify(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app._handle_snapshot(_snapshot(tag="4.5.13", issue_state="open"))  # seed
        app._handle_snapshot(_snapshot(tag="4.5.13", issue_state="closed"))
        self.assertEqual(len(app.notify_calls), 1)

        app._handle_snapshot(_snapshot(tag="4.5.13", issue_state="closed"))
        self.assertEqual(len(app.notify_calls), 1)


class NetworkFailure(unittest.TestCase):
    def test_failed_snapshot_does_not_notify_or_raise_or_touch_state(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app._handle_snapshot(_snapshot(tag="4.5.13", issue_state="open"))  # seed
        state_before = dict(app._state)

        app._handle_snapshot({"ok": False, "error": "connection timed out"})  # must not raise

        self.assertEqual(app.notify_calls, [])
        self.assertEqual(app._state, state_before)
        self.assertTrue(any(level == "WARNING" for level, _ in app.logs))

    def test_on_snapshot_callback_never_raises_even_with_no_result(self):
        """Mirrors how submit_to_executor's callback is actually invoked (see module
        docstring "Threading") - result=None must be handled, never propagate."""
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app._on_snapshot()  # no kwargs at all
        self.assertEqual(app.notify_calls, [])


class FetchSnapshotPureFunction(unittest.TestCase):
    """fetch_snapshot() itself: never raises, always returns a dict."""

    def test_success_returns_expected_fields(self):
        release_resp = MagicMock(status_code=200)
        release_resp.raise_for_status.return_value = None
        release_resp.json.return_value = {
            "tag_name": "4.5.14",
            "html_url": "https://github.com/AppDaemon/appdaemon/releases/tag/4.5.14",
            "published_at": "2026-08-20T00:00:00Z",
        }
        issue_resp = MagicMock(status_code=200)
        issue_resp.raise_for_status.return_value = None
        issue_resp.json.return_value = {"state": "open"}

        with patch.object(arw.requests, "get", side_effect=[release_resp, issue_resp]) as get_mock:
            result = arw.fetch_snapshot("AppDaemon/appdaemon", 2599, 10)

        self.assertEqual(
            result,
            {
                "ok": True,
                "tag_name": "4.5.14",
                "html_url": "https://github.com/AppDaemon/appdaemon/releases/tag/4.5.14",
                "published_at": "2026-08-20T00:00:00Z",
                "issue_state": "open",
            },
        )
        self.assertEqual(get_mock.call_count, 2)
        self.assertIn("releases/latest", get_mock.call_args_list[0].args[0])
        self.assertIn("issues/2599", get_mock.call_args_list[1].args[0])
        self.assertEqual(get_mock.call_args_list[0].kwargs.get("timeout"), 10)

    def test_network_error_is_caught_and_reported(self):
        with patch.object(arw.requests, "get", side_effect=arw.requests.exceptions.ConnectionError("boom")):
            result = arw.fetch_snapshot("AppDaemon/appdaemon", 2599, 10)
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])

    def test_http_error_status_is_caught_and_reported(self):
        bad_resp = MagicMock(status_code=404)
        bad_resp.raise_for_status.side_effect = arw.requests.exceptions.HTTPError("404 Not Found")
        with patch.object(arw.requests, "get", return_value=bad_resp):
            result = arw.fetch_snapshot("AppDaemon/appdaemon", 2599, 10)
        self.assertFalse(result["ok"])
        self.assertIn("404", result["error"])

    def test_malformed_body_is_reported_not_raised(self):
        release_resp = MagicMock(status_code=200)
        release_resp.raise_for_status.return_value = None
        release_resp.json.return_value = {}  # no tag_name
        issue_resp = MagicMock(status_code=200)
        issue_resp.raise_for_status.return_value = None
        issue_resp.json.return_value = {"state": "open"}

        with patch.object(arw.requests, "get", side_effect=[release_resp, issue_resp]):
            result = arw.fetch_snapshot("AppDaemon/appdaemon", 2599, 10)
        self.assertFalse(result["ok"])


class StatePersistence(unittest.TestCase):
    def test_missing_file_loads_as_never_seen(self):
        app = make_app()
        self.assertEqual(app._state, {"last_seen_tag": None, "last_seen_issue_state": None})

    def test_save_then_load_round_trips(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app._state = {"last_seen_tag": "4.5.13", "last_seen_issue_state": "open"}
        app._save_state()

        with open(app.state_file) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, {"last_seen_tag": "4.5.13", "last_seen_issue_state": "open"})

        reloaded = make_app(state_file=app.state_file)
        self.assertEqual(reloaded._state, {"last_seen_tag": "4.5.13", "last_seen_issue_state": "open"})

    def test_save_is_atomic_tmp_then_replace(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app._state = {"last_seen_tag": "4.5.13", "last_seen_issue_state": "open"}
        app._save_state()
        self.assertFalse(os.path.exists(app.state_file + ".tmp"))
        self.assertTrue(os.path.exists(app.state_file))


class WeeklyScheduling(unittest.TestCase):
    """check_day/check_time gate: run_daily fires every day, but only Mondays (by
    default) actually trigger a check."""

    def _app_with_weekday_gate(self, weekday):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))
        app.check_weekday = 0  # monday
        app.get_now = lambda: types.SimpleNamespace(weekday=lambda: weekday)
        app.started = []
        app._start_check = lambda: app.started.append(True)
        return app

    def test_wrong_weekday_does_not_start_a_check(self):
        app = self._app_with_weekday_gate(weekday=2)  # wednesday
        app._on_weekly_tick()
        self.assertEqual(app.started, [])

    def test_configured_weekday_starts_a_check(self):
        app = self._app_with_weekday_gate(weekday=0)  # monday
        app._on_weekly_tick()
        self.assertEqual(app.started, [True])

    def test_startup_tick_always_starts_a_check_regardless_of_weekday(self):
        app = self._app_with_weekday_gate(weekday=5)  # saturday
        app._on_startup_tick()
        self.assertEqual(app.started, [True])


def make_init_app(args=None):
    """AppdaemonReleaseWatch built via the REAL initialize() against a faked AD
    surface, to check the scheduling/dependency wiring itself - mirrors
    make_init_app() in test_gw2000a_watchdog.py."""
    app = arw.AppdaemonReleaseWatch.__new__(arw.AppdaemonReleaseWatch)
    app.args = dict(args or {})
    app.log = lambda *a, **kw: None
    app.get_app = MagicMock(return_value=MagicMock())
    app.run_daily = MagicMock()
    app.run_in = MagicMock()
    app.submit_to_executor = MagicMock()
    app.create_task = MagicMock()
    app.initialize()
    return app


class InitializeWiring(unittest.TestCase):
    def test_schedules_weekly_and_startup_checks_and_resolves_notifier(self):
        path = _fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        app = make_init_app(args={"state_file": path})

        app.get_app.assert_called_once_with("MobileNotifier")
        app.run_daily.assert_called_once()
        daily_args, _ = app.run_daily.call_args
        self.assertEqual(daily_args[0], app._on_weekly_tick)
        self.assertEqual(daily_args[1], "09:00:00")

        app.run_in.assert_called_once()
        in_args, _ = app.run_in.call_args
        self.assertEqual(in_args[0], app._on_startup_tick)
        self.assertEqual(in_args[1], 90.0)

    def test_unknown_check_day_defaults_to_monday_and_warns(self):
        path = _fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        warnings = []
        app = arw.AppdaemonReleaseWatch.__new__(arw.AppdaemonReleaseWatch)
        app.args = {"state_file": path, "check_day": "funday"}
        app.log = lambda msg, level="INFO": warnings.append((level, msg))
        app.get_app = MagicMock(return_value=MagicMock())
        app.run_daily = MagicMock()
        app.run_in = MagicMock()
        app.initialize()
        self.assertEqual(app.check_weekday, 0)
        self.assertTrue(any(level == "WARNING" for level, _ in warnings))


if __name__ == "__main__":
    unittest.main()
