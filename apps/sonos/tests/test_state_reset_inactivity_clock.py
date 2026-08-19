# tests/test_state_reset_inactivity_clock.py - 2026-08-19 reload-proof inactivity clock.
#
# The 5-min auto-reset used a pure in-memory run_in timer. AppDaemon re-inits every app whenever
# the HA websocket drops (it was dropping every ~15-20 min), and each re-init re-armed a FRESH
# full-length timer for any already-idle speaker - silently restarting the countdown. The fix
# anchors the countdown to a per-coordinator "inactive since" epoch persisted on disk, so remaining
# time reflects REAL elapsed idle time and survives reloads. These tests pin that behaviour.
#
# Same stub trick as the other sonos/appliances tests: appdaemon isn't installed in the test env,
# so appdaemon.plugins.hass.hassapi is stubbed before import.

from __future__ import annotations

import sys
import tempfile
import types
import unittest
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

import state_reset as sr  # noqa: E402


class _FakeNow:
    """Minimal stand-in for AppDaemon's get_now() return value: only .timestamp() is used."""

    def __init__(self, ts):
        self._ts = ts

    def timestamp(self):
        return self._ts


def make_app(state_path, now=1000.0, inactivity_sec=1200, min_delay=5.0):
    """SonosStateReset wired with fake AppDaemon callables and a real (temp) state file, without
    running initialize(). `app.clock` drives _now_ts via the stubbed get_now."""
    app = sr.SonosStateReset.__new__(sr.SonosStateReset)
    app.inactivity_sec = inactivity_sec
    app._min_reset_delay = min_delay
    app._inactivity_timers = {}
    app._inactivity_generation = {}
    app._state_path = Path(state_path)

    app.clock = now
    app.get_now = lambda: _FakeNow(app.clock)

    app.scheduled = []  # (callback, delay, kwargs)
    app.log_calls = []

    app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
    app.get_state = lambda entity, attribute=None, **kw: "idle"

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    # Load whatever is on disk (starts empty on a fresh temp file).
    app._inactive_since = app._load_inactive_since()
    return app


def last_delay(app):
    return app.scheduled[-1][1]


class FreshArmUsesFullWindow(unittest.TestCase):
    def test_first_arm_sets_anchor_and_schedules_full_window(self):
        with tempfile.TemporaryDirectory() as d:
            app = make_app(Path(d) / "s_state.json")
            app._start_inactivity_timer("media_player.living_room", "idle")

            self.assertAlmostEqual(last_delay(app), 1200, delta=0.01)
            self.assertIn("media_player.living_room", app._inactive_since)
            self.assertEqual(app._inactive_since["media_player.living_room"], 1000.0)


class ReloadDoesNotRestartCountdown(unittest.TestCase):
    def test_rearm_after_reload_uses_remaining_not_full_window(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s_state.json"
            first = make_app(path, now=1000.0)
            first._start_inactivity_timer("media_player.living_room", "idle")

            # Simulate an AppDaemon reload 500s later: a brand-new app instance reads the SAME
            # persisted anchor. The countdown must resume with 700s left, not restart at 1200s.
            second = make_app(path, now=1500.0)
            self.assertEqual(second._inactive_since.get("media_player.living_room"), 1000.0)
            second._start_inactivity_timer("media_player.living_room", "idle")
            self.assertAlmostEqual(last_delay(second), 700, delta=0.01)

    def test_blown_clock_floors_to_min_delay_not_negative(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s_state.json"
            first = make_app(path, now=1000.0)
            first._start_inactivity_timer("media_player.living_room", "idle")

            # Reload well past the window: remaining would be negative; must floor to min_delay so
            # it fires just out of the fragile reconnect window, never inside the arming callback.
            second = make_app(path, now=1000.0 + 5000.0, min_delay=5.0)
            second._start_inactivity_timer("media_player.living_room", "idle")
            self.assertEqual(last_delay(second), 5.0)


class FlapsAndFinishReuseOrClearAnchor(unittest.TestCase):
    def test_paused_idle_flap_keeps_original_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            app = make_app(Path(d) / "s_state.json", now=1000.0)
            app._start_inactivity_timer("media_player.living_room", "paused")
            # A flap 30s later invalidates the in-memory handle and re-arms; anchor must be reused.
            del app._inactivity_timers["media_player.living_room"]
            app.clock = 1030.0
            app._start_inactivity_timer("media_player.living_room", "idle")
            self.assertEqual(app._inactive_since["media_player.living_room"], 1000.0)
            self.assertAlmostEqual(last_delay(app), 1170, delta=0.01)

    def test_clear_drops_anchor_and_next_arm_is_full_window_again(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s_state.json"
            app = make_app(path, now=1000.0)
            app._start_inactivity_timer("media_player.living_room", "idle")
            app._clear_inactive_since("media_player.living_room")
            self.assertNotIn("media_player.living_room", app._inactive_since)

            # A fresh instance must not see the cleared anchor on disk either.
            reborn = make_app(path, now=2000.0)
            self.assertNotIn("media_player.living_room", reborn._inactive_since)
            reborn._start_inactivity_timer("media_player.living_room", "idle")
            self.assertAlmostEqual(last_delay(reborn), 1200, delta=0.01)


class LoadIsForgiving(unittest.TestCase):
    def test_missing_file_loads_empty_without_logging(self):
        with tempfile.TemporaryDirectory() as d:
            app = make_app(Path(d) / "never_written.json")
            self.assertEqual(app._inactive_since, {})
            self.assertEqual(app.log_calls, [])

    def test_garbage_file_loads_empty_with_one_warning(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s_state.json"
            path.write_text("{ this is not json", encoding="utf-8")
            app = make_app(path)
            self.assertEqual(app._inactive_since, {})
            warnings = [m for lvl, m in app.log_calls if lvl == "WARNING"]
            self.assertTrue(any("inactivity store" in m for m in warnings))


if __name__ == "__main__":
    unittest.main()
