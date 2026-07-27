# tests/test_washer_vibration.py - Vibration telemetry (TELEMETRY ONLY) unit tests for
# washer_monitor.py: pulse/on-time accounting, the feedback-record summary, and the
# post-save unload-window patch. Same __new__ + monkeypatched-callables harness as
# test_washer_unavailable_grace.py in this directory (no running AppDaemon required).
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import collections
import json
import os
import sys
import tempfile
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


def make_app(vibration_sensor="binary_sensor.washer_vibration_vibration", state="Running"):
    """WasherMonitor with fake log/run_in/cancel_timer/timer_running, without running
    AppDaemon's initialize() - same trick as test_washer_unavailable_grace.py's make_app().
    _now_utc/_format_local/_format_utc/_strftime_local are the REAL methods (no AppDaemon
    dependency); tests override _now_utc per-call for deterministic timestamps."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.vibration_sensor = vibration_sensor
    app.state = state

    # Same defaults/reset shape as initialize() / _begin_running_cycle in washer_monitor.py.
    app.vibration_pulse_count = 0
    app.vibration_on_seconds = 0.0
    app.first_vibration_at = None
    app.last_vibration_at = None
    app._vibration_on_started = None
    app._vibration_events = collections.deque(maxlen=500)
    app._unload_patch_timer = None
    app.feedback_file = None

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
    return app


def _warning_logged(app):
    return any(kw.get("level") == "WARNING" for _a, kw in app.log_calls)


class VibrationChangedRunningVsOff(unittest.TestCase):
    """(a) pulses while Running are counted (pulse_count, first/last_at); pulses while not
    Running are not counted but still land in _vibration_events (used later for the
    unload-window count, which is state-agnostic)."""

    def test_pulse_while_running_counts_and_sets_first_last(self):
        app = make_app(state="Running")
        t0 = datetime(2026, 7, 27, 11, 40, 0, tzinfo=timezone.utc)
        app._now_utc = lambda: t0

        app._vibration_changed(app.vibration_sensor, "state", "off", "on", {})

        self.assertEqual(app.vibration_pulse_count, 1)
        self.assertEqual(app.first_vibration_at, t0)
        self.assertEqual(app.last_vibration_at, t0)
        self.assertEqual(list(app._vibration_events), [t0])
        self.assertEqual(app._vibration_on_started, (t0, True))
        self.assertEqual(app.log_calls, [])  # no error swallowed

    def test_pulse_while_off_not_counted_but_recorded_in_events(self):
        app = make_app(state="Off")
        t0 = datetime(2026, 7, 27, 9, 0, 0, tzinfo=timezone.utc)
        app._now_utc = lambda: t0

        app._vibration_changed(app.vibration_sensor, "state", "off", "on", {})

        self.assertEqual(app.vibration_pulse_count, 0)
        self.assertIsNone(app.first_vibration_at)
        self.assertIsNone(app.last_vibration_at)
        self.assertEqual(list(app._vibration_events), [t0])  # still recorded (state-agnostic)
        self.assertEqual(app._vibration_on_started, (t0, False))

    def test_second_running_pulse_updates_last_but_not_first(self):
        app = make_app(state="Running")
        t0 = datetime(2026, 7, 27, 11, 40, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(hours=2, minutes=20)

        app._now_utc = lambda: t0
        app._vibration_changed(app.vibration_sensor, "state", "off", "on", {})
        app._now_utc = lambda: t0 + timedelta(seconds=1)
        app._vibration_changed(app.vibration_sensor, "state", "on", "off", {})
        app._now_utc = lambda: t1
        app._vibration_changed(app.vibration_sensor, "state", "off", "on", {})

        self.assertEqual(app.vibration_pulse_count, 2)
        self.assertEqual(app.first_vibration_at, t0)
        self.assertEqual(app.last_vibration_at, t1)
        self.assertEqual(len(app._vibration_events), 2)


class VibrationOnOffPairing(unittest.TestCase):
    """(b) on/off pairing accumulates vibration_on_seconds only for pulses that started while
    Running; a pulse whose off-edge never arrives in time is capped at 600s."""

    def test_on_off_pair_accumulates_seconds_while_running(self):
        app = make_app(state="Running")
        t0 = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        app._now_utc = lambda: t0
        app._vibration_changed(app.vibration_sensor, "state", "off", "on", {})

        app._now_utc = lambda: t0 + timedelta(seconds=5)
        app._vibration_changed(app.vibration_sensor, "state", "on", "off", {})

        self.assertAlmostEqual(app.vibration_on_seconds, 5.0)
        self.assertIsNone(app._vibration_on_started)

    def test_pulse_started_while_not_running_is_never_credited(self):
        """State flips to Running mid-pulse (edge case): the pulse still does not count because
        it started while not Running - matches the "was_running" captured at the ON edge."""
        app = make_app(state="Off")
        t0 = datetime(2026, 7, 27, 8, 0, 0, tzinfo=timezone.utc)
        app._now_utc = lambda: t0
        app._vibration_changed(app.vibration_sensor, "state", "off", "on", {})

        app.state = "Running"
        app._now_utc = lambda: t0 + timedelta(seconds=30)
        app._vibration_changed(app.vibration_sensor, "state", "on", "off", {})

        self.assertEqual(app.vibration_on_seconds, 0.0)

    def test_missed_off_edge_capped_at_600_seconds(self):
        app = make_app(state="Running")
        t0 = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        app._now_utc = lambda: t0
        app._vibration_changed(app.vibration_sensor, "state", "off", "on", {})

        # Off-edge only arrives 15 min later (900s) - a real pulse is never this long; the
        # missed-off-edge sanity guard caps the credited time at 600s.
        app._now_utc = lambda: t0 + timedelta(minutes=15)
        app._vibration_changed(app.vibration_sensor, "state", "on", "off", {})

        self.assertEqual(app.vibration_on_seconds, 600.0)

    def test_unknown_and_unavailable_edges_clear_pending_without_crediting_or_logging_above_debug(self):
        app = make_app(state="Running")
        t0 = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        app._now_utc = lambda: t0
        app._vibration_changed(app.vibration_sensor, "state", "off", "on", {})

        app._now_utc = lambda: t0 + timedelta(seconds=10)
        app._vibration_changed(app.vibration_sensor, "state", "on", "unavailable", {})

        self.assertIsNone(app._vibration_on_started)
        self.assertEqual(app.vibration_on_seconds, 0.0)
        # Only the original "on" edge was recorded; "unavailable" is not itself a pulse edge.
        self.assertEqual(len(app._vibration_events), 1)
        self.assertFalse(_warning_logged(app))
        for _a, kw in app.log_calls:
            self.assertNotEqual(kw.get("level"), "INFO")

    def test_callback_never_raises_on_garbage_input(self):
        app = make_app(state="Running")
        app._now_utc = lambda: datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        try:
            app._vibration_changed(app.vibration_sensor, "state", None, "on", {})
            app._vibration_changed(app.vibration_sensor, "state", "on", None, {})
        except Exception as e:  # pragma: no cover - the assertion below is the real check
            self.fail(f"_vibration_changed raised: {e}")


class VibrationSummary(unittest.TestCase):
    """(c) _vibration_summary is None when the sensor is unconfigured, or configured but
    silent (both pulse_count and on_seconds are zero); otherwise it returns the documented
    {pulse_count, on_seconds, first_at, last_at} shape."""

    def test_none_when_sensor_unconfigured(self):
        app = make_app(vibration_sensor=None)
        app.vibration_pulse_count = 5
        app.vibration_on_seconds = 42.0
        self.assertIsNone(app._vibration_summary())

    def test_none_when_configured_but_empty(self):
        app = make_app()
        self.assertIsNone(app._vibration_summary())

    def test_not_none_when_pulse_open_but_seconds_still_zero(self):
        app = make_app()
        app.vibration_pulse_count = 1  # pulse counted on the ON edge; off hasn't arrived yet
        self.assertIsNotNone(app._vibration_summary())

    def test_summary_shape_when_data_present(self):
        app = make_app()
        app.vibration_pulse_count = 31
        app.vibration_on_seconds = 412.34
        app.first_vibration_at = datetime(2026, 7, 27, 11, 40, 0, tzinfo=timezone.utc)
        app.last_vibration_at = datetime(2026, 7, 27, 14, 0, 0, tzinfo=timezone.utc)

        summary = app._vibration_summary()

        self.assertEqual(summary["pulse_count"], 31)
        self.assertAlmostEqual(summary["on_seconds"], 412.3, places=1)
        self.assertIsNotNone(summary["first_at"])
        self.assertIsNotNone(summary["last_at"])
        self.assertEqual(set(summary.keys()), {"pulse_count", "on_seconds", "first_at", "last_at"})


class ScheduleVibrationUnloadPatch(unittest.TestCase):
    """_schedule_vibration_unload_patch: no-ops when unconfigured or ts-less; otherwise
    arms a single run_in(_patch_unload_vibration, 600, ...) handle, cancelling any prior
    pending one (see the docstring in washer_monitor.py for why a single handle is enough)."""

    def test_noop_when_sensor_unconfigured(self):
        app = make_app(vibration_sensor=None)
        app._schedule_vibration_unload_patch({"ts": "2026-07-27T10:00:00+02:00"})
        self.assertEqual(app.scheduled, [])
        self.assertIsNone(app._unload_patch_timer)

    def test_noop_when_record_has_no_ts(self):
        app = make_app()
        app._schedule_vibration_unload_patch({})
        self.assertEqual(app.scheduled, [])

    def test_schedules_patch_600s_out_with_ts_and_save_moment(self):
        app = make_app()
        now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        app._now_utc = lambda: now

        app._schedule_vibration_unload_patch({"ts": "2026-07-27T10:00:00+02:00"})

        self.assertEqual(len(app.scheduled), 1)
        cb, delay, kw = app.scheduled[0]
        self.assertEqual(cb, app._patch_unload_vibration)
        self.assertEqual(delay, 600)
        self.assertEqual(kw["ts"], "2026-07-27T10:00:00+02:00")
        self.assertEqual(kw["save_moment"], app._format_utc(now))
        self.assertIsNotNone(app._unload_patch_timer)

    def test_second_save_cancels_first_pending_timer(self):
        app = make_app()
        app._now_utc = lambda: datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)

        app._schedule_vibration_unload_patch({"ts": "a"})
        first_handle = app._unload_patch_timer
        app._timer_running = True  # simulate the first timer still pending

        app._schedule_vibration_unload_patch({"ts": "b"})

        self.assertIn(first_handle, app.canceled_timers)
        self.assertEqual(len(app.scheduled), 2)
        self.assertIsNotNone(app._unload_patch_timer)
        self.assertIsNot(app._unload_patch_timer, first_handle)


class UnloadVibrationPatch(unittest.TestCase):
    """(d) _patch_unload_vibration writes unload_pulse_count/unload_window_min into the
    record matching ts, counting only _vibration_events within [save_moment, +10min]."""

    def _write_feedback(self, cycles):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as f:
            json.dump({"version": 2, "cycles": cycles}, f)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_patches_matching_record_with_pulse_count_in_window_only(self):
        app = make_app()
        ts_target = "2026-07-27T14:00:00+02:00"
        path = self._write_feedback([
            {"ts": "2026-07-27T10:00:00+02:00", "confirmed": "eco"},
            {"ts": ts_target, "confirmed": "bomuld"},
        ])
        app.feedback_file = path

        save_moment = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        app._vibration_events = collections.deque([
            save_moment - timedelta(seconds=5),               # before window - excluded
            save_moment + timedelta(seconds=1),                # inside
            save_moment + timedelta(minutes=5),                 # inside
            save_moment + timedelta(minutes=10),                # inside (inclusive boundary)
            save_moment + timedelta(minutes=10, seconds=1),    # after window - excluded
        ], maxlen=500)

        app._patch_unload_vibration({"ts": ts_target, "save_moment": app._format_utc(save_moment)})

        with open(path) as f:
            data = json.load(f)
        target = next(c for c in data["cycles"] if c["ts"] == ts_target)
        other = next(c for c in data["cycles"] if c["ts"] != ts_target)
        self.assertEqual(target["vibration"]["unload_pulse_count"], 3)
        self.assertEqual(target["vibration"]["unload_window_min"], 10)
        self.assertNotIn("vibration", other)
        self.assertIsNone(app._unload_patch_timer)

    def test_missing_record_logs_warning_and_leaves_file_untouched(self):
        app = make_app()
        path = self._write_feedback([{"ts": "2026-07-27T10:00:00+02:00", "confirmed": "eco"}])
        app.feedback_file = path
        save_moment = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)

        app._patch_unload_vibration({"ts": "no-such-ts", "save_moment": app._format_utc(save_moment)})

        self.assertTrue(_warning_logged(app))
        with open(path) as f:
            data = json.load(f)
        self.assertNotIn("vibration", data["cycles"][0])

    def test_missing_feedback_file_logs_warning_and_does_not_raise(self):
        app = make_app()
        app.feedback_file = "/nonexistent/path/should/not/exist.json"
        save_moment = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)

        app._patch_unload_vibration({"ts": "whatever", "save_moment": app._format_utc(save_moment)})

        self.assertTrue(_warning_logged(app))

    def test_missing_ts_or_save_moment_is_a_silent_noop(self):
        app = make_app()
        app.feedback_file = "/should/not/be/touched.json"

        app._patch_unload_vibration({})

        self.assertEqual(app.log_calls, [])


if __name__ == "__main__":
    unittest.main()
