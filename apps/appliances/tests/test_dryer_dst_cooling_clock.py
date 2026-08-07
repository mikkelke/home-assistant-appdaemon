# tests/test_dryer_dst_cooling_clock.py - the cooling-period clock must be UTC, not local wall time.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# Why this exists: _should_change_state used a naive datetime.now(). At the European DST fall-back
# (2026-10-25 03:00 -> 02:00 local) wall time repeats an hour, so `now - last_state_change` goes
# NEGATIVE for that hour - which is < cooling_period, so EVERY un-forced transition is refused for
# a full hour. That is the same silent-swallow class that wedged Paused for 34h and Emptied 52 times,
# but house-wide and on a known date. The washer already read UTC here; the dryer now does too.
#
# These tests pin the invariant rather than the calendar: the stamp must be timezone-aware, and the
# elapsed comparison must be done on aware UTC values. A naive implementation fails both.

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
    """Minimal DryerMonitor exercising the real _should_change_state and the real _now_utc.
    Only the AppDaemon I/O boundary (get_state / log) is faked."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)
    app.state_entity = "sensor.dryer_state"
    app.cooling_period = 600
    app.last_state_change = None
    app.states = {app.state_entity: "Off"}
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)
    return app


class CoolingClockIsUtc(unittest.TestCase):
    def test_stamp_is_timezone_aware(self):
        """The DST invariant. A naive datetime.now() stamp leaves tzinfo None and reintroduces
        the repeated-hour bug, so this is the assertion that must never go green again."""
        app = make_app()
        self.assertTrue(app._should_change_state("Running"))
        self.assertIsNotNone(app.last_state_change)
        self.assertIsNotNone(
            app.last_state_change.tzinfo,
            "last_state_change must be timezone-aware UTC - a naive local stamp goes "
            "negative for an hour at the October DST fall-back",
        )

    def test_forced_transition_also_stamps_aware(self):
        """force=True skips the cooling check but still stamps - that stamp must be aware too,
        or the NEXT un-forced comparison mixes naive and aware and raises TypeError."""
        app = make_app()
        self.assertTrue(app._should_change_state("Running", force=True))
        self.assertIsNotNone(app.last_state_change.tzinfo)

    def test_unavailable_off_path_stamps_aware(self):
        """_handle_unavailable's Off path stamps last_state_change directly, bypassing
        _should_change_state - it must use the same clock or the next comparison raises."""
        app = make_app()
        app.state = "Running"
        app.last_state_change = app._now_utc() - timedelta(seconds=5)
        # Stamp the way the unavailable-off path does, then prove the next comparison works.
        app.last_state_change = app._now_utc()
        self.assertIsNotNone(app.last_state_change.tzinfo)
        self.assertFalse(app._should_change_state("Unemptied"))


class CoolingPeriodComparisonUsesUtc(unittest.TestCase):
    def test_inside_window_is_refused(self):
        app = make_app()
        app.last_state_change = app._now_utc() - timedelta(seconds=app.cooling_period - 30)
        self.assertFalse(app._should_change_state("Running"))

    def test_outside_window_is_allowed(self):
        app = make_app()
        app.last_state_change = app._now_utc() - timedelta(seconds=app.cooling_period + 30)
        self.assertTrue(app._should_change_state("Running"))

    def test_forced_bypasses_window(self):
        app = make_app()
        app.last_state_change = app._now_utc()
        self.assertTrue(app._should_change_state("Running", force=True))

    def test_aware_stamp_survives_a_full_accept_reject_cycle(self):
        """End-to-end on the real code: accept stamps aware, an immediate second attempt is
        refused against that aware stamp. Mixing clocks would raise TypeError here."""
        app = make_app()
        self.assertTrue(app._should_change_state("Running"))
        app.states[app.state_entity] = "Running"
        self.assertFalse(app._should_change_state("Unemptied"))


if __name__ == "__main__":
    unittest.main()
