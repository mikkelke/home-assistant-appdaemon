# tests/test_washer_restore_wipe_anchor.py - the 13:24 mis-anchor (2026-08-12).
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# The incident: HA restarted at 13:24 mid-wash; the entity was wiped and re-created, so its
# last_changed said 13:24 even though cycle_start_time (restored from the persisted
# attribute) correctly said 11:41. _restore_running_state's "cycle 2" correction - built for
# the legitimate case where a stale attribute predates the entity actually going Running -
# took the wipe artifact at face value and shifted the real 11:41 start to 13:24:
#   "Restore: correcting start_time to entity last_changed (cycle 2) (was 11:41, now 13:24)"
# Every duration guard, ETA and feedback record downstream then ran off a start almost two
# hours late.
#
# The fix: any restore anchor within restore_wipe_anchor_grace_s (default 300 s) of the
# app's own init time is the restart re-creating the entity, not information - the
# correction is skipped and the attribute-restored start_time kept. A genuinely old
# last_changed (the real "cycle 2" case) still corrects.
#
# The tests run the REAL _restore_running_state (this repo's incident history says stubbing
# the function whose interaction with its caller IS the bug hides it again); only the
# AppDaemon I/O boundary (get_state / get_history / run_in / timer_running / cancel_timer /
# log) plus the set_state-writing _push_corrected_start_time_to_entity are faked.

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
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

UTC = timezone.utc
# Incident timestamps (local 11:41 / 13:24, stored as the UTC the entity actually carries).
CYCLE_START = "2026-08-12T09:41:00Z"
CYCLE_START_DT = datetime(2026, 8, 12, 9, 41, 0, tzinfo=UTC)
WIPE_LAST_CHANGED = "2026-08-12T11:24:07Z"     # entity re-created by the 13:24 HA restart
INIT_NOW = datetime(2026, 8, 12, 11, 24, 30, tzinfo=UTC)  # app re-init seconds later


def make_restore_app(*, cycle_start=CYCLE_START, last_changed=WIPE_LAST_CHANGED, now=INIT_NOW):
    """WasherMonitor with just enough state to run the REAL _restore_running_state.

    Every attribute below is one _restore_running_state (or a helper it calls) reads on the
    path exercised here; get_history returns [] so the power-history correction and heating
    restore short-circuit, isolating the last_changed anchor logic under test."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.args = {}
    app.state_entity = "sensor.washer_state"
    app.power_sensor = "sensor.washer_plug_power"
    app.energy_sensor = "sensor.washer_plug_energy"
    app.confirm_entity = None
    app.start_w = 6.0
    app.use_energy_detection = False
    app.pause_window_minutes = 3
    app.restore_start_gap_minutes = 15
    app.restore_wipe_anchor_grace_s = 300
    app.max_running_hours = 4
    app.observed_heating = False
    app.heating_phase_count = 0
    app.max_power_seen = 0.0
    app.last_high_energy_at = None
    app.detected_programme = "unknown"
    app.detected_temperature = None
    app.programme_confirmed_by_user = False
    app.confirmed_by_username = None
    app.expected_dur_at_start = None
    app._guard_bar_class = None
    app._delayed_start_trimmed = False
    app.start_time = None
    app.energy_start = None
    app.last_door_closed_at = None
    app.last_door_closed_trusted = False
    app.running_watchdog_timer = None
    app.poll_timer = "sentinel-poll"          # already armed -> skip re-arm plumbing
    app.history_poll_timer = "sentinel-hist"  # already armed -> skip re-arm plumbing

    app.now = now
    app._now_utc = lambda: app.now

    app.entity_attrs = {"cycle_start_time": cycle_start}
    app.entity_last_changed = last_changed

    def get_state(entity, **kw):
        attr = kw.get("attribute")
        if entity == app.state_entity:
            if attr == "all":
                return {"attributes": dict(app.entity_attrs), "last_changed": app.entity_last_changed}
            if attr:
                return app.entity_attrs.get(attr)
            return "Running"
        if entity == app.power_sensor:
            return "0.0"  # boot_watts < start_w -> the trailing recovery block short-circuits
        return None

    app.get_state = get_state
    app.get_history = lambda **kw: []

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.push_calls = []
    app._push_corrected_start_time_to_entity = lambda: app.push_calls.append(app.start_time)
    app._cycle_actor_from_state_attrs = lambda attrs: {"person": None, "method": None}

    app.scheduled = []
    app.canceled_timers = []
    app.timer_running = lambda handle: True
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)

    def run_in(cb, delay, **kw):
        handle = f"timer#{len(app.scheduled)}:{getattr(cb, '__name__', cb)}"
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    return app


def logged(app, needle):
    return any(needle in str(a[0]) for a, _kw in app.log_calls)


class WipeAnchorWithinGraceKeepsRealStart(unittest.TestCase):
    def test_the_1324_incident_start_time_survives(self):
        """Entity last_changed 13:24 = the restart re-creating the entity, seconds before the
        app's own init. Without the fix the "cycle 2" correction shifts the real 11:41 start
        to 13:24; with it, the attribute-restored start_time is kept and nothing is pushed."""
        app = make_restore_app()
        app._restore_running_state()
        self.assertEqual(app.start_time, CYCLE_START_DT)
        self.assertEqual(app.push_calls, [])
        self.assertTrue(logged(app, "restart artifact"))
        self.assertFalse(logged(app, "cycle 2"))

    def test_grace_is_configurable(self):
        """A tightened grace (10 s) lets an anchor 23 s old through to the correction -
        proving the gate reads restore_wipe_anchor_grace_s, not a hardcoded 300."""
        app = make_restore_app()
        app.restore_wipe_anchor_grace_s = 10
        app._restore_running_state()
        self.assertEqual(app.start_time, datetime(2026, 8, 12, 11, 24, 7, tzinfo=UTC))
        self.assertTrue(logged(app, "cycle 2"))


class GenuineOldAnchorStillCorrects(unittest.TestCase):
    def test_cycle_2_correction_still_works(self):
        """The correction's legitimate case: the entity went Running hours before init (a
        stale cycle_start_time from a previous wash survived on the attributes). last_changed
        is nowhere near init, so the correction must still fire exactly as before."""
        app = make_restore_app(
            cycle_start="2026-08-12T05:00:00Z",
            last_changed="2026-08-12T09:00:00Z",
            now=INIT_NOW,  # 11:24 - the 09:00 anchor is 2.4 h old, clearly not the wipe
        )
        app._restore_running_state()
        self.assertEqual(app.start_time, datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC))
        self.assertEqual(len(app.push_calls), 1)
        self.assertTrue(logged(app, "cycle 2"))
        self.assertFalse(logged(app, "restart artifact"))

    def test_small_gap_below_pause_window_still_ignored(self):
        """Anchor old enough to be trusted but within pause_window of start_time: the
        pre-existing gap threshold still applies (no correction for tiny drifts)."""
        app = make_restore_app(
            cycle_start="2026-08-12T08:58:30Z",
            last_changed="2026-08-12T09:00:00Z",  # 90 s gap < 3 min pause window
            now=INIT_NOW,
        )
        app._restore_running_state()
        self.assertEqual(app.start_time, datetime(2026, 8, 12, 8, 58, 30, tzinfo=UTC))
        self.assertEqual(app.push_calls, [])


if __name__ == "__main__":
    unittest.main()
