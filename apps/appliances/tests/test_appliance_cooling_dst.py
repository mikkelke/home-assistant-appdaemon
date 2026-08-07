# tests/test_appliance_cooling_dst.py - DST fall-back regression for _should_change_state's
# cooling-period comparison, covering both DryerMonitor and DishwasherMonitor.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# The incident: _should_change_state compared naive datetime.now() against last_state_change. At
# the Europe/Copenhagen October DST fall-back the local clock jumps an hour BACKWARDS (03:00 CEST
# -> 02:00 CET), so `now - last_state_change` goes negative for the whole repeated hour - and a
# negative delta is trivially < cooling_period, which silently refuses every un-forced transition
# until the hour is out. Both DryerMonitor._should_change_state and
# DishwasherMonitor._should_change_state now compare self._now_utc() (aware UTC) instead - see the
# docstring on each. Every writer of last_state_change must stay on _now_utc() too, or the
# subtraction raises TypeError on mixed naive/aware operands the moment the two disagree; the
# dryer's other writer (_power_unavailable_off_timeout) is exercised below for exactly that
# reason.
#
# Per this repo's incident history, stubbing out the very function whose interaction with its
# caller IS the bug would hide it again - so every test below calls the real
# _should_change_state (and, for the plug-outage test, the real _power_unavailable_off_timeout,
# _reset_programme_selectors_to_unconfirmed and _reset_cycle_tracking it calls in turn). Only the
# AppDaemon I/O boundary (log / get_state / _set_state_entity / run_in / cancel_timer /
# timer_running) is faked, following test_dryer_pause_exit.py's make_app harness style.
#
# The real Europe/Copenhagen 2026-10-25 fall-back instants used below (03:00 CEST -> 02:00 CET;
# CEST = UTC+2, CET = UTC+1):
#   2026-10-25 00:55:00+00:00 -> 02:55 CEST (stamp time)
#   2026-10-25 01:00:00+00:00 -> 02:00 CET  (+5 real min;  naive delta -55 min)
#   2026-10-25 01:07:00+00:00 -> 02:07 CET  (+12 real min; naive delta -48 min)

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import dishwasher_monitor  # noqa: E402
import dryer_monitor  # noqa: E402

# See the module docstring above for what each looks like in naive Europe/Copenhagen local time.
FALL_BACK_STAMP_UTC = datetime(2026, 10, 25, 0, 55, 0, tzinfo=timezone.utc)          # 02:55 CEST
FALL_BACK_INSIDE_COOLING_UTC = datetime(2026, 10, 25, 1, 0, 0, tzinfo=timezone.utc)  # 02:00 CET
FALL_BACK_PAST_COOLING_UTC = datetime(2026, 10, 25, 1, 7, 0, tzinfo=timezone.utc)    # 02:07 CET


def make_dryer_app():
    """DryerMonitor built with __new__ (initialize() never runs), holding only the attributes
    _should_change_state touches, plus - for the plug-outage writer test only - the attributes
    _power_unavailable_off_timeout, _reset_programme_selectors_to_unconfirmed and
    _reset_cycle_tracking touch along their real (non-stubbed) code paths. Only the AppDaemon I/O
    boundary is faked, mirroring test_dryer_pause_exit.py's make_app."""
    app = dryer_monitor.DryerMonitor.__new__(dryer_monitor.DryerMonitor)

    app.power_sensor = "sensor.dryer_plug_power"
    app.state_entity = "sensor.dryer_state"
    app.cooling_period = 600
    app.power_unavailable_off_after_seconds = 180

    app.state = "Off"
    app.states = {}
    app.last_state_change = None

    # Real _reset_programme_selectors_to_unconfirmed short-circuits on this flag, so the
    # plug-outage test never needs to fake programme input_selects or call_service.
    app.reset_programme_on_idle = False

    # Attributes the real _reset_cycle_tracking (called from _power_unavailable_off_timeout)
    # reads and/or writes - only reached by the plug-outage test, harmless for the others.
    app.start_time = None
    app.energy_start = None
    app.door_opened_time = None
    app.door_opened_during_cycle = False
    app.keep_fresh_detected = False
    app.power_readings = []
    app.high_power_counter = 0
    app.poll_timer = None
    app.pause_timer = None
    app.low_power_timer = None
    app.pattern_check_timer = None
    app.classify_timer = None
    app.expected_dur_at_start = None
    app.detected_programme = "unknown"
    app.max_power_w = 0.0
    app.running_watchdog_timer = None
    app.unemptied_watchdog_timer = None
    app.emptied_watchdog_timer = None
    app.start_confirm_timer = None
    app.door_fast_start_armed_until = None

    app.log_calls = []
    app.set_state_calls = []
    app.canceled_timers = []
    app._timer_running = False

    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.timer_running = lambda handle: app._timer_running
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)

    def set_state_entity(**kw):
        app.set_state_calls.append(kw)
        if "state" in kw:
            app.states[app.state_entity] = kw["state"]

    app._set_state_entity = set_state_entity

    def run_in(cb, delay, **kw):
        return object()

    app.run_in = run_in
    return app


def make_dishwasher_app():
    """DishwasherMonitor built with __new__, holding only the attributes _should_change_state
    touches. cooling_period is hardcoded to 300 in dishwasher_monitor.py's initialize() (never
    read from args), so it is set here to match rather than derived from anything."""
    app = dishwasher_monitor.DishwasherMonitor.__new__(dishwasher_monitor.DishwasherMonitor)

    app.state_entity = "sensor.dishwasher_state"
    app.cooling_period = 300

    app.state = "Off"
    app.states = {}
    app.last_state_change = None

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)

    return app


class DryerCoolingCheckCrossesTheFallBackBoundary(unittest.TestCase):
    """The core regression. Under the old naive datetime.now() code, crossing the fall-back made
    `now - last_state_change` go negative (naive local time ran BACKWARDS between the two calls),
    which is trivially < cooling_period and refuses the transition regardless of how much real
    time actually passed. Both directions are checked: past the cooling period must now be
    allowed (proves the fix), and still inside it must remain refused (proves the fix did not
    just make _should_change_state always return True)."""

    def test_past_cooling_period_across_fall_back_is_allowed(self):
        """720 real seconds have elapsed (past the 600s cooling_period). The old naive code saw
        naive local time go from 02:55 CEST to 02:07 CET - a -48 minute delta, which is < 600 and
        would have wrongly refused this transition."""
        app = make_dryer_app()
        app.states[app.state_entity] = "Off"
        app.last_state_change = FALL_BACK_STAMP_UTC
        app._now_utc = lambda: FALL_BACK_PAST_COOLING_UTC

        self.assertTrue(app._should_change_state("Running"))

    def test_inside_cooling_period_across_fall_back_is_still_refused(self):
        """Only 300 real seconds have elapsed (still inside the 600s cooling_period) - the
        cooling check must keep working across the DST boundary, not just stop mattering."""
        app = make_dryer_app()
        app.states[app.state_entity] = "Off"
        app.last_state_change = FALL_BACK_STAMP_UTC
        app._now_utc = lambda: FALL_BACK_INSIDE_COOLING_UTC

        self.assertFalse(app._should_change_state("Running"))


class DryerLastStateChangeStaysAwareAfterAnAcceptedTransition(unittest.TestCase):
    """The invariant every writer of last_state_change depends on: it must always be an aware
    UTC datetime, or the very next cooling-period comparison raises TypeError on a naive/aware
    subtraction instead of returning a bool."""

    def test_accepted_transition_stamps_an_aware_utc_datetime(self):
        app = make_dryer_app()
        app.states[app.state_entity] = "Off"
        app._now_utc = lambda: FALL_BACK_STAMP_UTC

        self.assertTrue(app._should_change_state("Running"))
        self.assertIsNotNone(app.last_state_change.tzinfo)
        self.assertEqual(app.last_state_change.utcoffset(), timedelta(0))


class DryerPlugOutageWriterStampsAwareAndDoesNotPoisonTheNextCheck(unittest.TestCase):
    """_power_unavailable_off_timeout is the dryer's OTHER writer of last_state_change, outside
    _should_change_state itself. If it alone reverted to a naive stamp, last_state_change would
    go naive while _should_change_state's `now` stays aware - so the very next un-forced check
    would raise TypeError instead of returning a bool. This is the mixed-operand hazard: exactly
    what breaks if someone reverts only one of the two writers."""

    def test_plug_outage_off_stamps_aware_and_next_check_returns_a_bool(self):
        app = make_dryer_app()
        app.states[app.state_entity] = "Running"
        app.state = "Running"
        app.states[app.power_sensor] = "unavailable"
        app.power_unavailable_off_timer = "timer#pending-outage"
        app._now_utc = lambda: FALL_BACK_STAMP_UTC

        app._power_unavailable_off_timeout({})

        self.assertEqual(app.state, "Off")
        self.assertIsNotNone(app.last_state_change.tzinfo)
        self.assertEqual(app.last_state_change.utcoffset(), timedelta(0))

        app._now_utc = lambda: FALL_BACK_PAST_COOLING_UTC
        result = app._should_change_state("Running")
        self.assertIsInstance(result, bool)
        self.assertTrue(result)  # 720 real seconds have passed - past the 600s cooling_period


class DishwasherCoolingCheckCrossesTheFallBackBoundary(unittest.TestCase):
    """Same regression as the dryer's, mirrored for DishwasherMonitor. cooling_period is 300 here
    (not 600 - dishwasher_monitor.py hardcodes it rather than reading it from args)."""

    def test_past_cooling_period_across_fall_back_is_allowed(self):
        app = make_dishwasher_app()
        app.states[app.state_entity] = "Off"
        app.last_state_change = FALL_BACK_STAMP_UTC
        app._now_utc = lambda: FALL_BACK_PAST_COOLING_UTC

        self.assertTrue(app._should_change_state("Running"))


class DishwasherLastStateChangeStaysAwareAfterAnAcceptedTransition(unittest.TestCase):
    """Same invariant as the dryer's: last_state_change must stay an aware UTC datetime.
    DishwasherMonitor._should_change_state is the sole writer for this appliance - there is no
    dishwasher equivalent of the dryer's separate plug-outage writer."""

    def test_accepted_transition_stamps_an_aware_utc_datetime(self):
        app = make_dishwasher_app()
        app.states[app.state_entity] = "Off"
        app._now_utc = lambda: FALL_BACK_STAMP_UTC

        self.assertTrue(app._should_change_state("Running"))
        self.assertIsNotNone(app.last_state_change.tzinfo)
        self.assertEqual(app.last_state_change.utcoffset(), timedelta(0))


if __name__ == "__main__":
    unittest.main()
