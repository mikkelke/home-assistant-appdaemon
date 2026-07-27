# tests/test_wakeup_restart_survival.py - Reboot-survival fixes for wakeup_bedroom.py
# (2026-07-27): persisted last_fire_date (dedup across an AD restart), late catch-up firing
# the routine or pushing a missed-alarm notice, and the Adaptive Lighting reconcile. Same
# __new__ + monkeypatched-callables harness as the other apps/rutines/tests files (no
# running AppDaemon required).
# Run from repo root: python3 -m unittest discover -s apps/rutines/tests -q

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lights"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds"))

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

import wakeup_bedroom as wb  # noqa: E402

ALARM_TIME = "input_datetime.wakeup_bedroom"
ALARM_ENABLED = "input_boolean.wakeup_bedroom"
BED_SESSION = "input_boolean.bedroom_bed_session"
ADAPTIVE_BRIGHTNESS = "switch.adaptive_lighting_adapt_brightness_bedroom_bed_lights"


def make_app(now, states, *, last_fire_date=None, state=None, ramp_active=False):
    """WakeupRoutine with just the state the restart-survival methods need, without
    running AppDaemon's initialize() - same trick as test_wakeup_heartbeat.py's make_app()."""
    app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
    app.alarm_time_entity = ALARM_TIME
    app.alarm_enabled_entity = ALARM_ENABLED
    app.bed_session_entity = BED_SESSION
    app.adaptive_brightness_switch = ADAPTIVE_BRIGHTNESS
    app.notify_target = ["mikkel"]
    app.user_log = "test_log"
    app.ramp_active = ramp_active
    app.persons = ["person.mikkel"]

    app.datetime = lambda: now
    app.get_state = lambda entity, **kw: states.get((entity, kw.get("attribute")))
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

    app.last_fire_date = last_fire_date
    app._state = dict(state or {})
    app._save_state = MagicMock()

    app.alarm_fired = []
    app._alarm_fire = lambda arg: app.alarm_fired.append(arg)
    app._late_catchup_in_progress = False

    app.turn_on_calls = []
    app.turn_on = lambda entity: app.turn_on_calls.append(entity)

    app.notify_calls = []

    class FakeNotifier:
        def notify(self, **kwargs):
            app.notify_calls.append(kwargs)

    app.get_app = lambda name: FakeNotifier()
    app.create_task = lambda x: x

    # _schedule_daily_alarm scaffolding - only exercised by GraceRefireDedup below
    app.alarm_timer = None
    app.alarm_run_at = None
    app.timer_running = lambda handle: False
    app.cancel_timer = MagicMock()
    app.run_daily = MagicMock(return_value="daily-handle")
    app.run_at = MagicMock(return_value="at-handle")
    app.scheduled = []

    def run_in(cb, delay, **kw):
        app.scheduled.append((cb, delay, kw))
        return object()

    app.run_in = run_in

    def both_away():
        return all(states.get((p, None)) not in ("home", "Home", "present") for p in app.persons)

    app._both_away = both_away
    return app


def _logged(app, needle):
    return any(needle in str(a) for a, kw in app.log_calls)


class GraceRefireDedup(unittest.TestCase):
    """(a) last_fire_date, loaded from the state file at init, must stop
    _schedule_daily_alarm's own 0-120s 'just passed' refire from duplicating a wake-up
    that already fired moments before an AD restart."""

    STATES = {
        (ALARM_ENABLED, None): "on",
        (ALARM_TIME, None): "06:15:00",
        ("person.mikkel", None): "home",
    }

    def test_refires_when_not_yet_fired_today(self):
        now = datetime(2026, 7, 27, 6, 15, 30)  # +30s, inside the 120s grace window
        app = make_app(now, self.STATES, last_fire_date=None)
        app._schedule_daily_alarm()
        self.assertEqual(len(app.scheduled), 1)
        cb, delay, _ = app.scheduled[0]
        self.assertIs(cb, app._alarm_fire)
        self.assertEqual(delay, 1)

    def test_skips_refire_when_already_fired_today(self):
        now = datetime(2026, 7, 27, 6, 15, 30)
        app = make_app(now, self.STATES, last_fire_date=date(2026, 7, 27))
        app._schedule_daily_alarm()
        self.assertEqual(app.scheduled, [])
        self.assertTrue(_logged(app, "already fired today"))

    def test_yesterdays_fire_does_not_block_todays_grace_refire(self):
        now = datetime(2026, 7, 27, 6, 15, 30)
        app = make_app(now, self.STATES, last_fire_date=date(2026, 7, 26))
        app._schedule_daily_alarm()
        self.assertEqual(len(app.scheduled), 1)


class LateCatchupFire(unittest.TestCase):
    """(b) Between the 120s grace window and 30 min, with the alarm still enabled and the
    bed session (the app's existing in-bed gate) still active, init fires the routine
    through _alarm_fire itself - bypassing its +/-90s window via _late_catchup_in_progress."""

    STATES_IN_BED = {
        (ALARM_ENABLED, None): "on",
        (ALARM_TIME, None): "06:15:00",
        (BED_SESSION, None): "on",
    }

    def test_fires_within_window_when_still_in_bed(self):
        now = datetime(2026, 7, 27, 6, 25, 0)  # +600s
        app = make_app(now, self.STATES_IN_BED)
        app._late_catchup_after_restart()
        self.assertEqual(app.alarm_fired, [None])
        self.assertTrue(_logged(app, "Late catch-up after restart"))
        self.assertFalse(app._late_catchup_in_progress)  # cleared after the call

    def test_bypasses_the_trigger_window_guard_while_firing(self):
        # Capture the flag's value *during* the call - this is what lets _alarm_fire's
        # own +/-90s guard let a many-minutes-late fire through.
        now = datetime(2026, 7, 27, 6, 25, 0)
        app = make_app(now, self.STATES_IN_BED)
        captured = []
        app._alarm_fire = lambda arg: captured.append(app._late_catchup_in_progress)
        app._late_catchup_after_restart()
        self.assertEqual(captured, [True])

    def test_does_not_fire_when_bed_session_inactive(self):
        now = datetime(2026, 7, 27, 6, 25, 0)
        states = dict(self.STATES_IN_BED)
        states[(BED_SESSION, None)] = "off"
        app = make_app(now, states)
        app._late_catchup_after_restart()
        self.assertEqual(app.alarm_fired, [])

    def test_does_not_fire_inside_the_existing_120s_grace_window(self):
        # delta=90s is _schedule_daily_alarm's job, not catch-up's.
        now = datetime(2026, 7, 27, 6, 16, 30)
        app = make_app(now, self.STATES_IN_BED)
        app._late_catchup_after_restart()
        self.assertEqual(app.alarm_fired, [])

    def test_does_not_fire_exactly_at_the_120s_boundary(self):
        now = datetime(2026, 7, 27, 6, 17, 0)  # +120s exactly
        app = make_app(now, self.STATES_IN_BED)
        app._late_catchup_after_restart()
        self.assertEqual(app.alarm_fired, [])

    def test_does_not_fire_past_30_minutes(self):
        now = datetime(2026, 7, 27, 6, 46, 0)  # +1860s
        app = make_app(now, self.STATES_IN_BED)
        app._late_catchup_after_restart()
        self.assertEqual(app.alarm_fired, [])

    def test_does_not_fire_when_already_fired_today(self):
        now = datetime(2026, 7, 27, 6, 25, 0)
        app = make_app(now, self.STATES_IN_BED, last_fire_date=date(2026, 7, 27))
        app._late_catchup_after_restart()
        self.assertEqual(app.alarm_fired, [])

    def test_does_not_fire_when_alarm_toggle_off(self):
        now = datetime(2026, 7, 27, 6, 25, 0)
        states = dict(self.STATES_IN_BED)
        states[(ALARM_ENABLED, None)] = "off"
        app = make_app(now, states)
        app._late_catchup_after_restart()
        self.assertEqual(app.alarm_fired, [])


class MissedAlarmPush(unittest.TestCase):
    """(b), second half: past 30 min and still unfired, push exactly once via
    MobileNotifier and remember the date so a second restart the same morning does not
    repeat it."""

    STATES = {
        (ALARM_ENABLED, None): "on",
        (ALARM_TIME, None): "06:15:00",
    }

    def test_pushes_once_past_30_minutes_unfired(self):
        now = datetime(2026, 7, 27, 6, 50, 0)  # +2100s
        app = make_app(now, self.STATES)
        app._late_catchup_after_restart()
        self.assertEqual(len(app.notify_calls), 1)
        self.assertEqual(app.notify_calls[0]["message"], "Wakeup alarm was missed during a restart")
        self.assertEqual(app._state.get("missed_push_date"), "2026-07-27")
        app._save_state.assert_called_once()

    def test_exactly_30_minutes_pushes_not_fires(self):
        now = datetime(2026, 7, 27, 6, 45, 0)  # +1800s exactly
        app = make_app(now, self.STATES)
        app._late_catchup_after_restart()
        self.assertEqual(app.alarm_fired, [])
        self.assertEqual(len(app.notify_calls), 1)

    def test_does_not_push_twice_the_same_day(self):
        now = datetime(2026, 7, 27, 6, 50, 0)
        app = make_app(now, self.STATES, state={"missed_push_date": "2026-07-27"})
        app._late_catchup_after_restart()
        self.assertEqual(app.notify_calls, [])

    def test_pushes_again_on_a_new_day(self):
        now = datetime(2026, 7, 27, 6, 50, 0)
        app = make_app(now, self.STATES, state={"missed_push_date": "2026-07-26"})
        app._late_catchup_after_restart()
        self.assertEqual(len(app.notify_calls), 1)

    def test_does_not_push_when_already_fired_today(self):
        now = datetime(2026, 7, 27, 6, 50, 0)
        app = make_app(now, self.STATES, last_fire_date=date(2026, 7, 27))
        app._late_catchup_after_restart()
        self.assertEqual(app.notify_calls, [])
        self.assertEqual(app.alarm_fired, [])

    def test_does_not_push_when_alarm_disabled(self):
        now = datetime(2026, 7, 27, 6, 50, 0)
        states = dict(self.STATES)
        states[(ALARM_ENABLED, None)] = "off"
        app = make_app(now, states)
        app._late_catchup_after_restart()
        self.assertEqual(app.notify_calls, [])


class AdaptiveLightingReconcile(unittest.TestCase):
    """(c) A restart mid-ramp leaves adaptive_brightness_switch off with no restore path
    left to run. Init should turn it back on, but only when nothing is using it."""

    def _app(self, switch_state, ramp_active):
        states = {(ADAPTIVE_BRIGHTNESS, None): switch_state}
        return make_app(datetime(2026, 7, 27, 6, 20, 0), states, ramp_active=ramp_active)

    def test_turns_on_when_off_and_no_ramp_running(self):
        app = self._app("off", ramp_active=False)
        app._reconcile_adaptive_lighting_after_restart()
        self.assertEqual(app.turn_on_calls, [ADAPTIVE_BRIGHTNESS])
        self.assertTrue(_logged(app, "restoring it"))

    def test_leaves_alone_when_already_on(self):
        app = self._app("on", ramp_active=False)
        app._reconcile_adaptive_lighting_after_restart()
        self.assertEqual(app.turn_on_calls, [])

    def test_leaves_alone_when_a_ramp_is_active(self):
        # A late catch-up (or anything else) already running its own ramp must not be
        # fought - see initialize()'s call order (reconcile before catch-up).
        app = self._app("off", ramp_active=True)
        app._reconcile_adaptive_lighting_after_restart()
        self.assertEqual(app.turn_on_calls, [])


class StateFileRoundTrip(unittest.TestCase):
    """_load_state/_save_state: deploy_advisor's path convention + house_events' atomic
    tmp+replace write (see initialize's restart-survival comment)."""

    def _app(self, path):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        app.state_file = path
        app.user_log = "test_log"
        app.log = lambda *a, **kw: None
        return app

    def test_missing_file_loads_empty_dict(self):
        app = self._app("/nonexistent/dir/wakeup_bedroom_state.json")
        self.assertEqual(app._load_state(), {})

    def test_save_then_load_round_trips(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app = self._app(path)
        app._state = {"last_fire_date": "2026-07-27", "last_fire_at": "2026-07-27T06:15:01"}
        app._save_state()

        reloaded = self._app(path)._load_state()
        self.assertEqual(reloaded, app._state)

    def test_save_leaves_no_tmp_file_behind(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app = self._app(path)
        app._state = {"last_fire_date": "2026-07-27"}
        app._save_state()
        self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
