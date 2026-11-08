"""ETA of an unconfirmed dryer run: the median length of the recent real runs that lasted longer
than this one has so far, instead of the programme the energy-and-time classifier guesses mid-run
(which reads every young run as Finish uld, then Skjorter)."""

from __future__ import annotations

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

import dryer_monitor as dm  # noqa: E402

_ORIGINAL_PROFILES = dm.DryerMonitor.PROGRAMME_PROFILES


def tearDownModule():
    dm.DryerMonitor.PROGRAMME_PROFILES = _ORIGINAL_PROFILES


START = datetime(2026, 11, 8, 13, 16, 55, tzinfo=timezone.utc)
DURATIONS = [48.0, 49.0, 64.0, 81.0, 88.0, 95.0, 101.0, 106.0, 121.0, 135.0, 158.0]


def make_app(elapsed_min, *, energy_kwh=0.06, confirmed_key=None, durations=DURATIONS):
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)
    app.state_entity = "sensor.dryer_state"
    app.states = {app.state_entity: "Running"}
    app.published = {}
    app.start_time = START
    app.energy_start = 95.9
    app.keep_fresh_detected = False
    app.main_cycle_power = 400
    app.overrun_floor_min = 10
    app._run_durations = tuple(durations)
    app._learned_durations = {}
    dm.DryerMonitor.PROGRAMME_PROFILES = {
        "skjorter__skabstoert": {"label": "Skjorter - Skabstørt", "duration_min": 60},
        "bomuld__strygetoert": {"label": "Bomuld - Strygetørt", "duration_min": 100},
        "finish_uld": {"label": "Finish uld", "duration_min": 5},
    }
    app._now_utc = lambda: START + timedelta(minutes=elapsed_min)
    app._get_energy_used = lambda: energy_kwh
    app._get_current_power = lambda: 600
    app._get_confirmed_programme_key = lambda: confirmed_key
    app._started_by_attrs = lambda: {}
    app._format_utc = lambda dt: dt.isoformat()
    app._format_local = lambda dt: dt.isoformat()

    def get_state(entity, attribute=None, **kw):
        if attribute == "all":
            return {"attributes": dict(app.published)}
        return app.states.get(entity)

    def set_state_entity(state=None, attributes=None, replace=False):
        app.published = dict(attributes or {})

    app.get_state = get_state
    app._set_state_entity = set_state_entity
    return app


class UnconfirmedRunUsesTypicalLength(unittest.TestCase):
    def test_young_run_is_not_read_as_finish_uld(self):
        app = make_app(6, energy_kwh=0.04)
        app._update_running_attributes()
        self.assertEqual(app.published["detected_programme"], "finish_uld")
        self.assertEqual(app.published["programme_duration_min"], 95)
        self.assertEqual(app.published["estimated_remaining_min"], 89)
        self.assertLess(app.published["progress_pct"], 10)

    def test_typical_length_grows_with_elapsed_time(self):
        app = make_app(90, energy_kwh=0.9)
        app._update_running_attributes()
        self.assertEqual(app.published["programme_duration_min"], 114)

    def test_past_the_longest_runs_falls_back_to_the_classifier(self):
        app = make_app(140, energy_kwh=1.4)
        app._update_running_attributes()
        self.assertGreater(app.published.get("overrun_min", 0), 0)

    def test_confirmed_programme_wins(self):
        app = make_app(6, energy_kwh=0.04, confirmed_key="skjorter__skabstoert")
        app._update_running_attributes()
        self.assertEqual(app.published["programme_duration_min"], 60)

    def test_no_history_keeps_the_classifier(self):
        app = make_app(20, energy_kwh=0.2, durations=[])
        app._update_running_attributes()
        self.assertEqual(app.published["programme_duration_min"], 60)


class RunDurationsComeFromRealRuns(unittest.TestCase):
    def test_feedback_load_skips_tiny_and_energyless_runs(self):
        cycles = [
            {"duration_min": 101.4, "energy_kwh": 1.05, "confirmed": "x"},
            {"duration_min": 7.1, "energy_kwh": 0.002, "confirmed": "finish_uld"},
            {"duration_min": 121.8, "energy_kwh": None, "confirmed": "unknown"},
            {"duration_min": 48.4, "energy_kwh": 0.34, "confirmed": "y"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dryer_feedback.json")
            with open(path, "w") as f:
                json.dump({"version": 2, "cycles": cycles}, f)
            app = dm.DryerMonitor.__new__(dm.DryerMonitor)
            app.feedback_file = path
            app.log = lambda *a, **kw: None
            app._load_and_apply_feedback()
        self.assertEqual(app._run_durations, (101.4, 48.4))


if __name__ == "__main__":
    unittest.main()
