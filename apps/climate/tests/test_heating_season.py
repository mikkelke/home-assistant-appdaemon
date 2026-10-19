# tests/test_heating_season.py - the floor-heating controller's season advice (heating_season.py):
# when to ask the owner to start the season in the autumn and whether it can stop in the spring,
# the morning means it keeps, the quiet period between asks and the state that survives a
# restart. Pure logic, so no AppDaemon harness is needed.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import heating_plan as hp  # noqa: E402
import heating_season as ss  # noqa: E402

DAY = "2026-10-19"
ADVISED = ("family", "kristines_room", "claudias_room")
BASES = {"family": 21.5, "kristines_room": 21.5, "claudias_room": 21.5}
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
        "season_advisor": {"zones": list(ADVISED)},
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
    return {
        name: [{"day": d, "ticks": ticks, "call_hours": hours} for d in days] for name in zones
    }


def idle_days(today=DAY, n=5, **kw):
    return records([shifted(today, -k) for k in range(1, n + 1)], **kw)


class StartAdvice(unittest.TestCase):
    def test_two_cold_mornings_and_a_cold_forecast_advise_the_start(self):
        a = advisor()
        cold(a, {"family": 20.8, "kristines_room": 21.6, "claudias_room": 20.9})
        v = a.evaluate(inputs())
        self.assertEqual((v.advice, v.push, v.reason), (START, True, ""))
        self.assertEqual(v.failed, ("family", "claudias_room"))
        self.assertEqual(v.title, "Heating season?")
        self.assertEqual(
            v.message,
            "Family room was 20.8 °C this morning with the heat off, Claudia's room 20.9. "
            "Next 3 days average 7.0 °C outside.",
        )

    def test_only_the_rooms_that_failed_are_named(self):
        a = advisor()
        cold(a, {"family": 21.4, "kristines_room": 21.6, "claudias_room": 20.9})
        v = a.evaluate(inputs())
        self.assertEqual(v.failed, ("claudias_room",))
        self.assertEqual(
            v.message,
            "Claudia's room was 20.9 °C this morning with the heat off. "
            "Next 3 days average 7.0 °C outside.",
        )

    def test_the_message_names_each_failed_room_once_and_leads_with_the_first(self):
        a = advisor()
        cold(a, {"family": 20.5, "kristines_room": 20.6, "claudias_room": 20.7})
        v = a.evaluate(inputs())
        self.assertEqual(
            v.message,
            "Family room was 20.5 °C this morning with the heat off, Kristine's room 20.6, "
            "Claudia's room 20.7. Next 3 days average 7.0 °C outside.",
        )

    def test_the_latest_morning_is_the_one_in_the_message(self):
        a = advisor()
        morning(a, "family", DAY, 20.6)
        morning(a, "family", shifted(DAY, -1), 20.9)
        v = a.evaluate(inputs())
        self.assertIn("Family room was 20.6 °C", v.message)

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

    def test_the_limit_follows_the_zones_base_target(self):
        a = advisor()
        cold(a, {"family": 21.2})
        self.assertEqual(a.evaluate(inputs()).advice, NONE)
        a = advisor()
        cold(a, {"family": 21.2})
        self.assertEqual(a.evaluate(inputs(bases={**BASES, "family": 22.0})).advice, START)

    def test_the_margin_is_configurable(self):
        cfg = replace(ADVICE, margin_c=0.0)
        a = advisor(cfg)
        cold(a, {"family": 21.2})
        self.assertEqual(a.evaluate(inputs()).advice, START)

    def test_a_mild_forecast_holds_the_advice_back(self):
        for mean, advice in ((9.9, START), (10.0, NONE), (14.0, NONE)):
            a = advisor()
            cold(a, {"family": 20.5})
            v = a.evaluate(inputs(forecast_mean=mean))
            self.assertEqual(v.advice, advice, mean)
            if advice == NONE:
                self.assertEqual(v.reason, ss.REASON_MILD)

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
        a = advisor(season_on=True, since="2026-10-01")
        v = a.evaluate(inputs(season_on=True, forecast_mean=None, daily=idle_days()))
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_NO_FORECAST))
        self.assertTrue(a.due(DAY, 9 * 60))

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
        a = advisor(season_on=True, since="2026-10-01")
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
    def judge(self, daily=None, mean=14.0, since="2026-10-01", cfg=ADVICE, day=DAY):
        """The verdict for a season on since `since`; by default five idle days, a warm outlook."""
        a = advisor(cfg, season_on=True, since=since)
        daily = idle_days(today=day, n=cfg.idle_days) if daily is None else daily
        return a.evaluate(inputs(day=day, season_on=True, forecast_mean=mean, daily=daily))

    def test_five_idle_days_and_a_warm_forecast_advise_stopping(self):
        v = self.judge()
        self.assertEqual((v.advice, v.push, v.reason), (STOP, True, ""))
        self.assertEqual(v.title, "Heating can go off for the summer?")
        self.assertEqual(
            v.message,
            "No room has asked for heat for 5 days. Next 3 days average 14.0 °C outside.",
        )
        self.assertEqual(v.call_hours, (0.0,) * 5)

    def test_the_days_are_the_five_before_today(self):
        daily = records([shifted(DAY, -k) for k in range(0, 5)])  # today, but not the 5th day back
        v = self.judge(daily)
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_NO_DATA))

    def test_all_the_zones_together_must_stay_under_the_limit_each_day(self):
        for hours, advice in ((0.0, STOP), (0.04, STOP), (0.06, NONE)):
            v = self.judge(idle_days(hours=hours, zones=("family", "bathroom", "guest", "x")))
            self.assertEqual(v.advice, advice, hours)
            if advice == NONE:
                self.assertEqual(v.reason, ss.REASON_CALLS)

    def test_one_day_with_heat_wanted_is_enough_to_say_no(self):
        daily = idle_days()
        daily["claudias_room"][2]["call_hours"] = 0.2
        v = self.judge(daily)
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_CALLS))
        daily["claudias_room"][2]["call_hours"] = 0.19
        self.assertEqual(self.judge(daily).advice, STOP)

    def test_a_cool_forecast_holds_the_advice_back(self):
        for mean, advice in ((12.1, STOP), (12.0, NONE), (8.0, NONE)):
            v = self.judge(mean=mean)
            self.assertEqual(v.advice, advice, mean)
            if advice == NONE:
                self.assertEqual(v.reason, ss.REASON_COOL)

    def test_a_missing_day_or_a_zone_without_a_record_is_unknown(self):
        daily = idle_days()
        del daily["family"][0]
        v = self.judge(daily)
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_NO_DATA))
        daily = idle_days()
        del daily["bathroom"]
        self.assertEqual(self.judge(daily).advice, STOP)  # no entry, no zone
        v = self.judge({})
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_NO_DATA))

    def test_a_day_the_controller_barely_saw_is_unknown(self):
        for ticks, advice in ((ss.MIN_DAY_TICKS - 1, NONE), (ss.MIN_DAY_TICKS, STOP)):
            daily = idle_days()
            daily["family"][1]["ticks"] = ticks
            v = self.judge(daily)
            self.assertEqual(v.advice, advice, ticks)
            if advice == NONE:
                self.assertEqual(v.reason, ss.REASON_NO_DATA)

    def test_junk_records_are_unknown_and_never_raise(self):
        for junk in ({"day": "x"}, {"day": shifted(DAY, -1), "ticks": "many"}, None, 5):
            daily = idle_days()
            daily["family"][0] = junk
            self.assertEqual(self.judge(daily).advice, NONE, junk)

    def test_a_young_season_waits_for_whole_days(self):
        # the five whole days before the 19th are the 14th to the 18th, so it must start earlier
        for since, advice in (("2026-10-13", STOP), ("2026-10-14", NONE), (DAY, NONE)):
            v = self.judge(since=since)
            self.assertEqual(v.advice, advice, since)
            if advice == NONE:
                self.assertEqual(v.reason, ss.REASON_YOUNG)

    def test_a_season_first_seen_on_counts_from_that_day(self):
        a = ss.SeasonAdvisor(ADVICE, CFG.zones)
        a.note_season(True, DAY)
        self.assertEqual(a.snapshot()["on_since"], DAY)
        a.note_season(True, shifted(DAY, 3))
        self.assertEqual(a.snapshot()["on_since"], DAY)

    def test_a_season_switched_off_forgets_when_it_started(self):
        a = advisor(season_on=True, since="2026-10-01")
        a.note_season(False, DAY)
        self.assertIsNone(a.snapshot()["on_since"])

    def test_the_stop_ask_is_not_repeated_for_three_days_either(self):
        a = advisor(season_on=True, since="2026-10-01")
        pushes = []
        for k in range(4):
            day = shifted(DAY, k)
            daily = idle_days(today=day)
            v = a.evaluate(inputs(day=day, season_on=True, forecast_mean=14.0, daily=daily))
            pushes.append((v.advice, v.push))
        self.assertEqual(pushes, [(STOP, True), (STOP, False), (STOP, False), (STOP, True)])

    def test_the_idle_days_and_hours_are_configurable(self):
        cfg = replace(ADVICE, idle_days=2, idle_hours=1.0)
        daily = idle_days(hours=0.1)  # 0.5 h a day over the five zones
        v = self.judge(daily, cfg=cfg)
        self.assertEqual(v.advice, STOP)
        self.assertIn("for 2 days", v.message)
        v = self.judge(daily)
        self.assertEqual((v.advice, v.reason), (NONE, ss.REASON_CALLS))

    def test_one_idle_day_reads_in_the_singular(self):
        v = self.judge(cfg=replace(ADVICE, idle_days=1))
        self.assertIn("for 1 day.", v.message)

    def test_the_log_record_has_the_hours_it_used(self):
        v = self.judge()
        self.assertEqual(
            v.to_dict(),
            {
                "day": DAY,
                "advice": STOP,
                "pushed": True,
                "forecast_mean": 14.0,
                "call_hours": [0.0] * 5,
                "message": v.message,
            },
        )


class Labels(unittest.TestCase):
    def test_a_room_with_one_resident_is_theirs_and_the_rest_go_by_their_name(self):
        z = CFG.zones
        self.assertEqual(ss.zone_label(z["family"]), "Family room")
        self.assertEqual(ss.zone_label(z["kristines_room"]), "Kristine's room")
        self.assertEqual(ss.zone_label(z["claudias_room"]), "Claudia's room")
        self.assertEqual(ss.zone_label(z["bathroom"]), "Bathroom")
        self.assertEqual(ss.zone_label(z["guest_bathroom"]), "Guest bathroom")


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
                    {"__future__", "dataclasses", "datetime", "typing", "heating_plan"},
                    line,
                )


if __name__ == "__main__":
    unittest.main()
