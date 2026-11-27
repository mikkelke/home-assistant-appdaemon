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


def armed(key="beef"):
    tracker = Tracker()
    tracker.command("probe_preset", {"key": key}, T - 10)
    return tracker


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
        tracker = armed()
        probe(tracker)
        self.assertEqual((tracker.probe["phase"], tracker.probe["preset"]), ("tracking", "beef"))
        expected = {"beef": (54, 50, 15), "lamb": (58, 54, 15), "pork": (63, 60, 15),
                    "chicken": (66, 64, 10), "duck": (54, 51, 10), "fish": (50, 48, 5),
                    "chicken_thigh": (80, 78, 5)}
        for key, values in expected.items():
            tracker.command("probe_preset", {"key": key}, T + 1)
            self.assertEqual(tuple(tracker.probe[field] for field in ("serve", "pull", "rest")), values)
        self.assertEqual(len(PRESETS), 8)
        tracker.command("probe_preset", {"key": "custom", "serve": 57.5}, T + 2)
        self.assertEqual((tracker.probe["serve"], tracker.probe["pull"], tracker.probe["rest"]), (57.5, 54.5, 15))

    def test_no_target_never_arms_a_pull_and_pushes_once_in_meat(self):
        tracker = Tracker()
        effects = probe(tracker, T, 51, 100)
        self.assertEqual((tracker.probe["phase"], tracker.probe["preset"], tracker.probe["pull"]),
                         ("tracking", None, None))
        effects += probe(tracker, T + 30, 60, 100)
        effects += probe(tracker, T + 60, 90, 120)
        self.assertEqual([push["kind"] for push in pushes(effects)], ["no_target"])
        self.assertEqual(tracker.probe["phase"], "tracking")
        self.assertIsNone(tracker.probe["eta"])
        tracker.command("probe_preset", {"key": "chicken_thigh"}, T + 61)
        self.assertEqual([push["kind"] for push in pushes(probe(tracker, T + 90, 79, 120))], ["pull"])

    def test_in_meat_needs_continuous_thirty_seconds_and_three_degrees(self):
        tracker = armed()
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
        tracker = armed()
        probe(tracker)
        tracker.probe_reading(T + 29, None, None, None)
        probe(tracker, T + 31)
        self.assertFalse(tracker.probe["in_meat"])
        probe(tracker, T + 61)
        self.assertTrue(tracker.probe["in_meat"])

    def test_numeric_gap_or_restart_does_not_prove_continuous_in_meat(self):
        tracker = armed()
        probe(tracker)
        tracker = Tracker(json.loads(json.dumps(tracker.to_dict())))
        probe(tracker, T + 120)
        self.assertFalse(tracker.probe["in_meat"])
        probe(tracker, T + 150)
        self.assertTrue(tracker.probe["in_meat"])

    def test_numeric_rest_gap_cannot_trigger_plateau(self):
        tracker = armed()
        in_meat(tracker, 51)
        tracker.command("probe_pulled", {}, T + 30)
        probe(tracker, T + 150, 51)
        self.assertEqual(tracker.probe["phase"], "resting")
        for at in (180, 210, 240, 270):
            probe(tracker, T + at, 51)
        self.assertEqual(tracker.probe["phase"], "carve")

    def test_no_probe_push_without_in_meat_then_pull_once_and_never_auto_pulled(self):
        tracker = armed()
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
        tracker = armed()
        in_meat(tracker)
        self.assertEqual(pushes(probe(tracker, T + 60, 47)), [])
        self.assertEqual(tracker.probe["phase"], "near")
        other = armed()
        for at in range(0, 301, 30):
            self.assertEqual(pushes(probe(other, T + at, 20 + at / 60 * 3)), [])
        self.assertEqual(other.probe["phase"], "near")
        self.assertAlmostEqual(other.probe["eta"] - (T + 300), 300)

    def test_pulled_locks_coldest_index_ignores_surface_cooling_and_tracks_peak(self):
        tracker = armed()
        in_meat(tracker, 51)
        probe(tracker, T + 31, 51, needles=[55, 51, 54, 56])
        tracker.command("probe_pulled", {}, T + 32)
        self.assertEqual((tracker.probe["core_index"], tracker.probe["pulled_core"]), (1, 51))
        probe(tracker, T + 62, 30, needles=[30, 54.1, 40, 45])
        self.assertEqual((tracker.probe["core"], tracker.probe["peak"]), (54.1, 54.1))
        probe(tracker, T + 92, 25, needles=[25, 53.5, 30, 40])
        self.assertEqual((tracker.probe["core"], tracker.probe["peak"]), (53.5, 54.1))

    def test_rest_plateau_after_two_minutes_carve_push_and_report_once(self):
        tracker = armed()
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
        tracker = armed()
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
        tracker = armed()
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
        tracker = armed()
        in_meat(tracker, 51)
        tracker.probe_reading(T + 40, None, None, None)
        self.assertEqual(pushes(tracker.tick(T + 131)), [])
        self.assertTrue(tracker.probe["lost"])
        probe(tracker, T + 150, 51)
        self.assertFalse(tracker.probe["lost"])
        self.assertNotIn("stale_since", tracker.probe)

    def test_done_suppresses_rearming_until_unplug_and_next_cook_starts_without_a_plan(self):
        tracker = armed("duck")
        tracker.command("probe_steps", {"steps": [{"id": "a", "core": 30, "push": "x"}]}, T - 1)
        probe(tracker)
        self.assertEqual((tracker.probe["preset"], len(tracker.probe["steps"])), ("duck", 1))
        effects = tracker.command("probe_done", {}, T + 10)
        self.assertEqual(effects[0]["record"]["preset"], "duck")
        probe(tracker, T + 20)
        self.assertEqual(tracker.probe["phase"], "off")
        tracker.probe_reading(T + 30, None, None, None)
        tracker.tick(T + 630)
        probe(tracker, T + 631)
        self.assertEqual((tracker.probe["phase"], tracker.probe["preset"], tracker.probe["steps"]),
                         ("tracking", None, []))

    def test_plan_set_before_the_probe_goes_in_carries_into_the_cook(self):
        tracker = Tracker()
        tracker.command("probe_preset", {"key": "chicken_thigh"}, T - 5, owner="claudia")
        tracker.command("probe_hold", {"guest_push": "Ud med fadet", "ceiling": 85}, T - 4)
        probe(tracker)
        self.assertEqual((tracker.probe["pull"], tracker.probe["owner"], tracker.probe["hold"]["ceiling"]),
                         (78, "claudia", 85))

    def test_a_plan_left_on_the_idle_lane_expires_after_six_hours(self):
        tracker = Tracker()
        tracker.command("probe_preset", {"key": "chicken_thigh"}, T - 6 * 3600 - 1, owner="claudia")
        probe(tracker)
        self.assertEqual((tracker.probe["pull"], tracker.probe.get("owner")), (None, None))

    def test_saved_off_lane_from_an_old_cook_is_not_a_target(self):
        tracker = Tracker({"probe": {"phase": "off", "preset": "custom", "serve": 83, "pull": 80, "rest": 15}})
        self.assertIsNone(tracker.probe["pull"])
        probe(tracker)
        self.assertIsNone(tracker.probe["pull"])

    def test_shadow_eta_once_a_minute_persisted_in_record_without_early_push(self):
        tracker = armed()
        for at in range(0, 301):
            self.assertEqual(pushes(probe(tracker, T + at, 20 + at / 60)), [])
        self.assertEqual(len(tracker.probe["shadow"]), 6)
        self.assertIsNotNone(tracker.probe["shadow"][-1]["eta"])
        tracker = Tracker(json.loads(json.dumps(tracker.to_dict())))
        effects = tracker.command("probe_done", {}, T + 301)
        self.assertEqual(len(effects[0]["record"]["shadow"]), 6)


THIGHS = [{"id": "olives", "core": 65, "push": "Kom oliven i fadet", "say": "Add the olives now."},
          {"id": "pan", "before_pull": 3, "push": "Sæt panden på", "say": "Put the pan on."}]


class PlanTests(unittest.TestCase):
    def test_core_and_before_pull_steps_fire_once_with_owner_and_announcement(self):
        tracker = armed("chicken_thigh")
        self.assertEqual(tracker.command("probe_steps", {"steps": THIGHS}, T - 1, owner="kristine"),
                         [{"type": "state"}])
        self.assertEqual(pushes(probe(tracker, T, 66, 150)), [])
        effects = probe(tracker, T + 30, 66, 150)
        step = pushes(effects, "step_olives")
        self.assertEqual((len(step), step[0]["title"], step[0]["say"], step[0]["owner"]),
                         (1, "Kom oliven i fadet", "Add the olives now.", "kristine"))
        effects = []
        for at in range(60, 900, 30):
            effects += probe(tracker, T + at, 66 + (at - 60) / 50, 150)
            if pushes(effects, "step_pan"):
                break
        self.assertEqual(tracker.probe["phase"], "near")
        self.assertLessEqual(tracker.probe["eta"] - (T + at), 180)
        self.assertEqual(len(pushes(effects, "step_olives")), 0)
        self.assertEqual(len(pushes(effects, "step_pan")), 1)
        self.assertEqual([row[0] for row in tracker.probe["fired"]], ["olives", "pan"])
        tracker.command("probe_steps", {"steps": THIGHS}, T + 901)
        self.assertEqual([push["kind"] for push in pushes(probe(tracker, T + 930, 79, 150))], ["pull"])

    def test_steps_wait_for_meat_and_before_pull_needs_a_target(self):
        tracker = Tracker()
        tracker.command("probe_steps", {"steps": THIGHS}, T - 1)
        self.assertEqual(pushes(probe(tracker, T, 70, 70)), [])
        effects = probe(tracker, T + 60, 70, 150) + probe(tracker, T + 90, 70, 150)
        self.assertEqual(sorted(push["kind"] for push in pushes(effects)), ["no_target", "step_olives"])

    def test_hold_reports_pull_as_holding_warns_at_ceiling_and_fires_guest_once(self):
        tracker = armed("chicken_thigh")
        tracker.command("probe_hold", {"ceiling": 85, "guest_push": "Tag fadet ud og start panden",
                                       "guest_say": "The guest is here."}, T - 1)
        in_meat(tracker, 70)
        effects = probe(tracker, T + 60, 78.5, 150)
        self.assertEqual(pushes(effects, "pull")[0]["message"], "Holding until the guest rings")
        self.assertEqual(tracker.probe["phase"], "pull")
        effects = probe(tracker, T + 90, 85.5, 150)
        ceiling = pushes(effects, "ceiling")
        self.assertEqual((len(ceiling), ceiling[0]["say"]), (1, "The core is past 85 degrees."))
        self.assertEqual(pushes(probe(tracker, T + 120, 86, 150)), [])
        effects = tracker.guest_rang(T + 130)
        guest = pushes(effects, "guest")
        self.assertEqual((guest[0]["title"], guest[0]["say"], tracker.probe["guest_at"]),
                         ("Tag fadet ud og start panden", "The guest is here.", T + 130))
        self.assertEqual(tracker.guest_rang(T + 140), [])
        restored = Tracker(json.loads(json.dumps(tracker.to_dict())))
        self.assertEqual(pushes(probe(restored, T + 150, 86, 150)), [])

    def test_guest_before_pull_fires_at_pull_and_ring_without_hold_is_ignored(self):
        tracker = armed("chicken_thigh")
        in_meat(tracker, 60)
        self.assertEqual(tracker.guest_rang(T + 40), [])
        tracker.command("probe_hold", {"guest_push": "Ud med fadet"}, T + 41)
        self.assertEqual(pushes(tracker.guest_rang(T + 42)), [])
        effects = probe(tracker, T + 60, 78, 150)
        self.assertEqual(pushes(effects, "pull")[0]["title"], "Pull the roast now · 78.0°")
        self.assertEqual(len(pushes(effects, "guest")), 1)

    def test_timer_fires_once_with_core_and_cancel(self):
        tracker = armed()
        in_meat(tracker, 45)
        tracker.command("timer_start", {"minutes": 2, "push": "Tjek kartoflerne", "say": "Check the potatoes."},
                        T + 40, owner="claudia")
        self.assertEqual(pushes(tracker.tick(T + 159)), [])
        effects = pushes(tracker.tick(T + 160))
        self.assertEqual((effects[0]["title"], effects[0]["message"], effects[0]["say"], effects[0]["owner"]),
                         ("Tjek kartoflerne", "Core 45.0°", "Check the potatoes.", "claudia"))
        self.assertIsNone(tracker.timer)
        tracker.command("timer_start", {"minutes": 1}, T + 200)
        self.assertEqual(tracker.timer["push"], "Kitchen timer done")
        restored = Tracker(json.loads(json.dumps(tracker.to_dict())))
        self.assertEqual(restored.command("timer_cancel", {}, T + 210), [{"type": "state"}])
        self.assertEqual(pushes(restored.tick(T + 300)), [])

    def test_bad_plans_are_ignored(self):
        tracker = Tracker()
        bad = [[{"id": "a", "push": "x"}], [{"id": "a", "core": 60, "before_pull": 5, "push": "x"}],
               [{"id": "A B", "core": 60, "push": "x"}], [{"id": "a", "core": 60}],
               [{"id": "a", "core": 60, "push": "x"}, {"id": "a", "core": 70, "push": "y"}],
               [{"id": "a", "core": 200, "push": "x"}], [{"id": "a", "before_pull": 0, "push": "x"}],
               [{"id": "a", "core": 60, "push": "x", "say": ""}], "nope",
               [{"id": f"s{index}", "core": 60, "push": "x"} for index in range(11)]]
        for steps in bad:
            self.assertEqual(tracker.command("probe_steps", {"steps": steps}, T)[0]["type"], "ignored")
        for data in ({}, {"ceiling": 85}, {"guest_push": "x", "ceiling": "hot"}):
            self.assertEqual(tracker.command("probe_hold", data, T)[0]["type"], "ignored")
        for data in ({}, {"minutes": 0}, {"minutes": 5, "push": ""}):
            self.assertEqual(tracker.command("timer_start", data, T)[0]["type"], "ignored")
        self.assertEqual(tracker.command("timer_cancel", {}, T)[0]["type"], "ignored")
        self.assertEqual(tracker.command("timer_start", {}, T, owner="mikkel")[0]["type"], "ignored")
        self.assertEqual(tracker.command("probe_steps", {"steps": []}, T), [{"type": "state"}])


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
        tracker.command("probe_preset", {"key": "beef"}, T + 1)
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
