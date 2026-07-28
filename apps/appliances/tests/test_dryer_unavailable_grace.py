# tests/test_dryer_unavailable_grace.py - Reboot-survival fixes for the dryer monitor.
# Run from repo root: python3 -m unittest appdaemon.apps.appliances.tests.test_dryer_unavailable_grace
#
# Ported from test_washer_unavailable_grace.py's harness (WasherMonitor.__new__-style
# instantiation, without running AppDaemon's initialize()). Two behaviors diverge from the
# washer on purpose (see dryer_monitor.py comments at the relevant call sites):
#   - The dryer's own state entity going unavailable is log-only (dishwasher_monitor.py's
#     policy for its own state entity), never routed into the forced-Off grace.
#   - initialize()'s restore now re-arms the watchdogs the audit found permanently disarmed
#     across a restart: the 5h running watchdog (Running and Paused), the pause timeout
#     (Paused), and the 24h Unemptied auto-clear watchdog (new Unemptied restore branch).

from __future__ import annotations

import sys
import types
import unittest
from datetime import timedelta
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

import dryer_monitor as dm  # noqa: E402


def make_app(power_unavailable_off_after_seconds=180, timer_running=False):
    """DryerMonitor with fake get_state/log/run_in/timer_running/cancel_timer, without running
    AppDaemon's initialize(). Mirrors test_washer_unavailable_grace.py's make_app."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)
    app.power_sensor = "sensor.dryer_plug_power"
    app.state_entity = "sensor.dryer_state"
    app.power_unavailable_off_after_seconds = power_unavailable_off_after_seconds
    app.power_unavailable_off_timer = None
    # Dead-plug phone-page watchdog rides the same unavailable path; the grace and the page
    # are separate timers with separate thresholds.
    app.plug_outage_push_after_seconds = 180
    app._plug_outage_push_timer = None
    app._plug_outage_pushed = False
    # Attributes _power_changed reads unconditionally before any Running-only branch.
    app.main_cycle_power = 400.0
    app.start_w = 8.0
    app.stop_w = 5.0
    app.high_power_counter = 0
    app.door_fast_start_armed_until = None
    app.start_confirm_timer = None
    app.run_for = 60
    app.door_close_fast_confirm_s = 12
    app.low_power_timer = None
    app.pattern_check_timer = None
    app.keep_fresh_detected = False
    app.power_reading_interval = 15
    app.pattern_window = 10

    app.state = "Off"
    app.states = {}
    app.log_calls = []
    app.set_state_calls = []
    app.reset_calls = []
    app.scheduled = []
    app.canceled_timers = []
    app._timer_running = timer_running

    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.timer_running = lambda handle: app._timer_running
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)
    app._reset_programme_selectors_to_unconfirmed = lambda: app.reset_calls.append("selectors")
    app._reset_cycle_tracking = lambda: app.reset_calls.append("cycle_tracking")

    def set_state_entity(**kw):
        app.set_state_calls.append(kw)
        if "state" in kw:
            app.states[app.state_entity] = kw["state"]

    app._set_state_entity = set_state_entity

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    return app


def scheduled_callbacks(app):
    return [cb for cb, _delay, _kw in app.scheduled]


def scheduled_delay_for(app, cb):
    matches = [d for c, d, _kw in app.scheduled if c == cb]
    return matches[0] if matches else None


class UnavailableStartsGraceInsteadOfForcingOff(unittest.TestCase):
    """A plug blip must not force-wipe an in-progress cycle: the wipe is now scheduled behind
    power_unavailable_off_after_seconds instead of running immediately."""

    def test_unavailable_schedules_grace_timer_without_wiping(self):
        app = make_app()
        app.state = "Running"  # a cycle is in progress - must survive the blip untouched
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        self.assertIsNotNone(app.power_unavailable_off_timer)
        self.assertEqual(app.set_state_calls, [])
        self.assertEqual(app.reset_calls, [])
        self.assertEqual(app.state, "Running")  # untouched - no instant wipe
        grace = [(cb, d) for cb, d, _kw in app.scheduled if cb == app._power_unavailable_off_timeout]
        self.assertEqual(grace, [(app._power_unavailable_off_timeout, app.power_unavailable_off_after_seconds)])
        # The dead-plug page watchdog rides the same event - both arm, neither wipes.
        self.assertIn(app._plug_outage_push_timeout, scheduled_callbacks(app))

    def test_second_power_sensor_unavailable_event_while_timer_running_does_not_reschedule(self):
        """Covers the double-invocation dedup: the dedicated 'unavailable' listen_state on
        power_sensor AND the unavailable branch of _power_changed both call this."""
        app = make_app()
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        before = len(app.scheduled)

        app._timer_running = True  # simulate the grace timer still running
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        self.assertEqual(len(app.scheduled), before)


class StateEntityUnavailableIsLogOnly(unittest.TestCase):
    """The dryer's own state entity dropping out is a UI-visibility problem, not a
    cycle-tracking one (dishwasher_monitor.py's policy for its own state entity) - never
    schedule the forced-Off grace or touch cycle state for it."""

    def test_state_entity_unavailable_logs_and_returns(self):
        app = make_app()
        app._handle_unavailable(app.state_entity, None, None, "unavailable", {})
        self.assertEqual(app.scheduled, [])
        self.assertIsNone(app.power_unavailable_off_timer)
        self.assertEqual(app.set_state_calls, [])
        self.assertEqual(app.reset_calls, [])
        self.assertTrue(app.log_calls)
        last_args, last_kwargs = app.log_calls[-1]
        self.assertEqual(last_kwargs.get("level"), "WARNING")
        self.assertIn(app.state_entity, str(last_args[0]))

    def test_state_entity_unavailable_does_not_disturb_a_pending_power_grace(self):
        app = make_app()
        app._handle_unavailable(app.power_sensor, None, None, "unavailable", {})
        pending = app.power_unavailable_off_timer
        before = len(app.scheduled)

        app._handle_unavailable(app.state_entity, None, None, "unavailable", {})
        self.assertEqual(app.power_unavailable_off_timer, pending)
        self.assertEqual(len(app.scheduled), before)
        self.assertEqual(app.canceled_timers, [])


class UnavailableGraceTimeout(unittest.TestCase):
    """Timeout re-checks the power sensor before wiping the cycle, so a recovery landing right
    at the deadline is not mistaken for a real outage."""

    def test_still_unavailable_wipes_cycle_state(self):
        app = make_app(power_unavailable_off_after_seconds=180)
        app.power_unavailable_off_timer = "handle-1"
        app.states[app.power_sensor] = "unavailable"
        app._power_unavailable_off_timeout({})
        self.assertEqual(app.state, "Off")
        self.assertIn({"state": "Off"}, app.set_state_calls)
        self.assertEqual(app.reset_calls, ["selectors", "cycle_tracking"])
        self.assertIsNone(app.power_unavailable_off_timer)

    def test_recovered_before_timeout_does_not_wipe(self):
        app = make_app()
        app.power_unavailable_off_timer = "handle-1"
        app.states[app.power_sensor] = "120.5"
        app._power_unavailable_off_timeout({})
        self.assertEqual(app.set_state_calls, [])
        self.assertEqual(app.reset_calls, [])
        self.assertIsNone(app.power_unavailable_off_timer)


class PowerChangedCancelsGrace(unittest.TestCase):
    """A numeric power reading proves the plug is back, so the pending forced-Off must be
    cancelled - mirrors washer_monitor.py's _power_changed calling
    _cancel_power_unavailable_grace right after the successful float(new) parse."""

    def test_numeric_reading_cancels_pending_grace_timer(self):
        app = make_app(timer_running=True)
        app.power_unavailable_off_timer = "handle-1"
        app.states[app.state_entity] = "Off"
        app._power_changed(app.power_sensor, "state", "unavailable", "0.0", {})
        self.assertIsNone(app.power_unavailable_off_timer)
        self.assertIn("handle-1", app.canceled_timers)


def make_restore_app(
    state,
    attrs,
    last_changed=None,
    max_running_hours=5,
    pause_timeout_minutes=10,
    unemptied_timeout_hours=24,
):
    """DryerMonitor with just enough stubbed to exercise _restore_running_state /
    _restore_unemptied_state directly, without running the rest of initialize()."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)
    app.state_entity = "sensor.dryer_state"
    app.state = state
    app.max_running_hours = max_running_hours
    app.pause_timeout_minutes = pause_timeout_minutes
    app.unemptied_timeout_hours = unemptied_timeout_hours

    app.start_time = None
    app.energy_start = None
    app.detected_programme = "unknown"
    app.expected_dur_at_start = None
    app.poll_timer = None
    app.classify_timer = None
    app.running_watchdog_timer = None
    app.unemptied_watchdog_timer = None
    app.pause_timer = None

    entity_all = {"attributes": dict(attrs), "last_changed": last_changed}
    app.get_state = lambda entity, **kw: entity_all if kw.get("attribute") == "all" else None

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

    app.scheduled = []
    app.canceled_timers = []
    app._timer_running = False
    app.timer_running = lambda handle: app._timer_running
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    # Out of scope for these restore-timer tests (exercised separately by test_dryer_overrun.py
    # and the classification/ETA suites) - stub to a no-op so it does not need a full fake state
    # entity / selector wiring.
    app._update_running_attributes = lambda: None
    return app


class RestoreReArmsWatchdogs(unittest.TestCase):
    """initialize() used to leave every watchdog disarmed after a restore: the 5h running
    watchdog was only ever armed in _confirm_running, a restored Paused never got its pause
    timeout back, and there was no Unemptied restore branch at all (24h auto-clear lost across
    every deploy). All three are re-armed here, from the time remaining rather than a fresh
    full period, with a floor so a timer is never scheduled with ~0 delay."""

    def test_running_restore_rearms_running_watchdog_with_remaining_time(self):
        app = make_restore_app("Running", {}, max_running_hours=5)
        now = app._now_utc()
        cycle_start = now - timedelta(minutes=60)
        attrs = {
            "cycle_start_time": app._format_utc(cycle_start),
            "energy_at_start": "0.10",
            "detected_programme": "bomuld__skabstoert",
            "programme_duration_min": "140",
        }
        app.get_state = lambda entity, **kw: (
            {"attributes": attrs, "last_changed": None} if kw.get("attribute") == "all" else None
        )
        app._restore_running_state()
        delay = scheduled_delay_for(app, app._running_watchdog_timeout)
        self.assertIsNotNone(delay)
        # 5h max - 60min already elapsed = 4h remaining.
        self.assertAlmostEqual(delay, 4 * 3600, delta=5)
        # Running restore must not touch the (Paused-only) pause timer.
        self.assertIsNone(scheduled_delay_for(app, app._pause_timeout))

    def test_running_restore_floors_at_60s_when_already_overdue(self):
        app = make_restore_app("Running", {}, max_running_hours=5)
        now = app._now_utc()
        cycle_start = now - timedelta(hours=6)  # already past the 5h cap
        attrs = {
            "cycle_start_time": app._format_utc(cycle_start),
            "programme_duration_min": "140",
        }
        app.get_state = lambda entity, **kw: (
            {"attributes": attrs, "last_changed": None} if kw.get("attribute") == "all" else None
        )
        app._restore_running_state()
        delay = scheduled_delay_for(app, app._running_watchdog_timeout)
        self.assertEqual(delay, 60)

    def test_paused_restore_rearms_running_watchdog_and_pause_timeout(self):
        """The 5h running watchdog spans Running AND any Paused time within it (never
        cancelled on pause - see _transition_to_paused), so Paused restore re-arms it too, on
        top of its own pause timeout."""
        app = make_restore_app("Paused", {}, max_running_hours=5, pause_timeout_minutes=10)
        now = app._now_utc()
        cycle_start = now - timedelta(hours=1)
        paused_since = now - timedelta(minutes=5)
        attrs = {
            "cycle_start_time": app._format_utc(cycle_start),
            "programme_duration_min": "140",
        }
        app.get_state = lambda entity, **kw: (
            {"attributes": attrs, "last_changed": app._format_utc(paused_since)}
            if kw.get("attribute") == "all" else None
        )
        app._restore_running_state()
        running_delay = scheduled_delay_for(app, app._running_watchdog_timeout)
        pause_delay = scheduled_delay_for(app, app._pause_timeout)
        self.assertIsNotNone(running_delay)
        self.assertAlmostEqual(running_delay, 4 * 3600, delta=5)  # 5h - 1h elapsed
        self.assertIsNotNone(pause_delay)
        self.assertAlmostEqual(pause_delay, 5 * 60, delta=5)  # 10min - 5min elapsed

    def test_paused_restore_arms_full_pause_timeout_when_last_changed_missing(self):
        app = make_restore_app("Paused", {}, pause_timeout_minutes=10)
        attrs = {
            "cycle_start_time": app._format_utc(app._now_utc()),
            "programme_duration_min": "140",
        }
        app.get_state = lambda entity, **kw: (
            {"attributes": attrs, "last_changed": None} if kw.get("attribute") == "all" else None
        )
        app._restore_running_state()
        delay = scheduled_delay_for(app, app._pause_timeout)
        self.assertEqual(delay, 10 * 60)

    def test_unemptied_restore_rearms_watchdog_with_remaining_time(self):
        app = make_restore_app("Unemptied", {}, unemptied_timeout_hours=24)
        now = app._now_utc()
        unemptied_since = now - timedelta(hours=2)
        app.get_state = lambda entity, **kw: (
            {"attributes": {}, "last_changed": app._format_utc(unemptied_since)}
            if kw.get("attribute") == "all" else None
        )
        app._restore_unemptied_state()
        delay = scheduled_delay_for(app, app._unemptied_watchdog_timeout)
        self.assertIsNotNone(delay)
        # 24h - 2h already elapsed = 22h remaining.
        self.assertAlmostEqual(delay, 22 * 3600, delta=5)

    def test_unemptied_restore_arms_full_period_when_last_changed_missing(self):
        app = make_restore_app("Unemptied", {}, unemptied_timeout_hours=24)
        app.get_state = lambda entity, **kw: (
            {"attributes": {}, "last_changed": None} if kw.get("attribute") == "all" else None
        )
        app._restore_unemptied_state()
        delay = scheduled_delay_for(app, app._unemptied_watchdog_timeout)
        self.assertEqual(delay, 24 * 3600)
        debug_logs = [a for a, kw in app.log_calls if kw.get("level") == "DEBUG"]
        self.assertTrue(any("unemptied watchdog" in str(a[0]).lower() for a in debug_logs))


def make_full_init_app(sensor_state, helper_state, ui_state_entity="input_select.dryer_state"):
    """DryerMonitor with initialize() run for real (heavier than make_app/make_restore_app
    above, but the sensor-missing -> ui_state_select seed fallback lives inline at the top of
    initialize(), before any restore is dispatched, so there is no smaller real entry point to
    call directly). feedback_file/programmes_file point at nonexistent paths so initialize()
    falls back to built-in defaults instead of touching real repo data files."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)
    app.args = {
        "power_sensor": "sensor.dryer_plug_power",
        "energy_sensor": "sensor.dryer_plug_energy",
        "door_sensor": "binary_sensor.dryer_door_contact",
        "state_entity": "sensor.dryer_state",
        "ui_state_entity": ui_state_entity,
        "start_w": 8,
        "stop_w": 5,
        "run_for": 60,
        "stop_for": 60,
        "feedback_file": "/nonexistent/dryer_feedback_test.json",
        "programmes_file": "/nonexistent/dryer_programmes_test.yaml",
    }
    app.states = {
        "sensor.dryer_state": sensor_state,
        # Keeps bootstrap on the simple plug-outage-grace path (avoids pulling in _power_changed).
        "sensor.dryer_plug_power": "unavailable",
    }
    if ui_state_entity:
        app.states[ui_state_entity] = helper_state

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.get_app = lambda name: None
    app.listen_state = lambda *a, **kw: None
    app.call_service = lambda *a, **kw: None
    app.timer_running = lambda handle: False

    app.scheduled = []

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in

    app.restore_calls = []
    app._restore_running_state = lambda: app.restore_calls.append("running")
    app._restore_unemptied_state = lambda: app.restore_calls.append("unemptied")

    app.set_state_calls = []

    def set_state_entity(**kw):
        app.set_state_calls.append(kw)
        if "state" in kw:
            app.states[app.state_entity] = kw["state"]

    app._set_state_entity = set_state_entity
    return app


class StateHelperSeedFallback(unittest.TestCase):
    """HA restarts erase the AD-store-backed state sensor but not the input_select UI mirror
    (survives via HA's own restore_state); initialize() falls back to it when the sensor itself
    reads None/unknown/unavailable, so a helper-seeded state still reaches the SAME restore
    branch a real sensor read would. washer_monitor.py and dishwasher_monitor.py carry the
    identical fallback (not separately covered here)."""

    def test_missing_sensor_does_not_seed_running_from_helper(self):
        """Running/Paused are NOT seedable: cycle_start_time lived in the erased sensor's
        attributes, so a seeded Running would have start_time None - and the watchdog re-arm and
        finish detection are both gated on start_time, which left the dryer in Running forever
        (run_min 0 blocks the 80% guard on every poll). Off is the older, working behaviour:
        _power_changed re-detects a dryer that is genuinely still running."""
        app = make_full_init_app(sensor_state=None, helper_state="Running")
        app.initialize()
        self.assertEqual(app.state, "Off")
        self.assertEqual(app.restore_calls, [])
        skipped = [
            a for a, kw in app.log_calls
            if kw.get("level") == "INFO" and "not seeding" in str(a[0]).lower()
        ]
        self.assertTrue(skipped, "expected an INFO explaining why the Running mirror was not seeded")
        self.assertIn("input_select.dryer_state", str(skipped[0][0]))

    def test_missing_sensor_seeds_unemptied_from_helper_and_invokes_restore(self):
        app = make_full_init_app(sensor_state="unavailable", helper_state="Unemptied")
        app.initialize()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.restore_calls, ["unemptied"])

    def test_present_sensor_ignores_helper(self):
        """Sensor already has a valid state - the helper (even a differing one) must never
        override it."""
        app = make_full_init_app(sensor_state="Paused", helper_state="Running")
        app.initialize()
        self.assertEqual(app.state, "Paused")
        seed_logs = [a for a, kw in app.log_calls if "seeded" in str(a[0]).lower()]
        self.assertEqual(seed_logs, [])

    def test_missing_sensor_and_invalid_helper_falls_back_to_off(self):
        app = make_full_init_app(sensor_state="unknown", helper_state="unknown")
        app.initialize()
        self.assertEqual(app.state, "Off")
        self.assertEqual(app.restore_calls, [])
        seed_logs = [a for a, kw in app.log_calls if "seeded" in str(a[0]).lower()]
        self.assertEqual(seed_logs, [])


if __name__ == "__main__":
    unittest.main()


class StateHelperSeedIsClockFreeOnly(unittest.TestCase):
    """The mirror may only reseed states that do not depend on cycle_start_time.

    4086e2e seeded Running/Paused too; because initialize() republishes the erased sensor with no
    attributes BEFORE the restore branch reads cycle_start_time back off it, start_time stayed None
    and every timer that could end the cycle is gated on start_time - the machine sat in Running
    forever with no announcement and no feedback record. Seeding only the clock-free states makes
    that unreachable by construction.
    """

    def test_paused_mirror_is_not_seeded(self):
        app = make_full_init_app(sensor_state=None, helper_state="Paused")
        app.initialize()
        self.assertEqual(app.state, "Off")
        self.assertEqual(app.restore_calls, [])

    def test_emptied_mirror_is_still_seeded(self):
        app = make_full_init_app(sensor_state=None, helper_state="Emptied")
        app.initialize()
        self.assertEqual(app.state, "Emptied")

    def test_unemptied_mirror_still_reaches_its_restore_branch(self):
        """The case the mirror exists for: the empty-me reminder survives an HA restart."""
        app = make_full_init_app(sensor_state=None, helper_state="Unemptied")
        app.initialize()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.restore_calls, ["unemptied"])

    def test_unseedable_mirror_never_writes_the_helper_back(self):
        """A rejected seed must stay read-only: writing Off back through _sync_ui_select would
        destroy the only surviving evidence of the cycle."""
        app = make_full_init_app(sensor_state=None, helper_state="Running")
        app.initialize()
        published = [kw.get("state") for kw in app.set_state_calls if "state" in kw]
        self.assertNotIn("Running", published)
        self.assertEqual(app.states.get("input_select.dryer_state"), "Running")
