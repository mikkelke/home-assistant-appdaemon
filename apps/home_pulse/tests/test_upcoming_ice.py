# tests/test_upcoming_ice.py - the ice bath's cold spell, its row and its one push a day, on synthetic series
# and the shipped thresholds.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

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
LOCAL = ZoneInfo("Europe/Copenhagen")
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)


def at(day, hour, minute=0, month=11):
    """A house-time moment in 2026."""
    return datetime(2026, month, day, hour, minute, tzinfo=LOCAL)


def feed(readings, st=None):
    """The state after `readings`, [(time, value)], in order."""
    for when, value in readings:
        st = ice.step(st, CFG, value, when)
    return st


class ParseTests(unittest.TestCase):
    def test_the_shipped_thresholds_are_the_rule_as_asked_for(self):
        self.assertEqual((CFG["entity"], CFG["person"], CFG["area"]),
                         ("sensor.ice_bath_temperature", "person.claudia", "rooftop"))
        self.assertEqual((CFG["likely_at"], CFG["clear_above"], CFG["hold"]), (0.3, 0.5, 2 * HOUR))
        self.assertEqual((CFG["push_from"], CFG["push_until"]), (time(7), time(22)))
        self.assertEqual(len(SHIPPED), 8)

    def test_a_missing_or_bad_value_fails_at_start(self):
        for key in SHIPPED:
            with self.assertRaises(ValueError, msg=key):
                ice.parse({k: v for k, v in SHIPPED.items() if k != key})
        for bad in ({"ice_likely_at_or_below_c": "cold"}, {"ice_likely_at_or_below_c": 0.6}, {"ice_likely_for_hours": 0},
                    {"ice_likely_for_hours": -1}, {"ice_push_from": "22:00:00"}, {"ice_push_from": 420},
                    {"ice_push_until": "later"}, {"ice_temperature": "binary_sensor.ice"},
                    {"ice_person": "device_tracker.claudia"}, {"ice_area": ""}):
            with self.assertRaises(ValueError, msg=bad):
                ice.parse({**SHIPPED, **bad})
        with self.assertRaises(ValueError):
            ice.parse(None)

    def test_the_two_thresholds_may_be_equal(self):
        self.assertEqual(ice.parse({**SHIPPED, "ice_clear_above_c": 0.3})["clear_above"], 0.3)


class StepTests(unittest.TestCase):
    def test_a_reading_at_or_below_the_threshold_starts_a_spell_at_its_own_time(self):
        st = feed([(at(21, 3, 10), 0.3)])
        self.assertEqual(st, {"since": core.utc_iso(at(21, 3, 10)), "value": 0.3})

    def test_later_cold_readings_keep_the_start_and_follow_the_value(self):
        st = feed([(at(21, 3, 10), 0.3), (at(21, 3, 40), 0.1), (at(21, 5, 0), -0.1)])
        self.assertEqual(st, {"since": core.utc_iso(at(21, 3, 10)), "value": -0.1})

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
        self.assertEqual(st, {"since": core.utc_iso(at(21, 3, 10)), "value": 0.4})
        st = feed([(at(21, 5, 20), 0.5)], st)
        self.assertEqual(st, {"since": core.utc_iso(at(21, 3, 10)), "value": 0.5})

    def test_a_reading_above_the_clear_threshold_ends_any_spell(self):
        st = feed([(at(21, 3, 10), 0.2), (at(21, 9, 0), 0.51)])
        self.assertEqual(st, {})

    def test_no_reading_changes_nothing_however_long_it_lasts(self):
        st = feed([(at(21, 3, 10), 0.2)])
        self.assertEqual(ice.step(st, CFG, None, at(21, 23, 0)), st)
        self.assertEqual(ice.step({}, CFG, None, at(21, 23, 0)), {})
        self.assertEqual(ice.step(None, CFG, None, at(21, 23, 0)), {})

    def test_the_day_of_the_last_push_survives_every_step(self):
        st = {"pushed_on": "2026-11-20"}
        for value in (0.2, 0.4, 3.0, None):
            st = ice.step(st, CFG, value, at(21, 3, 10))
            self.assertEqual(st["pushed_on"], "2026-11-20", value)

    def test_the_state_given_is_never_changed(self):
        st = {"since": core.utc_iso(at(21, 3, 10)), "value": 0.2}
        before = dict(st)
        ice.step(st, CFG, 3.0, at(21, 9, 0))
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
        self.assertEqual(row["title"], "Ice bath at 0.1 C since 03:10")
        for field in ("at", "detail", "audience", "audience_users", "chip", "next", "done"):
            self.assertNotIn(field, row)
        self.assertTrue(core.is_valid_row(row))
        self.assertEqual(core.finalize_rows([row]), [row])

    def test_the_text_follows_the_last_reading_and_keeps_the_start(self):
        st = feed([(at(21, 3, 10), 0.1), (at(21, 5, 30), 0.2), (at(21, 6, 0), 0.4)])
        self.assertEqual(ice.rows(st, CFG, at(21, 6, 0))[0]["title"], "Ice bath at 0.4 C since 03:10")

    def test_a_spell_from_an_earlier_day_names_its_day(self):
        st = feed([(at(19, 22, 40), 0.0)])
        self.assertEqual(ice.rows(st, CFG, at(21, 7, 0))[0]["title"], "Ice bath at 0.0 C since 19.11 22:40")

    def test_the_start_is_house_time_not_utc(self):
        st = feed([(datetime(2026, 11, 21, 2, 10, tzinfo=timezone.utc), 0.1)])
        self.assertEqual(ice.rows(st, CFG, at(21, 7, 0))[0]["title"], "Ice bath at 0.1 C since 03:10")

    def test_below_zero_is_shown_as_it_is(self):
        st = feed([(at(21, 3, 10), -0.4)])
        self.assertEqual(ice.rows(st, CFG, at(21, 7, 0))[0]["title"], "Ice bath at -0.4 C since 03:10")

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
        self.st = feed([(at(21, 3, 10), 0.1)])

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
            st = feed([(at(day, 1, 0, month), 0.1)])
            for hour, minute, expected in ((6, 59, False), (7, 0, True), (21, 59, True), (22, 0, False)):
                utc = datetime(2026, month, day, hour, minute, tzinfo=timezone.utc) - timedelta(hours=offset)
                self.assertEqual(ice.push_due(st, CFG, True, utc), expected, (month, hour, minute))

    def test_only_while_ice_is_likely(self):
        self.assertFalse(self.due(at(21, 5, 9)))
        self.assertFalse(self.due(at(21, 12), st={}))
        self.assertFalse(self.due(at(21, 12), st=feed([(at(21, 3, 10), 0.1), (at(21, 9, 0), 0.7)])))

    def test_only_while_the_person_is_home(self):
        self.assertFalse(self.due(at(21, 12), home=False))

    def test_once_a_house_day(self):
        sent = ice.pushed(self.st, at(21, 7, 0))
        self.assertEqual(sent["pushed_on"], "2026-11-21")
        self.assertFalse(self.due(at(21, 8, 0), st=sent))
        self.assertFalse(self.due(at(21, 21, 59), st=sent))
        self.assertTrue(self.due(at(22, 7, 0), st=sent))

    def test_the_house_day_is_not_the_utc_day(self):
        sent = ice.pushed(self.st, datetime(2026, 11, 21, 21, 30, tzinfo=timezone.utc))
        self.assertEqual(sent["pushed_on"], "2026-11-21")
        late = datetime(2026, 11, 21, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(ice.pushed(self.st, late)["pushed_on"], "2026-11-22")

    def test_the_push_states_the_reading_and_the_start_and_names_the_category(self):
        self.assertEqual(ice.push(self.st, at(21, 7, 0)),
                         {"title": "Ice bath", "message": "At 0.1 °C since 03:10", "category": "ice_bath"})
        earlier = feed([(at(19, 22, 40), 0.0)])
        self.assertEqual(ice.push(earlier, at(21, 7, 0))["message"], "At 0.0 °C since 19.11 22:40")

    def test_pushing_does_not_end_the_spell(self):
        sent = ice.pushed(self.st, at(21, 7, 0))
        self.assertTrue(ice.likely(sent, CFG, at(21, 7, 0)))
        self.assertEqual(len(ice.rows(sent, CFG, at(21, 7, 0))), 1)


class RestoreTests(unittest.TestCase):
    def test_a_saved_state_comes_back_as_it_was(self):
        st = ice.pushed(feed([(at(21, 3, 10), 0.1)]), at(21, 7, 0))
        self.assertEqual(ice.restore(st), st)

    def test_nothing_invalid_comes_back(self):
        for raw in (None, "cold", [], 3, {}, {"since": "yesterday", "value": 0.1}, {"since": "2026-11-21T03:10:00+01:00"},
                    {"value": 0.1}, {"since": "2026-11-21T03:10:00+01:00", "value": "cold"}, {"pushed_on": "yesterday"},
                    {"pushed_on": 20261121}):
            self.assertEqual(ice.restore(raw), {}, raw)

    def test_a_valid_part_survives_an_invalid_one(self):
        raw = {"since": "2026-11-21T03:10:00+01:00", "value": 0.1, "pushed_on": "someday", "extra": 1}
        self.assertEqual(ice.restore(raw), {"since": "2026-11-21T02:10:00+00:00", "value": 0.1})
        self.assertEqual(ice.restore({"pushed_on": "2026-11-20", "since": "x"}), {"pushed_on": "2026-11-20"})


def series(anchors, start, end):
    """What the thermometer sends: [(time, value)] at each minute the value, linear between the anchors
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


def replay(readings, home, start, end):
    """The pushes (time, message) the rule makes over [start, end] when `home(time)` says whether the person
    is home, evaluating every minute, and the minutes the row was up."""
    st, pushes, row_minutes = {}, [], 0
    queue = sorted(readings, key=lambda item: item[0])
    t = start
    while t <= end:
        while queue and queue[0][0] <= t:
            when, value = queue.pop(0)
            st = ice.step(st, CFG, value, when)
        if ice.push_due(st, CFG, home(t), t):
            pushes.append((t, ice.push(st, t)["message"]))
            st = ice.pushed(st, t)
        row_minutes += bool(ice.rows(st, CFG, t))
        t += MIN
    return pushes, row_minutes


def always(t):
    return True


class ReplayTests(unittest.TestCase):
    START, END = at(20, 12), at(23, 12)

    def dip(self, hours_cold=10.0):
        """Day 20 afternoon 3 C, down to 0.2 C by 00:00, flat, then back up to 3 C after `hours_cold` hours."""
        anchors = [(0, 3.0), (12, 0.2), (12 + hours_cold, 0.2), (14 + hours_cold, 3.0)]
        return series(anchors, self.START, self.END)

    def test_a_dip_to_zero_overnight_with_her_home_gives_one_push_at_seven(self):
        readings = self.dip(hours_cold=10)
        self.assertLess(min(value for _, value in readings), 0.31)
        cold = next(when for when, value in readings if value <= 0.3)
        pushes, row_minutes = replay(readings, always, self.START, self.END)
        self.assertEqual(pushes, [(at(21, 7, 0), f"At 0.2 °C since {cold.astimezone(LOCAL):%d.%m %H:%M}")])
        self.assertGreater(row_minutes, 6 * 60)

    def test_a_dip_that_never_gets_cold_enough_gives_nothing(self):
        readings = series([(0, 3.0), (12, 0.4), (24, 0.4), (30, 3.0)], self.START, self.END)
        self.assertEqual(replay(readings, always, self.START, self.END), ([], 0))

    def test_coming_home_later_in_the_morning_pushes_then(self):
        pushes, _ = replay(self.dip(), lambda t: t >= at(21, 9, 30), self.START, self.END)
        self.assertEqual([when for when, _ in pushes], [at(21, 9, 30)])

    def test_coming_home_in_the_evening_pushes_then_if_the_water_is_still_cold(self):
        pushes, _ = replay(self.dip(hours_cold=30), lambda t: t >= at(21, 18, 15), self.START, self.END)
        self.assertEqual([when for when, _ in pushes], [at(21, 18, 15)])

    def test_coming_home_at_night_pushes_at_seven(self):
        pushes, _ = replay(self.dip(hours_cold=36), lambda t: t >= at(21, 23, 0), self.START, self.END)
        self.assertEqual([when for when, _ in pushes], [at(22, 7, 0)])

    def test_ice_that_becomes_likely_in_the_afternoon_pushes_two_hours_after_the_water_got_cold(self):
        start, end = at(21, 8), at(21, 23)
        readings = series([(0, 4.0), (4, 0.3), (24, 0.3)], start, end)
        cold = next(when for when, value in readings if value <= 0.3)
        pushes, _ = replay(readings, always, start, end)
        self.assertEqual([when for when, _ in pushes], [cold + 2 * HOUR])
        self.assertTrue(at(21, 7) < cold + 2 * HOUR < at(21, 22))

    def test_a_dip_of_one_and_a_half_hours_gives_nothing(self):
        start, end = at(21, 8), at(21, 20)
        readings = series([(0, 4.0), (1, 0.2), (2.5, 0.2), (3, 4.0)], start, end)
        self.assertEqual(replay(readings, always, start, end), ([], 0))

    def test_a_reading_above_the_threshold_restarts_the_two_hours(self):
        start, end = at(21, 8), at(21, 20)
        readings = [(start, 4.0), (at(21, 9), 0.2), (at(21, 10, 30), 0.4), (at(21, 10, 31), 0.2)]
        pushes, _ = replay(readings, always, start, end)
        self.assertEqual([when for when, _ in pushes], [at(21, 12, 31)])

    def test_a_reading_between_the_thresholds_after_two_hours_does_not(self):
        start, end = at(21, 8), at(21, 20)
        readings = [(start, 4.0), (at(21, 9), 0.2), (at(21, 11, 30), 0.4), (at(21, 11, 31), 0.2)]
        pushes, row_minutes = replay(readings, always, start, end)
        self.assertEqual([when for when, _ in pushes], [at(21, 11)])
        self.assertEqual(row_minutes, (at(21, 20) - at(21, 11)) / MIN + 1)

    def test_a_long_freeze_gives_one_push_each_morning_naming_when_it_began(self):
        start, end = at(20, 12), at(24, 12)
        readings = series([(0, 3.0), (10, 0.0), (80, 0.0), (84, 3.0)], start, end)
        pushes, _ = replay(readings, always, start, end)
        self.assertEqual([when for when, _ in pushes], [at(21, 7), at(22, 7), at(23, 7)])
        self.assertTrue(pushes[0][1].startswith("At 0.0 °C since 2"))
        self.assertIn("since 20.11 2", pushes[1][1])

    def test_a_sensor_that_goes_quiet_does_not_restart_the_clock(self):
        start, end = at(21, 8), at(21, 20)
        readings = [(start, 4.0), (at(21, 12, 0), 0.2), (at(21, 12, 30), None), (at(21, 16, 0), 0.1)]
        pushes, _ = replay(readings, always, start, end)
        self.assertEqual([when for when, _ in pushes], [at(21, 14, 0)])

    def test_the_water_warming_above_the_clear_threshold_takes_the_row_away_and_a_later_dip_pushes_again(self):
        start, end = at(21, 8), at(23, 20)
        readings = series([(0, 0.2), (3, 0.2), (4, 3.0), (30, 3.0), (32, 0.2), (60, 0.2)], start, end)
        second = next(when for when, value in readings if when > at(22, 12) and value <= 0.3)
        pushes, row_minutes = replay(readings, always, start, end)
        self.assertEqual([when for when, _ in pushes], [at(21, 10), second + 2 * HOUR, at(23, 7)])
        self.assertGreater(row_minutes, 0)


if __name__ == "__main__":
    unittest.main()
