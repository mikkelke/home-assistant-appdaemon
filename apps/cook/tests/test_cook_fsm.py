import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cook_fsm import PRESETS, Tracker

T = 1800000000


def pushes(effects, kind=None):
    return [effect for effect in effects if effect["type"] == "push" and (kind is None or effect["kind"] == kind)]


def bath_ready(tracker, now=T):
    return tracker.bath_reading(now, "heat", 58, 58, False)


def probe(tracker, now=T, core=40, ambient=100, needles=None):
    return tracker.probe_reading(now, core, ambient, needles or [core, core + 1, core + 2, core + 3])


def in_meat(tracker, core=40):
    probe(tracker, T, core)
    return probe(tracker, T + 30, core)


class BathTests(unittest.TestCase):
    def test_defaults_and_start_wait_for_actual_heating(self):
        tracker = Tracker()
        self.assertEqual((tracker.bath["phase"], tracker.bath["last_target"], tracker.bath["last_minutes"]),
                         ("off", 58, 150))
        effects = tracker.command("bath_start", {"target": 60}, T)
        self.assertEqual(effects, [{"type": "service", "op": "bath_start", "target": 60}])
        self.assertEqual(tracker.bath["phase"], "off")
        tracker.bath_reading(T + 1, "heat", 35, 60, False)
        self.assertEqual(tracker.bath["phase"], "heating")

    def test_heat_overshoot_cool_to_ready_and_ready_push_once(self):
        tracker = Tracker()
        tracker.bath_reading(T, "heat", 40, 58, False)
        tracker.bath_reading(T + 30, "heat", 61.4, 58, False)
        self.assertEqual(tracker.bath["phase"], "too_hot")
        tracker.bath_reading(T + 60, "heat", 59, 58, False)
        self.assertEqual(tracker.bath["phase"], "too_hot")
        effects = bath_ready(tracker, T + 90)
        self.assertEqual(tracker.bath["phase"], "ready")
        self.assertEqual(pushes(effects)[0]["actions"], [{"action": "COOK_BAG_IN", "title": "Bag in"}])
        tracker.bath_reading(T + 100, "heat", 56, 58, False)
        self.assertEqual(pushes(bath_ready(tracker, T + 120)), [])

    def test_bag_in_before_ready_records_temperature_and_last_minutes(self):
        tracker = Tracker()
        tracker.bath_reading(T, "heat", 40, 58, False)
        tracker.command("bag_in", {"minutes": 120}, T + 10)
        self.assertEqual((tracker.bath["phase"], tracker.bath["in_at"], tracker.bath["done_at"]),
                         ("cooking", T + 10, T + 7210))
        self.assertEqual((tracker.bath["in_temp"], tracker.bath["in_target"]), (40, 58))
        self.assertEqual(tracker.bath["last_minutes"], 120)
        self.assertEqual(pushes(bath_ready(tracker, T + 60)), [])
        self.assertEqual(tracker.bath["phase"], "cooking")

    def test_complete_extend_chill_clear_and_do_not_restart_while_holding(self):
        tracker = Tracker()
        bath_ready(tracker)
        tracker.command("bag_in", {"minutes": 1}, T)
        self.assertEqual(tracker.tick(T + 59), [])
        done = tracker.tick(T + 60)
        self.assertEqual(tracker.bath["phase"], "done")
        self.assertEqual([item["action"] for item in pushes(done)[0]["actions"]], ["COOK_ICE", "COOK_STOP"])
        self.assertFalse(any(effect["type"] == "service" for effect in done))
        tracker.command("bath_extend", {"minutes": 30}, T + 65)
        self.assertEqual((tracker.bath["phase"], tracker.bath["done_at"]), ("cooking", T + 1860))
        self.assertEqual(pushes(tracker.tick(T + 1860)), [])
        tracker.command("chill", {}, T + 1861)
        self.assertEqual(tracker.bath["chill_until"], T + 2761)
        self.assertEqual(pushes(tracker.tick(T + 2761))[0]["kind"], "chill_done")
        effects = tracker.command("bath_clear", {}, T + 2800)
        self.assertEqual(effects[0]["record"]["outcome"], "chilled")
        self.assertEqual(tracker.bath["phase"], "off")
        self.assertEqual(bath_ready(tracker, T + 2900), [])
        tracker.bath_reading(T + 3000, "off", 58, 58, False)
        self.assertEqual(pushes(bath_ready(tracker, T + 3100))[0]["kind"], "bath_ready")

    def test_explicit_new_start_rearms_after_clear_without_mode_edge(self):
        tracker = Tracker()
        bath_ready(tracker)
        tracker.command("bath_clear", {}, T + 1)
        tracker.command("bath_start", {"target": 60}, T + 2)
        tracker.bath_reading(T + 3, "heat", 50, 60, False)
        self.assertEqual(tracker.bath["phase"], "heating")

    def test_extend_during_cooking_adds_to_existing_deadline(self):
        tracker = Tracker()
        bath_ready(tracker)
        tracker.command("bag_in", {"minutes": 120}, T)
        tracker.command("bath_extend", {"minutes": 30}, T + 600)
        self.assertEqual((tracker.bath["phase"], tracker.bath["done_at"], tracker.bath["minutes"]),
                         ("cooking", T + 9000, 150))

    def test_stopping_before_done_is_interrupted_and_after_done_is_done(self):
        for method in ("sensor", "command"):
            with self.subTest(method=method):
                tracker = Tracker()
                bath_ready(tracker)
                tracker.command("bag_in", {"minutes": 10}, T)
                if method == "sensor":
                    tracker.bath_reading(T + 30, "off", 58, 58, False)
                else:
                    tracker.command("bath_stop", {}, T + 30)
                    self.assertEqual(tracker.bath["phase"], "cooking")
                    tracker.bath_reading(T + 31, "off", 58, 58, False)
                stopped_at = T + (30 if method == "sensor" else 31)
                self.assertEqual((tracker.bath["phase"], tracker.bath["stopped_at"]), ("interrupted", stopped_at))
                self.assertEqual(pushes(tracker.tick(T + 700)), [])
        tracker = Tracker()
        bath_ready(tracker)
        tracker.command("bag_in", {"minutes": 1}, T)
        effects = tracker.bath_reading(T + 65, "off", 58, 58, False)
        self.assertEqual(tracker.bath["phase"], "done")
        self.assertEqual(pushes(effects)[0]["kind"], "bath_done")
        tracker.command("bath_stop", {}, T + 66)
        self.assertEqual(tracker.bath["phase"], "done")
        effects = tracker.bath_reading(T + 96, "off", 58, 58, False)
        self.assertEqual(tracker.bath["phase"], "off")
        self.assertEqual([e["record"]["outcome"] for e in effects if e["type"] == "closed"], ["done"])

    def test_outage_during_cook_stalls_then_interrupts_even_after_deadline(self):
        tracker = Tracker()
        bath_ready(tracker)
        tracker.command("bag_in", {"minutes": 5}, T)
        tracker.bath_reading(T + 100, "unavailable", None, None, False)
        self.assertEqual(pushes(tracker.tick(T + 300)), [])
        self.assertEqual(tracker.bath["phase"], "cooking")
        effects = tracker.tick(T + 701)
        self.assertEqual(pushes(effects), [])
        self.assertEqual((tracker.bath["phase"], tracker.bath["stopped_at"]), ("interrupted", T + 100))

    def test_outage_reconnect_before_grace_completes_and_long_outage_interrupts_without_tick(self):
        for downtime, expected in ((200, "done"), (601, "interrupted")):
            tracker = Tracker()
            bath_ready(tracker)
            tracker.command("bag_in", {"minutes": 5}, T)
            tracker.bath_reading(T + 100, "unavailable", None, None, False)
            tracker.bath_reading(T + 100 + downtime, "heat", 58, 58, False)
            self.assertEqual(tracker.bath["phase"], expected)

    def test_low_water_immediate_repeat_once_and_clear(self):
        tracker = Tracker()
        effects = tracker.bath_reading(T, "off", 25, 58, True)
        self.assertEqual(len(pushes(effects, "low_water")), 1)
        self.assertTrue(pushes(effects)[0]["wake"])
        self.assertEqual(tracker.tick(T + 599), [])
        self.assertEqual(len(pushes(tracker.tick(T + 600), "low_water")), 1)
        tracker = Tracker(json.loads(json.dumps(tracker.to_dict())))
        self.assertEqual(tracker.tick(T + 1200), [])
        tracker.bath_reading(T + 1201, "off", 25, 58, False)
        self.assertFalse(tracker.bath["low_water"])
        self.assertEqual(len(pushes(tracker.bath_reading(T + 1300, "off", 25, 58, True), "low_water")), 1)

    def test_ready_rate_survives_episode_and_restart(self):
        tracker = Tracker()
        for at, value in ((0, 50), (30, 52), (60, 54), (90, 56), (120, 58)):
            tracker.bath_reading(T + at, "heat", value, 58, False)
        tracker.command("bath_clear", {}, T + 121)
        tracker = Tracker(json.loads(json.dumps(tracker.to_dict())))
        tracker.command("bath_start", {"target": 58}, T + 122)
        tracker.bath_reading(T + 123, "heat", 40, 58, False)
        self.assertAlmostEqual(tracker.bath["rate"], 4)
        self.assertAlmostEqual(tracker.bath["ready_at"], T + 123 + 270)

    def test_restart_mid_cook_preserves_clock_and_done_push_once(self):
        tracker = Tracker()
        bath_ready(tracker)
        tracker.command("bag_in", {"minutes": 1}, T)
        tracker = Tracker(json.loads(json.dumps(tracker.to_dict())))
        self.assertEqual(len(pushes(tracker.tick(T + 60))), 1)
        tracker = Tracker(json.loads(json.dumps(tracker.to_dict())))
        self.assertEqual(pushes(tracker.tick(T + 61)), [])


class ProbeTests(unittest.TestCase):
    def test_automatic_tracking_and_preset_table(self):
        tracker = Tracker()
        probe(tracker)
        self.assertEqual((tracker.probe["phase"], tracker.probe["preset"]), ("tracking", "beef"))
        expected = {"beef": (54, 50, 15), "lamb": (58, 54, 15), "pork": (63, 60, 15),
                    "chicken": (66, 64, 10), "duck": (54, 51, 10), "fish": (50, 48, 5)}
        for key, values in expected.items():
            tracker.command("probe_preset", {"key": key}, T + 1)
            self.assertEqual(tuple(tracker.probe[field] for field in ("serve", "pull", "rest")), values)
        self.assertEqual(len(PRESETS), 7)
        tracker.command("probe_preset", {"key": "custom", "serve": 57.5}, T + 2)
        self.assertEqual((tracker.probe["serve"], tracker.probe["pull"], tracker.probe["rest"]), (57.5, 54.5, 15))

    def test_in_meat_needs_continuous_thirty_seconds_and_three_degrees(self):
        tracker = Tracker()
        probe(tracker, T, 40, 42.9)
        probe(tracker, T + 30, 40, 43)
        self.assertFalse(tracker.probe["in_meat"])
        probe(tracker, T + 59, 40, 43)
        self.assertFalse(tracker.probe["in_meat"])
        probe(tracker, T + 60, 40, 43)
        self.assertTrue(tracker.probe["in_meat"])
        probe(tracker, T + 61, 40, 40)
        self.assertTrue(tracker.probe["in_meat"])

    def test_unavailable_resets_in_meat_confirmation(self):
        tracker = Tracker()
        probe(tracker)
        tracker.probe_reading(T + 29, None, None, None)
        probe(tracker, T + 31)
        self.assertFalse(tracker.probe["in_meat"])
        probe(tracker, T + 61)
        self.assertTrue(tracker.probe["in_meat"])

    def test_numeric_gap_or_restart_does_not_prove_continuous_in_meat(self):
        tracker = Tracker()
        probe(tracker)
        tracker = Tracker(json.loads(json.dumps(tracker.to_dict())))
        probe(tracker, T + 120)
        self.assertFalse(tracker.probe["in_meat"])
        probe(tracker, T + 150)
        self.assertTrue(tracker.probe["in_meat"])

    def test_numeric_rest_gap_cannot_trigger_plateau(self):
        tracker = Tracker()
        in_meat(tracker, 51)
        tracker.command("probe_pulled", {}, T + 30)
        probe(tracker, T + 150, 51)
        self.assertEqual(tracker.probe["phase"], "resting")
        for at in (180, 210, 240, 270):
            probe(tracker, T + at, 51)
        self.assertEqual(tracker.probe["phase"], "carve")

    def test_no_probe_push_without_in_meat_then_pull_once_and_never_auto_pulled(self):
        tracker = Tracker()
        self.assertEqual(pushes(probe(tracker, T, 51.2, 51.2)), [])
        self.assertEqual(tracker.probe["phase"], "pull")
        self.assertEqual(tracker.probe["_pull_at"], T)
        self.assertEqual(pushes(probe(tracker, T + 30, 51.2, 60)), [])
        effects = probe(tracker, T + 60, 51.2, 60)
        self.assertEqual(pushes(effects)[0]["title"], "Pull the roast now · 51.2°")
        self.assertEqual(pushes(effects)[0]["message"], "Rests to about 54°")
        probe(tracker, T + 90, 20)
        self.assertEqual(tracker.probe["phase"], "pull")
        self.assertEqual(pushes(tracker.tick(T + 100)), [])

    def test_near_temperature_or_eta_has_no_push(self):
        tracker = Tracker()
        in_meat(tracker)
        self.assertEqual(pushes(probe(tracker, T + 60, 47)), [])
        self.assertEqual(tracker.probe["phase"], "near")
        other = Tracker()
        for at in range(0, 301, 30):
            self.assertEqual(pushes(probe(other, T + at, 20 + at / 60 * 3)), [])
        self.assertEqual(other.probe["phase"], "near")
        self.assertAlmostEqual(other.probe["eta"] - (T + 300), 300)

    def test_pulled_locks_coldest_index_ignores_surface_cooling_and_tracks_peak(self):
        tracker = Tracker()
        in_meat(tracker, 51)
        probe(tracker, T + 31, 51, needles=[55, 51, 54, 56])
        tracker.command("probe_pulled", {}, T + 32)
        self.assertEqual((tracker.probe["core_index"], tracker.probe["pulled_core"]), (1, 51))
        probe(tracker, T + 62, 30, needles=[30, 54.1, 40, 45])
        self.assertEqual((tracker.probe["core"], tracker.probe["peak"]), (54.1, 54.1))
        probe(tracker, T + 92, 25, needles=[25, 53.5, 30, 40])
        self.assertEqual((tracker.probe["core"], tracker.probe["peak"]), (53.5, 54.1))

    def test_rest_plateau_after_two_minutes_carve_push_and_report_once(self):
        tracker = Tracker()
        in_meat(tracker, 51)
        tracker.command("probe_pulled", {}, T + 30)
        effects = []
        for at in (60, 90, 120, 149):
            effects += probe(tracker, T + at, 51.01)
        self.assertEqual(tracker.probe["phase"], "resting")
        effects += probe(tracker, T + 150, 51.01)
        self.assertEqual(tracker.probe["phase"], "carve")
        self.assertEqual(pushes(effects)[0]["title"], "Carve · peaked 51.0°")
        self.assertEqual([effect["text"] for effect in effects if effect["type"] == "report"],
                         ["Roast rested · peaked 51.0°"])
        restored = Tracker(json.loads(json.dumps(tracker.to_dict())))
        self.assertEqual(restored.probe["_pull_at"], T)
        self.assertEqual(restored.probe["_carved_at"], T + 150)
        self.assertEqual(restored.probe["carve_at"], T + 930)
        self.assertEqual(pushes(probe(restored, T + 180, 51)), [])

    def test_maximum_rest_and_no_false_plateau_across_a_gap(self):
        tracker = Tracker()
        tracker.command("probe_preset", {"key": "fish"}, T - 1)
        in_meat(tracker, 49)
        tracker.command("probe_pulled", {}, T + 30)
        tracker.probe_reading(T + 40, None, None, None)
        self.assertEqual(pushes(tracker.tick(T + 150)), [])
        probe(tracker, T + 160, 49)
        self.assertEqual(tracker.probe["phase"], "resting")
        effects = tracker.tick(T + 330)
        self.assertEqual(tracker.probe["phase"], "carve")
        self.assertEqual(len(pushes(effects, "carve")), 1)

    def test_lost_only_after_ninety_seconds_push_once_before_pull_then_unplug_closes(self):
        tracker = Tracker()
        in_meat(tracker)
        tracker.probe_reading(T + 40, None, None, None)
        self.assertEqual(tracker.probe["core"], 40)
        self.assertEqual(tracker.probe["stale_since"], T + 40)
        self.assertEqual(pushes(tracker.tick(T + 130)), [])
        self.assertFalse(tracker.probe["lost"])
        self.assertEqual(len(pushes(tracker.tick(T + 131), "lost")), 1)
        self.assertTrue(tracker.probe["lost"])
        tracker = Tracker(json.loads(json.dumps(tracker.to_dict())))
        self.assertEqual(pushes(tracker.tick(T + 200)), [])
        effects = tracker.tick(T + 640)
        self.assertEqual(tracker.probe["phase"], "off")
        self.assertEqual(effects[0]["record"]["kind"], "probe")

    def test_no_lost_push_after_pull_and_recovery_clears_alarm(self):
        tracker = Tracker()
        in_meat(tracker, 51)
        tracker.probe_reading(T + 40, None, None, None)
        self.assertEqual(pushes(tracker.tick(T + 131)), [])
        self.assertTrue(tracker.probe["lost"])
        probe(tracker, T + 150, 51)
        self.assertFalse(tracker.probe["lost"])
        self.assertNotIn("stale_since", tracker.probe)

    def test_done_suppresses_rearming_until_unplug_then_remembers_last_preset(self):
        tracker = Tracker()
        tracker.command("probe_preset", {"key": "duck"}, T - 1)
        probe(tracker)
        effects = tracker.command("probe_done", {}, T + 10)
        self.assertEqual(effects[0]["record"]["preset"], "duck")
        probe(tracker, T + 20)
        self.assertEqual(tracker.probe["phase"], "off")
        tracker.probe_reading(T + 30, None, None, None)
        tracker.tick(T + 630)
        probe(tracker, T + 631)
        self.assertEqual((tracker.probe["phase"], tracker.probe["preset"]), ("tracking", "duck"))

    def test_shadow_eta_once_a_minute_persisted_in_record_without_early_push(self):
        tracker = Tracker()
        for at in range(0, 301):
            self.assertEqual(pushes(probe(tracker, T + at, 20 + at / 60)), [])
        self.assertEqual(len(tracker.probe["shadow"]), 6)
        self.assertIsNotNone(tracker.probe["shadow"][-1]["eta"])
        tracker = Tracker(json.loads(json.dumps(tracker.to_dict())))
        effects = tracker.command("probe_done", {}, T + 301)
        self.assertEqual(len(effects[0]["record"]["shadow"]), 6)


class ValidationTests(unittest.TestCase):
    def test_invalid_ops_and_phases_are_ignored(self):
        tracker = Tracker()
        invalid = [("nonsense", {}), ("bag_in", {}), ("bath_stop", {}), ("bath_extend", {"minutes": 30}),
                   ("chill", {}), ("bath_clear", {}), ("probe_pulled", {}), ("probe_done", {}),
                   ("silence", {}), ("bath_start", {"target": "nan"}),
                   ("probe_preset", {"key": "custom", "serve": "nan"}),
                   ("probe_preset", {"key": "missing"})]
        for op, data in invalid:
            self.assertEqual(tracker.command(op, data, T), [{"type": "ignored", "op": op}])
        bath_ready(tracker)
        for value in (0, -1, True, "nan", "bad"):
            self.assertEqual(tracker.command("bag_in", {"minutes": value}, T)[0]["type"], "ignored")
        in_meat(tracker, 51)
        self.assertEqual(tracker.command("probe_preset", {"key": "fish"}, T + 31)[0]["type"], "ignored")

    def test_junk_restore_returns_defaults_and_round_trip_is_independent(self):
        for saved in (None, [], {"bath": "bad"}, {"bath": {"phase": "cooking"}},
                      {"probe": {"phase": "other"}}):
            tracker = Tracker(saved)
            self.assertEqual((tracker.bath["phase"], tracker.probe["phase"]), ("off", "off"))
        self.assertEqual(Tracker({"bath": {"phase": "off", "_samples": None}}).bath["_samples"], [])
        tracker = Tracker()
        bath_ready(tracker)
        in_meat(tracker)
        snapshot = tracker.to_dict()
        again = Tracker(json.loads(json.dumps(snapshot)))
        self.assertEqual(again.to_dict(), snapshot)
        snapshot["bath"]["_samples"].clear()
        self.assertNotEqual(tracker.to_dict(), snapshot)


if __name__ == "__main__":
    unittest.main()
