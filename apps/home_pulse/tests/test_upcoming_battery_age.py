# tests/test_upcoming_battery_age.py - battery age facts: daily values, battery changes, lifetimes,
# the trend's empty day (daily medians or the days coarse steps happened) and the replace-by day, on
# synthetic series and on the recorder's daily values of 14.10.2026
# (fixtures/battery_days_2026_10_14.json, run-length encoded [day, days, median, min, max]).
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "appliances"))
import upcoming_battery_age as age  # noqa: E402
import upcoming_maint as maint  # noqa: E402
from upcoming_maint import HOUSE_TZ  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "battery_days_2026_10_14.json"
CONFIG = Path(__file__).resolve().parents[1] / "house_upcoming.yaml"
DAY = timedelta(days=1)


def load_fixture():
    data = json.loads(FIXTURE.read_text())
    days = {}
    for entity, runs in data["runs"].items():
        out = []
        for start, count, med, low, high in runs:
            first = date.fromisoformat(start)
            out += [(first + i * DAY, med, low, high) for i in range(count)]
        days[entity] = out
    return datetime.fromisoformat(data["now"]), days


def flat(values, start=date(2026, 1, 1)):
    return [(start + i * DAY, v, v, v) for i, v in enumerate(values)]


def noon(day):
    return datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=12)


def stepped(start, levels):
    """Daily values of a coarse reporter from `start`: [(days, level by day, lowest by night)]."""
    out, day = [], start
    for count, level, low in levels:
        for _ in range(count):
            out.append((day, level, low, level))
            day += DAY
    return out


class DailyValueTests(unittest.TestCase):
    def test_a_day_is_the_time_weighted_median_of_what_held(self):
        start = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)
        series = [(start, 50.0), (start + timedelta(hours=1), 10.0), (start + timedelta(hours=1, minutes=5), 50.0),
                  (start + timedelta(hours=16), 80.0)]
        days = age.daily_stats(series, start + DAY)
        self.assertEqual(days, [(date(2026, 10, 13), 50.0, 10.0, 80.0)])

    def test_unavailable_spans_do_not_count_and_a_whole_day_without_reading_is_absent(self):
        start = datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)
        series = [(start, 40.0), (start + timedelta(hours=2), None), (start + 3 * DAY, 90.0)]
        days = age.daily_stats(series, start + 3 * DAY + timedelta(hours=5))
        self.assertEqual([d[0] for d in days], [date(2026, 10, 11), date(2026, 10, 14)])
        self.assertEqual(days[0][1], 40.0)

    def test_the_autumn_day_has_twenty_five_hours(self):
        midnight = datetime(2026, 10, 25, 0, 0, tzinfo=HOUSE_TZ)
        series = [(midnight, 50.0), (datetime(2026, 10, 25, 12, 0, tzinfo=HOUSE_TZ), 80.0)]
        days = age.daily_stats(series, datetime(2026, 10, 26, 0, 0, tzinfo=HOUSE_TZ))
        self.assertEqual(days, [(date(2026, 10, 25), 50.0, 50.0, 80.0)])

    def test_history_rows_become_a_series(self):
        rows = [[{"state": "52", "last_changed": "2026-07-12T12:00:00+00:00"},
                 {"state": "unavailable", "last_changed": datetime(2026, 7, 12, 12, 36, 39, tzinfo=timezone.utc)},
                 {"state": "81"}]]
        series = age.series_from_history(rows)
        self.assertEqual([v for _, v in series], [52.0, None])


class ChangeTests(unittest.TestCase):
    def test_a_rise_of_thirty_that_holds_is_a_change(self):
        days = flat([60] * 10 + [30, 30, 95, 95, 95, 95])
        self.assertEqual(age.detect_changes(days), [date(2026, 1, 13)])

    def test_a_short_spike_or_a_twenty_point_bounce_is_not(self):
        self.assertEqual(age.detect_changes(flat([40] * 10 + [95] + [40] * 8)), [])
        self.assertEqual(age.detect_changes(flat([20, 40] * 10)), [])

    def test_the_first_week_of_a_series_is_never_a_change(self):
        self.assertEqual(age.detect_changes(flat([10, 10, 95, 95, 95, 95, 95, 95, 95, 95])), [])

    def test_changes_three_weeks_apart_merge_into_the_first(self):
        days = flat([20] * 10 + [60] * 10 + [95] * 10)
        self.assertEqual(age.detect_changes(days), [date(2026, 1, 11)])

    def test_after_a_whole_day_without_reading_twenty_points_is_enough(self):
        days = flat([52] * 10) + [(date(2026, 1, 20), 81, 81, 81)]
        self.assertEqual(age.detect_changes(days), [date(2026, 1, 20)])
        self.assertEqual(age.detect_changes(flat([52] * 10 + [81])), [])

    def test_cells_swapped_late_in_the_day_are_dated_that_day(self):
        days = flat([27] * 10) + [(date(2026, 1, 11), 27, 27, 92)] + flat([92] * 6, start=date(2026, 1, 12))
        self.assertEqual(age.detect_changes(days), [date(2026, 1, 11)])


class FactTests(unittest.TestCase):
    NOW = datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)

    def test_a_lifetime_ends_where_the_dead_cells_stopped_reporting(self):
        days = flat([90] * 10 + [10] * 2 + [95] * 30 + [40] * 30)
        self.assertEqual(days[-1][0], date(2026, 3, 13))
        days += flat([80] * 7, start=date(2026, 3, 19))
        facts = age.battery_facts(days, self.NOW, trend=False)
        self.assertEqual(facts["changes"], ["2026-01-13", "2026-03-19"])
        self.assertEqual(facts["last_set_days"], (date(2026, 3, 13) - date(2026, 1, 13)).days)

    def test_the_first_set_of_a_series_is_never_a_lifetime(self):
        facts = age.battery_facts(flat([90] * 10 + [10] * 2 + [95] * 30), self.NOW)
        self.assertEqual(facts["in_since"], "2026-01-13")
        self.assertNotIn("last_set_days", facts)

    def test_history_from_keeps_old_regime_sets_out_of_lifetimes_and_trends(self):
        days = flat([90] * 10 + [20] + [90] * 40 + [20] + [90] * 40)
        facts = age.battery_facts(days, self.NOW)
        self.assertEqual(facts["changes"], ["2026-01-12", "2026-02-22"])
        self.assertEqual(facts["last_set_days"], 41)
        later = age.battery_facts(days, self.NOW, history_from=date(2026, 2, 1))
        self.assertEqual(later["in_since"], "2026-02-22")
        self.assertNotIn("last_set_days", later)

    def test_a_straight_drain_gives_the_day_it_reads_empty(self):
        start = date(2026, 8, 1)
        days = flat([30] * 10, start=start - 10 * DAY) + [(start + i * DAY, 100 - 0.6 * i, 100 - 0.6 * i, 100 - 0.6 * i)
                                                          for i in range(60)]
        now = noon(start + 59 * DAY)
        facts = age.battery_facts(days, now)
        self.assertEqual(facts["in_since"], "2026-08-01")
        self.assertEqual(facts["empty_on"], (start + 166 * DAY).isoformat())
        self.assertEqual(age.battery_facts(days, now, empty=40)["empty_on"], (start + 100 * DAY).isoformat())
        self.assertNotIn("empty_on", age.battery_facts(days, now, trend=False))

    def test_too_few_values_too_young_or_flat_cells_get_no_trend(self):
        start = date(2026, 9, 1)
        steps = flat([30] * 10, start=start - 10 * DAY) + flat([100] * 20 + [75] * 20, start=start)
        now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        self.assertNotIn("empty_on", age.battery_facts(steps, now))
        young = flat([30] * 10, start=start - 10 * DAY) + [(start + i * DAY, 100 - i, 100 - i, 100 - i) for i in range(15)]
        self.assertNotIn("empty_on", age.battery_facts(young, datetime(2026, 9, 15, 12, tzinfo=timezone.utc)))

    def test_lifetimes_are_the_in_regime_sets_that_ended(self):
        days = flat([90] * 10 + [20] + [90] * 40 + [20] + [90] * 40 + [20] + [90] * 30)
        facts = age.battery_facts(days, self.NOW, trend=False)
        self.assertEqual(facts["lifetimes"], [41, 41])
        self.assertEqual(facts["last_set_days"], 41)
        self.assertEqual(age.battery_facts(days, self.NOW, history_from=date(2026, 2, 1))["lifetimes"], [41])


class StepTrendTests(unittest.TestCase):
    """The WS90's backup cells read 60, 40 or 20: a step shows first as a night's lowest reading."""

    SINCE = date(2026, 8, 30)
    NOW = datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)

    def test_the_days_the_steps_happened_give_the_empty_day(self):
        days = stepped(date(2026, 8, 19), [(13, 60, 60), (6, 60, 40), (32, 40, 40), (6, 40, 20)])
        self.assertEqual(age.step_points(days, self.SINCE, date(2026, 10, 14)), [(2.0, 40.0), (40.0, 20.0)])
        self.assertEqual(age.empty_day(days, self.SINCE, date(2026, 10, 14)), date(2026, 11, 16))
        facts = age.battery_facts(days, self.NOW, history_from=self.SINCE)
        self.assertEqual((facts["changes"], facts["empty_on"]), ([], "2026-11-16"))
        self.assertNotIn("empty_on", age.battery_facts(days, self.NOW))

    def test_a_one_night_dip_is_no_step(self):
        days = stepped(self.SINCE, [(10, 60, 60), (1, 60, 40), (40, 60, 60), (5, 40, 40)])
        self.assertEqual(age.step_points(days, self.SINCE, date(2026, 10, 30)), [(51.0, 40.0)])

    def test_steps_off_the_line_give_no_date(self):
        days = stepped(self.SINCE, [(2, 80, 80), (38, 60, 60), (1, 40, 40), (5, 20, 20)])
        points = age.step_points(days, self.SINCE, self.SINCE + 46 * DAY)
        self.assertEqual([level for _, level in points], [60.0, 40.0, 20.0])
        self.assertIsNone(age.empty_day(days, self.SINCE, self.SINCE + 46 * DAY))

    def test_steps_closer_than_three_weeks_give_no_date(self):
        days = stepped(self.SINCE, [(3, 80, 80), (8, 60, 60), (6, 40, 40), (4, 20, 20)])
        self.assertIsNone(age.empty_day(days, self.SINCE, self.SINCE + 21 * DAY))

    def test_small_steps_are_no_coarse_reporter(self):
        days = stepped(self.SINCE, [(10, 100, 100), (10, 97, 97), (10, 94, 94), (10, 91, 91)])
        self.assertIsNone(age.step_points(days, self.SINCE, self.SINCE + 40 * DAY))
        self.assertIsNone(age.empty_day(days, self.SINCE, self.SINCE + 40 * DAY))

    def test_a_trend_needs_a_start(self):
        days = stepped(date(2026, 8, 19), [(13, 60, 60), (6, 60, 40), (32, 40, 40), (6, 40, 20)])
        self.assertNotIn("empty_on", age.battery_facts(days, self.NOW))


class ReplaceByTests(unittest.TestCase):
    TODAY = date(2026, 10, 14)

    def test_the_trend_comes_first(self):
        facts = {"in_since": "2026-06-19", "lifetimes": [104, 207, 191], "empty_on": "2026-12-26"}
        self.assertEqual(age.replace_by(facts, [100], self.TODAY), (date(2026, 12, 26), "trend"))

    def test_then_the_devices_own_median_lifetime(self):
        facts = {"in_since": "2026-09-11", "lifetimes": [148, 127, 204]}
        self.assertEqual(age.replace_by(facts, [79, 98], self.TODAY), (date(2027, 2, 6), "own"))

    def test_then_the_models_median_lifetime(self):
        facts = {"in_since": "2026-09-15", "lifetimes": [335]}
        model = [79, 98, 127, 148, 204, 335]
        self.assertEqual(age.replace_by(facts, model, self.TODAY), (date(2026, 9, 15) + 137 * DAY, "model"))
        self.assertIsNone(age.replace_by(facts, [], self.TODAY))

    def test_a_day_already_past_is_no_estimate(self):
        facts = {"in_since": "2026-06-02", "lifetimes": [79, 98]}
        self.assertIsNone(age.replace_by(facts, [79, 98, 335], self.TODAY))
        self.assertIsNotNone(age.replace_by({"in_since": "2026-07-18", "lifetimes": [79, 98]}, [], self.TODAY))

    def test_no_basis_no_date(self):
        yale = {"in_since": "2026-02-22", "changes": ["2025-12-31", "2026-02-22"]}
        self.assertIsNone(age.replace_by(yale, [], self.TODAY, date(2026, 2, 22)))
        self.assertIsNone(age.replace_by({"in_since": "2026-02-21"}, [300], self.TODAY, date(2026, 2, 22)))
        self.assertIsNone(age.replace_by({}, [300], self.TODAY))


class HistoryHelperTests(unittest.TestCase):
    T = datetime(2026, 7, 12, 12, 36, 39, tzinfo=timezone.utc)

    def test_the_outage_a_series_ends_in(self):
        self.assertEqual(age.outage_onset([(self.T - DAY, 52.0), (self.T, None), (self.T + DAY, None)]), (self.T, True))
        self.assertEqual(age.outage_onset([(self.T, None), (self.T + DAY, 81.0)]), (None, True))
        self.assertEqual(age.outage_onset([(self.T, None)]), (None, False))
        self.assertEqual(age.outage_onset([]), (None, False))

    def test_low_dates_and_the_peak_of_the_current_cells(self):
        days = flat([40, 20, 40, 20, 40], start=date(2026, 10, 10))
        now = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)
        self.assertEqual(age.low_dates(days, 20, now), ["2026-10-11", "2026-10-13"])
        self.assertEqual(age.peak_since(days, date(2026, 10, 11)), 40)
        self.assertIsNone(age.peak_since([], None))


class LiveDataTests(unittest.TestCase):
    """Item 48 of the future-events plan, on the recorder's data of 14.10.2026."""

    @classmethod
    def setUpClass(cls):
        cls.now, cls.days = load_fixture()
        cls.inventory = maint.parse_inventory(yaml.safe_load(CONFIG.read_text())["HouseUpcoming"]["batteries"])

    def facts(self, entity, **kw):
        return age.battery_facts(self.days[entity], self.now, **kw)

    def inventory_facts(self):
        out = {}
        for entity, days in self.days.items():
            device = self.inventory["devices"][entity]
            model = self.inventory["models"][device["model"]]
            out[entity] = age.battery_facts(days, self.now, trend=model["trend"], history_from=device["history_from"],
                                            empty=model["empty"])
        return out

    def test_the_battery_changes_found(self):
        expected = {
            "sensor.bedroom_blind_battery": ["2025-02-02", "2025-05-17", "2025-12-10", "2026-06-19"],
            "sensor.bathroom_blind_battery": ["2025-02-02", "2025-05-18", "2025-12-10", "2026-06-19"],
            "sensor.kristines_room_presence_battery": ["2025-12-04", "2026-02-21", "2026-06-02"],
            "sensor.hallway_presence_battery": ["2025-05-20", "2025-10-15", "2026-02-19", "2026-09-11"],
            "sensor.guest_bathroom_presence_battery": ["2025-10-15", "2026-09-15"],
            "sensor.dining_room_window_1_battery": ["2025-10-15", "2026-10-14"],
            "sensor.ecowitt_battery_8351": [],
        }
        for entity, changes in expected.items():
            self.assertEqual(self.facts(entity)["changes"], changes, entity)

    def test_lifetimes_and_in_since(self):
        self.assertEqual(self.facts("sensor.bedroom_blind_battery")["lifetimes"], [104, 207, 191])
        self.assertEqual(self.facts("sensor.guest_bathroom_presence_battery")["last_set_days"], 335)
        self.assertEqual(self.facts("sensor.hallway_presence_battery")["lifetimes"], [148, 127, 204])
        self.assertEqual(self.facts("sensor.kristines_room_presence_battery")["lifetimes"], [79, 98])
        window = self.facts("sensor.dining_room_window_1_battery", trend=False)
        self.assertEqual((window["in_since"], window["last_set_days"]), ("2026-10-14", 270))
        self.assertNotIn("empty_on", window)

    def test_the_yale_shows_only_when_its_cells_went_in(self):
        yale = self.facts("sensor.yale_battery", trend=False, history_from=date(2026, 2, 22))
        self.assertEqual(yale["in_since"], "2026-02-22")
        self.assertNotIn("lifetimes", yale)
        self.assertNotIn("empty_on", yale)

    def test_the_blinds_and_the_roof_station_get_a_trend(self):
        trends = {entity: facts.get("empty_on") for entity, facts in self.inventory_facts().items()}
        self.assertEqual({entity: day for entity, day in trends.items() if day}, {
            "sensor.bedroom_blind_battery": "2026-12-26",
            "sensor.bathroom_blind_battery": "2027-05-01",
            "sensor.ecowitt_battery_8351": "2026-11-16",
        })

    def test_the_replace_by_days(self):
        facts = self.inventory_facts()
        lifetimes = {}
        for entity, found in facts.items():
            lifetimes.setdefault(self.inventory["devices"][entity]["model"], []).extend(found.get("lifetimes", []))
        self.assertEqual(sorted(lifetimes["zg204zm"]), [79, 98, 127, 148, 204, 335])
        due = {}
        for entity, found in facts.items():
            device = self.inventory["devices"][entity]
            due[entity] = age.replace_by(found, lifetimes[device["model"]], date(2026, 10, 14), device["history_from"])
        self.assertEqual({entity: day for entity, day in due.items() if day}, {
            "sensor.bedroom_blind_battery": (date(2026, 12, 26), "trend"),
            "sensor.bathroom_blind_battery": (date(2027, 5, 1), "trend"),
            "sensor.ecowitt_battery_8351": (date(2026, 11, 16), "trend"),
            "sensor.hallway_presence_battery": (date(2027, 2, 6), "own"),
            "sensor.guest_bathroom_presence_battery": (date(2027, 1, 30), "model"),
            "sensor.dining_room_window_1_battery": (date(2027, 7, 11), "model"),
        })


if __name__ == "__main__":
    unittest.main()
