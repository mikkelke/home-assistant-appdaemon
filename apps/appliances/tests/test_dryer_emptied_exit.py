# tests/test_dryer_emptied_exit.py - Emptied-state cooling-period bypass, plus the Emptied
# watchdog that backstops a missed door-close edge.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# The incident: _transition_to_emptied stamps last_state_change and force-transitions into
# Emptied (a door event bypasses cooling by design). The door-close that follows - almost always
# the same physical event, seconds later, since the user is standing right there - used to call
# _transition_to_off WITHOUT force, so _should_change_state's 600s cooling_period silently
# refused it 52 of 92 times since January. The dryer then sat in Emptied forever: there was no
# watchdog, and start detection is gated on current_state == "Off", so the NEXT cycle could not
# even be detected.
#
# Two fixes, both exercised here against the REAL implementation (see below for why):
#   1. _handle_door_closed's Emptied branch now calls _transition_to_off(..., force=True) - a
#      door event is discrete and physical, never what cooling_period exists to protect against.
#   2. A new Emptied watchdog (mirrors Unemptied's) so Emptied can never be terminal even if a
#      door-close edge is missed entirely: armed in _transition_to_emptied, cancelled inside
#      _reset_cycle_tracking (only once a transition to Off has actually landed - cancelling a
#      backstop before the transition it backs has succeeded is the exact shape of the 34h
#      Paused wedge documented in test_dryer_pause_exit.py), and re-armed on AppDaemon restart
#      by _restore_emptied_state.
#
# Per this repo's incident history, stubbing out the very function whose interaction with its
# caller IS the bug would hide it again - so every test below calls the real _handle_door_closed,
# real _transition_to_emptied, real _transition_to_off, real _emptied_watchdog_timeout, real
# _restore_emptied_state and, deliberately unlike test_dryer_pause_exit.py, the real
# _reset_cycle_tracking (the emptied-watchdog cancel this suite exists to prove lives inside
# it). Only the AppDaemon I/O boundary (get_state / set_state / run_in / get_history / log /
# timer_running / cancel_timer) is faked, plus _reset_programme_selectors_to_unconfirmed (it
# makes service calls) - following test_dryer_unavailable_grace.py's harness style.

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


def make_app():
    """DryerMonitor with fake get_state/log/run_in/get_history/timer_running/cancel_timer,
    without running AppDaemon's initialize(). Ported from test_dryer_pause_exit.py's make_app,
    extended with the Emptied watchdog's attributes, with two deliberate departures from that
    harness:

    - _reset_cycle_tracking is NOT stubbed here. It is where the emptied-watchdog cancel this
      suite exists to prove actually lives, and test_dryer_pause_exit.py stubbing it out is
      exactly the "stub the function whose interaction with its caller IS the bug" mistake this
      repo has a documented history of shipping. The extra attributes below
      (door_opened_during_cycle, expected_dur_at_start, power_readings) exist only because the
      real _reset_cycle_tracking touches them.
    - timer_running defaults to True, so _safe_cancel_timer actually calls cancel_timer (and
      records it) instead of silently no-opping - needed to assert on real cancellations.
    """
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)

    app.power_sensor = "sensor.dryer_plug_power"
    app.energy_sensor = "sensor.dryer_plug_energy"
    app.door_sensor = "binary_sensor.dryer_door_contact"
    app.state_entity = "sensor.dryer_state"

    app.start_w = 8.0
    app.stop_w = 5.0
    app.run_for = 60
    app.door_close_fast_confirm_s = 12
    app.door_close_fast_start_window_s = 600
    app.main_cycle_power = 400.0
    app.min_cycle_minutes = 60
    app.min_energy_kwh = 0.05
    app.cooling_period = 600
    app.unemptied_timeout_hours = 24.0
    app.emptied_timeout_minutes = 30.0

    app.state = "Off"
    app.states = {}
    app.last_state_change = None
    app.start_time = None
    app.energy_start = None
    app.detected_programme = "unknown"
    app.keep_fresh_detected = False
    app.notification_sent = False
    app.max_power_w = 0.0
    app.high_power_counter = 0
    app.power_readings = []
    app.expected_dur_at_start = None
    app.door_opened_during_cycle = False

    app.poll_timer = None
    app.pause_timer = None
    app.low_power_timer = None
    app.pattern_check_timer = None
    app.classify_timer = None
    app.running_watchdog_timer = None
    app.unemptied_watchdog_timer = None
    app.emptied_watchdog_timer = None
    app.start_confirm_timer = None
    app.door_opened_time = None
    app.door_fast_start_armed_until = None
    app.power_unavailable_off_timer = None
    app._plug_outage_push_timer = None
    app._plug_outage_pushed = False

    # No programme selectors configured -> _get_confirmed_programme_key() short-circuits to
    # None, and no Sonos/announce plumbing -> _transition_to_emptied's toggle-reset block is
    # skipped.
    app.programme_entity = None
    app.announce_entity = None
    app.sonos_notifier = None

    # Directory does not exist, so _save_cycle_feedback's write fails safely (caught + logged)
    # without touching real repo data - same trick as test_dryer_unavailable_grace.py's
    # make_full_init_app.
    app.feedback_file = "/nonexistent/dryer_feedback_test.json"

    app.log_calls = []
    app.set_state_calls = []
    app.reset_calls = []
    app.scheduled = []
    app.canceled_timers = []
    app._timer_running = True  # _safe_cancel_timer must actually cancel and record it

    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.get_history = lambda **kw: []  # no history -> _correct_duration falls back to wall-clock
    app.timer_running = lambda handle: app._timer_running
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)
    app._reset_programme_selectors_to_unconfirmed = lambda: app.reset_calls.append("selectors")
    # _reset_cycle_tracking is deliberately left real - see the make_app docstring above.

    def set_state_entity(**kw):
        app.set_state_calls.append(kw)
        if "state" in kw:
            app.states[app.state_entity] = kw["state"]

    app._set_state_entity = set_state_entity

    def run_in(cb, delay, **kw):
        # Counter-tagged, callback-named handle (not a bare object()) so assertion failures are
        # readable and tests can tell two scheduled timers apart by identity.
        idx = len(app.scheduled)
        handle = f"timer#{idx}:{getattr(cb, '__name__', cb)}"
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    return app


def make_emptied_app():
    """App already in Emptied, with last_state_change stamped to right now - deep inside the
    600s cooling window. This is exactly the condition _transition_to_emptied leaves behind (it
    force-stamps the cooling clock for its own transition), so every test built on this helper
    reproduces the shape of the incident: an immediately-following event landing inside a
    cooling window that was never meant to gate it."""
    app = make_app()
    app.states[app.state_entity] = "Emptied"
    app.state = "Emptied"
    app.last_state_change = app._now_utc()
    return app


def make_restore_app(emptied_timeout_minutes=30.0):
    """DryerMonitor with just enough stubbed to exercise _restore_emptied_state directly,
    without running the rest of initialize(). Scoped mirror of
    test_dryer_unavailable_grace.py's make_restore_app, for the Emptied watchdog only."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)
    app.state_entity = "sensor.dryer_state"
    app.emptied_timeout_minutes = emptied_timeout_minutes
    app.emptied_watchdog_timer = None

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

    app.scheduled = []
    app.canceled_timers = []
    app._timer_running = True
    app.timer_running = lambda handle: app._timer_running
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)

    def run_in(cb, delay, **kw):
        idx = len(app.scheduled)
        handle = f"timer#{idx}:{getattr(cb, '__name__', cb)}"
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    return app


def scheduled_callbacks(app):
    return [cb for cb, _delay, _kw in app.scheduled]


def scheduled_delay_for(app, cb):
    matches = [d for c, d, _kw in app.scheduled if c == cb]
    return matches[0] if matches else None


class DoorCloseFromEmptiedBypassesCoolingPeriod(unittest.TestCase):
    """The core regression: _transition_to_emptied force-stamps last_state_change on its way
    into Emptied, so the door-close that follows - almost always the same event, seconds
    later - must not be refused by _should_change_state's cooling check. Without force=True on
    that call, this was silently swallowed 52 of 92 times since January, leaving the dryer stuck
    in Emptied with the next cycle undetectable (start detection requires current_state ==
    "Off")."""

    def test_door_close_from_emptied_transitions_to_off_despite_cooling_period(self):
        """Direct regression test: fails without force=True on the _handle_door_closed side."""
        app = make_emptied_app()
        app._handle_door_closed("Emptied")
        self.assertEqual(app.state, "Off")
        self.assertEqual(app.states[app.state_entity], "Off")

    def test_transition_to_emptied_then_immediate_door_close_lands_off(self):
        """The true shape of the bug: both the cooling-clock stamp and the door-close come from
        the same real code path, back to back, rather than a synthetic last_state_change."""
        app = make_app()
        app.states[app.state_entity] = "Unemptied"
        app.state = "Unemptied"
        app._transition_to_emptied("Door opened - emptying")
        self.assertEqual(app.state, "Emptied")

        app._handle_door_closed("Emptied")
        self.assertEqual(app.state, "Off")

    def test_door_close_from_emptied_still_arms_fast_start_window(self):
        """Guards the statement ordering after the _transition_to_off call: the
        door_fast_start_armed_until assignment must still run on the same door-close, not become
        unreachable if that call is ever wrapped in an early return."""
        app = make_emptied_app()
        app.door_fast_start_armed_until = None
        before = app._now_utc()
        app._handle_door_closed("Emptied")
        self.assertEqual(app.state, "Off")
        self.assertIsNotNone(app.door_fast_start_armed_until)
        self.assertGreaterEqual(
            app.door_fast_start_armed_until,
            before + timedelta(seconds=app.door_close_fast_start_window_s),
        )
        self.assertLessEqual(
            app.door_fast_start_armed_until,
            before + timedelta(seconds=app.door_close_fast_start_window_s, minutes=1),
        )


class EmptiedWatchdogArmAndDisarm(unittest.TestCase):
    """The Emptied watchdog is armed the moment Emptied is entered (mirrors Unemptied's) and
    cancelled the moment a door-close actually lands Off - so it is never a stale, dangling
    handle once the state it backs has moved on."""

    def test_transition_to_emptied_arms_emptied_watchdog(self):
        app = make_app()
        app.states[app.state_entity] = "Unemptied"
        app.state = "Unemptied"
        app._transition_to_emptied("Door opened - emptying")
        self.assertIsNotNone(app.emptied_watchdog_timer)
        delay = scheduled_delay_for(app, app._emptied_watchdog_timeout)
        self.assertEqual(delay, 1800)  # 30 min default

    def test_door_close_from_emptied_cancels_the_watchdog(self):
        app = make_app()
        app.states[app.state_entity] = "Unemptied"
        app.state = "Unemptied"
        app._transition_to_emptied("Door opened - emptying")
        handle = app.emptied_watchdog_timer
        self.assertIsNotNone(handle)

        app._handle_door_closed("Emptied")
        self.assertIsNone(app.emptied_watchdog_timer)
        self.assertIn(handle, app.canceled_timers)


class EmptiedWatchdogTimeout(unittest.TestCase):
    """The backstop for a door-close that is never seen at all (missed Zigbee edge, or the door
    left open to air the drum). force=True here for the same reason as the door-close path: a
    backstop whose own cooling period can swallow it is not a backstop."""

    def test_watchdog_forces_off_while_still_emptied_inside_cooling_period(self):
        """Proves force=True: last_state_change is stamped to right now, deep inside the 600s
        window, and the watchdog must still win."""
        app = make_emptied_app()
        app.emptied_watchdog_timer = "timer#pending-emptied"
        app._emptied_watchdog_timeout({})
        self.assertEqual(app.state, "Off")
        self.assertIsNone(app.emptied_watchdog_timer)
        warnings = [a for a, kw in app.log_calls if kw.get("level") == "WARNING"]
        self.assertTrue(any("WATCHDOG" in str(a[0]) for a in warnings))

    def test_watchdog_is_a_noop_when_state_already_moved_on(self):
        app = make_app()
        app.states[app.state_entity] = "Running"
        app.state = "Running"
        app.emptied_watchdog_timer = "timer#pending-emptied"
        app._emptied_watchdog_timeout({})
        self.assertEqual(app.state, "Running")
        self.assertEqual(app.set_state_calls, [])
        self.assertEqual(app.reset_calls, [])
        self.assertIsNone(app.emptied_watchdog_timer)

    def test_watchdog_clears_its_own_handle_before_transitioning(self):
        """If the handle were cleared AFTER _transition_to_off instead of before, the real
        _reset_cycle_tracking (not stubbed in this file) would see its own still-set handle and
        pass it to cancel_timer while it is still firing - exactly the shape of the 34h Paused
        wedge (see the module docstring)."""
        app = make_emptied_app()
        firing_handle = "timer#currently-firing"
        app.emptied_watchdog_timer = firing_handle
        app._emptied_watchdog_timeout({})
        self.assertEqual(app.state, "Off")
        self.assertNotIn(firing_handle, app.canceled_timers)


class RestoreEmptiedState(unittest.TestCase):
    """After an AppDaemon restart (every deploy), initialize() must re-arm the Emptied watchdog
    from the time Emptied actually began, not from a fresh full period - otherwise a deploy
    landing while Emptied would extend, not preserve, the original deadline. Mirrors
    test_dryer_unavailable_grace.py's Unemptied restore coverage."""

    def test_rearms_with_elapsed_time_subtracted(self):
        app = make_restore_app(emptied_timeout_minutes=30)
        now = app._now_utc()
        emptied_since = now - timedelta(minutes=10)
        app.get_state = lambda entity, **kw: (
            {"attributes": {}, "last_changed": app._format_utc(emptied_since)}
            if kw.get("attribute") == "all" else None
        )
        app._restore_emptied_state()
        delay = scheduled_delay_for(app, app._emptied_watchdog_timeout)
        self.assertIsNotNone(delay)
        # 30 min - 10 min already elapsed = 20 min remaining.
        self.assertAlmostEqual(delay, 20 * 60, delta=5)

    def test_arms_full_period_when_last_changed_missing(self):
        app = make_restore_app(emptied_timeout_minutes=30)
        app.get_state = lambda entity, **kw: (
            {"attributes": {}, "last_changed": None} if kw.get("attribute") == "all" else None
        )
        app._restore_emptied_state()
        delay = scheduled_delay_for(app, app._emptied_watchdog_timeout)
        self.assertEqual(delay, 30 * 60)
        debug_logs = [a for a, kw in app.log_calls if kw.get("level") == "DEBUG"]
        self.assertTrue(any("emptied watchdog" in str(a[0]).lower() for a in debug_logs))

    def test_floors_at_60s_when_the_whole_period_already_lapsed(self):
        app = make_restore_app(emptied_timeout_minutes=30)
        now = app._now_utc()
        emptied_since = now - timedelta(hours=5)  # AppDaemon was down well past the 30 min window
        app.get_state = lambda entity, **kw: (
            {"attributes": {}, "last_changed": app._format_utc(emptied_since)}
            if kw.get("attribute") == "all" else None
        )
        app._restore_emptied_state()
        delay = scheduled_delay_for(app, app._emptied_watchdog_timeout)
        self.assertEqual(delay, 60)


class EmptiedIsNotTerminalForTheNextCycle(unittest.TestCase):
    """Start detection is gated on current_state == "Off" (_power_changed's high-power branch),
    so a dryer stuck in Emptied did not just fail to clear itself - it also could not detect the
    next cycle starting. This proves the door-close fix reopens that path end to end."""

    def test_door_close_then_high_power_schedules_a_start_confirm_timer(self):
        app = make_emptied_app()
        app._handle_door_closed("Emptied")
        self.assertEqual(app.state, "Off")

        # Next cycle starts: a high power reading arrives while state is Off.
        app._power_changed(app.power_sensor, "state", "0.0", "500", {})
        self.assertIsNotNone(app.start_confirm_timer)
        delay = scheduled_delay_for(app, app._confirm_running)
        # door_fast_start_armed_until was just set by the door-close above, so the fast-start
        # window applies (door_close_fast_confirm_s), not the slow run_for confirm.
        self.assertEqual(delay, app.door_close_fast_confirm_s)


class UnforcedTransitionToOffStillRefusedInsideCoolingPeriod(unittest.TestCase):
    """Regression guard mirroring test_dryer_pause_exit.py's equivalent class: force=False must
    remain _transition_to_off's default. Only the Emptied door-close path and the Emptied
    watchdog (both explicit force=True call sites) bypass cooling now - every other call site,
    invoked the old way, must still respect it."""

    def test_transition_to_off_unforced_is_refused_inside_cooling_period(self):
        app = make_emptied_app()
        app._transition_to_off("some other reason")
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.set_state_calls, [])


def make_full_init_app(sensor_state, helper_state=None, ui_state_entity="input_select.dryer_state"):
    """DryerMonitor with initialize() run for real. Ported from test_dryer_unavailable_grace.py's
    make_full_init_app, with _restore_emptied_state added to the recorded restore stubs.

    The three _restore_* bodies ARE stubbed here on purpose - this harness exists to test the
    dispatch in initialize(), and the real _restore_emptied_state body is covered separately by
    RestoreEmptiedState above. feedback_file/programmes_file point at nonexistent paths so
    initialize() falls back to built-in defaults instead of touching real repo data files."""
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
    app._restore_emptied_state = lambda: app.restore_calls.append("emptied")

    app.set_state_calls = []

    def set_state_entity(**kw):
        app.set_state_calls.append(kw)
        if "state" in kw:
            app.states[app.state_entity] = kw["state"]

    app._set_state_entity = set_state_entity
    return app


class InitializeRearmsTheEmptiedWatchdog(unittest.TestCase):
    """AppDaemon restarts on EVERY deploy, and run_in timers do not survive that - so arming the
    watchdog in _transition_to_emptied is only half the fix. Without a dispatch in initialize(),
    any deploy landing while the dryer sits in Emptied silently drops the only backstop that
    state has, which is precisely the situation this whole change exists to prevent.

    These tests exercise the REAL initialize() rather than calling _restore_emptied_state
    directly: deleting the `elif self.state == "Emptied"` branch is an easy and completely silent
    regression, and every test that calls the restore helper directly still passes without it."""

    def test_emptied_sensor_state_dispatches_the_emptied_restore(self):
        app = make_full_init_app(sensor_state="Emptied")
        app.initialize()
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.restore_calls, ["emptied"])

    def test_emptied_seeded_from_helper_also_dispatches_the_restore(self):
        """HA restarts erase the AD-store-backed state sensor but not the input_select mirror.
        Emptied is in initialize()'s seedable_states (it carries no cycle clock), so a
        helper-seeded Emptied must reach the same restore branch a real sensor read would."""
        app = make_full_init_app(sensor_state=None, helper_state="Emptied")
        app.initialize()
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.restore_calls, ["emptied"])

    def test_other_states_do_not_dispatch_the_emptied_restore(self):
        """Guards against a mis-ordered or over-broad elif swallowing the sibling branches."""
        for sensor_state, expected in (
            ("Unemptied", ["unemptied"]),
            ("Running", ["running"]),
            ("Paused", ["running"]),
            ("Off", []),
        ):
            with self.subTest(sensor_state=sensor_state):
                app = make_full_init_app(sensor_state=sensor_state)
                app.initialize()
                self.assertEqual(app.restore_calls, expected)


if __name__ == "__main__":
    unittest.main()
