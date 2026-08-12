# tests/test_washer_start_time_provenance.py - Step 1 of the HA-restart start_time fight fix.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# 2026-08-12 incident (washer_monitor.log): sensor.washer_state is an AppDaemon set_state
# entity - HA erases it on every restart, and initialize() recreates it. Two start_time
# heuristics inside _check_energy_finish then fought every 5 minutes:
#   Correcting start_time from state history: was 13:24 -> 11:41 (no last_door_closed_at)
#   Correcting start_time to entity last_changed (cycle 2 start) (was 11:41, now 13:24)
# The second line's "entity last_changed" was OUR OWN recreation timestamp, not a real
# cycle-2 transition. A wash that really started 11:41 kept reporting cycle_start_time
# 13:24 (progress_pct 24 instead of ~93) and finish detection was blocked for ~1.5h.
#
# Two fixes under test, driving the REAL WasherMonitor._check_energy_finish (and, for the
# reset case, _reset_cycle_tracking) end to end (same WasherMonitor.__new__ harness style as
# test_washer_guard_bar.py):
#   (a) self._entity_recreated_at, stamped in initialize() when the state entity was found
#       erased - the "entity last_changed = cycle 2 start" correction (block C) now skips
#       whenever the candidate last_changed falls within a 5s window of our own recreation
#       stamp. This must be a bounded window, not an open-ended '>=': the stamp is never
#       cleared on its own, so '>=' would silence block C for the rest of the app's lifetime
#       after any HA restart - a genuine cycle-2 start hours later must still fall through to
#       the rank gate below. A directly observed ("live") start also clears the stamp, as a
#       second, independent defence.
#   (b) self._start_time_source / _start_time_rank(): "entity_last_changed" (rank 6, the
#       weakest source) may only overwrite start_time when the incumbent source is rank >= 6
#       - it can never beat state_history, power_history, live or a trusted door close.
#       _reset_cycle_tracking() clears _start_time_source alongside start_time so a stale
#       rank from a finished cycle can never block a correction on the next one.

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

import washer_monitor as wm  # noqa: E402

# 2026-08-12 incident times (UTC). NOW is pinned well past the 130-minute warm-programme
# classification gate in _classify_programme (see docstring there) regardless of start_time,
# so every tick classifies as a plain 'eco' without needing power/energy history fixtures -
# this suite is about start_time provenance, not classification.
START_TIME = datetime(2026, 8, 12, 11, 41, 0, tzinfo=timezone.utc)      # real wash start (state_history)
RECREATED_AT = datetime(2026, 8, 12, 13, 24, 24, tzinfo=timezone.utc)   # app recreated sensor.washer_state post-HA-restart
GENUINE_CYCLE_2_START = datetime(2026, 8, 12, 16, 0, 0, tzinfo=timezone.utc)  # real cycle-2 start, hours after the restart
NOW = datetime(2026, 8, 12, 14, 0, 0, tzinfo=timezone.utc)              # this energy-check tick


def make_app(start_time, start_time_source, entity_recreated_at, entity_last_changed):
    """WasherMonitor with production guard/classification knobs and a fixed clock.
    _check_energy_finish runs FOR REAL, end to end; only the AppDaemon surface (get_state,
    log, run_in, _set_state_entity) is stubbed."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)

    app.state_entity = "sensor.washer_state"
    app.energy_sensor = "sensor.washer_plug_energy"
    app.power_sensor = "sensor.washer_plug_power"

    app.now = NOW
    app._now_utc = lambda: app.now

    # start_time provenance under test
    app.start_time = start_time
    app._start_time_source = start_time_source
    app._entity_recreated_at = entity_recreated_at
    app.last_door_closed_at = None
    app.last_door_closed_trusted = False  # incident: no trusted door close survives an HA restart
    app._delayed_start_trimmed = False
    app.pause_window_minutes = 3
    app._last_infer_start_attempt = None

    # Delayed-start detection kept inert - out of scope for this fix.
    app.detect_delayed_start = False
    app._delay_waiting = False
    app._delay_plateau_start = None
    app.delay_energy_floor_kwh = 0.05
    app.delay_plateau_minutes = 30

    app.energy_start = None

    # Classification / guard bar
    app.confirm_entity = None
    app.temperature_entity = None
    app.programme_confirmed_by_user = False
    app.observed_heating = True
    app.heating_phase_count = 2
    app.max_power_seen = 2114.0
    app.expected_dur_at_start = None
    app._guard_bar_class = None
    app._live_class_key = None
    app._live_class_since = None
    app._history_centroids = {}
    app.guard_reclass_stable_minutes = 15.0
    app.guard_energy_disproof_margin = 1.10
    app.detected_programme = "unknown"
    app.detected_temperature = None

    # Finish guards
    app.finish_min_run_minutes_warm = 100.0
    app.finish_min_run_minutes_cold = 50.0
    app.finish_guard_fraction = 0.92
    app.finish_debug_window_minutes = 15
    app.max_running_hours = 5
    app.min_cycle_minutes = 25
    app.min_energy_kwh = 0.2

    # FinishingTail / tail-pulse tracking - not engaged in this scenario.
    app.in_finishing_tail = False
    app.in_finishing_tail_entered_at = None
    app.last_tail_pulse_at = None
    app.tail_pattern_locked = False
    app.tail_pattern_cycle_seconds = None
    app.tail_pattern_last_pulse_at = None
    app.tail_pattern_locked_at = None

    # Misc display/attrs written every tick
    app.last_high_energy_at = None
    app.confirmed_by_username = None
    app._session_cost_kr = 0.0
    app.delayed_start_show_waiting = True
    app._local_tz_obj = timezone.utc
    app.energy_check_interval = 30
    app.energy_check_timer = None

    # Stubbed AppDaemon surface
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

    state_entity_all = {
        "attributes": {},
        "last_changed": app._format_utc(entity_last_changed) if entity_last_changed else None,
    }

    def get_state(entity, **kw):
        attribute = kw.get("attribute")
        if entity == app.state_entity:
            if attribute is None:
                return "Running"
            if attribute == "all":
                return state_entity_all
            return None  # last_door_closed_trusted / last_off_at: nothing trusted survived
        if entity == app.energy_sensor:
            return "unavailable"  # drives the natural end-of-tick reschedule-and-return
        return None

    app.get_state = get_state
    # get_history is deliberately left unset: _infer_start_from_state_history's own broad
    # except turns the resulting AttributeError into a safe "no history" (None), exactly like
    # a fresh reload with no recorder access yet - AppDaemon's history API is not what this
    # test is about.

    app.scheduled = []

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in

    app.set_state_calls = []
    app._set_state_entity = lambda **kw: app.set_state_calls.append(kw)

    return app


def log_messages(app, level):
    return [str(args[0]) for args, kw in app.log_calls if kw.get("level") == level]


class RecreationIsIgnored(unittest.TestCase):
    """The 2026-08-12 incident itself: start_time already correctly holds 11:41 (sourced from
    state_history); the entity's last_changed reads 13:24:24 only because THIS app recreated
    the HA-erased entity at that instant. Must not be read as a cycle-2 transition."""

    def test_start_time_unchanged_when_last_changed_is_our_own_recreation(self):
        app = make_app(
            start_time=START_TIME,
            start_time_source="state_history",
            entity_recreated_at=RECREATED_AT,
            entity_last_changed=RECREATED_AT,
        )
        app._check_energy_finish({})
        self.assertEqual(app.start_time, START_TIME)
        self.assertEqual(app._start_time_source, "state_history")
        debug_msgs = log_messages(app, "DEBUG")
        self.assertTrue(
            any("our own post-restart recreation" in m for m in debug_msgs),
            "expected a DEBUG log explaining the skip",
        )
        info_msgs = log_messages(app, "INFO")
        self.assertFalse(
            any("cycle 2 start" in m for m in info_msgs),
            "must not log the entity-last-changed correction it skipped",
        )


class GenuineCycleTwoAfterRestart(unittest.TestCase):
    """The recreation guard must be a bounded window around the recreation instant, not an
    open-ended '>=' - otherwise it silences block C for the rest of the app's lifetime after
    any HA restart. Here the restart (and its recreation stamp) happened at 13:24:24, but the
    entity's last_changed is a genuine cycle-2 start hours later (16:00) - clearly not the
    recreation itself - and must still correct start_time."""

    def test_start_time_corrects_even_though_a_restart_happened_earlier(self):
        app = make_app(
            start_time=START_TIME,
            start_time_source=None,  # unclaimed
            entity_recreated_at=RECREATED_AT,  # a restart DID happen, hours before this transition
            entity_last_changed=GENUINE_CYCLE_2_START,
        )
        app._check_energy_finish({})
        self.assertEqual(app.start_time, GENUINE_CYCLE_2_START)
        self.assertEqual(app._start_time_source, "entity_last_changed")


class GenuineCycleTwoStillCorrects(unittest.TestCase):
    """No restart happened (_entity_recreated_at is None) and nothing has claimed a stronger
    provenance for start_time yet - entity last_changed moving well past pause_window_minutes
    IS a real cycle-2 start, and the heuristic must still slide start_time to it. Proves the
    guards added here do not disable the original mechanism."""

    def test_start_time_moves_to_entity_last_changed(self):
        later_changed = START_TIME + timedelta(minutes=24)  # well beyond pause_window_minutes (3 min)
        app = make_app(
            start_time=START_TIME,
            start_time_source=None,  # nothing has claimed provenance yet
            entity_recreated_at=None,  # not a post-restart recreation
            entity_last_changed=later_changed,
        )
        app._check_energy_finish({})
        self.assertEqual(app.start_time, later_changed)
        self.assertEqual(app._start_time_source, "entity_last_changed")


class AntiOscillation(unittest.TestCase):
    """Same incident fixture as RecreationIsIgnored, driven twice in a row (mirrors the
    5-minute energy-check tick firing repeatedly) - start_time must not flip-flop."""

    def test_start_time_stable_across_two_ticks(self):
        app = make_app(
            start_time=START_TIME,
            start_time_source="state_history",
            entity_recreated_at=RECREATED_AT,
            entity_last_changed=RECREATED_AT,
        )
        app._check_energy_finish({})
        first = app.start_time
        app._check_energy_finish({})
        second = app.start_time
        self.assertEqual(first, START_TIME)
        self.assertEqual(second, START_TIME)


def make_reset_app(start_time_source):
    """Minimal WasherMonitor for exercising _reset_cycle_tracking() directly. Only the
    attributes it (and the _reset_input_selectors / _set_programme_helpers_default it calls
    for the full Off cleanup) read are needed - nothing about classification or block C."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.start_time = START_TIME
    app._start_time_source = start_time_source
    app.confirm_entity = None
    app.temperature_entity = None
    app.spin_entity = None
    app.energy_check_timer = None
    app.unemptied_door_recheck_timer = None
    app.poll_timer = None
    app.history_poll_timer = None
    app.pause_timer = None
    app.running_watchdog_timer = None
    app.unemptied_watchdog_timer = None
    app.emptied_watchdog_timer = None
    app.log = lambda *a, **kw: None
    app.call_service = lambda *a, **kw: None
    return app


class ProvenanceDoesNotLeakAcrossCycles(unittest.TestCase):
    """_start_time_source must reset alongside start_time itself - a stale rank left over from
    the previous cycle (e.g. 'state_history', rank 4) would otherwise block a legitimate block
    C correction on the very next cycle."""

    def test_reset_cycle_tracking_clears_start_time_source(self):
        app = make_reset_app(start_time_source="state_history")
        app._reset_cycle_tracking()
        self.assertIsNone(app._start_time_source)
        self.assertIsNone(app.start_time)


if __name__ == "__main__":
    unittest.main()
