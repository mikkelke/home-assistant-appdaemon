# tests/test_washer_drum_clean.py - the drum clean as its own washer programme: last_drum_clean on
# sensor.washer_state for HouseUpcoming's monthly row, the confirm push and the programme picker
# that record one, and its place in learning.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_washer_restart_survival as trs  # noqa: E402  (installs the appdaemon stub)

import washer_classify as wcls  # noqa: E402
import washer_feedback as wfb  # noqa: E402
import washer_profiles as wp  # noqa: E402

PROGRAMMES = yaml.safe_load((Path(wfb.__file__).with_name("washer_programmes.yaml")).read_text(encoding="utf-8"))
PROFILES = wp.merge_profiles(PROGRAMMES["programmes"])


def record(ts, confirmed="drum_clean", temp=None, user=True, completion="completed", end=None,
           duration=150.0, energy=1.4, bursts=3, valid=True):
    rec = {
        "ts": ts,
        "duration_min": duration,
        "energy_kwh": energy,
        "heating_bursts": bursts,
        "max_power_w": 2100.0,
        "predicted": "bomuld",
        "predicted_temperature": "90",
        "confirmed": confirmed,
        "confirmed_temperature": temp,
        "programme_user_confirmed": user,
        "completion_class": completion,
        "valid_for_learning": valid,
    }
    if end:
        rec["effective_end_at"] = end
    return rec


def utc(ts):
    return datetime.fromisoformat(ts).astimezone(timezone.utc).isoformat()


class ProgrammeTests(unittest.TestCase):
    def test_a_programme_of_its_own_without_a_temperature(self):
        self.assertEqual(PROGRAMMES["programme_display_order"][-1], "drum_clean")
        self.assertEqual(PROFILES["drum_clean"]["label"], "Drum clean")
        self.assertFalse(wp.programme_has_temperature(PROFILES, "drum_clean"))
        self.assertEqual(wp.learn_key_for(PROFILES, "drum_clean", "90°C"), "drum_clean")
        self.assertEqual(wp.build_label_to_key(PROFILES, PROGRAMMES["programme_display_order"])["Drum clean"],
                         "drum_clean")

    def test_bomuld_90s_envelope_decides_whether_it_counts(self):
        def completion(duration, energy):
            return wcls.classify_cycle_completion(
                run_minutes=duration, energy_kwh=energy, confirmed="drum_clean", transition_path="spin_end",
                profile=wp.get_profile(PROFILES, "drum_clean"), user_conf=True, guard_fraction=0.60,
                min_cycle_minutes=25, min_energy_kwh=0.1, validation_key="drum_clean")["completion_class"]

        self.assertEqual(completion(147.0, 1.40), "completed")
        self.assertEqual(completion(20.0, 0.30), "interrupted")
        self.assertEqual(completion(199.0, 0.80), "suspect")


class LastDrumCleanTests(unittest.TestCase):
    def test_no_record_is_unknown(self):
        self.assertEqual(wfb.last_drum_clean([]), "")
        self.assertEqual(wfb.last_drum_clean(None), "")

    def test_a_confirmed_drum_clean_counts_at_its_effective_end(self):
        rec = record("2026-10-14T15:12:00+02:00", end="2026-10-14T14:31:30+02:00")
        self.assertEqual(wfb.last_drum_clean([rec]), "2026-10-14T12:31:30+00:00")
        self.assertEqual(wfb.last_drum_clean([record("2026-10-14T15:12:00+02:00")]), "2026-10-14T13:12:00+00:00")

    def test_only_a_completed_clean_a_person_confirmed_counts(self):
        self.assertEqual(wfb.last_drum_clean([record("2026-10-14T15:12:00+02:00", user=False)]), "")
        self.assertEqual(wfb.last_drum_clean([record("2026-10-14T15:12:00+02:00", completion="interrupted")]), "")
        self.assertEqual(wfb.last_drum_clean([record("2026-10-14T15:12:00+02:00", completion="suspect")]), "")

    def test_a_laundry_bomuld_90_is_no_drum_clean(self):
        self.assertEqual(wfb.last_drum_clean([record("2026-10-14T15:12:00+02:00", confirmed="bomuld", temp="90")]), "")

    def test_the_newest_clean_wins(self):
        cycles = [
            record("2026-08-01T10:00:00+02:00"),
            record("2026-09-01T10:00:00+02:00"),
            record("2026-09-20T10:00:00+02:00", confirmed="bomuld", temp="90"),
        ]
        self.assertEqual(wfb.last_drum_clean(cycles), "2026-09-01T08:00:00+00:00")


class LaundryLearningTests(unittest.TestCase):
    """A drum clean draws what a laundry Bomuld 60 or 90 draws, so it keeps its own duration bucket
    but never becomes a centroid the classifier could answer."""

    LAUNDRY = [record(f"2026-06-{day}T14:00:00+02:00", confirmed="bomuld", temp="90", duration=147.0, energy=1.402,
                      bursts=13) for day in (10, 11, 12)]
    DRUM = [record(f"2026-07-{day}T14:00:00+02:00", duration=140.0, energy=1.2, bursts=12) for day in (10, 11)]

    def test_no_centroid_but_its_own_duration(self):
        buckets, centroids, _ = wfb.aggregate_cycles(self.LAUNDRY + self.DRUM, PROFILES)
        self.assertEqual(centroids, wfb.aggregate_cycles(self.LAUNDRY, PROFILES)[1])
        self.assertEqual(set(centroids), {"bomuld|90°C"})
        self.assertEqual(buckets["drum_clean"]["durations"], [140.0, 140.0])
        self.assertEqual(buckets["bomuld|90°C"]["durations"], [147.0] * 3)

    def test_the_classifier_never_answers_a_drum_clean(self):
        _, centroids, _ = wfb.aggregate_cycles(self.LAUNDRY + self.DRUM, PROFILES)
        self.assertEqual(wcls.classify_from_history(centroids, 140.0, 1.2, 12), "bomuld|90°C")


class ConfirmTests(unittest.TestCase):
    TS = trs.NOW.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _app(self, cycles, sensor_state="Off"):
        tmpdir = tempfile.mkdtemp(prefix="washer_drum_")
        path = os.path.join(tmpdir, "washer_feedback.json")
        with open(path, "w") as f:
            json.dump({"version": 2, "cycles": cycles}, f)
        app = trs.make_full_init_app(sensor_state=sensor_state, helper_state="Off", extra_args={"feedback_file": path})
        app.sent = []

        class Notifier:
            def notify(self, **kwargs):
                app.sent.append(kwargs)

            def clear_notification(self, **kwargs):
                return None

        app.get_app = lambda name: Notifier() if name == "MobileNotifier" else None
        app.create_task = lambda task: task
        return app, path

    def _published(self, app):
        return [c["attributes"].get("last_drum_clean") for c in app.set_state_calls
                if c["entity"] == "sensor.washer_state" and c["attributes"] is not None]

    @staticmethod
    def _saved(path):
        with open(path) as f:
            return json.load(f)["cycles"][-1]

    def test_the_push_action_round_trips(self):
        action = wfb.encode_confirm_action(self.TS, "drum_clean", None)
        self.assertEqual(action, f"WASHER_CONFIRM|{self.TS}|drum_clean|-")
        self.assertEqual(wfb.parse_confirm_action(action), (self.TS, "drum_clean", None))

    def test_every_confirm_push_offers_it(self):
        app, _ = self._app([])
        app._send_confirm_push({"ts": self.TS, "predicted": "bomuld", "predicted_temperature": "60"})
        app._send_confirm_push({"ts": self.TS, "predicted": "unknown"})
        guessed, unknown = (sent["data"]["data"]["actions"] for sent in app.sent)
        self.assertEqual([a["title"] for a in guessed], ["Yes, Bomuld 60°C", "Drum clean", "Other..."])
        self.assertEqual([a["title"] for a in unknown], ["Drum clean", "Choose in dashboard"])
        self.assertEqual(guessed[1], unknown[0])
        self.assertEqual(wfb.parse_confirm_action(guessed[1]["action"]), (self.TS, "drum_clean", None))

    def test_tapping_it_records_the_clean_and_republishes_at_once(self):
        app, path = self._app([record(self.TS, confirmed="bomuld", temp="90", user=False)])
        app.set_state_calls.clear()
        app._on_confirm_push_action("mobile_app_notification_action",
                                    {"action": wfb.encode_confirm_action(self.TS, "drum_clean", None)}, {})
        saved = self._saved(path)
        self.assertEqual((saved["confirmed"], saved["confirmed_temperature"], saved["programme_user_confirmed"]),
                         ("drum_clean", None, True))
        self.assertEqual((saved["completion_class"], saved["valid_for_learning"]), ("completed", True))
        self.assertEqual(self._published(app)[-1], utc(self.TS))
        self.assertEqual(app.states["sensor.washer_state"], "Off")

    def test_the_programme_picker_offers_it_and_stores_no_temperature(self):
        app, path = self._app([record(self.TS, confirmed="bomuld", temp="90", user=False)])
        self.assertEqual(app._programme_display_order[-1], "drum_clean")
        self.assertEqual(app._LABEL_TO_KEY["Drum clean"], "drum_clean")
        app.set_state_calls.clear()
        app._update_last_feedback_user_confirmed("drum_clean", "90°C", "Mikkel")
        saved = self._saved(path)
        self.assertEqual((saved["confirmed"], saved["confirmed_temperature"]), ("drum_clean", None))
        self.assertEqual(self._published(app)[-1], utc(self.TS))

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

    def test_a_confirmation_that_changes_nothing_publishes_nothing(self):
        app, _ = self._app([record("2026-01-01T10:00:00+01:00"), record(self.TS, confirmed="bomuld", temp="60", user=False)])
        app.set_state_calls.clear()
        app._update_last_feedback_user_confirmed("bomuld", "60°C", "Mikkel")
        self.assertEqual(self._published(app), [])


if __name__ == "__main__":
    unittest.main()
