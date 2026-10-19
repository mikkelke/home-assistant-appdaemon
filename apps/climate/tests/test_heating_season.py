# tests/test_heating_season.py - the floor-heating controller's season advice (heating_season.py):
# when to ask the owner to turn the heat on in the autumn and whether it can go off in the spring,
# the morning means it keeps, the quiet period between asks and the state that survives a
# restart. Pure logic, so no AppDaemon harness is needed.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import heating_plan as hp  # noqa: E402
import heating_season as ss  # noqa: E402

DAY = "2026-10-19"
SPRING = "2026-03-26"
ADVISED = ("family", "kristines_room", "claudias_room")
BASES = {"family": 21.5, "kristines_room": 21.5, "claudias_room": 21.5}
AIRS = {"family": 22.4, "kristines_room": 22.6, "claudias_room": 22.9}
ROOMS = {
    "family": ["sensor.living_room_feel", "sensor.dining_room_feel", "sensor.kitchen_feel"],
    "kristines_room": ["sensor.kristines_room_feel"],
    "claudias_room": ["sensor.claudias_room_feel"],
}
START, STOP, NONE = ss.ADVICE_START, ss.ADVICE_STOP, ss.ADVICE_NONE


def zone(name, **extra):
    raw = {
        "thermostat": f"climate.{name}_thermostat",
        "control_air": ["sensor.x"],
        "target_entity": f"input_number.heating_target_{name}",
        "base_c": 21.5,
        "lower_limit_c": 19.0,
        "upper_limit_c": 24.0,
    }
    raw.update(extra)
    return raw


CFG = hp.parse_config(
    {
        "season_entity": "input_boolean.heating_season",
        "zones": {
            "family": zone("family"),
            "kristines_room": zone("kristines_room", residents=["kristine"]),
            "claudias_room": zone("claudias_room", residents=["claudia"]),
            "bathroom": zone("bathroom"),
            "guest_bathroom": zone("guest_bathroom", residents=["a", "b"]),
        },
        "season_advisor": {
            "zones": {
                "family": {"rooms": ROOMS["family"]},
                "kristines_room": {"rooms": ROOMS["kristines_room"], "margin_c": 0.2},
                "claudias_room": {"rooms": ROOMS["claudias_room"]},
            }
        },
    }
)
ADVICE = CFG.season_advisor
ALL_ZONES = tuple(CFG.zones)


def shifted(day, days):
    return ss._shift(day, days)


def advisor(cfg=ADVICE, saved=None, season_on=False, since=DAY):
    a = ss.SeasonAdvisor(cfg, CFG.zones, saved)
    a.note_season(season_on, since)
    return a


def morning(a, name, day, air, samples=36):
    """A morning's readings from 05:00 on, five minutes apart."""
    for i in range(samples):
        a.observe(name, day, hp.SEASON_MORNING_FROM + 5 * i, air)


def cold(a, airs, days=2, today=DAY):
    """The last `days` mornings, each zone at its own air."""
    for k in range(days):
        for name, air in airs.items():
            morning(a, name, shifted(today, -k), air)


def inputs(**kw):
    fields = dict(day=DAY, season_on=False, forecast_mean=7.0, bases=BASES)
    fields.update(kw)
    return ss.Inputs(**fields)


def records(days, hours=0.0, ticks=288, zones=ALL_ZONES):
    return {name: [{"day": d, "ticks": ticks, "call_hours": hours} for d in days] for name in zones}


def week(totals, today=SPRING, ticks=288):
    """The closed days before `today`, newest first, as one zone's records. A None leaves the
    day out."""
    return {
        "house": [
            {"day": shifted(today, -k), "ticks": ticks, "call_hours": t}
            for k, t in enumerate(totals, 1)
            if t is not None
        ]
    }


class StartAdvice(unittest.TestCase):
    def test_two_cold_mornings_and_a_cold_forecast_advise_the_start(self):
        a = advisor()
        cold(a, {"family": 20.8, "kristines_room": 21.6, "claudias_room": 20.9})
        v = a.evaluate(inputs())
        self.assertEqual((v.advice, v.push, v.reason), (START, True, ""))
        self.assertEqual(v.failed, ("family", "claudias_room"))
        self.assertEqual(v.title, "Heating on?")
        self.assertEqual(
            v.message,
            "Family room 20.8 °C this morning (target 21.5), Claudia's room 20.9. "
            "Next 3 days 7 °C. Time to turn the heat on?",
        )

    def test_only_the_rooms_that_failed_are_named(self):
        a = advisor()
        cold(a, {"family": 21.4, "kristines_room": 21.6, "claudias_room": 20.9})
        v = a.evaluate(inputs())
        self.assertEqual(v.failed, ("claudias_room",))
        self.assertEqual(
            v.message,
            "Claudia's room 20.9 °C this morning (target 21.5). "
            "Next 3 days 7 °C. Time to turn the heat on?",
        )

    def test_the_room_furthest_below_its_target_leads_and_the_others_follow(self):
        a = advisor()
        cold(a, {"family": 20.8, "kristines_room": 21.2, "claudias_room": 20.3})
        v = a.evaluate(inputs())
        self.assertEqual(
            v.message,
            "Claudia's room 20.3 °C this morning (target 21.5), family room 20.8, "
            "Kristine's room 21.2. Next 3 days 7 °C. Time to turn the heat on?",
        )
        self.assertEqual(v.failed, ("family", "kristines_room", "claudias_room"))

    def test_the_target_shown_is_the_leading_rooms_own(self):
        a = advisor()
        cold(a, {"family": 21.0})
        v = a.evaluate(inputs(bases={**BASES, "family": 22.0}))
        self.assertIn("Family room 21.0 °C this morning (target 22.0).", v.message)

    def test_the_latest_morning_is_the_one_in_the_message(self):
        a = advisor()
        morning(a, "family", DAY, 20.6)
        morning(a, "family", shifted(DAY, -1), 20.9)
        self.assertIn("Family room 20.6 °C", a.evaluate(inputs()).message)

    def test_one_cold_morning_is_not_enough(self):
        a = advisor()
        morning(a, "family", DAY, 20.5)
        morning(a, "family", shifted(DAY, -1), 21.8)
        v = a.evaluate(inputs())
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_WARM))

    def test_a_morning_at_the_limit_is_not_cold_and_just_under_it_is(self):
        # base 21.5 minus the 0.5 margin
        a = advisor()
        cold(a, {"family": 21.0})
        self.assertEqual(a.evaluate(inputs()).advice, NONE)
        a = advisor()
        cold(a, {"family": 20.99})
        self.assertEqual(a.evaluate(inputs()).advice, START)

    def test_kristine_is_judged_closer_to_her_target(self):
        for air, advice in ((21.2, START), (21.25, START), (21.35, NONE), (21.4, NONE)):
            a = advisor()
            cold(a, {"kristines_room": air})
            self.assertEqual(a.evaluate(inputs()).advice, advice, air)
        a = advisor()
        cold(a, {"family": 21.2, "claudias_room": 21.2})
        self.assertEqual(a.evaluate(inputs()).advice, NONE)

    def test_the_limits_it_used_are_in_the_verdict(self):
        a = advisor()
        cold(a, {"family": 20.8, "kristines_room": 21.6, "claudias_room": 20.9})
        self.assertEqual(
            a.evaluate(inputs()).to_dict()["limits"],
            {"family": 21.0, "kristines_room": 21.3, "claudias_room": 21.0},
        )

    def test_the_limit_follows_the_zones_base_target(self):
        a = advisor()
        cold(a, {"family": 21.2})
        self.assertEqual(a.evaluate(inputs()).advice, NONE)
        a = advisor()
        cold(a, {"family": 21.2})
        self.assertEqual(a.evaluate(inputs(bases={**BASES, "family": 22.0})).advice, START)

    def test_the_default_margin_and_the_zone_margins_are_configurable(self):
        a = advisor(replace(ADVICE, margin_c=0.0))
        cold(a, {"family": 21.2})
        self.assertEqual(a.evaluate(inputs()).advice, START)
        a = advisor(replace(ADVICE, margins={}))
        cold(a, {"kristines_room": 21.2})
        self.assertEqual(a.evaluate(inputs()).advice, NONE)

    def test_the_forecast_gate_is_twelve_degrees(self):
        for mean, advice in ((11.9, START), (12.0, NONE), (14.0, NONE)):
            a = advisor()
            cold(a, {"family": 20.5})
            v = a.evaluate(inputs(forecast_mean=mean))
            self.assertEqual(v.advice, advice, mean)
            if advice == NONE:
                self.assertEqual(v.reason, ss.REASON_MILD)

    def test_the_outlook_is_whole_degrees(self):
        for mean, text in (
            (7.4, "Next 3 days 7 °C."),
            (7.6, "Next 3 days 8 °C."),
            (-0.4, "Next 3 days 0 °C."),
        ):
            a = advisor()
            cold(a, {"family": 20.5})
            self.assertIn(text, a.evaluate(inputs(forecast_mean=mean)).message, mean)

    def test_more_mornings_take_more_cold_mornings(self):
        cfg = replace(ADVICE, mornings=3)
        a = advisor(cfg)
        cold(a, {"family": 20.5}, days=2)
        v = a.evaluate(inputs())
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_NO_DATA))
        a = advisor(cfg)
        cold(a, {"family": 20.5}, days=3)
        self.assertEqual(a.evaluate(inputs()).advice, START)

    def test_a_zone_that_is_not_advised_is_not_watched(self):
        a = advisor()
        cold(a, {"bathroom": 18.0, "guest_bathroom": 18.0})
        self.assertEqual(a.snapshot()["mornings"], {})
        self.assertEqual(a.evaluate(inputs()).advice, NONE)

    def test_the_verdict_carries_the_means_it_used_for_the_sensor_and_the_log(self):
        a = advisor()
        cold(a, {"family": 20.5, "claudias_room": 20.7})
        morning(a, "kristines_room", DAY, 21.9)
        v = a.evaluate(inputs())
        self.assertEqual(
            v.to_dict(),
            {
                "day": DAY,
                "advice": START,
                "pushed": True,
                "forecast_mean": 7.0,
                "mornings": {
                    "family": [20.5, 20.5],
                    "kristines_room": [21.9, None],
                    "claudias_room": [20.7, 20.7],
                },
                "limits": {"family": 21.0, "claudias_room": 21.0},
                "failed": ["family", "claudias_room"],
                "message": v.message,
            },
        )
        json.dumps(v.to_dict())


class MissingData(unittest.TestCase):
    def test_no_readings_means_no_advice(self):
        v = advisor().evaluate(inputs())
        self.assertEqual((v.advice, v.push, v.reason), (NONE, False, ss.REASON_NO_DATA))

    def test_a_morning_with_too_few_readings_is_unknown_not_cold(self):
        a = advisor()
        morning(a, "family", shifted(DAY, -1), 20.5)
        morning(a, "family", DAY, 20.5, samples=ss.MIN_MORNING_SAMPLES - 1)
        self.assertEqual(a.evaluate(inputs()).reason, ss.REASON_NO_DATA)
        a = advisor()
        morning(a, "family", shifted(DAY, -1), 20.5)
        morning(a, "family", DAY, 20.5, samples=ss.MIN_MORNING_SAMPLES)
        self.assertEqual(a.evaluate(inputs()).advice, START)

    def test_a_zone_with_an_unknown_morning_is_not_judged_but_the_others_are(self):
        a = advisor()
        morning(a, "family", DAY, 20.5)
        cold(a, {"claudias_room": 20.7})
        v = a.evaluate(inputs())
        self.assertEqual((v.advice, v.failed), (START, ("claudias_room",)))

    def test_a_zone_without_a_base_target_is_not_judged(self):
        a = advisor()
        cold(a, {"family": 20.5})
        v = a.evaluate(inputs(bases={}))
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_NO_DATA))

    def test_no_forecast_decides_nothing_and_leaves_the_day_open(self):
        a = advisor()
        cold(a, {"family": 20.5})
        v = a.evaluate(inputs(forecast_mean=None))
        self.assertEqual((v.advice, v.push, v.reason), (NONE, False, ss.REASON_NO_FORECAST))
        self.assertTrue(a.due(DAY, 9 * 60))
        self.assertIsNone(a.last)
        self.assertEqual(a.snapshot()["asked"], {})
        self.assertEqual(a.evaluate(inputs()).advice, START)

    def test_no_forecast_in_the_spring_either(self):
        a = advisor(season_on=True, since="2025-10-26")
        v = a.evaluate(
            ss.Inputs(day=SPRING, season_on=True, forecast_mean=None, daily=week([0.0] * 7))
        )
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_NO_FORECAST))
        self.assertTrue(a.due(SPRING, 9 * 60))

    def test_readings_count_from_five_to_nine_and_not_outside(self):
        a = advisor()
        for minute in (4 * 60 + 55, 9 * 60, 12 * 60):
            for _ in range(10):
                a.observe("family", DAY, minute, 20.0)
        self.assertEqual(a.snapshot()["mornings"], {})
        for minute in (5 * 60, 8 * 60 + 55):
            a.observe("family", DAY, minute, 20.0)
        self.assertEqual(a.snapshot()["mornings"], {"family": {DAY: [40.0, 2]}})

    def test_no_reading_is_no_sample(self):
        a = advisor()
        a.observe("family", DAY, 6 * 60, None)
        self.assertEqual(a.snapshot()["mornings"], {})

    def test_a_morning_with_the_heat_on_is_not_a_heat_off_morning(self):
        a = advisor(season_on=True, since="2025-10-26")
        morning(a, "family", DAY, 20.5)
        self.assertEqual(a.snapshot()["mornings"], {})
        a.note_season(False, DAY)
        morning(a, "family", DAY, 20.5)
        self.assertEqual(a.snapshot()["mornings"]["family"][DAY][1], 36)

    def test_readings_before_the_season_is_known_are_not_kept(self):
        a = ss.SeasonAdvisor(ADVICE, CFG.zones)
        morning(a, "family", DAY, 20.5)
        self.assertEqual(a.snapshot()["mornings"], {})

    def test_old_mornings_are_dropped(self):
        a = advisor()
        morning(a, "family", "2026-10-01", 20.5, samples=ss.MIN_MORNING_SAMPLES)
        morning(a, "family", DAY, 20.5, samples=ss.MIN_MORNING_SAMPLES)
        self.assertEqual(list(a.snapshot()["mornings"]["family"]), [DAY])


class OnceADay(unittest.TestCase):
    def test_it_is_due_from_the_set_time_and_not_before(self):
        a = advisor()
        self.assertFalse(a.due(DAY, 7 * 60 + 59))
        self.assertTrue(a.due(DAY, 8 * 60))
        self.assertTrue(a.due(DAY, 23 * 60))

    def test_a_judged_day_is_used_up_and_the_next_day_is_due_at_the_time(self):
        a = advisor()
        cold(a, {"family": 20.5})
        a.evaluate(inputs())
        self.assertFalse(a.due(DAY, 8 * 60))
        self.assertFalse(a.due(DAY, 23 * 60))
        tomorrow = shifted(DAY, 1)
        self.assertFalse(a.due(tomorrow, 7 * 60))
        self.assertTrue(a.due(tomorrow, 8 * 60))

    def test_a_day_judged_to_no_advice_is_used_up_too(self):
        a = advisor()
        a.evaluate(inputs())
        self.assertFalse(a.due(DAY, 9 * 60))

    def test_it_waits_for_the_season_to_be_known(self):
        a = ss.SeasonAdvisor(ADVICE, CFG.zones)
        self.assertFalse(a.due(DAY, 9 * 60))
        a.note_season(False, DAY)
        self.assertTrue(a.due(DAY, 9 * 60))

    def test_the_time_is_configurable(self):
        a = advisor(replace(ADVICE, evaluate_at=9 * 60 + 30))
        self.assertFalse(a.due(DAY, 9 * 60 + 29))
        self.assertTrue(a.due(DAY, 9 * 60 + 30))


class QuietPeriod(unittest.TestCase):
    def advisor(self, cfg=ADVICE):
        a = advisor(cfg)
        morning(a, "family", shifted(DAY, -1), 20.5)
        return a

    def ask(self, a, k):
        """The judgement of the day k days after DAY, once its own morning has been cold."""
        day = shifted(DAY, k)
        morning(a, "family", day, 20.5)
        return a.evaluate(inputs(day=day))

    def test_an_ask_is_not_repeated_for_three_days_and_then_it_is(self):
        a = self.advisor()
        seen = []
        for k in range(4):
            v = self.ask(a, k)
            seen.append((v.advice, v.push, v.reason))
        self.assertEqual(
            seen,
            [
                (START, True, ""),
                (START, False, ss.REASON_ASKED),
                (START, False, ss.REASON_ASKED),
                (START, True, ""),
            ],
        )

    def test_the_period_is_counted_in_calendar_days_not_hours(self):
        a = self.advisor()
        self.ask(a, 0)
        self.assertEqual(a.snapshot()["asked"], {START: DAY})
        self.assertFalse(self.ask(a, 2).push)
        self.assertTrue(self.ask(a, 3).push)
        self.assertEqual(a.snapshot()["asked"], {START: shifted(DAY, 3)})

    def test_the_period_is_configurable(self):
        a = self.advisor(replace(ADVICE, reask_days=1))
        self.assertEqual([self.ask(a, k).push for k in range(3)], [True, True, True])

    def test_a_quiet_day_still_shows_what_the_rule_says(self):
        a = self.advisor()
        self.ask(a, 0)
        v = self.ask(a, 1)
        self.assertEqual(v.advice, START)
        self.assertEqual((a.last["advice"], a.last["pushed"]), (START, False))
        self.assertEqual(a.last["reason"], ss.REASON_ASKED)
        self.assertEqual(a.last["day"], shifted(DAY, 1))

    def test_a_switch_of_the_season_starts_afresh(self):
        a = self.advisor()
        self.ask(a, 0)
        self.assertIsNotNone(a.last)
        a.note_season(True, shifted(DAY, 1))
        self.assertEqual((a.snapshot()["asked"], a.last), ({}, None))
        a.note_season(False, shifted(DAY, 1))
        self.assertTrue(self.ask(a, 1).push)

    def test_the_same_season_again_changes_nothing(self):
        a = self.advisor()
        self.ask(a, 0)
        a.note_season(False, shifted(DAY, 1))
        self.assertEqual(a.snapshot()["asked"], {START: DAY})
        self.assertIsNotNone(a.last)


class StopAdvice(unittest.TestCase):
    def judge(self, daily=None, mean=7.0, since="2025-10-26", day=SPRING, airs=None, cfg=ADVICE):
        """The verdict for a season on since `since`; by default a week without heat and a mild
        outlook."""
        a = advisor(cfg, season_on=True, since=since)
        daily = week([0.0] * cfg.look_back_days, today=day) if daily is None else daily
        airs = AIRS if airs is None else airs
        inp = ss.Inputs(day=day, season_on=True, forecast_mean=mean, airs=airs, daily=daily)
        return a.evaluate(inp)

    def test_a_week_without_much_heat_and_a_mild_outlook_advise_turning_it_off(self):
        v = self.judge()
        self.assertEqual((v.advice, v.push, v.reason), (STOP, True, ""))
        self.assertEqual(v.title, "Heating off?")
        self.assertEqual(
            v.message,
            "Heat ran 0.0 h a day last week, rooms 22-23 °C. Next 3 days 7 °C. Turn the heat off?",
        )
        self.assertEqual(v.call_hours, (0.0,) * 7)

    def test_the_heat_figure_is_the_typical_day_so_a_blip_does_not_outvote_the_rule(self):
        v = self.judge(week([0.4, 6.0, 0.6, 0.5, 9.0, 0.7, 0.5]))
        self.assertEqual(v.advice, STOP)
        self.assertIn("Heat ran 0.6 h a day last week,", v.message)

    def test_blips_are_allowed_five_idle_days_of_seven_are_enough(self):
        for busy, advice in ((0, STOP), (1, STOP), (2, STOP), (3, NONE), (7, NONE)):
            v = self.judge(week([5.0] * busy + [0.5] * (7 - busy)))
            self.assertEqual(v.advice, advice, busy)
            if advice == NONE:
                self.assertEqual(v.reason, ss.REASON_CALLS)

    def test_a_day_is_idle_under_two_hours_and_two_hours_exactly_is_not(self):
        for hours, advice in ((1.99, STOP), (2.0, NONE)):
            v = self.judge(week([hours] * 3 + [0.5] * 4))
            self.assertEqual(v.advice, advice, hours)

    def test_all_the_zones_together_make_the_day(self):
        days = [shifted(SPRING, -k) for k in range(1, 8)]
        five = ("a", "b", "c", "d", "e")
        self.assertEqual(self.judge(records(days, hours=0.3, zones=five)).advice, STOP)
        v = self.judge(records(days, hours=0.5, zones=five))
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_CALLS))

    def test_an_unknown_day_is_not_an_idle_one(self):
        v = self.judge(week([None, 0.5, 0.5, None, 0.5, 0.5, 0.5]))
        self.assertEqual(v.advice, STOP)
        self.assertEqual(v.to_dict()["call_hours"][0], None)
        # four idle days and one unknown could have been five
        v = self.judge(week([None, 0.5, 0.5, 5.0, 0.5, 0.5, 5.0]))
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_NO_DATA))
        # three idle days and one unknown cannot
        v = self.judge(week([None, 0.5, 0.5, 5.0, 0.5, 5.0, 5.0]))
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_CALLS))
        v = self.judge({})
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_NO_DATA))

    def test_a_day_the_controller_barely_saw_is_unknown(self):
        short = ss.MIN_DAY_TICKS - 1
        daily = week([0.5] * 7)
        for record in daily["house"][:3]:
            record["ticks"] = short
        v = self.judge(daily)
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_NO_DATA))
        for record in daily["house"][:3]:
            record["ticks"] = ss.MIN_DAY_TICKS
        self.assertEqual(self.judge(daily).advice, STOP)

    def test_junk_records_are_unknown_and_never_raise(self):
        for junk in ({"day": "x"}, {"day": shifted(SPRING, -1), "ticks": "many"}, None, 5):
            daily = week([0.5] * 7)
            daily["house"][0] = junk
            v = self.judge(daily)
            self.assertIsNone(v.call_hours[0], junk)
            self.assertEqual(v.advice, STOP, junk)

    def test_the_outlook_must_be_at_least_five_degrees_and_has_no_upper_limit(self):
        for mean, advice in ((4.9, NONE), (5.0, STOP), (13.0, STOP), (22.0, STOP)):
            v = self.judge(mean=mean)
            self.assertEqual(v.advice, advice, mean)
            if advice == NONE:
                self.assertEqual(v.reason, ss.REASON_COOL)

    def test_it_only_asks_between_the_first_of_march_and_the_end_of_september(self):
        cases = (
            ("2026-02-28", NONE),
            ("2026-03-01", STOP),
            ("2026-06-15", STOP),
            ("2026-09-30", STOP),
            ("2026-10-01", NONE),
            ("2026-12-15", NONE),
        )
        for day, advice in cases:
            v = self.judge(day=day)
            self.assertEqual(v.advice, advice, day)
            if advice == NONE:
                self.assertEqual(v.reason, ss.REASON_DATES, day)

    def test_a_young_season_waits_for_its_first_week(self):
        for since, advice in (
            (shifted(SPRING, -7), STOP),
            (shifted(SPRING, -6), NONE),
            (SPRING, NONE),
        ):
            v = self.judge(since=since)
            self.assertEqual(v.advice, advice, since)
            if advice == NONE:
                self.assertEqual(v.reason, ss.REASON_YOUNG)

    def test_an_autumn_start_never_triggers_it(self):
        # the season starts on 26 October; by March it is old, but in November it is neither
        self.assertEqual(self.judge(day="2026-11-05", since="2026-10-26").reason, ss.REASON_DATES)
        self.assertEqual(self.judge(day="2027-03-05", since="2026-10-26").advice, STOP)

    def test_a_season_first_seen_on_counts_from_that_day(self):
        a = ss.SeasonAdvisor(ADVICE, CFG.zones)
        a.note_season(True, SPRING)
        self.assertEqual(a.snapshot()["on_since"], SPRING)
        a.note_season(True, shifted(SPRING, 3))
        self.assertEqual(a.snapshot()["on_since"], SPRING)

    def test_a_season_switched_off_forgets_when_it_started(self):
        a = advisor(season_on=True, since="2025-10-26")
        a.note_season(False, SPRING)
        self.assertIsNone(a.snapshot()["on_since"])

    def test_the_rooms_are_a_range_or_one_figure_and_may_be_missing(self):
        v = self.judge(airs={"family": 22.4, "kristines_room": 21.6, "claudias_room": None})
        self.assertIn("rooms 22 °C.", v.message)
        v = self.judge(airs={"family": 21.4, "kristines_room": 23.4})
        self.assertIn("rooms 21-23 °C.", v.message)
        v = self.judge(airs={})
        self.assertEqual(
            v.message, "Heat ran 0.0 h a day last week. Next 3 days 7 °C. Turn the heat off?"
        )

    def test_the_period_reads_naturally(self):
        for days, phrase in ((1, "yesterday"), (5, "over the last 5 days"), (7, "last week")):
            cfg = replace(ADVICE, look_back_days=days, idle_days=1)
            self.assertIn(f"h a day {phrase},", self.judge(cfg=cfg).message, days)

    def test_the_stop_ask_is_not_repeated_for_three_days_either(self):
        a = advisor(season_on=True, since="2025-10-26")
        pushes = []
        for k in range(4):
            day = shifted(SPRING, k)
            inp = ss.Inputs(
                day=day, season_on=True, forecast_mean=7.0, daily=week([0.0] * 7, today=day)
            )
            v = a.evaluate(inp)
            pushes.append((v.advice, v.push))
        self.assertEqual(pushes, [(STOP, True), (STOP, False), (STOP, False), (STOP, True)])

    def test_the_thresholds_are_configurable(self):
        cfg = replace(
            ADVICE,
            look_back_days=3,
            idle_days=2,
            idle_hours=1.0,
            stop_min_c=8.0,
            season_min_days=0,
            stop_from=(1, 1),
            stop_until=(12, 31),
        )
        winter = "2026-01-10"
        v = self.judge(week([0.5, 5.0, 0.9], today=winter), 8.0, since=winter, day=winter, cfg=cfg)
        self.assertEqual(v.advice, STOP)
        self.assertEqual(self.judge(week([0.5, 5.0, 1.0]), mean=8.0, cfg=cfg).advice, NONE)
        self.assertEqual(self.judge(week([0.5, 0.5, 0.5]), mean=7.9, cfg=cfg).advice, NONE)

    def test_the_log_record_has_the_hours_it_used(self):
        v = self.judge()
        self.assertEqual(
            v.to_dict(),
            {
                "day": SPRING,
                "advice": STOP,
                "pushed": True,
                "forecast_mean": 7.0,
                "call_hours": [0.0] * 7,
                "message": v.message,
            },
        )


# The five controller zones' call hours of each day and the outdoor mean of the day, from the
# 2025/26 season analysis. The heat ran 2-7 h a day in the family zone until the 9th of March,
# was nearly idle on the 10th to the 13th, came back for a cold snap from the 14th to the 17th and
# ended on the 18th; the family setpoint was lowered on the 9th of April.
SEASON_2026 = [
    ("2026-02-22", 24.28, 1.3),
    ("2026-02-23", 20.25, 3.7),
    ("2026-02-24", 19.33, 1.7),
    ("2026-02-25", 21.45, 0.9),
    ("2026-02-26", 22.32, 5.2),
    ("2026-02-27", 15.18, 8.1),
    ("2026-02-28", 13.17, 7.0),
    ("2026-03-01", 16.57, 5.8),
    ("2026-03-02", 11.57, 6.8),
    ("2026-03-03", 10.52, 6.9),
    ("2026-03-04", 2.15, 5.2),
    ("2026-03-05", 3.97, 4.3),
    ("2026-03-06", 2.10, 5.9),
    ("2026-03-07", 2.08, 6.4),
    ("2026-03-08", 7.52, 7.1),
    ("2026-03-09", 5.42, 7.3),
    ("2026-03-10", 0.60, 7.1),
    ("2026-03-11", 0.38, 7.9),
    ("2026-03-12", 0.87, 7.1),
    ("2026-03-13", 0.67, 6.0),
    ("2026-03-14", 4.48, 4.6),
    ("2026-03-15", 7.22, 5.0),
    ("2026-03-16", 14.17, 3.8),
    ("2026-03-17", 8.08, 4.1),
    ("2026-03-18", 1.20, 5.8),
    ("2026-03-19", 1.97, 7.4),
    ("2026-03-20", 1.23, 4.9),
    ("2026-03-21", 0.30, 4.2),
    ("2026-03-22", 0.43, 6.3),
    ("2026-03-23", 0.15, 6.8),
    ("2026-03-24", 0.38, 7.3),
    ("2026-03-25", 0.23, 7.1),
    ("2026-03-26", 0.77, 5.7),
    ("2026-03-27", 3.33, 5.2),
    ("2026-03-28", 0.33, 5.3),
    ("2026-03-29", 0.38, 5.7),
    ("2026-03-30", 0.52, 5.7),
    ("2026-03-31", 0.28, 7.0),
    ("2026-04-01", 3.70, 7.5),
    ("2026-04-02", 0.00, 7.0),
    ("2026-04-03", 0.15, 5.6),
    ("2026-04-04", 0.33, 8.0),
]


class SpringReplay2026(unittest.TestCase):
    """The stop rule run over the end of the 2025/26 season, a day at a time, with the mean of
    the day's and the next two days' outdoor means standing in for the 72 hour forecast."""

    def run_season(self, first="2026-03-01", last="2026-04-02", shift=0):
        def moved(day):
            return shifted(day, shift)

        daily = {
            "house": [
                {"day": moved(d), "ticks": 288, "call_hours": hours} for d, hours, _t in SEASON_2026
            ]
        }
        outdoor = {d: t for d, _h, t in SEASON_2026}
        a = advisor(season_on=True, since="2025-10-26")
        out, day = {}, date.fromisoformat(first)
        while day.isoformat() <= last:
            d = day.isoformat()
            ahead = [outdoor[shifted(d, k - shift)] for k in range(3)]
            inp = ss.Inputs(
                day=d, season_on=True, forecast_mean=sum(ahead) / 3, airs=AIRS, daily=daily
            )
            out[d] = a.evaluate(inp)
            day += timedelta(days=1)
        return out

    def test_early_march_with_the_family_zone_running_two_to_seven_hours_asks_nothing(self):
        for d, v in self.run_season(last="2026-03-22").items():
            self.assertEqual(v.advice, NONE, d)

    def test_four_idle_days_inside_the_cold_snap_are_not_the_end(self):
        verdicts = self.run_season()
        for d in ("2026-03-14", "2026-03-15", "2026-03-16", "2026-03-17"):
            self.assertEqual(verdicts[d].reason, ss.REASON_CALLS, d)

    def test_the_first_ask_comes_on_the_23rd_when_five_of_the_last_seven_days_were_idle(self):
        v = self.run_season()["2026-03-23"]
        self.assertEqual((v.advice, v.push), (STOP, True))
        self.assertEqual(
            v.message,
            "Heat ran 1.2 h a day last week, rooms 22-23 °C. Next 3 days 7 °C. Turn the heat off?",
        )
        self.assertEqual(v.call_hours, (0.43, 0.30, 1.23, 1.97, 1.20, 8.08, 14.17))

    def test_the_blips_on_the_27th_and_the_1st_do_not_end_the_advice(self):
        verdicts = self.run_season()
        for d, v in verdicts.items():
            if d >= "2026-03-23":
                self.assertEqual(v.advice, STOP, d)

    def test_it_asks_every_third_day_while_nothing_is_done(self):
        verdicts = self.run_season()
        pushed = [d for d, v in verdicts.items() if v.push]
        self.assertEqual(pushed, ["2026-03-23", "2026-03-26", "2026-03-29", "2026-04-01"])

    def test_the_same_weeks_in_midwinter_ask_nothing(self):
        verdicts = self.run_season(first="2026-01-01", last="2026-01-31", shift=-59)
        self.assertEqual({v.advice for v in verdicts.values()}, {NONE})
        self.assertIn(ss.REASON_DATES, {v.reason for v in verdicts.values()})


class Labels(unittest.TestCase):
    def test_a_room_with_one_resident_is_that_residents_and_the_rest_go_by_their_name(self):
        z = CFG.zones
        self.assertEqual(ss.zone_label(z["family"]), "family room")
        self.assertEqual(ss.zone_label(z["kristines_room"]), "Kristine's room")
        self.assertEqual(ss.zone_label(z["claudias_room"]), "Claudia's room")
        self.assertEqual(ss.zone_label(z["bathroom"]), "bathroom")
        self.assertEqual(ss.zone_label(z["guest_bathroom"]), "guest bathroom")


class DayCallHours(unittest.TestCase):
    def test_the_hours_of_all_zones_add_up(self):
        daily = records([DAY], hours=0.1, zones=("a", "b", "c"))
        self.assertAlmostEqual(ss.day_call_hours(daily, DAY), 0.3)

    def test_an_empty_house_is_unknown(self):
        self.assertIsNone(ss.day_call_hours({}, DAY))


class SavedState(unittest.TestCase):
    def test_everything_survives_a_restart(self):
        a = advisor()
        cold(a, {"family": 20.5, "claudias_room": 20.7})
        a.evaluate(inputs())
        saved = json.loads(json.dumps(a.snapshot()))
        b = ss.SeasonAdvisor(ADVICE, CFG.zones, saved)
        self.assertEqual(b.snapshot(), a.snapshot())
        self.assertFalse(b.due(DAY, 9 * 60))
        cold(b, {"family": 20.5, "claudias_room": 20.7}, days=1, today=shifted(DAY, 1))
        v = b.evaluate(inputs(day=shifted(DAY, 1)))
        self.assertEqual((v.advice, v.push, v.reason), (START, False, ss.REASON_ASKED))

    def test_the_morning_means_alone_are_enough_to_judge_after_a_restart(self):
        a = advisor()
        cold(a, {"family": 20.5})
        b = ss.SeasonAdvisor(ADVICE, CFG.zones, json.loads(json.dumps(a.snapshot())))
        self.assertEqual(b.evaluate(inputs()).advice, START)

    def test_a_new_install_starts_empty(self):
        a = ss.SeasonAdvisor(ADVICE, CFG.zones)
        self.assertEqual(
            a.snapshot(),
            {
                "season_on": None,
                "on_since": None,
                "evaluated": "",
                "asked": {},
                "mornings": {},
                "last": None,
            },
        )

    def test_junk_in_the_state_is_survivable(self):
        junk = [
            None,
            "x",
            [],
            {"season_on": "yes", "on_since": 5, "evaluated": [], "asked": 3, "mornings": []},
            {"asked": {START: "yesterday", STOP: 4}, "mornings": {"family": 5}},
            {"mornings": {"family": {"x": [1, 2], DAY: "no", "2026-10-18": [1]}}},
            {"mornings": {"family": {DAY: [None, 3]}, "bathroom": {DAY: [1.0, 9]}}},
            {"mornings": {"family": {DAY: [True, 3]}}},
            {"last": {"advice": "maybe", "day": DAY}},
            {"last": [1, 2]},
            {"last": {"advice": START}},
        ]
        for saved in junk:
            a = ss.SeasonAdvisor(ADVICE, CFG.zones, saved)
            a.note_season(False, DAY)
            morning(a, "family", DAY, 20.5)
            a.observe("claudias_room", DAY, 6 * 60, 20.5)
            a.evaluate(inputs())
            json.dumps(a.snapshot())

    def test_a_saved_verdict_that_is_not_one_is_dropped(self):
        for last in ({"advice": "maybe", "day": DAY}, {"advice": START}, {"day": DAY}, "x"):
            self.assertIsNone(ss.SeasonAdvisor(ADVICE, CFG.zones, {"last": last}).last, last)
        ok = {"advice": START, "day": DAY, "pushed": True}
        self.assertEqual(ss.SeasonAdvisor(ADVICE, CFG.zones, {"last": ok}).last, ok)

    def test_a_zone_that_is_no_longer_advised_is_forgotten(self):
        saved = {"mornings": {"bathroom": {DAY: [1.0, 9]}, "family": {DAY: [2.0, 3]}}}
        a = ss.SeasonAdvisor(ADVICE, CFG.zones, saved)
        self.assertEqual(a.snapshot()["mornings"], {"family": {DAY: [2.0, 3]}})


class Rules(unittest.TestCase):
    def test_the_module_imports_nothing_that_could_write(self):
        source = (Path(__file__).resolve().parents[1] / "heating_season.py").read_text()
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                self.assertIn(
                    line.split()[1],
                    {
                        "__future__",
                        "dataclasses",
                        "datetime",
                        "statistics",
                        "typing",
                        "heating_plan",
                    },
                    line,
                )


if __name__ == "__main__":
    unittest.main()
