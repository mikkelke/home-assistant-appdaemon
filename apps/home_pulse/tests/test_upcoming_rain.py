# tests/test_upcoming_rain.py - the rain rows for the open terrace doors, one for each door in its own
# room, on replayed forecast fixtures: the forecast row and the row that says to close a door in the rain.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

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
HOME = timedelta(hours=1)
AWAY = timedelta(hours=3)
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


def rows(forecast, door_state, fetched_at=NOW, alert_on=False, now=NOW, ahead=AWAY):
    return rain.rain_rows(forecast, fetched_at, door_state, DOORS_CFG, alert_on, now, ahead)


def row(forecast, door_state, **kwargs):
    """The only row there is, None for none."""
    found = rows(forecast, door_state, **kwargs)
    assert len(found) <= 1, found
    return found[0] if found else None


class RainRowTests(unittest.TestCase):
    def test_a_door_open_ten_minutes_and_rain_in_two_hours_gives_a_row_at_that_hour(self):
        found = row(hours([0, 0, 0.3, 1.2]), doors(door1_open_for=10 * MIN))
        self.assertEqual((found["id"], found["at"]), ("rain:living_room", "2026-07-14T15:00:00+00:00"))
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

    def test_rain_beyond_the_horizon_is_no_row(self):
        self.assertIsNone(row(hours([0, 0, 0, 0, 3.0]), doors(door1_open_for=40 * MIN)))
        self.assertIsNotNone(row(hours([0, 0, 0, 3.0]), doors(door1_open_for=40 * MIN)))
        self.assertIsNone(row(hours([0, 0, 0, 3.0]), doors(door1_open_for=40 * MIN), ahead=timedelta(hours=2)))

    def test_while_somebody_is_home_it_looks_one_hour_ahead_and_otherwise_three(self):
        state = doors(door1_open_for=40 * MIN)
        soon, later = hours([0, 0.6]), hours([0, 0, 0.6])
        self.assertEqual(row(soon, state, ahead=HOME)["at"], "2026-07-14T14:00:00+00:00")
        self.assertIsNone(row(later, state, ahead=HOME))
        self.assertEqual(row(later, state, ahead=AWAY)["at"], "2026-07-14T15:00:00+00:00")
        self.assertEqual(row(soon, state, ahead=AWAY)["at"], "2026-07-14T14:00:00+00:00")

    def test_the_horizon_is_the_short_one_unless_zone_home_counts_nobody(self):
        for state in ("1", "2", "3", 1, "unknown", "unavailable", None, "", "0.5", "10"):
            self.assertEqual(rain.horizon(state, HOME, AWAY), HOME, state)
        for state in ("0", 0, " 0 "):
            self.assertEqual(rain.horizon(state, HOME, AWAY), AWAY, state)

    def test_closed_doors_or_a_door_open_under_ten_minutes_give_no_row(self):
        forecast = hours([2.0])
        self.assertIsNone(row(forecast, doors()))
        self.assertIsNone(row(forecast, doors(door1_open_for=9 * MIN)))

    def test_an_empty_or_ninety_one_minute_old_forecast_gives_no_row(self):
        open_now = doors(door1_open_for=2 * 60 * MIN)
        self.assertIsNone(row([], open_now))
        self.assertIsNone(row(hours([2.0, 2.0, 2.0]), open_now, fetched_at=NOW - 91 * MIN))
        self.assertIsNotNone(row(hours([2.0, 2.0, 2.0]), open_now, fetched_at=NOW - 90 * MIN))

    def test_each_door_open_long_gets_its_own_row_in_its_own_room_at_the_same_hour(self):
        found = rows(hours([0, 1.0]), doors(door1_open_for=20 * MIN, door2_open_for=15 * MIN))
        at = "2026-07-14T14:00:00+00:00"
        self.assertEqual([(r["id"], r["title"], r["detail"], r["area"], r["at"]) for r in found], [
            ("rain:living_room", "Rain forecast", "living-room terrace door open", "living_room", at),
            ("rain:office", "Rain forecast", "Claudia's terrace door open", "office", at)])
        self.assertTrue(all(core.is_valid_row(r) for r in found))
        only = rows(hours([0, 1.0]), doors(door1_open_for=20 * MIN, door2_open_for=5 * MIN))
        self.assertEqual([r["id"] for r in only], ["rain:living_room"])
        self.assertEqual([r["id"] for r in rows(hours([0, 1.0]), doors(door2_open_for=15 * MIN))], ["rain:office"])

    def test_the_forecast_rows_are_all_gone_together_when_no_rain_is_coming(self):
        both = doors(door1_open_for=20 * MIN, door2_open_for=15 * MIN)
        self.assertEqual(rows(hours([0.1, 0.1, 0.1]), both), [])
        self.assertEqual(rows([], both), [])
        self.assertEqual(rows(hours([2.0]), both, fetched_at=NOW - 91 * MIN), [])
        self.assertEqual(rows(hours([2.0]), both, fetched_at=None), [])


class RainingTests(unittest.TestCase):
    def test_while_the_alert_is_on_an_open_door_has_a_row_that_says_to_close_it(self):
        found = row(hours([2.0]), doors(door1_open_for=20 * MIN), alert_on=True)
        self.assertEqual((found["id"], found["title"], found["detail"], found["area"]),
                         ("rain:living_room", "Raining, close the living-room terrace door", "rain on the open door",
                          "living_room"))
        self.assertEqual((found["kind"], found["icon"]), ("rain", "mdi:weather-pouring"))
        self.assertTrue(core.is_valid_row(found))

    def test_the_row_is_at_the_current_hour_so_it_does_not_change_with_every_tick(self):
        state = doors(door2_open_for=20 * MIN)
        for minutes in (0, 20, 59):
            now = datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc) + minutes * MIN
            found = row([], state, alert_on=True, now=now)
            self.assertEqual(found["at"], "2026-07-14T13:00:00+00:00", minutes)
        self.assertEqual(row([], state, alert_on=True, now=NOW + 40 * MIN)["at"], "2026-07-14T14:00:00+00:00")

    def test_every_open_door_has_its_row_and_a_closed_one_has_none(self):
        both = rows([], doors(door1_open_for=2 * MIN, door2_open_for=2 * MIN), alert_on=True)
        self.assertEqual([(r["id"], r["title"], r["area"]) for r in both], [
            ("rain:living_room", "Raining, close the living-room terrace door", "living_room"),
            ("rain:office", "Raining, close the Claudia's terrace door", "office")])
        self.assertEqual([r["id"] for r in rows([], doors(door2_open_for=2 * MIN), alert_on=True)], ["rain:office"])
        self.assertEqual(rows([], doors(), alert_on=True), [])

    def test_a_door_that_has_only_just_opened_in_the_rain_counts_and_no_forecast_is_needed(self):
        found = rows([], doors(door1_open_for=MIN), alert_on=True, fetched_at=None)
        self.assertEqual([r["id"] for r in found], ["rain:living_room"])

    def test_when_the_alert_goes_off_the_forecast_row_comes_back_or_nothing_does(self):
        state = doors(door1_open_for=30 * MIN)
        self.assertEqual([r["title"] for r in rows(hours([1.0, 1.0]), state, alert_on=True)],
                         ["Raining, close the living-room terrace door"])
        self.assertEqual([r["title"] for r in rows(hours([1.0, 1.0]), state)], ["Rain forecast"])
        self.assertEqual(rows(hours([0, 0]), state), [])

    def test_a_door_the_config_does_not_know_still_gets_a_row_named_after_its_entity(self):
        state = {"binary_sensor.roof_hatch": (True, NOW - 20 * MIN)}
        found = rain.rain_rows([], None, state, {}, True, NOW, AWAY)
        self.assertEqual((found[0]["id"], found[0]["title"], "area" in found[0]),
                         ("rain:roof_hatch", "Raining, close the terrace door", False))

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
        found = rows(hours([0, 0.4]), doors(door1_open_for=10 * MIN))
        line = rain.fixture(NOW, hours([0, 0.4, 0, 0, 0]), [DOOR1], found, AWAY)
        self.assertEqual(line["fetched"], "2026-07-14T13:20:00+00:00")
        self.assertEqual(line["hours"][:2],
                         [["2026-07-14T13:00:00+00:00", -20, 0.0], ["2026-07-14T14:00:00+00:00", 40, 0.4]])
        self.assertEqual(len(line["hours"]), rain.FIXTURE_HOURS)
        self.assertEqual((line["doors"], line["row_at"], line["ahead_min"]),
                         ([DOOR1], "2026-07-14T14:00:00+00:00", 180))
        self.assertIsNone(rain.fixture(NOW, hours([0]), [DOOR1], [], HOME)["row_at"])
        self.assertEqual(rain.fixture(NOW, hours([0]), [DOOR1], [], HOME)["ahead_min"], 60)


if __name__ == "__main__":
    unittest.main()
