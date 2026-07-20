from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import solar_window as sw  # noqa: E402


class OnWindow(unittest.TestCase):
    def test_true_at_window_azimuth(self):
        self.assertTrue(sw.on_window(70))

    def test_true_near_lower_edge(self):
        self.assertTrue(sw.on_window(16))

    def test_true_near_upper_edge(self):
        self.assertTrue(sw.on_window(124))

    def test_false_just_past_upper_edge(self):
        self.assertFalse(sw.on_window(130))

    def test_false_far_off_window(self):
        self.assertFalse(sw.on_window(200))

    def test_wraparound_across_zero(self):
        # Raw diff (359 - 70 = 289) looks nowhere near the window, but the real
        # circular distance is 71 deg (360 - 289) - still outside the 55 deg
        # tolerance, so this must resolve to False via correct wraparound math,
        # not accidentally via the huge raw diff.
        self.assertFalse(sw.on_window(359))


class BeamHeat(unittest.TestCase):
    def test_true_when_on_window_high_enough_and_bright(self):
        self.assertTrue(sw.beam_heat(70, 10, 300))

    def test_false_just_under_radiation_threshold(self):
        self.assertFalse(sw.beam_heat(70, 10, 249))

    def test_false_at_min_elevation_strict(self):
        self.assertFalse(sw.beam_heat(70, 3, 300))

    def test_false_off_window(self):
        self.assertFalse(sw.beam_heat(200, 10, 300))


class DailyHighFromForecast(unittest.TestCase):
    def test_daily_envelope_picks_todays_high(self):
        resp = {"result": {"response": {"weather.forecast_home": {"forecast": [
            {"datetime": "2026-07-20", "temperature": 27, "templow": 16},
            {"datetime": "2026-07-21", "temperature": 30},
        ]}}}}
        self.assertEqual(sw.daily_high_from_forecast(resp, "2026-07-20"), 27.0)

    def test_hourly_shaped_same_date_takes_max(self):
        resp = {"forecast": [
            {"datetime": "2026-07-20T06:00:00", "temperature": 18},
            {"datetime": "2026-07-20T15:00:00", "temperature": 29},
            {"datetime": "2026-07-20T22:00:00", "temperature": 20},
            {"datetime": "2026-07-21T06:00:00", "temperature": 15},
        ]}
        self.assertEqual(sw.daily_high_from_forecast(resp, "2026-07-20"), 29.0)

    def test_native_temperature_fallback(self):
        resp = {"forecast": [{"datetime": "2026-07-20", "native_temperature": 24}]}
        self.assertEqual(sw.daily_high_from_forecast(resp, "2026-07-20"), 24.0)

    def test_today_missing_returns_none(self):
        resp = {"forecast": [{"datetime": "2026-07-21", "temperature": 22}]}
        self.assertIsNone(sw.daily_high_from_forecast(resp, "2026-07-20"))

    def test_empty_response_returns_none(self):
        self.assertIsNone(sw.daily_high_from_forecast({}, "2026-07-20"))

    def test_garbage_response_returns_none(self):
        self.assertIsNone(sw.daily_high_from_forecast("garbage", "2026-07-20"))


if __name__ == "__main__":
    unittest.main()
