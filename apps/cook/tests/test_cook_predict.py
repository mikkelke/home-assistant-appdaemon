import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cook_predict as predict


class PredictionsTests(unittest.TestCase):
    def test_numeric_inputs_are_finite_and_not_booleans(self):
        for value in (None, True, False, "missing", math.nan, math.inf):
            self.assertIsNone(predict.number(value))
        self.assertEqual(predict.number("0"), 0)

    def test_least_squares_is_per_minute_and_handles_irregular_times(self):
        samples = [[at, 20 + at / 60 * 2] for at in (0, 17, 60, 130, 240)]
        self.assertAlmostEqual(predict.slope(samples, 240, 300), 2)
        self.assertIsNone(predict.slope([[1, 2], [1, 3]], 1, 300))

    def test_bath_needs_four_samples_and_rising_rate(self):
        samples = [[at, 20 + at / 60] for at in (0, 30, 60)]
        self.assertEqual(predict.bath_eta(samples, 60, 21, 58), (None, None))
        samples.append([90, 21.5])
        eta, rate = predict.bath_eta(samples, 90, 21.5, 58)
        self.assertAlmostEqual(rate, 1)
        self.assertAlmostEqual(eta, 2280)
        self.assertEqual(predict.bath_eta([[0, 50], [30, 50], [60, 50], [90, 50]], 90, 50, 58),
                         (None, None))

    def test_bath_falls_back_to_previous_episode_rate(self):
        self.assertEqual(predict.bath_eta([], 100, 40, 58, 2), (640, 2))
        self.assertEqual(predict.bath_eta([], 100, 40, 58, 0.1), (None, None))

    def test_probe_gates_five_minutes_and_point_two_degrees_per_minute(self):
        samples = [[at, 20 + at / 60] for at in range(0, 301, 30)]
        self.assertIsNone(predict.probe_eta(samples[:-1], 270, 24.5, 50)[0])
        eta, rate = predict.probe_eta(samples, 300, 25, 50)
        self.assertAlmostEqual(eta, 1800)
        self.assertAlmostEqual(rate, 1)
        self.assertIsNone(predict.probe_eta([[0, 20], [300, 20.9]], 300, 20.9, 50)[0])

    def test_old_data_expires_and_already_at_pull_is_now(self):
        samples = [[0, 20], [300, 25]]
        self.assertEqual(predict.probe_eta(samples, 1000, 25, 50), (None, None))
        self.assertEqual(predict.probe_eta(samples, 300, 51, 50), (300, 1))

    def test_sampling_is_bounded_and_high_frequency_does_not_bias_fit(self):
        samples = []
        for at in range(1000):
            predict.add_sample(samples, at, at / 60, 300)
        self.assertLessEqual(len(samples), 31)
        self.assertAlmostEqual(predict.slope(samples, 999, 300), 1)


if __name__ == "__main__":
    unittest.main()
