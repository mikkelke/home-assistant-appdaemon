# tests/test_upcoming_drum.py - the drum clean's cheapest start: published prices only, the search
# window before and after tomorrow's prices are out, the saving rule and the row text.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import upcoming_drum as drum  # noqa: E402
from upcoming_maint import HOUSE_TZ  # noqa: E402

TODAY = date(2026, 10, 15)
TOMORROW = TODAY + timedelta(days=1)
KEYS = ("bomuld_90", "bomuld_60")


def local(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=HOUSE_TZ)


def raw(day, prices):
    """The price entity's raw points for `day`: `prices` is one kr/kWh per hour, or a {hour: kr} map
    over a default of 2.0."""
    if isinstance(prices, dict):
        prices = [prices.get(hour, 2.0) for hour in range(24)]
    return [{"hour": local(day, hour).isoformat(), "price": price} for hour, price in enumerate(prices)]


def price_attrs(today, tomorrow=None, valid=False):
    return {"raw_today": raw(TODAY, today), "raw_tomorrow": [] if tomorrow is None else raw(TOMORROW, tomorrow),
            "tomorrow_valid": valid}


def profiles(**programmes):
    listed = [{"key": key, "label": key, "n": 5, "p": p} for key, p in programmes.items()]
    return {"schema": 1, "appliances": {"washer": {"programmes": listed}}}


# Two hours at 10 Wh a minute: 1.2 kWh.
TWO_HOURS = profiles(bomuld_60=",".join(["100"] * 120))


class WindowTests(unittest.TestCase):
    NOW = local(TODAY, 10)

    def test_before_tomorrows_prices_the_run_must_end_with_today(self):
        attrs = price_attrs({22: 0.5, 23: 0.5}, tomorrow={0: 0.1, 1: 0.1, 2: 0.1}, valid=False)
        start, text = drum.drum_start(attrs, TWO_HOURS, KEYS, self.NOW)
        self.assertEqual(start, local(TODAY, 22))
        self.assertEqual(text, "cheapest start 22:00 - saves 1,80 kr vs now")

    def test_after_tomorrows_prices_the_search_runs_to_the_end_of_tomorrow(self):
        attrs = price_attrs({22: 0.5, 23: 0.5}, tomorrow={22: 0.1, 23: 0.1}, valid=True)
        start, text = drum.drum_start(attrs, TWO_HOURS, KEYS, self.NOW)
        self.assertEqual(start, local(TOMORROW, 22))
        self.assertEqual(text, "cheapest start 22:00 - saves 2,28 kr vs now")

    def test_no_start_whose_run_ends_after_the_last_price(self):
        attrs = price_attrs({}, tomorrow={}, valid=False)
        self.assertIsNone(drum.drum_start(attrs, TWO_HOURS, KEYS, local(TODAY, 22, 1)))
        start, _ = drum.drum_start(attrs, TWO_HOURS, KEYS, local(TODAY, 21, 50))
        self.assertEqual(start, local(TODAY, 21, 50))

    def test_starts_are_now_then_every_quarter_hour(self):
        attrs = price_attrs({11: 0.5, 12: 0.5, 13: 0.5})
        start, _ = drum.drum_start(attrs, TWO_HOURS, KEYS, local(TODAY, 10, 7) + timedelta(seconds=20))
        self.assertEqual(start, local(TODAY, 11))
        start, text = drum.drum_start(price_attrs({10: 0.5, 11: 0.5}), TWO_HOURS, KEYS,
                                      local(TODAY, 10, 7) + timedelta(seconds=20))
        self.assertEqual((start, text), (local(TODAY, 10, 8), "start now"))


class SavingRuleTests(unittest.TestCase):
    NOW = local(TODAY, 10)

    def test_starting_now_wins_unless_later_saves_ten_ore_and_ten_percent(self):
        start, text = drum.drum_start(price_attrs([1.0] * 11 + [0.95] * 13), TWO_HOURS, KEYS, self.NOW)
        self.assertEqual((start, text), (self.NOW, "start now"))
        start, text = drum.drum_start(price_attrs([10.0] * 11 + [9.5] * 13), TWO_HOURS, KEYS, self.NOW)
        self.assertEqual((start, text), (self.NOW, "start now"))
        start, text = drum.drum_start(price_attrs([1.0] * 11 + [0.85] * 13), TWO_HOURS, KEYS, self.NOW)
        self.assertEqual((start, text), (self.NOW, "start now"))
        start, text = drum.drum_start(price_attrs([1.0] * 11 + [0.8] * 13), TWO_HOURS, KEYS, self.NOW)
        self.assertEqual((start, text), (local(TODAY, 11), "cheapest start 11:00 - saves 0,12 kr vs now"))

    def test_a_glitched_price_now_leaves_no_saving_to_claim(self):
        attrs = price_attrs({10: 109.57, 11: 1.0, 12: 1.0})
        self.assertEqual(drum.drum_start(attrs, TWO_HOURS, KEYS, self.NOW), (local(TODAY, 11), "cheapest start 11:00"))

    def test_ties_keep_the_earliest_start(self):
        start, text = drum.drum_start(price_attrs({}), TWO_HOURS, KEYS, self.NOW)
        self.assertEqual((start, text), (self.NOW, "start now"))


class SourceTests(unittest.TestCase):
    def test_only_whole_published_hours_in_range_are_prices(self):
        points = raw(TODAY, {}) + [{"hour": local(TOMORROW, 0, 15).isoformat(), "price": 0.1},
                                   {"hour": local(TOMORROW, 1).isoformat(), "price": True},
                                   {"hour": local(TOMORROW, 2).isoformat(), "price": 109.57},
                                   {"hour": "noon", "price": 1.0}, "x"]
        prices = drum.hour_prices({"raw_today": points, "tomorrow_valid": True})
        self.assertEqual(len(prices), 24)
        self.assertEqual(len(drum.hour_prices(price_attrs({}, tomorrow={}, valid="true"))), 24)
        self.assertEqual(len(drum.hour_prices(price_attrs({}, tomorrow={}, valid=True))), 48)
        self.assertEqual(drum.hour_prices(None), {})

    def test_the_autumn_day_has_twenty_five_priced_hours(self):
        start = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)
        points = [{"hour": (start + i * timedelta(hours=1)).astimezone(HOUSE_TZ).isoformat(), "price": 1.0}
                  for i in range(25)]
        self.assertEqual(len(drum.hour_prices({"raw_today": points})), 25)

    def test_the_first_published_programme_of_the_list(self):
        both = profiles(bomuld_60="10,20", bomuld_90="30,40")
        self.assertEqual(drum.programme_profile(both, "washer", KEYS), ("bomuld_90", [3.0, 4.0]))
        self.assertEqual(drum.programme_profile(profiles(bomuld_60="10,20"), "washer", KEYS), ("bomuld_60", [1.0, 2.0]))
        broken = profiles(bomuld_90="1,x", bomuld_60="5")
        self.assertEqual(drum.programme_profile(broken, "washer", KEYS), ("bomuld_60", [0.5]))
        self.assertIsNone(drum.programme_profile({**both, "schema": 2}, "washer", KEYS))
        self.assertIsNone(drum.programme_profile(profiles(eco="10"), "washer", KEYS))
        self.assertIsNone(drum.programme_profile(None, "washer", KEYS))

    def test_no_profile_or_no_prices_no_start(self):
        self.assertIsNone(drum.drum_start(price_attrs({}), profiles(eco="10"), KEYS, local(TODAY, 10)))
        self.assertIsNone(drum.drum_start(None, TWO_HOURS, KEYS, local(TODAY, 10)))


if __name__ == "__main__":
    unittest.main()
