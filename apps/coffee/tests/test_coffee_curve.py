# tests/test_coffee_curve.py - the 1 Hz curve: sample and hold, gaps on a stale link, the app's spike
# filter, pours and pauses, the totals and the curve file.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_curve as cc  # noqa: E402

T0 = 1_800_000_000.0


class CupFilterTests(unittest.TestCase):
    def test_a_spike_is_dropped_and_ordinary_steps_are_kept(self):
        f = cc.CupFilter()
        self.assertEqual([f.accept(v) for v in (0.0, 12.5, 400.0, 13.1)], [0.0, 12.5, None, 13.1])

    def test_a_jump_seen_four_times_in_a_row_is_real(self):
        f = cc.CupFilter()
        f.accept(10.0)
        self.assertEqual([f.accept(v) for v in (900.0, 901.0, 902.0, 903.0)], [None, None, None, 903.0])
        self.assertEqual(f.accept(904.0), 904.0)

    def test_a_kept_value_resets_the_count(self):
        f = cc.CupFilter()
        f.accept(10.0)
        for value in (900.0, 900.0, 11.0, 900.0, 900.0, 900.0):
            f.accept(value)
        self.assertEqual(f.accept(900.0), 900.0)


class CurveTests(unittest.TestCase):
    def test_one_sample_a_second_with_the_latest_values(self):
        curve = cc.Curve(T0)
        curve.sample(T0, 0.0, 0.0, True)
        curve.sample(T0 + 1.2, 3.4, 32.0, True)
        curve.sample(T0 + 3.9, 9.0, 101.5, True)
        self.assertEqual(curve.cup, [0, 34, 34, 90])
        self.assertEqual(curve.water, [0, 320, 320, 1015])

    def test_the_first_sample_fills_back_to_the_bloom(self):
        curve = cc.Curve(T0)
        curve.sample(T0 + 2.5, 1.0, 10.0, True)
        self.assertEqual(curve.cup, [10, 10, 10])

    def test_a_stale_link_leaves_gaps_that_are_never_filled(self):
        curve = cc.Curve(T0)
        curve.sample(T0 + 1, 2.0, 20.0, True, T0 + 5.5)
        curve.sample(T0 + 8, 2.0, 20.0, False, T0 + 5.5)
        curve.sample(T0 + 9, 5.0, 50.0, True, T0 + 18.7)
        self.assertEqual(curve.cup, [20, 20, 20, 20, 20, 20, None, None, None, 50])
        self.assertEqual(curve.fresh, [1, 1, 1, 1, 1, 1, 0, 0, 0, 1])

    def test_a_restored_curve_holds_nothing_so_the_downtime_is_a_gap(self):
        curve = cc.Curve(T0)
        curve.sample(T0 + 3, 5.0, 50.0, True)
        again = cc.Curve.from_dict(curve.to_dict())
        again.sample(T0 + 6, 6.0, 60.0, True)
        self.assertEqual(again.cup, [50, 50, 50, 50, None, None, 60])

    def test_an_unknown_value_is_a_gap_even_on_a_fresh_link(self):
        curve = cc.Curve(T0)
        curve.sample(T0 + 1, None, 10.0, True)
        self.assertEqual((curve.cup, curve.water), ([None, None], [100, 100]))

    def test_pours_are_seconds_from_the_bloom_and_counted_once(self):
        curve = cc.Curve(T0)
        self.assertTrue(curve.pour(T0, 0))
        self.assertTrue(curve.pour(T0 + 46.4, 1))
        self.assertFalse(curve.pour(T0 + 47, 1))
        self.assertEqual(curve.pours, [[0, 0], [46, 1]])

    def test_pauses_and_their_length(self):
        curve = cc.Curve(T0)
        curve.pause(T0 + 50)
        self.assertEqual(curve.paused_s(T0 + 60), 10)
        curve.resume(T0 + 70)
        curve.pause(T0 + 100)
        curve.resume(T0 + 105)
        self.assertEqual(curve.pauses, [[50, 70], [100, 105]])
        self.assertEqual(curve.paused_s(), 25)

    def test_the_end_stops_sampling_and_closes_an_open_pause(self):
        curve = cc.Curve(T0)
        curve.pause(T0 + 5)
        curve.end(T0 + 10, 200.0, 250.0, True)
        curve.sample(T0 + 20, 10.0, 10.0, True)
        self.assertEqual(len(curve.cup), 11)
        self.assertEqual(curve.end_s, 10)
        self.assertEqual(curve.pauses, [[5, 10]])

    def test_totals(self):
        curve = cc.Curve(T0)
        for second in range(0, 201):
            fresh = not 50 <= second < 54
            water = min(255.0, second * 2.0)
            curve.sample(T0 + second, second * 1.1, water, fresh)
        curve.end(T0 + 207, 217.4, 255.0, True)
        totals = curve.totals(T0 + 207, T0 + 145)
        self.assertEqual(totals["total_s"], 207)
        self.assertEqual(totals["drawdown_s"], 62)
        self.assertEqual(totals["cup_g"], 217.4)
        self.assertEqual(totals["water_ml"], 255.0)
        self.assertAlmostEqual(totals["coverage"], round(204 / 208, 2))

    def test_the_drawdown_falls_back_to_the_last_water_rise(self):
        curve = cc.Curve(T0)
        for second in range(0, 150):
            curve.sample(T0 + second, 1.0, min(255.0, second * 2.0), True)
        totals = curve.totals(T0 + 190)
        self.assertEqual(curve.last_water_rise(), 128)
        self.assertEqual(totals["drawdown_s"], 190 - 128)

    def test_the_file_ends_at_the_last_valid_sample(self):
        curve = cc.Curve(T0)
        curve.sample(T0 + 2, 5.0, 50.0, True, T0 + 2.5)
        curve.sample(T0 + 4, 5.0, 50.0, False, T0 + 2.5)
        curve.end(T0 + 4, 5.0, 50.0, False)
        data = curve.file_data("c1")
        self.assertEqual(data["cup"], [50, 50, 50])
        self.assertEqual((data["id"], data["step_s"], data["end_s"]), ("c1", 1, 4))

    def test_the_checkpoint_round_trips(self):
        curve = cc.Curve(T0)
        curve.sample(T0 + 3, 5.0, 50.0, True)
        curve.pour(T0, 0)
        curve.pause(T0 + 2)
        again = cc.Curve.from_dict(curve.to_dict())
        self.assertEqual(again.to_dict(), curve.to_dict())


class EncodingTests(unittest.TestCase):
    def test_csv_keeps_gaps_empty(self):
        self.assertEqual(cc.csv([0, 12, 35, None, None, 80]), "0,12,35,,,80")

    def test_every_takes_each_nth_sample(self):
        self.assertEqual(cc.every([0, 1, 2, 3, 4, 5, 6], 4), [0, 4])
        self.assertEqual(cc.every([0, 1], 1), [0, 1])

    def test_pairs(self):
        self.assertEqual(cc.pairs_csv([[0, 0], [46, 1]], ":"), "0:0,46:1")


if __name__ == "__main__":
    unittest.main()
