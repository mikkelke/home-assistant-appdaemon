# tests/test_upcoming_rain.py - the exploratory rain row for an open terrace door, on replayed
# forecast fixtures. Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "climate"))
import climate_model as cm  # noqa: E402
import upcoming_core as core  # noqa: E402
import upcoming_rain as rain  # noqa: E402

NOW = datetime(2026, 7, 14, 13, 20, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
DOOR1 = "binary_sensor.rooftop_door_1_contact"
DOOR2 = "binary_sensor.rooftop_door_2_contact"
DOORS_CFG = {
    DOOR1: {"label": "living-room terrace door", "area": "living_room"},
    DOOR2: {"label": "Claudia's terrace door", "area": "office"},
}


def envelope(mm_by_hour, start=datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc)):
    """A weather.get_forecasts response as AppDaemon 4.5.13 returns it, one met.no hour per value."""
    forecast = [{"datetime": (start + timedelta(hours=i)).isoformat(), "temperature": 18.0, "precipitation": mm}
                for i, mm in enumerate(mm_by_hour)]
    return {"id": 7, "type": "result", "success": True,
            "result": {"response": {"weather.forecast_home": {"forecast": forecast}}}}


def hours(mm_by_hour):
    return rain.forecast_hours(cm.parse_forecast_envelope(envelope(mm_by_hour), "weather.forecast_home"))


def doors(door1_open_for=None, door2_open_for=None):
    state = {}
    for entity, open_for in ((DOOR1, door1_open_for), (DOOR2, door2_open_for)):
        state[entity] = (open_for is not None, NOW - open_for if open_for is not None else NOW - timedelta(days=1))
    return state


def row(forecast, door_state, fetched_at=NOW, alert_on=False, now=NOW):
    open_long = rain.doors_open_long(door_state, now)
    return rain.rain_row(forecast, fetched_at, open_long, DOORS_CFG, alert_on, now)


class RainRowTests(unittest.TestCase):
    def test_a_door_open_ten_minutes_and_rain_in_two_hours_gives_a_row_at_that_hour(self):
        found = row(hours([0, 0, 0.3, 1.2]), doors(door1_open_for=10 * MIN))
        self.assertEqual(found["at"], "2026-07-14T15:00:00+00:00")
        self.assertEqual((found["title"], found["detail"], found["area"]),
                         ("Rain forecast", "living-room terrace door open", "living_room"))
        self.assertEqual((found["kind"], found["icon"]), ("rain", "mdi:weather-pouring"))
        self.assertNotIn("audience", found)
        self.assertNotIn("audience_users", found)
        self.assertTrue(core.is_valid_row(found))

    def test_rain_in_the_current_hour_counts(self):
        self.assertEqual(row(hours([0.5]), doors(door2_open_for=30 * MIN))["at"], "2026-07-14T13:00:00+00:00")

    def test_two_tenths_of_a_millimetre_is_no_row(self):
        self.assertIsNone(row(hours([0.2, 0.2, 0.2, 0.2]), doors(door1_open_for=10 * MIN)))

    def test_rain_beyond_three_hours_is_no_row(self):
        self.assertIsNone(row(hours([0, 0, 0, 0, 3.0]), doors(door1_open_for=40 * MIN)))

    def test_closed_doors_or_a_door_open_under_ten_minutes_give_no_row(self):
        forecast = hours([2.0])
        self.assertIsNone(row(forecast, doors()))
        self.assertIsNone(row(forecast, doors(door1_open_for=9 * MIN)))

    def test_an_empty_or_ninety_one_minute_old_forecast_gives_no_row(self):
        open_now = doors(door1_open_for=2 * 60 * MIN)
        self.assertIsNone(row([], open_now))
        self.assertIsNone(row(hours([2.0, 2.0, 2.0]), open_now, fetched_at=NOW - 91 * MIN))
        self.assertIsNotNone(row(hours([2.0, 2.0, 2.0]), open_now, fetched_at=NOW - 90 * MIN))

    def test_the_rooftop_rain_alert_takes_over(self):
        self.assertIsNone(row(hours([2.0]), doors(door1_open_for=20 * MIN), alert_on=True))

    def test_both_doors_open_point_at_the_rooftop(self):
        found = row(hours([0, 1.0]), doors(door1_open_for=20 * MIN, door2_open_for=15 * MIN))
        self.assertEqual((found["detail"], found["area"]), ("terrace doors open", "rooftop"))
        found = row(hours([0, 1.0]), doors(door2_open_for=15 * MIN))
        self.assertEqual((found["detail"], found["area"]), ("Claudia's terrace door open", "office"))

    def test_a_malformed_forecast_entry_is_skipped(self):
        self.assertEqual(rain.forecast_hours([{"datetime": "nope"}, {"precipitation": 1}, "x",
                                              {"datetime": "2026-07-14T14:00:00+00:00", "precipitation": None}]),
                         [(datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc), 0.0)])


class FetchTests(unittest.TestCase):
    def test_fetched_only_while_a_door_has_been_open_ten_minutes(self):
        closed = doors()
        self.assertFalse(rain.fetch_due(rain.doors_open_long(closed, NOW), closed, None, NOW))
        short = doors(door1_open_for=5 * MIN)
        self.assertFalse(rain.fetch_due(rain.doors_open_long(short, NOW), short, None, NOW))
        long = doors(door1_open_for=10 * MIN)
        self.assertTrue(rain.fetch_due(rain.doors_open_long(long, NOW), long, None, NOW))

    def test_then_every_thirty_minutes_and_again_for_a_new_spell(self):
        state = doors(door1_open_for=60 * MIN)
        open_long = rain.doors_open_long(state, NOW)
        self.assertFalse(rain.fetch_due(open_long, state, NOW - 29 * MIN, NOW))
        self.assertTrue(rain.fetch_due(open_long, state, NOW - 30 * MIN, NOW))
        reopened = doors(door1_open_for=12 * MIN)
        self.assertTrue(rain.fetch_due(rain.doors_open_long(reopened, NOW), reopened, NOW - 20 * MIN, NOW))

    def test_a_fixture_records_each_early_hour_with_its_lead(self):
        found = row(hours([0, 0.4]), doors(door1_open_for=10 * MIN))
        line = rain.fixture(NOW, hours([0, 0.4, 0, 0, 0]), [DOOR1], found)
        self.assertEqual(line["fetched"], "2026-07-14T13:20:00+00:00")
        self.assertEqual(line["hours"][:2], [["2026-07-14T13:00:00+00:00", -20, 0.0], ["2026-07-14T14:00:00+00:00", 40, 0.4]])
        self.assertEqual(len(line["hours"]), rain.FIXTURE_HOURS)
        self.assertEqual((line["doors"], line["row_at"]), ([DOOR1], "2026-07-14T14:00:00+00:00"))


if __name__ == "__main__":
    unittest.main()
