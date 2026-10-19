# tests/test_coffee_baseline.py - the usual's cohort, band, medians and pm, the frozen copy, the ETA, the
# live verdict, in_band and the deltas.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_baseline as cb  # noqa: E402

KEY = "k1"
PLAN = {"water": [0, 35, 70], "pours": "0-13:circular:93:45", "total_s": 202}


def record(i, total=205, cup=215.0, drawdown=60, **extra):
    return {"id": f"c{1000 + i}", "started_at": f"2026-10-{10 + i:02d}T06:00:00+00:00", "outcome": "complete",
            "variant": "usual", "coverage": 0.97, "recipe": {"key": KEY}, "bean": {"id": "b1"}, "total_s": total,
            "cup_g": cup, "drawdown_s": drawdown, **extra}


def curve(scale, length=200):
    return {"cup": [int(i * scale) for i in range(length)], "water": [i * 12 for i in range(length)]}


class CohortTests(unittest.TestCase):
    def test_only_complete_usual_brews_of_the_recipe_and_beans_with_coverage(self):
        records = [record(1), record(2, variant="base"), record(3, variant="trial"), record(4, outcome="stopped"),
                   record(5, coverage=0.5), record(6, recipe={"key": "other"}), record(7, bean={"id": "b2"}),
                   record(8, variant="tweak")]
        self.assertEqual([r["id"] for r in cb.cohort(records, KEY, "b1")], ["c1001", "c1002"])

    def test_the_newest_max_n_oldest_first(self):
        records = [record(i) for i in range(1, 26)]
        found = cb.cohort(list(reversed(records)), KEY, "b1")
        self.assertEqual(len(found), cb.MAX_N)
        self.assertEqual(found[0]["id"], "c1006")
        self.assertEqual(found[-1]["id"], "c1025")


class ComputeTests(unittest.TestCase):
    def test_building_below_five(self):
        records = [record(i) for i in range(1, 5)]
        base = cb.compute(records, {}, {"id": "r1"}, KEY, "b1", PLAN, "now")
        self.assertEqual((base["state"], base["n"]), ("building", 4))
        self.assertNotIn("cup_lo", base)
        self.assertEqual(base["plan_total_s"], 202)

    def test_ready_with_medians_pm_and_a_band(self):
        records = [record(1, total=200, cup=210), record(2, total=204, cup=214), record(3, total=206, cup=216),
                   record(4, total=210, cup=220), record(5, total=208, cup=218, drawdown=70)]
        curves = {r["id"]: curve(1 + i / 10) for i, r in enumerate(records)}
        base = cb.compute(records, curves, {"id": "r1"}, KEY, "b1", PLAN, "now")
        self.assertEqual(base["state"], "ready")
        self.assertEqual((base["total_s_median"], base["total_s_pm"]), (206, 5.0))
        self.assertEqual((base["cup_g_median"], base["cup_g_pm"]), (216, 5.0))
        self.assertEqual((base["drawdown_s_median"], base["drawdown_s_pm"]), (60, 5.0))
        self.assertEqual(len(base["cup_lo"]), 100)
        self.assertEqual(base["cup_lo"][10], 20)
        self.assertEqual(base["cup_hi"][10], 28)
        self.assertTrue(base["id"].startswith("bl"))

    def test_the_id_follows_the_cohort(self):
        self.assertEqual(cb.baseline_id(["c1", "c2"], KEY, "b1"), cb.baseline_id(["c1", "c2"], KEY, "b1"))
        self.assertNotEqual(cb.baseline_id(["c1", "c2"], KEY, "b1"), cb.baseline_id(["c1", "c3"], KEY, "b1"))

    def test_the_envelope_holds_a_brew_past_its_end_and_skips_gaps(self):
        lo, hi = cb.envelope([[10, 20, 30, 40], [12, None, 36, 48, 50, 52]])
        self.assertEqual(lo, [10, 30, 40])
        self.assertEqual(hi, [12, 36, 50])
        self.assertEqual(cb.envelope([]), ([], []))


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.base = {"state": "ready", "total_s_median": 206, "cup_g_median": 216, "drawdown_s_median": 60,
                     "cup_lo": [0, 10, 20, None, 40], "cup_hi": [5, 20, 30, None, 60]}

    def test_eta_from_the_baseline_or_the_plan_shifted_by_pauses(self):
        self.assertEqual(cb.eta(1000.0, self.base, 202, 0), (1206.0, "baseline"))
        self.assertEqual(cb.eta(1000.0, self.base, 202, 30), (1236.0, "baseline"))
        self.assertEqual(cb.eta(1000.0, {"state": "building"}, 202, 0), (1202.0, "plan"))
        self.assertEqual(cb.eta(None, self.base, 202, 0), (None, None))
        self.assertEqual(cb.eta(1000.0, None, None, 0), (None, None))

    def test_the_verdict_against_the_band(self):
        self.assertEqual(cb.verdict(self.base, 2, 15), "on_track")
        self.assertEqual(cb.verdict(self.base, 3, 5), "slower")
        self.assertEqual(cb.verdict(self.base, 4, 35), "ahead")
        self.assertIsNone(cb.verdict(self.base, 6, 30))
        self.assertIsNone(cb.verdict(self.base, 40, 30))
        self.assertIsNone(cb.verdict(self.base, 2, None))
        self.assertEqual(cb.verdict({"state": "building"}, 2, 15), "building")

    def test_in_band(self):
        self.assertEqual(cb.in_band(self.base, [0, 0, 15, 15, 25, 25, 0, 0, 50]), "inside")
        self.assertEqual(cb.in_band(self.base, [9, 0, 25, 0, 35, 0, 0, 0, 70]), "above")
        self.assertEqual(cb.in_band(self.base, [0, 0, 1, 0, 2, 0, 0, 0, 3]), "below")
        self.assertEqual(cb.in_band({"state": "building"}, [1, 2]), "building")
        self.assertEqual(cb.in_band(self.base, [None, None]), "building")

    def test_deltas_are_signed_strings(self):
        self.assertEqual(cb.deltas({"total_s": 210, "cup_g": 213.4, "drawdown_s": 60}, self.base),
                         {"d_total_s": "+4", "d_cup_g": "-3", "d_drawdown_s": "0"})
        self.assertEqual(cb.deltas({"total_s": 210}, {"state": "building"}), {})

    def test_the_frozen_copy_keeps_what_the_brew_needs(self):
        frozen = cb.frozen({**self.base, "id": "bl1", "n": 7, "brew_ids": ["c1"], "plan_water": [1, 2]})
        self.assertNotIn("brew_ids", frozen)
        self.assertEqual((frozen["id"], frozen["n"], frozen["cup_lo"]), ("bl1", 7, self.base["cup_lo"]))
        self.assertIsNone(cb.frozen({"state": "none"}))
        self.assertIsNone(cb.frozen(None))


if __name__ == "__main__":
    unittest.main()
