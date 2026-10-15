# tests/test_washer_drum_clean.py - the drum clean as its own washer programme: last_drum_clean on
# sensor.washer_state for HouseUpcoming's drum row, the confirm push button (only on Mikkel's
# phone, only while one is due, every INTERVAL_RUNS runs) and the programme picker that record one,
# and its place in learning.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "notify"))

import test_washer_restart_survival as trs  # noqa: E402  (installs the appdaemon stub)

import drum_clean  # noqa: E402
import mobile_notifier as mn  # noqa: E402
import run_ledger  # noqa: E402
import washer_classify as wcls  # noqa: E402
import washer_feedback as wfb  # noqa: E402
import washer_profiles as wp  # noqa: E402

PROGRAMMES = yaml.safe_load((Path(wfb.__file__).with_name("washer_programmes.yaml")).read_text(encoding="utf-8"))
PROFILES = wp.merge_profiles(PROGRAMMES["programmes"])
PROGRAMME_SELECT = "input_select.washer_confirmed_programme"
TEMPERATURE_SELECT = "input_select.washer_temperature"
DAY = timedelta(days=1)


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


def washer(cycles, sensor_state="Off", call_service_fn=None, **args):
    """(app, feedback path): a WasherMonitor run through its real initialize() on a feedback file
    holding `cycles`, with a MobileNotifier that records pushes in app.sent."""
    tmpdir = tempfile.mkdtemp(prefix="washer_drum_")
    path = os.path.join(tmpdir, "washer_feedback.json")
    with open(path, "w") as f:
        json.dump({"version": 2, "cycles": cycles}, f)
    app = trs.make_full_init_app(sensor_state=sensor_state, helper_state="Off",
                                 extra_args={"feedback_file": path, **args}, call_service_fn=call_service_fn)
    app.sent = []

    class Notifier:
        def notify(self, **kwargs):
            app.sent.append(kwargs)

        def clear_notification(self, **kwargs):
            return None

    app.get_app = lambda name: Notifier() if name == "MobileNotifier" else None
    app.create_task = lambda task: task
    return app, path


class ConfirmTests(unittest.TestCase):
    TS = trs.NOW.astimezone(timezone.utc).replace(microsecond=0).isoformat()

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

    def test_tapping_it_records_the_clean_and_republishes_at_once(self):
        app, path = washer([record(self.TS, confirmed="bomuld", temp="90", user=False)])
        app.set_state_calls.clear()
        app._on_confirm_push_action("mobile_app_notification_action",
                                    {"action": wfb.encode_confirm_action(self.TS, "drum_clean", None)}, {})
        saved = self._saved(path)
        self.assertEqual((saved["confirmed"], saved["confirmed_temperature"], saved["programme_user_confirmed"]),
                         ("drum_clean", None, True))
        self.assertEqual((saved["completion_class"], saved["valid_for_learning"]), ("completed", True))
        self.assertEqual(self._published(app)[-1], utc(self.TS))
        self.assertEqual(app.states["sensor.washer_state"], "Off")

    def test_the_programme_picker_offers_it_and_drops_a_leftover_temperature(self):
        calls = []
        app, path = washer([record(self.TS, confirmed="bomuld", temp="90", user=False)],
                           confirm_entity=PROGRAMME_SELECT, temperature_entity=TEMPERATURE_SELECT,
                           call_service_fn=lambda service, **kw: calls.append((service, kw)))
        options = [kw["options"] for service, kw in calls
                   if service == "input_select/set_options" and kw["entity_id"] == PROGRAMME_SELECT]
        self.assertEqual(options, [["Auto (unconfirmed)", "Ekspres", "Uld", "Bomuld", "Finvask", "Strygelet", "ECO",
                                    "Mørkt/Denim", "Outdoor", "Imprægnering", "Pumpe/Centrifugering",
                                    "Kun skyl/stivelse", "Drum clean"]])
        app.states[TEMPERATURE_SELECT] = "90°C"
        app.set_state_calls.clear()
        app._on_confirm_changed(PROGRAMME_SELECT, "state", "Bomuld", "Drum clean", {})
        saved = self._saved(path)
        self.assertEqual((saved["confirmed"], saved["confirmed_temperature"], saved["programme_user_confirmed"]),
                         ("drum_clean", None, True))
        self.assertEqual(self._published(app)[-1], utc(self.TS))

    def test_the_restore_publish_after_an_ha_restart_carries_it(self):
        app, _ = washer([record("2026-09-01T10:00:00+02:00")], sensor_state=None)
        calls = [c for c in app.set_state_calls if c["entity"] == "sensor.washer_state"]
        self.assertTrue(calls)
        self.assertFalse(any(c["replace"] for c in calls if set(c["attributes"]) == {"last_drum_clean"}))
        self.assertEqual({c["attributes"]["last_drum_clean"] for c in calls}, {"2026-09-01T08:00:00+00:00"})

    def test_every_later_publish_carries_it_and_unknown_is_an_empty_string(self):
        app, _ = washer([record("2026-06-10T14:01:15+02:00", user=False)])
        app._set_state_entity(state="Running", attributes={"progress_pct": 5}, replace=True)
        app._set_state_entity(state="Off")
        self.assertEqual(self._published(app), ["", ""])
        self.assertEqual(app.attrs_store["sensor.washer_state"], {"progress_pct": 5, "last_drum_clean": ""})

    def test_a_confirmation_that_changes_nothing_publishes_nothing(self):
        app, _ = washer([record("2026-01-01T10:00:00+01:00"), record(self.TS, confirmed="bomuld", temp="60", user=False)])
        app.set_state_calls.clear()
        app._update_last_feedback_user_confirmed("bomuld", "60°C", "Mikkel")
        self.assertEqual(self._published(app), [])


class ConfirmPushButtonTests(unittest.TestCase):
    """The "Drum clean" button reaches a phone only while a drum clean is due, which is
    drum_clean.INTERVAL_RUNS runs after the last one, and only when the phone is Mikkel's. Each push
    goes through the real MobileNotifier, which hands every phone its own buttons; every other push
    keeps the buttons it always had."""

    TS = ConfirmTests.TS
    NOW = trs.NOW
    GUESSED = ["Yes, Bomuld 60°C", "Other..."]

    def laundry(self, count, newest_end=None):
        """`count` laundry runs, the newest ending at `newest_end`, each four hours before the next."""
        newest_end = newest_end or self.NOW - timedelta(hours=1)
        return [record((newest_end - timedelta(hours=4 * i)).isoformat(), confirmed="bomuld", temp="60", user=False)
                for i in range(count - 1, -1, -1)]

    def _phones(self, cycles, started_by, predicted="bomuld", home=("mikkel", "kristine")):
        """{person: button titles} on every phone the confirm push for the cycle at TS reaches."""
        app, _ = washer(cycles)
        notifier = mn.MobileNotifier.__new__(mn.MobileNotifier)
        notifier.person_entities = ["person.mikkel", "person.kristine", "person.claudia"]
        notifier.device_mapping = {"mikkel": ["notify.mobile_app_mikkels_ofx9p"],
                                   "kristine": ["notify.mobile_app_kristine_iphone_2"],
                                   "claudia": ["notify.mobile_app_claudias_iphone"]}
        notifier.category_audience, notifier.platform_map, notifier.user_notification_service = {}, {}, None
        notifier.log = lambda *a, **kw: None
        sent = []

        async def call_service(service, **kwargs):
            sent.append((service, kwargs))

        async def get_state(entity, **kwargs):
            return "home" if entity.removeprefix("person.") in home else "not_home"

        notifier.call_service, notifier.get_state = call_service, get_state
        tasks = []
        app.get_app = lambda name: notifier if name == "MobileNotifier" else None
        app.create_task = tasks.append
        app._send_confirm_push({"ts": self.TS, "predicted": predicted, "predicted_temperature": "60",
                                "started_by": started_by})
        for task in tasks:
            asyncio.run(task)
        return {notifier._person_for_service(service.replace("notify/", "notify.", 1)):
                [action["title"] for action in kwargs["data"]["actions"]] for service, kwargs in sent}

    def test_not_due_no_button(self):
        cleaned = [record((self.NOW - 100 * DAY).isoformat())] + self.laundry(14)
        self.assertEqual(self._phones(cleaned, "mikkel"), {"mikkel": self.GUESSED})
        self.assertEqual(self._phones(cleaned, None), {"mikkel": self.GUESSED, "kristine": self.GUESSED})
        self.assertEqual(self._phones([], "mikkel"), {"mikkel": self.GUESSED})

    def test_due_but_sent_to_a_housemate_no_button(self):
        self.assertEqual(self._phones(self.laundry(15), "kristine"), {"kristine": self.GUESSED})
        self.assertEqual(self._phones(self.laundry(15), "claudia", predicted="unknown"),
                         {"claudia": ["Choose in dashboard"]})

    def test_due_and_sent_to_mikkel_the_button_appears(self):
        old_clean = [record((self.NOW - 300 * DAY).isoformat())]
        for cycles in (self.laundry(15), old_clean + self.laundry(15)):
            self.assertEqual(self._phones(cycles, "mikkel"),
                             {"mikkel": ["Yes, Bomuld 60°C", "Drum clean", "Other..."]})
        self.assertEqual(self._phones(self.laundry(15), "mikkel", predicted="unknown"),
                         {"mikkel": ["Drum clean", "Choose in dashboard"]})

    def test_a_home_broadcast_gives_it_to_mikkels_phone_only(self):
        self.assertEqual(self._phones(self.laundry(15), None),
                         {"mikkel": ["Yes, Bomuld 60°C", "Drum clean", "Other..."], "kristine": self.GUESSED})

    def test_the_button_confirms_the_cycle_as_a_drum_clean(self):
        app, _ = washer(self.laundry(15))
        actions = app._drum_clean_actions(self.TS, [{"action": "URI", "title": "Other...", "uri": "x"}])("mikkel")
        self.assertEqual(wfb.parse_confirm_action(actions[0]["action"]), (self.TS, "drum_clean", None))

    def test_an_unreadable_feedback_file_offers_no_button(self):
        app, path = washer(self.laundry(15))
        os.remove(path)
        self.assertIsNone(app._drum_clean_actions(self.TS, [{"action": "URI", "title": "Other...", "uri": "x"}]))


class DueTests(unittest.TestCase):
    """drum_clean.due counts finished runs after the last confirmed clean, or all of them while none is."""

    END = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def laundry(self, count, first_end):
        return [record((first_end + timedelta(hours=4 * i)).isoformat(), confirmed="bomuld", temp="60", user=False)
                for i in range(count)]

    def ledger(self, *groups):
        return run_ledger.ledger([rec for group in groups for rec in group])

    def test_the_interval_is_fifteen_runs(self):
        self.assertEqual(drum_clean.INTERVAL_RUNS, 15)

    def test_fifteen_runs_after_the_last_clean_make_it_due(self):
        clean = [record((self.END - 10 * DAY).isoformat())]
        for count, due in ((0, False), (14, False), (15, True), (40, True)):
            led = self.ledger(clean, self.laundry(count, self.END - 9 * DAY))
            self.assertEqual((drum_clean.runs_since(led), drum_clean.due(led)), (count, due), count)

    def test_none_on_record_counts_every_run(self):
        for count, due in ((0, False), (14, False), (15, True)):
            led = self.ledger(self.laundry(count, self.END - 9 * DAY))
            self.assertEqual((drum_clean.last_clean(led), drum_clean.runs_since(led), drum_clean.due(led)),
                             (None, count, due), count)

    def test_calendar_time_does_not_matter(self):
        quiet = self.ledger([record((self.END - 200 * DAY).isoformat())], self.laundry(3, self.END - 100 * DAY))
        self.assertFalse(drum_clean.due(quiet))
        busy = self.ledger([record((self.END - 5 * DAY).isoformat())], self.laundry(15, self.END - 4 * DAY))
        self.assertTrue(drum_clean.due(busy))

    def test_an_unconfirmed_bomuld_90_is_no_clean_and_an_interrupted_wash_is_no_run(self):
        led = self.ledger([record((self.END - 10 * DAY).isoformat(), confirmed="bomuld", temp="90", user=False)],
                          self.laundry(14, self.END - 9 * DAY))
        self.assertEqual(drum_clean.runs_since(led), 15)
        cut = [record((self.END - DAY).isoformat(), confirmed="ekspres", user=False, completion="interrupted")]
        self.assertEqual(drum_clean.runs_since(self.ledger(cut, self.laundry(14, self.END - 9 * DAY))), 14)

    def test_the_run_a_clean_was_saved_twice_for_is_not_a_run_since(self):
        first = record((self.END - 10 * DAY).isoformat(), duration=150.0)
        again = record((self.END - 10 * DAY + timedelta(minutes=40)).isoformat(), user=False, completion="suspect",
                       duration=190.0)
        self.assertEqual(drum_clean.runs_since(self.ledger([first, again])), 0)


if __name__ == "__main__":
    unittest.main()
