# tests/test_upcoming_drum.py - the cheapest start of the drum clean and of the dishwasher's hygiene run:
# published prices only, the search window before and after tomorrow's prices are out, the
# always-cheapest rule and its text, and the Timer setting, its stand-in profile and the wait for 13:00.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "appliances"))
import upcoming_drum as drum  # noqa: E402
from upcoming_maint import HOUSE_TZ  # noqa: E402

TODAY = date(2026, 10, 15)
TOMORROW = TODAY + timedelta(days=1)
KEYS = ("drum_clean", "bomuld_90", "bomuld_60")


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
        self.assertEqual(drum.drum_start(attrs, TWO_HOURS, KEYS, self.NOW),
                         (local(TODAY, 22), "saves 1,80 kr vs now", False))

    def test_after_tomorrows_prices_the_search_runs_to_the_end_of_tomorrow(self):
        attrs = price_attrs({22: 0.5, 23: 0.5}, tomorrow={22: 0.1, 23: 0.1}, valid=True)
        self.assertEqual(drum.drum_start(attrs, TWO_HOURS, KEYS, self.NOW),
                         (local(TOMORROW, 22), "saves 2,28 kr vs now", False))

    def test_no_start_whose_run_ends_after_the_last_price(self):
        attrs = price_attrs({}, tomorrow={}, valid=False)
        self.assertIsNone(drum.drum_start(attrs, TWO_HOURS, KEYS, local(TODAY, 22, 1)))
        self.assertEqual(drum.drum_start(attrs, TWO_HOURS, KEYS, local(TODAY, 21, 50)),
                         (local(TODAY, 21, 50), "cheapest now", True))

    def test_starts_are_now_then_every_quarter_hour(self):
        now = local(TODAY, 10, 7) + timedelta(seconds=20)
        start, _, _ = drum.drum_start(price_attrs({11: 0.5, 12: 0.5, 13: 0.5}), TWO_HOURS, KEYS, now)
        self.assertEqual(start, local(TODAY, 11))
        self.assertEqual(drum.drum_start(price_attrs({10: 0.5, 11: 0.5}), TWO_HOURS, KEYS, now),
                         (local(TODAY, 10, 8), "cheapest now", True))


class AlwaysCheapestTests(unittest.TestCase):
    NOW = local(TODAY, 10)

    def test_any_saving_moves_the_start(self):
        self.assertEqual(drum.drum_start(price_attrs([1.0] * 11 + [0.95] * 13), TWO_HOURS, KEYS, self.NOW),
                         (local(TODAY, 11), "saves 0,03 kr vs now", False))
        self.assertEqual(drum.drum_start(price_attrs([10.0] * 11 + [9.5] * 13), TWO_HOURS, KEYS, self.NOW),
                         (local(TODAY, 11), "saves 0,30 kr vs now", False))
        self.assertEqual(drum.drum_start(price_attrs([1.0] * 11 + [0.99] * 13), TWO_HOURS, KEYS, self.NOW),
                         (local(TODAY, 11), "saves 0,01 kr vs now", False))

    def test_starting_now_is_cheapest_when_nothing_later_saves_an_ore(self):
        self.assertEqual(drum.drum_start(price_attrs([0.5] * 11 + [1.0] * 13), TWO_HOURS, KEYS, self.NOW),
                         (local(TODAY, 10), "cheapest now", True))
        self.assertEqual(drum.drum_start(price_attrs([1.0] * 11 + [0.995] * 13), TWO_HOURS, KEYS, self.NOW),
                         (local(TODAY, 10), "cheapest now", True))

    def test_now_is_the_next_whole_minute(self):
        found = drum.drum_start(price_attrs({}), TWO_HOURS, KEYS, local(TODAY, 10) + timedelta(seconds=1))
        self.assertEqual(found, (local(TODAY, 10, 1), "cheapest now", True))

    def test_a_glitched_price_now_leaves_no_saving_to_claim(self):
        attrs = price_attrs({10: 109.57, 11: 1.0, 12: 1.0})
        self.assertEqual(drum.drum_start(attrs, TWO_HOURS, KEYS, self.NOW),
                         (local(TODAY, 11), "cheapest start 11:00", False))

    def test_ties_keep_the_earliest_start(self):
        self.assertEqual(drum.drum_start(price_attrs({}), TWO_HOURS, KEYS, self.NOW), (self.NOW, "cheapest now", True))
        start, _, _ = drum.drum_start(price_attrs({13: 0.5, 14: 0.5, 17: 0.5, 18: 0.5}), TWO_HOURS, KEYS, self.NOW)
        self.assertEqual(start, local(TODAY, 13))


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

    def test_drum_clean_then_bomuld_90_then_bomuld_60(self):
        every = profiles(bomuld_60="10,20", bomuld_90="30,40", drum_clean="50,60")
        self.assertEqual(drum.programme_profile(every, "washer", KEYS), ("drum_clean", [5.0, 6.0]))
        both = profiles(bomuld_60="10,20", bomuld_90="30,40")
        self.assertEqual(drum.programme_profile(both, "washer", KEYS), ("bomuld_90", [3.0, 4.0]))
        self.assertEqual(drum.programme_profile(profiles(bomuld_60="10,20"), "washer", KEYS), ("bomuld_60", [1.0, 2.0]))
        broken = profiles(drum_clean="", bomuld_90="1,x", bomuld_60="5")
        self.assertEqual(drum.programme_profile(broken, "washer", KEYS), ("bomuld_60", [0.5]))
        self.assertIsNone(drum.programme_profile({**both, "schema": 2}, "washer", KEYS))
        self.assertIsNone(drum.programme_profile(profiles(eco="10"), "washer", KEYS))
        self.assertIsNone(drum.programme_profile(None, "washer", KEYS))

    def test_the_drum_clean_profile_prices_the_start(self):
        # 1.5 kWh in one hour against Bomuld 60's 1.2 kWh in two.
        both = profiles(bomuld_60=",".join(["100"] * 120), drum_clean=",".join(["250"] * 60))
        self.assertEqual(drum.drum_start(price_attrs({22: 0.5}), both, KEYS, local(TODAY, 10)),
                         (local(TODAY, 22), "saves 2,25 kr vs now", False))

    def test_no_profile_or_no_prices_no_start(self):
        self.assertIsNone(drum.drum_start(price_attrs({}), profiles(eco="10"), KEYS, local(TODAY, 10)))
        self.assertIsNone(drum.drum_start(None, TWO_HOURS, KEYS, local(TODAY, 10)))


def programme(key, p, label=None):
    return {"key": key, "label": label or key, "n": 5, "p": p}


def dish_profiles(*programmes):
    return {"schema": 1, "appliances": {"dishwasher": {"programmes": list(programmes)}}}


def flat(dwh_per_min, minutes):
    return ",".join([str(dwh_per_min)] * minutes)


# Two hours at 10 Wh a minute for Intensive (1.2 kWh), 5 for ECO, 7 for Auto and 30 for the pooled
# profile, which is the most hungry and never the stand-in.
INTENSIVE = programme("intensive", flat(100, 120), "Intensive")
ECO = programme("eco", flat(50, 120), "ECO")
AUTO = programme("auto", flat(70, 120), "Auto")
POOLED = programme("typical", flat(300, 120), "Typical")


class PublishedProfileTests(unittest.TestCase):
    def test_a_programme_without_a_label_is_known_by_its_key(self):
        found = drum.published_profiles(dish_profiles({"key": "eco", "p": "10,20"}, programme("auto", "30", "Auto")),
                                        "dishwasher")
        self.assertEqual(found, {"eco": ("eco", [1.0, 2.0]), "auto": ("Auto", [3.0])})

    def test_only_valid_profiles_of_the_appliance_are_published(self):
        attrs = dish_profiles(programme("a", ""), programme("b", "1,x"), programme("c", "-5"), {"p": "10"},
                              {"key": "d"}, "e", programme("f", "10"))
        self.assertEqual(list(drum.published_profiles(attrs, "dishwasher")), ["f"])
        self.assertEqual(drum.published_profiles(attrs, "washer"), {})
        self.assertEqual(drum.published_profiles({**attrs, "schema": 2}, "dishwasher"), {})
        self.assertEqual(drum.published_profiles(None, "dishwasher"), {})
        self.assertEqual(drum.published_profiles({"schema": 1, "appliances": {"dishwasher": []}}, "dishwasher"), {})


class TimerProfileTests(unittest.TestCase):
    def test_the_programmes_own_profile_prices_the_run_however_little_energy_it_uses(self):
        attrs = dish_profiles(POOLED, AUTO, programme("intensive", "10,20", "Intensive"))
        self.assertEqual(drum.timer_profile(attrs, "dishwasher", "intensive"), ("Intensive", [1.0, 2.0], True))

    def test_without_it_the_published_programme_using_the_most_energy_does(self):
        attrs = dish_profiles(POOLED, ECO, AUTO, programme("quick", flat(10, 60), "QuickPowerWash"))
        self.assertEqual(drum.timer_profile(attrs, "dishwasher", "intensive"), ("Auto", [7.0] * 120, False))

    def test_the_pooled_typical_profile_is_no_programme(self):
        self.assertIsNone(drum.timer_profile(dish_profiles(POOLED), "dishwasher", "intensive"))
        attrs = dish_profiles(POOLED, ECO)
        self.assertEqual(drum.timer_profile(attrs, "dishwasher", "intensive"), ("ECO", [5.0] * 120, False))

    def test_equal_energy_keeps_the_programme_listed_first(self):
        attrs = dish_profiles(programme("a", flat(50, 120), "A"), programme("b", flat(100, 60), "B"))
        self.assertEqual(drum.timer_profile(attrs, "dishwasher", "intensive")[0], "A")

    def test_nothing_published_is_no_profile(self):
        self.assertIsNone(drum.timer_profile(dish_profiles(), "dishwasher", "intensive"))
        self.assertIsNone(drum.timer_profile(profiles(eco="10"), "dishwasher", "intensive"))
        self.assertIsNone(drum.timer_profile(None, "dishwasher", "intensive"))


class TimerSettingTests(unittest.TestCase):
    def test_half_hours_up_to_three_hours_and_whole_hours_after(self):
        settings = {14: 0, 15: 30, 44: 30, 45: 60, 75: 90, 120: 120, 164: 150, 165: 180, 180: 180, 181: 180,
                    209: 180, 210: 240, 239: 240, 269: 240, 270: 300, 395: 420, 385: 360, 1410: 1440, 1440: 1440}
        self.assertEqual({delay: drum.timer_minutes(delay) for delay in settings}, settings)

    def test_the_setting_reads_as_the_machine_counts_it(self):
        labels = {30: "30 min", 60: "1 h", 90: "1,5 h", 120: "2 h", 150: "2,5 h", 180: "3 h", 240: "4 h", 420: "7 h",
                  1440: "24 h"}
        self.assertEqual({minutes: drum.timer_label(minutes) for minutes in labels}, labels)


class TimerWindowTests(unittest.TestCase):
    def prices(self, cheap_hours):
        return drum.hour_prices(price_attrs({}, tomorrow={hour: 0.5 for hour in cheap_hours}, valid=True))

    def test_a_start_must_be_the_least_delay_away(self):
        wh = [10.0] * 60
        prices = drum.hour_prices(price_attrs({10: 0.5, 11: 0.5}))
        now = local(TODAY, 9, 50)
        self.assertEqual(drum.cheapest_start(wh, prices, now)[0], local(TODAY, 10))
        self.assertEqual(drum.cheapest_start(wh, prices, now, min_delay=15)[0], local(TODAY, 10, 15))
        self.assertEqual(drum.cheapest_start(wh, prices, now, min_delay=10)[0], local(TODAY, 10))

    def test_a_start_may_be_the_most_delay_away(self):
        wh = [10.0] * 60
        now = local(TODAY, 10)
        tomorrow_at_ten = self.prices({10})
        self.assertEqual(drum.cheapest_start(wh, tomorrow_at_ten, now)[0], local(TOMORROW, 10))
        self.assertEqual(drum.cheapest_start(wh, tomorrow_at_ten, now, max_delay=1440)[0], local(TOMORROW, 10))
        later = self.prices({11})
        self.assertEqual(drum.cheapest_start(wh, later, now)[0], local(TOMORROW, 11))
        self.assertEqual(drum.cheapest_start(wh, later, now, max_delay=1440), (now, 0))
        self.assertEqual(drum.cheapest_start(wh, later, now, max_delay=0), (now, 0))

    def test_a_start_a_minute_past_the_most_delay_is_out(self):
        # The energy is in the last quarter hour of the run, so 10:15 is the first start that earns it.
        wh = [0.0] * 45 + [10.0] * 15
        prices = self.prices({11})
        now = local(TODAY, 10, 14)
        self.assertEqual(drum.cheapest_start(wh, prices, now, max_delay=1441)[0], local(TOMORROW, 10, 15))
        self.assertEqual(drum.cheapest_start(wh, prices, now, max_delay=1440), (now, 0))


class TimerStartTests(unittest.TestCase):
    def start(self, attrs, now, *programmes):
        return drum.timer_start(attrs, dish_profiles(*(programmes or (INTENSIVE,))), "intensive", now)

    def test_the_cheapest_quarter_hour_comes_with_the_timer_to_set_from_now(self):
        attrs = price_attrs({}, tomorrow={2: 0.5, 3: 0.5, 4: 0.5}, valid=True)
        self.assertEqual(self.start(attrs, local(TODAY, 21)),
                         drum.Start(local(TOMORROW, 2), "saves 1,80 kr vs now", False, "timer 5 h"))

    def test_the_timer_is_the_setting_nearest_to_the_start(self):
        attrs = price_attrs({}, tomorrow={2: 0.5, 3: 0.5, 4: 0.5}, valid=True)
        self.assertEqual(self.start(attrs, local(TODAY, 19, 25)).timer, "timer 7 h")
        self.assertEqual(self.start(attrs, local(TODAY, 19, 35)).timer, "timer 6 h")
        attrs = price_attrs({23: 0.5}, tomorrow={0: 0.5}, valid=True)
        self.assertEqual(self.start(attrs, local(TODAY, 21, 40)),
                         drum.Start(local(TODAY, 23), "saves 1,20 kr vs now", False, "timer 1,5 h"))

    def test_the_timer_is_counted_from_the_next_whole_minute(self):
        attrs = price_attrs({}, tomorrow={2: 0.5, 3: 0.5, 4: 0.5}, valid=True)
        self.assertEqual(self.start(attrs, local(TODAY, 19, 30) - timedelta(seconds=10)).timer, "timer 7 h")
        self.assertEqual(self.start(attrs, local(TODAY, 19, 30)).timer, "timer 7 h")
        self.assertEqual(self.start(attrs, local(TODAY, 19, 30) + timedelta(seconds=10)).timer, "timer 6 h")
        self.assertEqual(self.start(attrs, local(TODAY, 19, 59) + timedelta(seconds=50)).timer, "timer 6 h")

    def test_a_start_under_a_quarter_hour_away_is_starting_now(self):
        attrs = price_attrs({22: 0.5, 23: 0.5})
        self.assertEqual(self.start(attrs, local(TODAY, 21, 55)),
                         drum.Start(local(TODAY, 21, 55), "cheapest now", True))
        self.assertEqual(self.start(attrs, local(TODAY, 21, 40)),
                         drum.Start(local(TODAY, 22), "saves 0,30 kr vs now", False, "timer 30 min"))
        self.assertEqual(drum.drum_start(attrs, profiles(bomuld_60=flat(100, 120)), KEYS, local(TODAY, 21, 55))[0],
                         local(TODAY, 22))

    def test_no_start_is_more_than_a_day_away(self):
        attrs = price_attrs({}, tomorrow={2: 0.5, 3: 0.5, 20: 0.1, 21: 0.1}, valid=True)
        now = local(TODAY, 13, 30)
        self.assertEqual(self.start(attrs, now),
                         drum.Start(local(TOMORROW, 2), "saves 1,80 kr vs now", False, "timer 13 h"))
        self.assertEqual(drum.drum_start(attrs, profiles(bomuld_60=flat(100, 120)), KEYS, now)[0], local(TOMORROW, 20))

    def test_starting_now_is_cheapest_when_nothing_later_saves_an_ore(self):
        self.assertEqual(self.start(price_attrs({}), local(TODAY, 10)),
                         drum.Start(local(TODAY, 10), "cheapest now", True))

    def test_without_a_price_now_the_start_says_no_saving(self):
        attrs = price_attrs({10: 109.57, 11: 1.0, 12: 1.0})
        self.assertEqual(self.start(attrs, local(TODAY, 10)), drum.Start(local(TODAY, 11), "", False, "timer 1 h"))

    def test_no_priced_start_or_no_profile_is_no_start(self):
        self.assertIsNone(self.start(price_attrs({}), local(TODAY, 22, 1)))
        self.assertIsNone(self.start(None, local(TODAY, 10)))
        self.assertIsNone(self.start(price_attrs({}), local(TODAY, 10), POOLED))
        self.assertIsNone(self.start(price_attrs({}), local(TODAY, 10), programme("intensive", "")))
        self.assertIsNone(drum.timer_start(price_attrs({}), None, "intensive", local(TODAY, 10)))

    def test_the_programmes_own_profile_prices_the_start_and_a_stand_in_is_named(self):
        attrs = price_attrs({}, tomorrow={2: 0.5, 3: 0.5, 4: 0.5}, valid=True)
        now = local(TODAY, 21)
        self.assertEqual(self.start(attrs, now, ECO, INTENSIVE).basis, "")
        self.assertEqual(self.start(attrs, now, ECO, INTENSIVE).price, "saves 1,80 kr vs now")
        stand_in = self.start(attrs, now, POOLED, ECO, AUTO)
        self.assertEqual((stand_in.price, stand_in.basis),
                         ("saves 1,26 kr vs now", "Priced on Auto, Intensive has too few runs"))
        self.assertEqual(self.start(attrs, now, ECO).price, "saves 0,90 kr vs now")
        self.assertEqual(self.start(price_attrs({}), now, ECO),
                         drum.Start(now, "cheapest now", True, "", "Priced on ECO, Intensive has too few runs"))


class WaitForPricesTests(unittest.TestCase):
    """Before the day's prices are out at 13:00 a best start after that is not named."""

    WAIT = drum.Start(None, "best start after 13:00 prices", False)

    def start(self, attrs, now, *programmes):
        return drum.timer_start(attrs, dish_profiles(*(programmes or (INTENSIVE,))), "intensive", now)

    def test_a_best_start_after_the_prices_wait_for_them(self):
        attrs = price_attrs({13: 0.5, 14: 0.5})
        self.assertEqual(self.start(attrs, local(TODAY, 9)), self.WAIT)
        self.assertEqual(self.start(attrs, local(TODAY, 12, 40)), self.WAIT)

    def test_a_best_start_before_them_is_named(self):
        attrs = price_attrs({11: 0.5, 12: 0.5})
        self.assertEqual(self.start(attrs, local(TODAY, 9)),
                         drum.Start(local(TODAY, 11), "saves 1,80 kr vs now", False, "timer 2 h"))
        attrs = price_attrs({12: 0.5, 13: 0.5})
        self.assertEqual(self.start(attrs, local(TODAY, 9)),
                         drum.Start(local(TODAY, 12), "saves 1,80 kr vs now", False, "timer 3 h"))

    def test_starting_now_is_named_too(self):
        self.assertEqual(self.start(price_attrs({}), local(TODAY, 9)),
                         drum.Start(local(TODAY, 9), "cheapest now", True))

    def test_once_tomorrows_prices_are_out_there_is_nothing_to_wait_for(self):
        attrs = price_attrs({13: 0.5, 14: 0.5}, tomorrow={}, valid=True)
        self.assertEqual(self.start(attrs, local(TODAY, 9)),
                         drum.Start(local(TODAY, 13), "saves 1,80 kr vs now", False, "timer 4 h"))

    def test_from_13_the_start_is_named_though_the_prices_are_late(self):
        attrs = price_attrs({13: 0.5, 14: 0.5})
        self.assertEqual(self.start(attrs, local(TODAY, 13)), drum.Start(local(TODAY, 13), "cheapest now", True))
        late = price_attrs({21: 0.5, 22: 0.5})
        self.assertEqual(self.start(late, local(TODAY, 13)),
                         drum.Start(local(TODAY, 21), "saves 1,80 kr vs now", False, "timer 8 h"))
        self.assertEqual(self.start(late, local(TODAY, 14)),
                         drum.Start(local(TODAY, 21), "saves 1,80 kr vs now", False, "timer 7 h"))

    def test_the_stand_in_is_named_while_waiting(self):
        found = self.start(price_attrs({13: 0.5, 14: 0.5}), local(TODAY, 9), ECO)
        self.assertEqual(found, drum.Start(None, "best start after 13:00 prices", False, "",
                                           "Priced on ECO, Intensive has too few runs"))


if __name__ == "__main__":
    unittest.main()
