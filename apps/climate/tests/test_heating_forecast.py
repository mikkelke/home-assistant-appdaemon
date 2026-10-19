# tests/test_heating_forecast.py - the floor-heating controller's weather side: Open-Meteo
# request and parser, NOAA solar position, daily aggregates, the mean over the coming hours and
# sun hours. Pure functions, so no AppDaemon harness is needed.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import heating_forecast as hf  # noqa: E402

TZ = ZoneInfo("Europe/Copenhagen")
LAT, LON = 55.68, 12.57
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "open_meteo_forecast.json"
FETCHED_AT = datetime(2026, 10, 17, 17, 25, tzinfo=timezone.utc).timestamp()


def _payload():
    return json.loads(FIXTURE.read_text())


def _forecast():
    return hf.parse_response(_payload(), FETCHED_AT)


def _local(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).timestamp()


class RequestUrl(unittest.TestCase):
    def test_location_is_rounded_to_two_decimals(self):
        url = hf.request_url(55.6841, 12.5666)
        self.assertIn("latitude=55.68&longitude=12.57&", url)

    def test_asks_for_epoch_times_kmh_and_four_days(self):
        url = hf.request_url(55.68, 12.57)
        for part in (
            "timeformat=unixtime",
            "wind_speed_unit=kmh",
            "forecast_days=4",
            "timezone=auto",
            "temperature_2m,wind_speed_10m,shortwave_radiation,cloud_cover",
        ):
            self.assertIn(part, url)


class ParseResponse(unittest.TestCase):
    def test_recorded_fixture_parses_to_three_days_of_hours(self):
        fc = _forecast()
        self.assertEqual(len(fc.times), 72)
        self.assertEqual(fc.fetched_at, FETCHED_AT)
        # The first stamp is local midnight; radiation is stamped at the end of its hour.
        self.assertEqual(datetime.fromtimestamp(fc.times[0], TZ).hour, 0)
        self.assertEqual(fc.ghi[36], 368.0)
        self.assertEqual(fc.temperature[0], 12.5)
        self.assertEqual(fc.hour_start(0), fc.times[0] - 3600)

    def test_missing_required_series_is_an_error(self):
        for name in ("temperature_2m", "wind_speed_10m", "shortwave_radiation"):
            payload = _payload()
            del payload["hourly"][name]
            with self.assertRaises(ValueError, msg=name):
                hf.parse_response(payload, FETCHED_AT)

    def test_missing_cloud_cover_is_tolerated(self):
        payload = _payload()
        del payload["hourly"]["cloud_cover"]
        fc = hf.parse_response(payload, FETCHED_AT)
        self.assertEqual(fc.cloud, (None,) * 72)

    def test_series_that_do_not_match_the_time_axis_are_an_error(self):
        payload = _payload()
        payload["hourly"]["temperature_2m"] = payload["hourly"]["temperature_2m"][:-1]
        with self.assertRaises(ValueError):
            hf.parse_response(payload, FETCHED_AT)

    def test_junk_payloads_are_errors(self):
        for junk in (
            None,
            [],
            {},
            {"hourly": {}},
            {"hourly": {"time": "x"}},
            {"hourly": {"time": []}},
        ):
            with self.assertRaises(ValueError, msg=repr(junk)):
                hf.parse_response(junk, FETCHED_AT)

    def test_time_axis_must_be_epoch_seconds_and_increasing(self):
        payload = _payload()
        payload["hourly"]["time"][3] = "2026-10-17T03:00"
        with self.assertRaises(ValueError):
            hf.parse_response(payload, FETCHED_AT)
        payload = _payload()
        payload["hourly"]["time"][3] = payload["hourly"]["time"][2]
        with self.assertRaises(ValueError):
            hf.parse_response(payload, FETCHED_AT)

    def test_nulls_and_junk_values_stay_none(self):
        payload = _payload()
        payload["hourly"]["shortwave_radiation"][5] = None
        payload["hourly"]["temperature_2m"][6] = "n/a"
        fc = hf.parse_response(payload, FETCHED_AT)
        self.assertIsNone(fc.ghi[5])
        self.assertIsNone(fc.temperature[6])
        self.assertEqual(fc.temperature[7], 11.0)


class Staleness(unittest.TestCase):
    def test_fresh_forecast_is_usable(self):
        fc = _forecast()
        self.assertTrue(hf.is_usable(fc, FETCHED_AT + 600, 12))
        self.assertAlmostEqual(hf.age_minutes(fc, FETCHED_AT + 600), 10.0)

    def test_forecast_older_than_the_limit_is_not_usable(self):
        fc = _forecast()
        self.assertTrue(hf.is_usable(fc, FETCHED_AT + 12 * 3600 - 1, 12))
        self.assertFalse(hf.is_usable(fc, FETCHED_AT + 12 * 3600 + 60, 12))

    def test_forecast_that_no_longer_covers_now_is_not_usable(self):
        fc = _forecast()
        self.assertFalse(hf.is_usable(fc, fc.times[-1] + 60, 100))

    def test_no_forecast_is_not_usable(self):
        self.assertFalse(hf.is_usable(None, FETCHED_AT, 12))


class SolarPosition(unittest.TestCase):
    @staticmethod
    def _scan(day, hours, step_min=1):
        base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
        for minute in range(hours[0] * 60, hours[1] * 60, step_min):
            ts = base + minute * 60
            yield ts, hf.solar_position(ts, LAT, LON)

    def test_winter_solstice_noon_is_low_and_due_south(self):
        ts, (az, el) = max(self._scan(date(2026, 12, 21), (9, 13)), key=lambda x: x[1][1])
        self.assertAlmostEqual(el, 90 - LAT - 23.44, delta=0.3)
        self.assertAlmostEqual(az, 180.0, delta=1.0)
        self.assertEqual(datetime.fromtimestamp(ts, timezone.utc).hour, 11)

    def test_summer_solstice_noon_is_high(self):
        _ts, (_az, el) = max(self._scan(date(2026, 6, 21), (9, 13)), key=lambda x: x[1][1])
        self.assertAlmostEqual(el, 90 - LAT + 23.44, delta=0.3)

    def test_equinox_sunrise_is_in_the_east(self):
        for _ts, (az, el) in self._scan(date(2026, 3, 20), (0, 12)):
            if el > 0:
                break
        self.assertAlmostEqual(az, 90.0, delta=1.5)

    def test_afternoon_is_in_the_west_and_night_is_below_the_horizon(self):
        ts = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc).timestamp()
        az, el = hf.solar_position(ts, LAT, LON)
        self.assertTrue(200 < az < 260, az)
        self.assertGreater(el, 0)
        _az, el = hf.solar_position(ts + 8 * 3600, LAT, LON)
        self.assertLess(el, 0)


class DayAggregates(unittest.TestCase):
    def test_a_full_day(self):
        dw = hf.day_weather(_forecast(), date(2026, 10, 18), TZ)
        self.assertEqual(dw.hours, 24)
        self.assertAlmostEqual(dw.t_mean, 9.2958, places=3)
        self.assertAlmostEqual(dw.hdd, 17 - 9.2958, places=3)
        self.assertAlmostEqual(dw.wind_mean, 15.9208, places=3)
        self.assertAlmostEqual(dw.ghi_kwh, 2.53, places=3)

    def test_each_day_gets_its_own_hours(self):
        fc = _forecast()
        self.assertAlmostEqual(hf.day_weather(fc, date(2026, 10, 17), TZ).ghi_kwh, 1.065, places=3)
        last = hf.day_weather(fc, date(2026, 10, 19), TZ)
        self.assertEqual(last.hours, 23)
        self.assertAlmostEqual(last.t_mean, 9.7, places=3)

    def test_hdd_never_goes_negative(self):
        payload = _payload()
        payload["hourly"]["temperature_2m"] = [21.0] * 72
        dw = hf.day_weather(hf.parse_response(payload, FETCHED_AT), date(2026, 10, 18), TZ)
        self.assertEqual(dw.hdd, 0.0)

    def test_day_the_forecast_barely_touches_is_none(self):
        self.assertIsNone(hf.day_weather(_forecast(), date(2026, 10, 16), TZ))
        self.assertIsNone(hf.day_weather(_forecast(), date(2026, 10, 25), TZ))

    def test_day_with_too_many_missing_values_is_none(self):
        payload = copy.deepcopy(_payload())
        for i in range(25, 40):
            payload["hourly"]["shortwave_radiation"][i] = None
        fc = hf.parse_response(payload, FETCHED_AT)
        self.assertIsNone(hf.day_weather(fc, date(2026, 10, 18), TZ))

    def test_the_day_is_the_local_day_across_the_utc_boundary(self):
        # 22:00 UTC is local midnight in October: the hour ending then is still the 17th.
        end = datetime(2026, 10, 17, 22, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(hf.hour_day(end, TZ), date(2026, 10, 17))
        self.assertEqual(hf.hour_day(end + 3600, TZ), date(2026, 10, 18))


class MeanAhead(unittest.TestCase):
    def test_the_mean_starts_at_the_hour_that_contains_the_instant(self):
        fc = _forecast()
        got = hf.mean_ahead(fc, fc.temperature, _local(date(2026, 10, 17), 6, 30), 24)
        # the hour stamped 07:00 is index 7
        self.assertAlmostEqual(got, sum(fc.temperature[7:31]) / 24, places=9)

    def test_an_instant_before_the_series_starts_at_the_first_hour(self):
        fc = _forecast()
        got = hf.mean_ahead(fc, fc.temperature, _local(date(2026, 10, 16), 23, 30), 72)
        self.assertAlmostEqual(got, sum(fc.temperature) / 72, places=9)

    def test_a_window_the_forecast_does_not_reach_is_none(self):
        fc = _forecast()
        eight = _local(date(2026, 10, 17), 8, 5)  # the hour stamped 09:00 is index 9
        self.assertIsNone(hf.mean_ahead(fc, fc.temperature, eight, 72))
        self.assertIsNotNone(hf.mean_ahead(fc, fc.temperature, eight, 63))
        self.assertIsNone(hf.mean_ahead(fc, fc.temperature, fc.times[-1] + 10, 1))

    def test_unknown_values_are_skipped_down_to_nine_tenths_of_the_window(self):
        payload = _payload()
        for i in range(7):
            payload["hourly"]["temperature_2m"][i * 10] = None
        fc = hf.parse_response(payload, FETCHED_AT)
        start = _local(date(2026, 10, 16), 23, 30)
        known = [v for v in fc.temperature if v is not None]
        self.assertEqual(len(known), 65)
        self.assertAlmostEqual(
            hf.mean_ahead(fc, fc.temperature, start, 72), sum(known) / 65, places=9
        )
        payload["hourly"]["temperature_2m"][71] = None
        fc = hf.parse_response(payload, FETCHED_AT)
        self.assertIsNone(hf.mean_ahead(fc, fc.temperature, start, 72))


class SunHours(unittest.TestCase):
    def _hours(self, day, az_min, az_max, ghi_min=150):
        starts = hf.sun_hours(_forecast(), day, TZ, LAT, LON, az_min, az_max, ghi_min)
        return [datetime.fromtimestamp(s, TZ).strftime("%H:%M") for s in starts]

    def test_dull_day_has_two_bright_hours(self):
        self.assertEqual(self._hours(date(2026, 10, 17), 100, 260), ["11:00", "14:00"])

    def test_clear_day_has_the_bright_afternoon_through_morning_span(self):
        got = self._hours(date(2026, 10, 18), 100, 260)
        self.assertEqual(got[0], "09:00")
        self.assertEqual(got[-1], "16:00")
        self.assertEqual(len(got), 8)

    def test_azimuth_range_picks_the_facade(self):
        # A west-looking facade only sees the sun after solar noon (about 12:55 local).
        self.assertEqual(
            self._hours(date(2026, 10, 18), 180, 290), ["13:00", "14:00", "15:00", "16:00"]
        )

    def test_threshold_is_inclusive_and_removes_weak_hours(self):
        self.assertEqual(len(self._hours(date(2026, 10, 18), 100, 260, ghi_min=157)), 8)
        self.assertEqual(len(self._hours(date(2026, 10, 18), 100, 260, ghi_min=158)), 7)
        self.assertEqual(self._hours(date(2026, 10, 17), 100, 260, ghi_min=400), [])

    def test_missing_irradiance_is_not_a_sun_hour(self):
        payload = _payload()
        payload["hourly"]["shortwave_radiation"][36] = None
        fc = hf.parse_response(payload, FETCHED_AT)
        starts = hf.sun_hours(fc, date(2026, 10, 18), TZ, LAT, LON, 100, 260, 150)
        self.assertEqual(len(starts), 7)


class ValueAt(unittest.TestCase):
    def test_picks_the_hour_containing_the_instant(self):
        fc = _forecast()
        ts = _local(date(2026, 10, 18), 12, 30)  # inside the hour stamped 13:00
        self.assertEqual(hf.value_at(fc, fc.ghi, ts), 408.0)
        self.assertIsNone(hf.value_at(fc, fc.ghi, fc.times[-1] + 10))


if __name__ == "__main__":
    unittest.main()
