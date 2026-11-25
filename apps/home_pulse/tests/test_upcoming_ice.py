# tests/test_upcoming_ice.py - the ice bath's cold spell, the freezing degree-hours that make its ice thick, its row and
# its one push per freeze-up, on synthetic series, the real night of 25.11.2026 and the shipped settings.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import json
import sys
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

HOME_PULSE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOME_PULSE))
import upcoming_core as core  # noqa: E402
import upcoming_ice as ice  # noqa: E402

APP = yaml.safe_load((HOME_PULSE / "house_upcoming.yaml").read_text())["HouseUpcoming"]
SHIPPED = {key: value for key, value in APP.items() if key.startswith("ice_")}
CFG = ice.parse(SHIPPED)
NIGHT = json.loads((Path(__file__).resolve().parent / "fixtures" / "ice_bath_2026_11_25.json").read_text())
LOCAL = ZoneInfo("Europe/Copenhagen")
MIN = timedelta(minutes=1)
TICK = 5 * MIN
HOUR = timedelta(hours=1)


def at(day, hour, minute=0, month=11):
    """A house-time moment in 2026."""
    return datetime(2026, month, day, hour, minute, tzinfo=LOCAL)


def feed(readings, st=None):
    """The state after `readings` of the water, [(time, value)], in order."""
    for when, value in readings:
        st = ice.step(st, CFG, value, when)
    return st


def hold(st, air, start, end, every=TICK):
    """The state after the app has evaluated every `every` from `start` to `end` while the air read `air`."""
    when = start
    while when <= end:
        st = ice.accrue(st, air, when)
        when += every
    return st


def frozen(since=at(21, 3, 10), water=0.1, degree_hours=80.0):
    """A spell begun at `since` with the degree-hours it has reached."""
    return {**feed([(since, water)]), "fdh": degree_hours}


class ParseTests(unittest.TestCase):
    def test_the_shipped_settings_are_the_rule_as_asked_for(self):
        self.assertEqual((CFG["entity"], CFG["air"], CFG["person"], CFG["area"]),
                         ("sensor.ice_bath_temperature", "sensor.gw2000a_outdoor_temperature", "person.claudia",
                          "rooftop"))
        self.assertEqual((CFG["likely_at"], CFG["clear_above"], CFG["hold"]), (0.3, 0.5, 2 * HOUR))
        self.assertEqual(CFG["thick_at"], 80.0)
        self.assertEqual((CFG["push_from"], CFG["push_until"]), (time(7), time(22)))
        self.assertEqual(len(SHIPPED), 10)

    def test_a_missing_or_bad_value_fails_at_start(self):
        for key in SHIPPED:
            with self.assertRaises(ValueError, msg=key):
                ice.parse({k: v for k, v in SHIPPED.items() if k != key})
        for bad in ({"ice_likely_at_or_below_c": "cold"}, {"ice_likely_at_or_below_c": 0.6}, {"ice_likely_for_hours": 0},
                    {"ice_likely_for_hours": -1}, {"ice_push_from": "22:00:00"}, {"ice_push_from": 420},
                    {"ice_push_until": "later"}, {"ice_temperature": "binary_sensor.ice"},
                    {"ice_air": "weather.home"}, {"ice_thick_degree_hours": 0}, {"ice_thick_degree_hours": -80},
                    {"ice_thick_degree_hours": "much"}, {"ice_thick_degree_hours": float("nan")},
                    {"ice_person": "device_tracker.claudia"}, {"ice_area": ""}):
            with self.assertRaises(ValueError, msg=bad):
                ice.parse({**SHIPPED, **bad})
        with self.assertRaises(ValueError):
            ice.parse(None)

    def test_the_two_thresholds_may_be_equal(self):
        self.assertEqual(ice.parse({**SHIPPED, "ice_clear_above_c": 0.3})["clear_above"], 0.3)

    def test_the_degree_hours_are_a_setting(self):
        self.assertEqual(ice.parse({**SHIPPED, "ice_thick_degree_hours": 12.5})["thick_at"], 12.5)


class StepTests(unittest.TestCase):
    def test_a_reading_at_or_below_the_threshold_starts_a_spell_at_its_own_time_with_no_degree_hours(self):
        st = feed([(at(21, 3, 10), 0.3)])
        self.assertEqual(st, {"since": core.utc_iso(at(21, 3, 10)), "value": 0.3, "fdh": 0.0})

    def test_later_cold_readings_keep_the_start_and_follow_the_value(self):
        st = feed([(at(21, 3, 10), 0.3), (at(21, 3, 40), 0.1), (at(21, 5, 0), -0.1)])
        self.assertEqual(st, {"since": core.utc_iso(at(21, 3, 10)), "value": -0.1, "fdh": 0.0})

    def test_a_reading_above_the_threshold_starts_nothing(self):
        for value in (0.31, 0.4, 0.5, 2.1):
            self.assertEqual(feed([(at(21, 3, 10), value)]), {}, value)

    def test_a_reading_between_the_thresholds_ends_a_spell_that_has_not_lasted_two_hours(self):
        st = feed([(at(21, 3, 10), 0.2), (at(21, 5, 9), 0.4)])
        self.assertEqual(st, {})
        st = feed([(at(21, 3, 10), 0.2), (at(21, 5, 9), 0.4), (at(21, 5, 10), 0.2)])
        self.assertEqual(st["since"], core.utc_iso(at(21, 5, 10)))

    def test_a_reading_between_the_thresholds_leaves_a_spell_that_has_lasted_two_hours(self):
        st = feed([(at(21, 3, 10), 0.2), (at(21, 5, 10), 0.4)])
        self.assertEqual(st, {"since": core.utc_iso(at(21, 3, 10)), "value": 0.4, "fdh": 0.0})
        st = feed([(at(21, 5, 20), 0.5)], st)
        self.assertEqual(st, {"since": core.utc_iso(at(21, 3, 10)), "value": 0.5, "fdh": 0.0})

    def test_a_reading_above_the_clear_threshold_ends_any_spell(self):
        st = feed([(at(21, 3, 10), 0.2), (at(21, 9, 0), 0.51)])
        self.assertEqual(st, {})

    def test_no_reading_changes_nothing_however_long_it_lasts(self):
        st = feed([(at(21, 3, 10), 0.2)])
        self.assertEqual(ice.step(st, CFG, None, at(21, 23, 0)), st)
        self.assertEqual(ice.step({}, CFG, None, at(21, 23, 0)), {})
        self.assertEqual(ice.step(None, CFG, None, at(21, 23, 0)), {})

    def test_the_degree_hours_the_air_and_the_told_flag_survive_every_reading_of_a_spell(self):
        st = {**frozen(degree_hours=55.5), "told": True, "air": -2.0, "air_at": core.utc_iso(at(21, 9, 0))}
        for value in (0.2, 0.4, 0.5, None):
            after = ice.step(st, CFG, value, at(21, 9, 0))
            for key in ("fdh", "told", "air", "air_at"):
                self.assertEqual(after[key], st[key], (key, value))

    def test_a_spell_that_ends_takes_everything_with_it_and_the_next_one_starts_from_nothing(self):
        st = {**frozen(degree_hours=95.0), "told": True, "air": -2.0, "air_at": core.utc_iso(at(21, 9, 0))}
        self.assertEqual(ice.step(st, CFG, 0.6, at(21, 9, 0)), {})
        again = feed([(at(21, 9, 0), 0.6), (at(21, 9, 10), 0.2)], st)
        self.assertEqual(again, {"since": core.utc_iso(at(21, 9, 10)), "value": 0.2, "fdh": 0.0})

    def test_the_state_given_is_never_changed(self):
        st = {"since": core.utc_iso(at(21, 3, 10)), "value": 0.2, "fdh": 4.0}
        before = dict(st)
        ice.step(st, CFG, 3.0, at(21, 9, 0))
        self.assertEqual(st, before)


class AccrueTests(unittest.TestCase):
    START = at(21, 3, 10)

    def spell(self):
        return ice.accrue(feed([(self.START, 0.1)]), -2.0, self.START)

    def test_the_first_reading_of_a_spell_only_becomes_the_held_one(self):
        st = self.spell()
        self.assertEqual((st["fdh"], st["air"], st["air_at"]), (0.0, -2.0, core.utc_iso(self.START)))

    def test_every_hour_of_frost_adds_its_degrees(self):
        st = ice.accrue(self.spell(), -2.0, self.START + 2 * HOUR)
        self.assertEqual(st["fdh"], 4.0)
        st = ice.accrue(st, -2.0, self.START + 2 * HOUR + 30 * MIN)
        self.assertEqual(st["fdh"], 5.0)

    def test_thaw_takes_the_degrees_back_and_never_goes_below_zero(self):
        st = ice.accrue(self.spell(), -2.0, self.START + 3 * HOUR)
        st = ice.accrue(st, 1.0, self.START + 3 * HOUR)
        self.assertEqual(st["fdh"], 6.0)
        st = ice.accrue(st, 1.0, self.START + 5 * HOUR)
        self.assertEqual(st["fdh"], 4.0)
        st = ice.accrue(st, 1.0, self.START + 24 * HOUR)
        self.assertEqual(st["fdh"], 0.0)
        st = ice.accrue(st, 1.0, self.START + 30 * HOUR)
        self.assertEqual(st["fdh"], 0.0)

    def test_the_air_counts_as_its_last_reading_until_the_next_one(self):
        st = ice.accrue(self.spell(), -4.0, self.START + HOUR)
        self.assertEqual(st["fdh"], 2.0)
        st = ice.accrue(st, 0.0, self.START + 2 * HOUR)
        self.assertEqual(st["fdh"], 6.0)
        st = ice.accrue(st, 3.0, self.START + 4 * HOUR)
        self.assertEqual(st["fdh"], 6.0)
        st = ice.accrue(st, 3.0, self.START + 5 * HOUR)
        self.assertEqual(st["fdh"], 3.0)

    def test_unavailable_air_adds_nothing_and_neither_does_the_gap_after_it(self):
        st = ice.accrue(self.spell(), None, self.START + HOUR)
        self.assertEqual((st["fdh"], "air" in st, "air_at" in st), (2.0, False, False))
        st = ice.accrue(st, None, self.START + 9 * HOUR)
        self.assertEqual(st["fdh"], 2.0)
        st = ice.accrue(st, -2.0, self.START + 10 * HOUR)
        self.assertEqual(st["fdh"], 2.0)
        st = ice.accrue(st, -2.0, self.START + 11 * HOUR)
        self.assertEqual(st["fdh"], 4.0)

    def test_nothing_is_kept_without_a_spell(self):
        self.assertEqual(ice.accrue({}, -5.0, self.START), {})
        self.assertEqual(ice.accrue(None, -5.0, self.START), {})

    def test_the_time_before_the_first_air_reading_adds_nothing(self):
        st = feed([(self.START, 0.1)])
        st = ice.accrue(st, -2.0, self.START + 3 * HOUR)
        self.assertEqual(st["fdh"], 0.0)

    def test_fractions_of_a_second_add_nothing_up(self):
        late = timedelta(microseconds=900_000)
        st = ice.accrue(feed([(self.START, 0.1)]), -2.0, self.START + late)
        for i in range(1, 25):
            st = ice.accrue(st, -2.0, self.START + i * TICK + late)
        self.assertAlmostEqual(st["fdh"], 4.0, places=9)

    def test_five_minute_steps_reach_the_threshold_exactly_when_it_is_due(self):
        end = self.START + 40 * HOUR
        st = hold(feed([(self.START, 0.1)]), -2.0, self.START, end - TICK)
        self.assertFalse(ice.thick(st, CFG, end - TICK))
        st = ice.accrue(st, -2.0, end)
        self.assertTrue(ice.thick(st, CFG, end))

    def test_told_goes_when_the_degree_hours_are_back_at_zero_and_not_before(self):
        st = ice.pushed(ice.accrue(self.spell(), -2.0, self.START + 40 * HOUR))
        st = ice.accrue(st, 2.0, self.START + 40 * HOUR)
        st = ice.accrue(st, 2.0, self.START + 79 * HOUR)
        self.assertEqual((st["fdh"], st["told"]), (2.0, True))
        st = ice.accrue(st, 2.0, self.START + 80 * HOUR)
        self.assertEqual(st["fdh"], 0.0)
        self.assertNotIn("told", st)

    def test_the_state_given_is_never_changed(self):
        st = self.spell()
        before = dict(st)
        ice.accrue(st, -3.0, self.START + HOUR)
        self.assertEqual(st, before)


class LikelyTests(unittest.TestCase):
    def test_ice_is_likely_once_the_spell_has_lasted_two_hours_by_the_clock(self):
        st = feed([(at(21, 3, 10), 0.2)])
        self.assertFalse(ice.likely(st, CFG, at(21, 3, 10)))
        self.assertFalse(ice.likely(st, CFG, at(21, 5, 9)))
        self.assertTrue(ice.likely(st, CFG, at(21, 5, 10)))
        self.assertTrue(ice.likely(st, CFG, at(21, 20, 0)))

    def test_no_spell_is_not_likely(self):
        self.assertFalse(ice.likely({}, CFG, at(21, 12)))
        self.assertFalse(ice.likely(None, CFG, at(21, 12)))

    def test_unavailable_readings_do_not_restart_the_clock(self):
        st = feed([(at(21, 3, 0), 0.1), (at(21, 3, 20), None), (at(21, 6, 0), None), (at(21, 6, 30), 0.2)])
        self.assertEqual(st["since"], core.utc_iso(at(21, 3, 0)))
        self.assertTrue(ice.likely(st, CFG, at(21, 6, 30)))

    def test_the_hours_are_a_setting(self):
        short = ice.parse({**SHIPPED, "ice_likely_for_hours": 0.5})
        st = ice.step({}, short, 0.1, at(21, 3, 0))
        self.assertFalse(ice.likely(st, short, at(21, 3, 29)))
        self.assertTrue(ice.likely(st, short, at(21, 3, 30)))


class ThickTests(unittest.TestCase):
    NOW = at(21, 20, 0)

    def test_ice_is_thick_from_eighty_degree_hours(self):
        self.assertFalse(ice.thick(frozen(degree_hours=79.9), CFG, self.NOW))
        self.assertTrue(ice.thick(frozen(degree_hours=80.0), CFG, self.NOW))
        self.assertTrue(ice.thick(frozen(degree_hours=240.0), CFG, self.NOW))

    def test_thick_ice_is_thin_ice_as_well(self):
        st = frozen()
        self.assertTrue(ice.thick(st, CFG, self.NOW) and ice.likely(st, CFG, self.NOW))

    def test_a_spell_younger_than_two_hours_is_neither(self):
        st = frozen(since=at(21, 19, 0), degree_hours=500.0)
        self.assertFalse(ice.thick(st, CFG, at(21, 20, 59)))
        self.assertTrue(ice.thick(st, CFG, at(21, 21, 0)))

    def test_no_spell_is_not_thick(self):
        self.assertFalse(ice.thick({}, CFG, self.NOW))
        self.assertFalse(ice.thick(None, CFG, self.NOW))

    def test_a_state_without_degree_hours_is_thin(self):
        st = feed([(at(21, 3, 10), 0.1)])
        st.pop("fdh")
        self.assertFalse(ice.thick(st, CFG, self.NOW))

    def test_the_degree_hours_are_a_setting(self):
        low = ice.parse({**SHIPPED, "ice_thick_degree_hours": 10})
        self.assertFalse(ice.thick(frozen(degree_hours=9.9), low, self.NOW))
        self.assertTrue(ice.thick(frozen(degree_hours=10.0), low, self.NOW))


class RowTests(unittest.TestCase):
    def test_no_row_before_two_hours(self):
        st = feed([(at(21, 3, 10), 0.1)])
        self.assertEqual(ice.rows(st, CFG, at(21, 5, 9)), [])
        self.assertEqual(ice.rows({}, CFG, at(21, 5, 9)), [])

    def test_the_row_is_the_ice_baths_own_untimed_and_for_everyone(self):
        st = feed([(at(21, 3, 10), 0.1)])
        [row] = ice.rows(st, CFG, at(21, 7, 0))
        self.assertEqual((row["id"], row["kind"], row["area"], row["icon"]),
                         ("ice:bath", "rain", "rooftop", "mdi:snowflake"))
        self.assertEqual(row["title"], "Ice bath: thin ice at 0.1 C since 03:10")
        self.assertEqual(row["level"], "thin")
        for field in ("at", "detail", "audience", "audience_users", "chip", "next", "done"):
            self.assertNotIn(field, row)
        self.assertTrue(core.is_valid_row(row))
        self.assertEqual(core.finalize_rows([row]), [row])

    def test_the_row_of_thick_ice_says_so_in_its_text_and_its_level(self):
        [row] = ice.rows(frozen(), CFG, at(21, 20, 0))
        self.assertEqual(row["title"], "Ice bath: thick ice at 0.1 C since 03:10")
        self.assertEqual(row["level"], "thick")
        self.assertTrue(core.is_valid_row(row))
        self.assertEqual(core.finalize_rows([row]), [row])

    def test_the_level_follows_the_degree_hours_both_ways(self):
        thin, thick = frozen(degree_hours=79.0), frozen(degree_hours=81.0)
        self.assertEqual(ice.rows(thin, CFG, at(21, 20, 0))[0]["level"], "thin")
        self.assertEqual(ice.rows(thick, CFG, at(21, 20, 0))[0]["level"], "thick")
        thawing = hold(thick, 2.0, at(21, 20, 0), at(21, 21, 0))
        self.assertEqual(ice.rows(thawing, CFG, at(21, 21, 0))[0]["level"], "thin")

    def test_the_text_follows_the_last_reading_and_keeps_the_start(self):
        st = feed([(at(21, 3, 10), 0.1), (at(21, 5, 30), 0.2), (at(21, 6, 0), 0.4)])
        self.assertEqual(ice.rows(st, CFG, at(21, 6, 0))[0]["title"], "Ice bath: thin ice at 0.4 C since 03:10")

    def test_a_spell_from_an_earlier_day_names_its_day(self):
        st = feed([(at(19, 22, 40), 0.0)])
        self.assertEqual(ice.rows(st, CFG, at(21, 7, 0))[0]["title"], "Ice bath: thin ice at 0.0 C since 19.11 22:40")
        st["fdh"] = 90.0
        self.assertEqual(ice.rows(st, CFG, at(21, 7, 0))[0]["title"], "Ice bath: thick ice at 0.0 C since 19.11 22:40")

    def test_the_start_is_house_time_not_utc(self):
        st = feed([(datetime(2026, 11, 21, 2, 10, tzinfo=timezone.utc), 0.1)])
        self.assertEqual(ice.rows(st, CFG, at(21, 7, 0))[0]["title"], "Ice bath: thin ice at 0.1 C since 03:10")

    def test_below_zero_is_shown_as_it_is_and_the_longest_text_fits(self):
        st = feed([(at(21, 3, 10), -0.4)])
        self.assertEqual(ice.rows(st, CFG, at(21, 7, 0))[0]["title"], "Ice bath: thin ice at -0.4 C since 03:10")
        st = {**feed([(at(19, 22, 40), -10.4)], None), "fdh": 90.0}
        [row] = ice.rows(st, CFG, at(21, 7, 0))
        self.assertEqual(row["title"], "Ice bath: thick ice at -10.4 C since 19.11 22:40")
        self.assertTrue(core.is_valid_row(row))

    def test_the_row_goes_when_the_water_is_above_the_clear_threshold_and_not_before(self):
        st = feed([(at(21, 3, 10), 0.1), (at(21, 7, 0), 0.5)])
        self.assertEqual(len(ice.rows(st, CFG, at(21, 7, 0))), 1)
        st = feed([(at(21, 7, 1), 0.6)], st)
        self.assertEqual(ice.rows(st, CFG, at(21, 7, 1)), [])

    def test_the_area_is_a_setting(self):
        other = ice.parse({**SHIPPED, "ice_area": "living_room"})
        st = ice.step({}, other, 0.1, at(21, 3, 0))
        self.assertEqual(ice.rows(st, other, at(21, 6, 0))[0]["area"], "living_room")


class PushTests(unittest.TestCase):
    def setUp(self):
        self.st = frozen()

    def due(self, now, home=True, st=None):
        return ice.push_due(self.st if st is None else st, CFG, home, now)

    def test_nothing_goes_before_seven_or_from_ten_in_the_evening(self):
        self.assertFalse(self.due(at(21, 6, 59)))
        self.assertTrue(self.due(at(21, 7, 0)))
        self.assertTrue(self.due(at(21, 21, 59)))
        self.assertFalse(self.due(at(21, 22, 0)))
        self.assertFalse(self.due(at(21, 23, 30)))
        self.assertFalse(self.due(at(22, 3, 0)))

    def test_the_hours_are_house_time_in_winter_and_summer(self):
        for month, day, offset in ((11, 21, 1), (7, 15, 2)):
            st = frozen(since=at(day, 1, 0, month))
            for hour, minute, expected in ((6, 59, False), (7, 0, True), (21, 59, True), (22, 0, False)):
                utc = datetime(2026, month, day, hour, minute, tzinfo=timezone.utc) - timedelta(hours=offset)
                self.assertEqual(ice.push_due(st, CFG, True, utc), expected, (month, hour, minute))

    def test_thin_ice_is_never_pushed(self):
        thin = frozen(degree_hours=79.9)
        for now in (at(21, 7, 0), at(21, 12, 0), at(22, 7, 0)):
            self.assertFalse(self.due(now, st=thin), now)

    def test_only_while_the_ice_is_thick(self):
        self.assertFalse(self.due(at(21, 5, 9)))
        self.assertFalse(self.due(at(21, 12), st={}))
        self.assertFalse(self.due(at(21, 12), st=feed([(at(21, 3, 10), 0.1), (at(21, 9, 0), 0.7)])))
        self.assertFalse(self.due(at(21, 12), st=frozen(degree_hours=10.0)))

    def test_only_while_the_person_is_home(self):
        self.assertFalse(self.due(at(21, 12), home=False))

    def test_once_a_freeze_up_not_once_a_day(self):
        sent = ice.pushed(self.st)
        self.assertTrue(sent["told"])
        for now in (at(21, 8, 0), at(21, 21, 59), at(22, 7, 0), at(24, 7, 0)):
            self.assertFalse(self.due(now, st=sent), now)

    def test_it_can_go_again_once_the_degree_hours_have_been_back_at_zero(self):
        sent = ice.pushed(frozen(since=at(21, 3, 10), degree_hours=80.0))
        sent = ice.accrue(sent, 2.0, at(21, 20, 0))
        sent = ice.accrue(sent, 2.0, at(23, 12, 0))
        self.assertEqual(sent["fdh"], 0.0)
        refrozen = ice.accrue(ice.accrue(sent, -2.0, at(23, 12, 0)), -2.0, at(25, 4, 0))
        self.assertTrue(ice.thick(refrozen, CFG, at(25, 8, 0)))
        self.assertTrue(self.due(at(25, 8, 0), st=refrozen))

    def test_it_can_go_again_once_the_spell_has_ended(self):
        sent = ice.pushed(self.st)
        warm = ice.step(sent, CFG, 2.0, at(22, 12, 0))
        self.assertEqual(warm, {})
        again = feed([(at(23, 12, 0), 0.1)], warm)
        self.assertNotIn("told", again)
        self.assertTrue(self.due(at(25, 8, 0), st={**again, "fdh": 80.0}))

    def test_the_push_names_thick_ice_states_the_reading_and_the_start_and_names_the_category(self):
        self.assertEqual(ice.push(self.st, at(21, 7, 0)),
                         {"title": "Ice bath: thick ice", "message": "At 0.1 °C since 03:10",
                          "category": "ice_bath"})
        earlier = frozen(since=at(19, 22, 40), water=0.0)
        self.assertEqual(ice.push(earlier, at(21, 7, 0))["message"], "At 0.0 °C since 19.11 22:40")

    def test_pushing_does_not_end_the_spell_or_change_the_row(self):
        sent = ice.pushed(self.st)
        self.assertTrue(ice.likely(sent, CFG, at(21, 7, 0)))
        self.assertEqual(ice.rows(sent, CFG, at(21, 7, 0)), ice.rows(self.st, CFG, at(21, 7, 0)))

    def test_the_state_given_is_never_changed(self):
        before = dict(self.st)
        ice.pushed(self.st)
        self.assertEqual(self.st, before)


class RestoreTests(unittest.TestCase):
    def test_a_saved_state_comes_back_as_it_was_without_the_held_air(self):
        saved = {**ice.pushed(frozen(degree_hours=55.5)), "air": -2.0, "air_at": core.utc_iso(at(21, 9, 0))}
        self.assertEqual(ice.restore(saved), {"since": core.utc_iso(at(21, 3, 10)), "value": 0.1, "fdh": 55.5,
                                              "told": True})

    def test_the_degree_hours_survive_a_restart_and_the_time_away_adds_none(self):
        saved = hold(feed([(at(21, 3, 10), 0.1)]), -2.0, at(21, 3, 10), at(21, 9, 10))
        restored = ice.restore(json.loads(json.dumps(saved)))
        self.assertEqual(restored["fdh"], saved["fdh"])
        self.assertAlmostEqual(restored["fdh"], 12.0)
        later = ice.accrue(restored, -2.0, at(21, 15, 10))
        self.assertEqual(later["fdh"], restored["fdh"])
        self.assertAlmostEqual(ice.accrue(later, -2.0, at(21, 16, 10))["fdh"], 14.0)

    def test_nothing_invalid_comes_back(self):
        for raw in (None, "cold", [], 3, {}, {"since": "yesterday", "value": 0.1}, {"since": "2026-11-21T03:10:00+01:00"},
                    {"value": 0.1}, {"since": "2026-11-21T03:10:00+01:00", "value": "cold"}, {"fdh": 90.0},
                    {"told": True}, {"pushed_on": "2026-11-21"}):
            self.assertEqual(ice.restore(raw), {}, raw)

    def test_a_valid_part_survives_an_invalid_one(self):
        raw = {"since": "2026-11-21T03:10:00+01:00", "value": 0.1, "fdh": "much", "told": "yes", "extra": 1}
        self.assertEqual(ice.restore(raw), {"since": "2026-11-21T02:10:00+00:00", "value": 0.1, "fdh": 0.0})
        for fdh in (-5.0, True, None, float("nan")):
            self.assertEqual(ice.restore({**raw, "fdh": fdh})["fdh"], 0.0, fdh)

    def test_a_state_saved_with_a_push_day_comes_back_as_a_spell_without_degree_hours(self):
        raw = {"since": "2026-11-25T03:49:55+00:00", "value": 0.1, "pushed_on": "2026-11-25"}
        self.assertEqual(ice.restore(raw), {"since": "2026-11-25T03:49:55+00:00", "value": 0.1, "fdh": 0.0})


def series(anchors, start, end):
    """What a thermometer sends: [(time, value)] at each minute the value, linear between the anchors
    [(hours after `start`, temperature)] and rounded to a tenth, differs from the one before. Like HA, an
    unchanged value is no event."""
    out, last, t = [], None, start
    while t <= end:
        hours = (t - start) / HOUR
        for (h0, v0), (h1, v1) in zip(anchors, anchors[1:]):
            if h0 <= hours <= h1:
                value = round(v0 + (v1 - v0) * (hours - h0) / (h1 - h0), 1)
                break
        else:
            value = round(anchors[-1][1], 1)
        if value != last:
            out.append((t, value))
            last = value
        t += MIN
    return out


def replay(water, air, home, start, end):
    """What the rule makes over [start, end] when the app evaluates every minute: the pushes (time, message), the
    minutes a row was up (time, level) and the last state. `water` and `air` are [(time, value)] as the thermometers
    send them, `home(time)` says whether the person is home."""
    events = sorted([(when, "water", value) for when, value in water] + [(when, "air", value) for when, value in air],
                    key=lambda event: event[0])
    st, held, pushes, shown = {}, None, [], []
    t = start
    while t <= end:
        while events and events[0][0] <= t:
            when, kind, value = events.pop(0)
            if kind == "water":
                st = ice.step(st, CFG, value, when)
            else:
                st = ice.accrue(st, value, when)
                held = value
        st = ice.accrue(st, held, t)
        if ice.push_due(st, CFG, home(t), t):
            pushes.append((t, ice.push(st, t)["message"]))
            st = ice.pushed(st)
        shown += [(t, row["level"]) for row in ice.rows(st, CFG, t)]
        t += MIN
    return pushes, shown, st


def always(t):
    return True


def since_text(when):
    return f"{when.astimezone(LOCAL):%d.%m %H:%M}"


class ReplayTests(unittest.TestCase):
    START, END = at(20, 12), at(24, 12)

    def freeze(self, air_c, hours_cold=80.0):
        """Water from 3 C down to 0.0 C in ten hours and flat there for `hours_cold`, under air at `air_c`."""
        water = series([(0, 3.0), (10, 0.0), (10 + hours_cold, 0.0), (12 + hours_cold, 3.0)], self.START, self.END)
        return water, [(self.START, air_c)], next(when for when, value in water if value <= 0.3)

    def test_a_hard_freeze_gives_thin_ice_first_and_one_push_the_minute_it_turns_thick(self):
        water, air, cold = self.freeze(-6.0)
        pushes, shown, _ = replay(water, air, always, self.START, self.END)
        thick = cold + timedelta(hours=80 / 6)
        self.assertEqual(pushes, [(thick, f"At 0.0 °C since {since_text(cold)}")])
        self.assertEqual(shown[0], (cold + 2 * HOUR, "thin"))
        self.assertEqual(next(when for when, level in shown if level == "thick"), thick)
        self.assertTrue(all(level == "thin" for when, level in shown if when < thick))

    def test_it_is_one_push_for_the_freeze_up_not_one_each_morning(self):
        water, air, cold = self.freeze(-6.0)
        pushes, shown, _ = replay(water, air, always, self.START, self.END)
        self.assertEqual(len(pushes), 1)
        self.assertGreater(sum(level == "thick" for _, level in shown), 60 * 60)

    def test_ice_that_turns_thick_at_night_waits_for_seven(self):
        water, air, cold = self.freeze(-3.0)
        thick = cold + timedelta(hours=80 / 3)
        self.assertGreaterEqual(thick, at(21, 22, 0))
        pushes, _, _ = replay(water, air, always, self.START, self.END)
        self.assertEqual([when for when, _ in pushes], [at(22, 7, 0)])

    def test_coming_home_later_pushes_then(self):
        water, air, cold = self.freeze(-6.0)
        pushes, _, _ = replay(water, air, lambda t: t >= at(21, 14, 5), self.START, self.END)
        self.assertEqual([when for when, _ in pushes], [at(21, 14, 5)])

    def test_coming_home_at_night_pushes_at_seven(self):
        water, air, cold = self.freeze(-6.0)
        pushes, _, _ = replay(water, air, lambda t: t >= at(21, 23, 0), self.START, self.END)
        self.assertEqual([when for when, _ in pushes], [at(22, 7, 0)])

    def test_ice_that_turns_thick_in_the_afternoon_pushes_at_once(self):
        start, end = at(21, 8), at(22, 12)
        water = [(start, 4.0), (at(21, 8, 10), 0.2)]
        pushes, _, _ = replay(water, [(start, -8.0)], always, start, end)
        self.assertEqual([when for when, _ in pushes], [at(21, 18, 10)])

    def test_a_cold_that_never_gets_the_water_down_gives_nothing(self):
        start, end = at(21, 8), at(23, 12)
        water = series([(0, 3.0), (12, 0.4), (60, 0.4)], start, end)
        self.assertEqual(replay(water, [(start, -10.0)], always, start, end)[:2], ([], []))

    def test_thin_ice_under_mild_air_never_pushes(self):
        start, end = at(21, 8), at(25, 12)
        pushes, shown, st = replay([(start, 0.1)], [(start, 1.0)], always, start, end)
        self.assertEqual(pushes, [])
        self.assertEqual({level for _, level in shown}, {"thin"})
        self.assertEqual(st["fdh"], 0.0)

    def test_a_thaw_takes_thick_ice_back_to_thin_and_it_is_not_pushed_again_when_the_cold_returns(self):
        start, end = at(21, 3, 0), at(21, 3, 0) + 70 * HOUR
        thaw, again = start + 40 * HOUR, start + 50 * HOUR
        air = [(start, -2.0), (thaw, 2.0), (again, -2.0)]
        pushes, shown, st = replay([(start, 0.1)], air, always, start, end)
        self.assertEqual(pushes, [(thaw, "At 0.1 °C since 21.11 03:00")])
        levels = dict(shown)
        self.assertEqual(levels[thaw - MIN], "thin")
        self.assertEqual(levels[thaw], "thick")
        self.assertEqual(levels[thaw + MIN], "thin")
        self.assertEqual(levels[again], "thin")
        self.assertEqual(levels[again + 10 * HOUR], "thick")
        self.assertTrue(st["told"])

    def test_degree_hours_back_at_zero_make_the_next_thick_ice_a_new_push(self):
        start = at(21, 3, 0)
        air = [(start, -2.0), (start + 40 * HOUR, 2.0), (start + 80 * HOUR, -2.0)]
        pushes, _, st = replay([(start, 0.1)], air, always, start, start + 125 * HOUR)
        self.assertEqual([when for when, _ in pushes], [start + 40 * HOUR, at(26, 7, 0)])
        self.assertTrue(st["told"])

    def test_a_spell_that_ends_makes_the_next_thick_ice_a_new_push(self):
        start = at(21, 3, 0)
        water = [(start, 0.1), (start + 45 * HOUR, 3.0), (start + 50 * HOUR, 0.1)]
        pushes, shown, st = replay(water, [(start, -2.0)], always, start, start + 95 * HOUR)
        self.assertEqual([when for when, _ in pushes], [start + 40 * HOUR, start + 90 * HOUR])
        self.assertIn((start + 45 * HOUR - MIN, "thick"), shown)
        self.assertNotIn(start + 46 * HOUR, [when for when, _ in shown])

    def test_an_air_sensor_that_goes_quiet_adds_nothing(self):
        start = at(21, 3, 0)
        air = [(start, -2.0), (start + 10 * HOUR, None), (start + 40 * HOUR, -2.0)]
        pushes, shown, _ = replay([(start, 0.1)], air, always, start, start + 80 * HOUR)
        self.assertEqual([when for when, _ in pushes], [at(24, 7, 0)])
        self.assertEqual(next(when for when, level in shown if level == "thick"), start + 70 * HOUR)

    def test_a_water_sensor_that_goes_quiet_does_not_restart_the_clock(self):
        start, end = at(21, 8), at(22, 12)
        water = [(start, 4.0), (at(21, 9, 0), 0.2), (at(21, 9, 30), None), (at(21, 14, 0), 0.1)]
        pushes, shown, _ = replay(water, [(start, -10.0)], always, start, end)
        self.assertEqual(shown[0], (at(21, 11, 0), "thin"))
        self.assertEqual([when for when, _ in pushes], [at(21, 17, 0)])


class RealNightTests(unittest.TestCase):
    """The night of 24-25.11.2026, as the two thermometers sent it: the water flickered between 0.3 and 0.4 for hours and
    the air stayed between -0.1 and +2.1 C."""

    @staticmethod
    def readings():
        start = datetime.fromisoformat(NIGHT["start"])
        return ([(start + timedelta(seconds=s), v) for s, v in NIGHT["water"]],
                [(start + timedelta(seconds=s), v) for s, v in NIGHT["air"]])

    def test_the_night_gives_thin_ice_at_ten_to_seven_and_no_push_and_no_degree_hours(self):
        water, air = self.readings()
        self.assertEqual((len(water), len(air)), (240, 324))
        pushes, shown, st = replay(water, air, always, at(24, 22, 0), at(25, 9, 0))
        self.assertEqual(pushes, [])
        self.assertEqual(shown[0], (at(25, 6, 50), "thin"))
        self.assertEqual({level for _, level in shown}, {"thin"})
        self.assertEqual(shown[-1][0], at(25, 9, 0))
        self.assertEqual(st["fdh"], 0.0)
        self.assertEqual(st["since"][:16], "2026-11-25T03:49")

    def test_the_flicker_before_four_forty_nine_never_made_a_spell_of_two_hours(self):
        water, air = self.readings()
        _, shown, _ = replay([w for w in water if w[0] < at(25, 4, 49)], air, always, at(24, 22, 0), at(25, 4, 48))
        self.assertEqual(shown, [])


if __name__ == "__main__":
    unittest.main()
