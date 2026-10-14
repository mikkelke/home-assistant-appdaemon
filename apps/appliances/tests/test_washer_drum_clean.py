# tests/test_washer_drum_clean.py - last_drum_clean on sensor.washer_state: the newest completed,
# user-confirmed Bomuld 90 cycle, for HouseUpcoming's monthly drum-clean row.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_washer_restart_survival as trs  # noqa: E402  (installs the appdaemon stub)

import washer_feedback as wfb  # noqa: E402


def record(ts, confirmed="bomuld", temp="90", user=True, completion="completed", predicted_temp=None, end=None):
    rec = {
        "ts": ts,
        "duration_min": 150.0,
        "energy_kwh": 1.4,
        "heating_bursts": 3,
        "max_power_w": 2100.0,
        "predicted": "bomuld",
        "predicted_temperature": predicted_temp if predicted_temp is not None else temp,
        "confirmed": confirmed,
        "confirmed_temperature": temp,
        "programme_user_confirmed": user,
        "completion_class": completion,
    }
    if end:
        rec["effective_end_at"] = end
    return rec


class LastDrumCleanTests(unittest.TestCase):
    def test_no_record_is_unknown(self):
        self.assertEqual(wfb.last_drum_clean([]), "")
        self.assertEqual(wfb.last_drum_clean(None), "")

    def test_predicted_90_is_not_a_clean(self):
        self.assertEqual(wfb.last_drum_clean([record("2026-06-10T14:01:15+02:00", user=False)]), "")

    def test_interrupted_90_is_not_a_clean(self):
        self.assertEqual(wfb.last_drum_clean([record("2026-06-10T14:01:15+02:00", completion="interrupted")]), "")
        self.assertEqual(wfb.last_drum_clean([record("2026-06-10T14:01:15+02:00", completion="suspect")]), "")

    def test_confirmed_completed_90_counts_at_its_effective_end(self):
        rec = record("2026-06-10T14:01:15+02:00", end="2026-06-10T13:21:30+02:00")
        self.assertEqual(wfb.last_drum_clean([rec]), "2026-06-10T11:21:30+00:00")

    def test_corrected_away_from_90_no_longer_counts(self):
        rec = record("2026-06-10T14:01:15+02:00", temp="60", predicted_temp="90")
        self.assertEqual(wfb.last_drum_clean([rec]), "")
        self.assertEqual(wfb.last_drum_clean([record("2026-06-10T14:01:15+02:00", confirmed="eco")]), "")

    def test_newest_clean_wins_and_degree_sign_is_accepted(self):
        cycles = [
            record("2026-08-01T10:00:00+02:00"),
            record("2026-09-01T10:00:00+02:00", temp="90°C"),
            record("2026-09-20T10:00:00+02:00", temp="60"),
        ]
        self.assertEqual(wfb.last_drum_clean(cycles), "2026-09-01T08:00:00+00:00")


class StateEntityTests(unittest.TestCase):
    def _app(self, cycles, sensor_state="Off"):
        tmpdir = tempfile.mkdtemp(prefix="washer_drum_")
        path = os.path.join(tmpdir, "washer_feedback.json")
        with open(path, "w") as f:
            json.dump({"version": 2, "cycles": cycles}, f)
        app = trs.make_full_init_app(sensor_state=sensor_state, helper_state="Off", extra_args={"feedback_file": path})
        return app, path

    def _published(self, app):
        return [c["attributes"].get("last_drum_clean") for c in app.set_state_calls
                if c["entity"] == "sensor.washer_state" and c["attributes"] is not None]

    def test_the_restore_publish_after_an_ha_restart_carries_it(self):
        app, _ = self._app([record("2026-09-01T10:00:00+02:00")], sensor_state=None)
        calls = [c for c in app.set_state_calls if c["entity"] == "sensor.washer_state"]
        self.assertTrue(calls)
        self.assertFalse(any(c["replace"] for c in calls if set(c["attributes"]) == {"last_drum_clean"}))
        self.assertEqual({c["attributes"]["last_drum_clean"] for c in calls}, {"2026-09-01T08:00:00+00:00"})

    def test_every_later_publish_carries_it_and_unknown_is_an_empty_string(self):
        app, _ = self._app([record("2026-06-10T14:01:15+02:00", user=False)])
        app._set_state_entity(state="Running", attributes={"progress_pct": 5}, replace=True)
        app._set_state_entity(state="Off")
        self.assertEqual(self._published(app), ["", ""])
        self.assertEqual(app.attrs_store["sensor.washer_state"], {"progress_pct": 5, "last_drum_clean": ""})

    def test_a_retrospective_confirmation_republishes_at_once(self):
        now = trs.NOW
        ts = now.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        app, _ = self._app([record(ts, user=False)])
        app.set_state_calls.clear()
        app._update_last_feedback_user_confirmed("bomuld", "90°C", "Mikkel")
        published = self._published(app)
        self.assertEqual(published[-1], datetime.fromisoformat(ts).astimezone(timezone.utc).isoformat())
        self.assertEqual(app.states["sensor.washer_state"], "Off")

    def test_a_confirmation_that_changes_nothing_publishes_nothing(self):
        now = trs.NOW
        ts = now.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        app, _ = self._app([record("2026-01-01T10:00:00+01:00"), record(ts, temp="60", user=False)])
        app.set_state_calls.clear()
        app._update_last_feedback_user_confirmed("bomuld", "60°C", "Mikkel")
        self.assertEqual(self._published(app), [])


if __name__ == "__main__":
    unittest.main()
