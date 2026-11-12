"""ETA of an unconfirmed washer run: the median length of the household's washes that lasted longer than this one has
so far and used the energy it has, instead of the programme the classifier guesses mid-run. The washes here are
synthetic: four light ones of 60 minutes and four heavy ones of 180, each with its energy profile in RunProfiles' cache.
Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta

import test_washer_restart_survival as trs  # installs the appdaemon stub and the app directory on sys.path

import run_profile_model as model  # noqa: E402
import washer_eta as weta  # noqa: E402

START = trs.NOW
ENERGY = "sensor.washer_plug_energy"
ENERGY_START = 0.10
CONFIRM = "input_select.washer_confirmed_programme"
TEMPERATURE = "input_select.washer_temperature"


def record(day, duration, kwh, **extra):
    rec = {"ts": f"2026-10-{day:02d}T10:00:00+00:00", "duration_min": duration, "energy_kwh": kwh,
           "completion_class": "completed"}
    rec.update(extra)
    return rec


def profile(rec):
    """The extraction RunProfiles holds for a wash that drew its energy evenly from a power onset 6 s before the cycle
    start."""
    start = model.parse_epoch(rec["ts"]) - rec["duration_min"] * 60
    minutes = int(rec["duration_min"])
    return {"status": "ok", "cst": start, "onset": start - 6, "end": start + rec["duration_min"] * 60,
            "unavail_s": 0.0, "dwh": [round(rec["energy_kwh"] * 10000 / minutes)] * minutes}


def cache_of(cycles):
    return {model.cache_key("washer", rec): profile(rec) for rec in cycles}


LIGHT = [record(day, 60, 0.3) for day in (1, 2, 3, 4)]
HEAVY = [record(day, 180, 1.8) for day in (5, 6, 7, 8)]
CYCLES = LIGHT + HEAVY


class Marks(unittest.TestCase):
    def test_a_profile_is_shifted_to_the_cycle_start_and_scaled_to_the_metered_energy(self):
        rec = record(1, 60, 0.6)
        entry = profile(rec)
        entry["onset"] = entry["cst"] + 60
        marks = weta.profile_marks(entry, 60, 0.66)
        self.assertEqual(len(marks), 12)
        self.assertEqual(marks[:3], [0.044, 0.099, 0.154])
        self.assertAlmostEqual(marks[-1], 0.649, places=3)

    def test_kwh_at_interpolates_between_marks_and_holds_the_last(self):
        marks = [0.05, 0.10, 0.15]
        self.assertEqual([round(weta.kwh_at(marks, m), 3) for m in (0, 2.5, 5, 7.5, 15, 20)],
                         [0.0, 0.025, 0.05, 0.075, 0.15, 0.15])

    def test_a_tick_between_boundaries_interpolates_them(self):
        marks = weta.note_marks([], (3.0, 0.03), 12.0, 0.12)
        self.assertEqual(marks, [0.05, 0.1])
        self.assertEqual(weta.note_marks(marks, (12.0, 0.12), 14.0, 0.14), marks)
        self.assertEqual(weta.note_marks(marks, (12.0, 0.12), 16.0, 0.16), [0.05, 0.1, 0.15])

    def test_a_boundary_no_earlier_tick_brackets_is_missed(self):
        self.assertEqual(weta.note_marks([], None, 12.0, 0.12), [None, None])


class History(unittest.TestCase):
    def test_only_real_washes_count_and_only_validated_profiles_give_marks(self):
        kept = record(1, 60, 0.3)
        mismatched = record(7, 60, 0.3)
        unprofiled = record(8, 60, 0.3)
        cycles = [
            kept,
            record(2, 60, 0.3, completion_class="interrupted"),
            record(3, 60, 0.3, selected_options={"soak": "on"}),
            record(4, 20, 0.3),
            record(5, 300, 3.0),
            record(6, 60, 0.05),
            mismatched,
            unprofiled,
        ]
        entries = cache_of(cycles[:-1])
        entries[model.cache_key("washer", mismatched)]["dwh"] = [200] * 60
        history = weta.eta_history(cycles, entries)
        self.assertEqual([d for d, _ in history], [60.0, 60.0, 60.0])
        self.assertEqual([m is not None for _, m in history], [True, False, False])
        self.assertEqual(history[0][1], weta.profile_marks(entries[model.cache_key("washer", kept)], 60, 0.3))


class Estimate(unittest.TestCase):
    def history(self):
        return weta.eta_history(CYCLES, cache_of(CYCLES))

    def test_before_the_first_mark_it_is_the_median_of_the_longer_washes(self):
        self.assertEqual(weta.typical_total_after(self.history(), [], 0.0, 0), 120)
        self.assertEqual(weta.typical_total_after(self.history(), [], 0.2, 4.0), 120)
        self.assertEqual(weta.typical_total_after(self.history(), [], 0.0, 70.0), 180)

    def test_energy_decides_among_the_washes_that_lasted_longer(self):
        self.assertEqual(weta.typical_total_after(self.history(), [0.05, 0.1, 0.15], 0.2, 21.0), 180)
        self.assertEqual(weta.typical_total_after(self.history(), [0.025, 0.05, 0.075], 0.1, 21.0), 60)

    def test_a_mark_the_run_missed_is_left_out(self):
        self.assertEqual(weta.typical_total_after(self.history(), [None, 0.1, None], 0.2, 21.0), 180)

    def test_too_few_longer_washes_give_no_estimate(self):
        self.assertIsNone(weta.typical_total_after(self.history(), [], 0.0, 180.0))
        self.assertIsNone(weta.typical_total_after([(60.0, None), (90.0, None)], [], 0.0, 0))

    def test_washes_without_marks_fall_back_to_their_durations(self):
        history = [(d, None) for d, _ in self.history()]
        self.assertEqual(weta.typical_total_after(history, [0.05, 0.1, 0.15], 0.2, 21.0), 120)
        self.assertEqual(weta.typical_total_after(history[:6], [0.05, 0.1, 0.15], 0.2, 21.0), 60)


class Monitor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def cache_file(self, cycles=CYCLES):
        path = os.path.join(self.tmp, "run_profiles_cache_state.json")
        with open(path, "w") as f:
            json.dump({"version": 1, "entries": cache_of(cycles)}, f)
        return path

    def running_app(self, cache=True, store=None):
        payload = store or trs.make_store_payload(
            state="Running", start_time=START, cycle_id="cycle-typical-eta", detected_programme="eco",
            energy_at_start=ENERGY_START)
        app = trs.make_full_init_app(
            sensor_state=None, helper_state="Off", power_watts=200, store_payload=payload, now=START,
            extra_args={"confirm_entity": CONFIRM, "temperature_entity": TEMPERATURE})
        app.states[CONFIRM] = "Auto (unconfirmed)"
        app.RUN_PROFILES_CACHE = self.cache_file() if cache else os.path.join(self.tmp, "none.json")
        app._reload_learning(CYCLES)
        return app

    def tick(self, app, minutes, kwh):
        app.now = START + timedelta(minutes=minutes)
        app.states[ENERGY] = ENERGY_START + kwh
        app._check_energy_finish({})
        return trs.last_publish(app)["attributes"]

    def run_to(self, app, minutes, per_min):
        for t in [*range(3, minutes, 4), minutes]:
            attrs = self.tick(app, t, per_min * t)
        return attrs

    def test_a_new_run_starts_at_the_median_of_the_real_washes(self):
        app = self.running_app()
        app._begin_running_cycle()
        attrs = trs.last_publish(app)["attributes"]
        self.assertEqual(attrs["programme_duration_min"], 120)
        self.assertEqual(attrs["estimated_remaining_min"], 120)

    def test_without_history_a_new_run_starts_at_the_unknown_programme(self):
        app = self.running_app()
        app._eta_cycles = ()
        app._begin_running_cycle()
        self.assertEqual(trs.last_publish(app)["attributes"]["programme_duration_min"], 180)

    def test_a_heavy_run_expects_the_heavy_washes_and_a_light_one_the_light(self):
        heavy = self.run_to(self.running_app(), 21, 0.01)
        light = self.run_to(self.running_app(), 21, 0.005)
        self.assertEqual(heavy["programme_duration_min"], 180)
        self.assertEqual(light["programme_duration_min"], 60)
        self.assertEqual(light["estimated_remaining_min"], 60 - 21)

    def test_without_the_profiles_the_washes_count_by_duration_alone(self):
        attrs = self.run_to(self.running_app(cache=False), 21, 0.01)
        self.assertEqual(attrs["programme_duration_min"], 120)

    def test_past_the_longest_washes_the_classified_programme_gives_the_duration(self):
        app = self.running_app()
        attrs = self.run_to(app, 185, 0.01)
        self.assertIsNone(weta.typical_total_after(app._eta_history, app._eta_marks, 1.85, 185.0))
        self.assertEqual(attrs["programme_duration_min"], app._get_programme_duration(
            app.detected_programme, app.detected_temperature, use_learned=False))

    def test_a_confirmed_programme_wins(self):
        app = self.running_app()
        app.states[CONFIRM] = "Bomuld"
        app.states[TEMPERATURE] = "30°C"
        app.programme_confirmed_by_user = True
        attrs = self.run_to(app, 21, 0.01)
        self.assertEqual(attrs["programme_duration_min"], app._get_programme_duration("bomuld", "30°C", use_learned=False))
        self.assertNotEqual(attrs["programme_duration_min"], 180)
        self.assertEqual(app._classify_programme(), ("bomuld", "30°C"))

    def test_the_marks_are_never_an_entity_attribute(self):
        attrs = self.run_to(self.running_app(), 21, 0.01)
        self.assertFalse([k for k in attrs if "mark" in k])

    def test_marks_are_stored_and_survive_a_restore_from_the_store(self):
        app = self.running_app()
        self.run_to(app, 15, 0.01)
        self.assertEqual(app._eta_marks, [0.05, 0.1, 0.15])
        app._save_cycle_state(force=True)
        with open(app.args["state_file"]) as f:
            stored = json.load(f)
        self.assertEqual(stored["eta_marks"], [0.05, 0.1, 0.15])
        restored = self.running_app(store=stored)
        self.assertEqual(restored._start_time_source, "durable_store")
        self.assertEqual(restored._eta_marks, [0.05, 0.1, 0.15])
        self.assertIsNone(restored._eta_prev)

    def test_a_new_cycle_begins_without_marks(self):
        app = self.running_app()
        self.run_to(app, 15, 0.01)
        self.assertTrue(app._eta_marks)
        app._begin_running_cycle()
        self.assertEqual((app._eta_marks, app._eta_prev), ([], None))

    def test_a_delayed_start_begins_the_marks_over(self):
        app = self.running_app()
        self.run_to(app, 15, 0.01)
        self.assertTrue(app._eta_marks)
        app._slide_start_for_delayed_start(app.now)
        self.assertEqual((app._eta_marks, app._eta_prev), ([], None))


if __name__ == "__main__":
    unittest.main()
