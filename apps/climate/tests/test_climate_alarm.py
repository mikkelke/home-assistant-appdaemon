# tests/test_climate_alarm.py - Reboot-survival fix for climate_alarm.py (2026-07-27):
# _last_notify_at (push cooldown) and _snooze_until (the "Recheck in 1 hour" clear button)
# are persisted to climate_alarm_state.json, and the startup eval at init respects a
# still-active persisted snooze instead of unconditionally firing at +2s. Same __new__ +
# monkeypatched-callables harness as the other apps/climate/tests files.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import climate_alarm as ca  # noqa: E402

FIXED_NOW = datetime(2026, 7, 27, 6, 30, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """datetime subclass whose now() is frozen at FIXED_NOW - lets the startup-eval
    snooze/cooldown math in climate_alarm.py be tested deterministically. fromisoformat
    stays the real inherited implementation."""

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


def _write_state(path, **kv):
    with open(path, "w") as f:
        json.dump(kv, f)


def _logged(app, needle):
    return any(needle in str(a) for a, kw in app.log_calls)


class StartupSnoozeGating(unittest.TestCase):
    """At init: a snooze that survived a restart must be respected (no immediate
    re-evaluate voiding "Recheck in 1 hour"); no snooze (or an already-expired one)
    falls back to the ordinary +2s startup eval."""

    def setUp(self):
        self._orig_datetime = ca.datetime
        ca.datetime = _FrozenDatetime

    def tearDown(self):
        ca.datetime = self._orig_datetime

    def _new_app(self, state_file):
        app = ca.ClimateAlarm.__new__(ca.ClimateAlarm)
        app.args = {
            "climate_entities": [],
            "window_door_entities": [],
            "temp_threshold": {},
            "state_file": state_file,
            "check_interval_min": 0,  # backup schedule irrelevant to this fix - see task scope
        }
        app.log_calls = []
        app.log = lambda *a, **kw: app.log_calls.append((a, kw))
        app.get_app = lambda name: None
        app.listen_state = MagicMock()
        app.run_every = MagicMock()
        app.scheduled = []

        def run_in(cb, delay, **kw):
            app.scheduled.append((cb, delay, kw))
            return f"handle-{len(app.scheduled)}"

        app.run_in = run_in
        return app

    def test_no_persisted_state_evaluates_at_plus_2s(self):
        app = self._new_app("/nonexistent/dir/climate_alarm_state.json")
        app.initialize()
        self.assertEqual(len(app.scheduled), 1)
        cb, delay, _ = app.scheduled[0]
        self.assertEqual(cb, app._run_evaluate)
        self.assertEqual(delay, 2)

    def test_expired_snooze_evaluates_at_plus_2s(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        _write_state(path, snooze_until=(FIXED_NOW - timedelta(minutes=5)).isoformat())

        app = self._new_app(path)
        app.initialize()

        self.assertEqual(len(app.scheduled), 1)
        cb, delay, _ = app.scheduled[0]
        self.assertEqual(delay, 2)

    def test_active_snooze_schedules_remaining_time_instead_of_plus_2s(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        _write_state(path, snooze_until=(FIXED_NOW + timedelta(minutes=45)).isoformat())

        app = self._new_app(path)
        app.initialize()

        self.assertEqual(len(app.scheduled), 1)
        cb, delay, _ = app.scheduled[0]
        self.assertEqual(cb, app._run_evaluate)
        self.assertEqual(delay, 45 * 60)
        self.assertEqual(app._clear_recheck_handle, "handle-1")
        self.assertTrue(_logged(app, "Snoozed until"))

    def test_active_snooze_still_loads_last_notify_at_for_the_cooldown(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        recent_notify = FIXED_NOW - timedelta(minutes=5)
        _write_state(
            path,
            snooze_until=(FIXED_NOW + timedelta(minutes=45)).isoformat(),
            last_notify_at=recent_notify.isoformat(),
        )

        app = self._new_app(path)
        app.initialize()

        self.assertEqual(app._last_notify_at, recent_notify)

    def test_persisted_last_notify_at_without_a_snooze_is_still_loaded(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        recent_notify = FIXED_NOW - timedelta(minutes=5)
        _write_state(path, last_notify_at=recent_notify.isoformat())

        app = self._new_app(path)
        app.initialize()

        self.assertEqual(app._last_notify_at, recent_notify)
        cb, delay, _ = app.scheduled[0]
        self.assertEqual(delay, 2)  # no snooze -> ordinary startup eval


class SetAlarmRespectsAndPersistsCooldown(unittest.IsolatedAsyncioTestCase):
    """_set_alarm's pre-existing cooldown gate, now fed by a loaded (possibly persisted)
    _last_notify_at; a real notify must save the new value (write on change)."""

    def _app(self):
        app = ca.ClimateAlarm.__new__(ca.ClimateAlarm)
        app.event_active_entity = "input_boolean.climate_alarm_active"
        app.message_entity = "input_text.climate_alarm_message"
        app.cooldown_min = 20
        app.notify_target = "home"
        app.log = lambda *a, **kw: None
        app.call_service = AsyncMock()
        app._last_notify_at = None
        app._state = {}
        app._save_state = MagicMock()

        app.notified = []

        class FakeNotifier:
            async def notify(self, **kwargs):
                app.notified.append(kwargs)

        app.mobile_notifier = FakeNotifier()
        return app

    async def test_first_alarm_notifies_and_saves_last_notify_at(self):
        app = self._app()
        await app._set_alarm([("climate.bedroom_thermostat", 15.0, 18.0)], [])
        self.assertEqual(len(app.notified), 1)
        self.assertIsNotNone(app._last_notify_at)
        app._save_state.assert_called_once()

    async def test_within_cooldown_skips_notify_and_does_not_resave(self):
        app = self._app()
        app._last_notify_at = datetime.now(timezone.utc)  # "just notified" (e.g. restored)
        await app._set_alarm([("climate.bedroom_thermostat", 15.0, 18.0)], [])
        self.assertEqual(app.notified, [])
        app._save_state.assert_not_called()

    async def test_state_is_still_set_even_when_notify_is_on_cooldown(self):
        # Docstring contract: "Cooldown applies to push notifications only; state is
        # always updated." - unrelated to this fix, must stay true.
        app = self._app()
        app._last_notify_at = datetime.now(timezone.utc)
        await app._set_alarm([("climate.bedroom_thermostat", 15.0, 18.0)], [])
        app.call_service.assert_any_call(
            "input_boolean/turn_on", entity_id=app.event_active_entity
        )


class ClearButtonPersistsSnoozeUntil(unittest.IsolatedAsyncioTestCase):
    """The 'Recheck in 1 hour' clear path must persist an absolute snooze_until (not just
    the relative run_in) so a restart mid-snooze can recompute the remaining time."""

    def _app(self):
        app = ca.ClimateAlarm.__new__(ca.ClimateAlarm)
        app.event_active_entity = "input_boolean.climate_alarm_active"
        app.message_entity = "input_text.climate_alarm_message"
        app.clear_recheck_sec = 3600
        app.log = lambda *a, **kw: None
        app.call_service = AsyncMock()
        app._snooze_until = None
        app._state = {}
        app._save_state = MagicMock()
        app.run_in = MagicMock(return_value="recheck-handle")
        return app

    async def test_sets_snooze_until_clear_recheck_sec_out_and_saves(self):
        app = self._app()
        before = datetime.now(timezone.utc)
        await app._clear_alarm_state_and_schedule_recheck()
        after = datetime.now(timezone.utc)

        self.assertIsNotNone(app._snooze_until)
        self.assertGreaterEqual(app._snooze_until, before + timedelta(seconds=3599))
        self.assertLessEqual(app._snooze_until, after + timedelta(seconds=3601))
        app._save_state.assert_called_once()
        self.assertEqual(app._clear_recheck_handle, "recheck-handle")


class StateFileRoundTrip(unittest.TestCase):
    """_load_state/_save_state: deploy_advisor's path convention + house_events' atomic
    tmp+replace write (see initialize's restart-survival comment)."""

    def _app(self, path):
        app = ca.ClimateAlarm.__new__(ca.ClimateAlarm)
        app.state_file = path
        app.log = lambda *a, **kw: None
        return app

    def test_missing_file_loads_empty_dict(self):
        app = self._app("/nonexistent/dir/climate_alarm_state.json")
        self.assertEqual(app._load_state(), {})

    def test_save_then_load_round_trips(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app = self._app(path)
        app._last_notify_at = datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)
        app._snooze_until = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)
        app._save_state()

        reloaded = self._app(path)._load_state()
        self.assertEqual(reloaded["last_notify_at"], app._last_notify_at.isoformat())
        self.assertEqual(reloaded["snooze_until"], app._snooze_until.isoformat())

    def test_save_leaves_no_tmp_file_behind(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app = self._app(path)
        app._last_notify_at = None
        app._snooze_until = None
        app._save_state()
        self.assertFalse(os.path.exists(path + ".tmp"))


class ParseDt(unittest.TestCase):
    def test_round_trips_iso_strings(self):
        dt = datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc)
        self.assertEqual(ca.ClimateAlarm._parse_dt(dt.isoformat()), dt)

    def test_none_and_garbage_are_none(self):
        self.assertIsNone(ca.ClimateAlarm._parse_dt(None))
        self.assertIsNone(ca.ClimateAlarm._parse_dt("not-a-date"))


if __name__ == "__main__":
    unittest.main()
