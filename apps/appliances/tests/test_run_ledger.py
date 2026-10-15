# tests/test_run_ledger.py - which feedback records are one physical run each: the count the
# maintenance rows of HouseUpcoming run on. The incident shapes are the real ones in the washer's
# and the dishwasher's feedback files.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_ledger as rl  # noqa: E402

NOW = datetime(2026, 10, 15, 10, 30, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def dish(ts, minutes=230.0, **extra):
    """A dishwasher record: ts is when the finish was saved, duration leaves out the drying tail."""
    return {"ts": ts, "duration_min": minutes, "energy_kwh": 0.75, "predicted": "eco", "confirmed": "eco",
            "programme_confirmed_by_human": False, **extra}


def wash(ts, minutes=150.0, end=None, completion="completed", **extra):
    rec = {"ts": ts, "duration_min": minutes, "energy_kwh": 1.2, "confirmed": "bomuld", "confirmed_temperature": "60",
           "programme_user_confirmed": False, "completion_class": completion, **extra}
    if end:
        rec["effective_end_at"] = end
    return rec


def at(day, hour, minute=0, month=10):
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


def iso(moment):
    return moment.isoformat()


class RunsTests(unittest.TestCase):
    def test_one_record_is_one_run_spanning_its_duration(self):
        runs = rl.finished_runs([dish("2026-10-11T02:59:40+00:00", 229.2)])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].end, datetime(2026, 10, 11, 2, 59, 40, tzinfo=timezone.utc))
        self.assertEqual(runs[0].end - runs[0].start, timedelta(minutes=229.2))

    def test_effective_end_beats_the_save_time_and_any_offset_reads_the_same(self):
        rec = wash("2026-10-14T17:43:24+02:00", 150.3, end="2026-10-14T17:40:51+02:00")
        run = rl.finished_runs([rec])[0]
        self.assertEqual(run.end, datetime(2026, 10, 14, 15, 40, 51, tzinfo=timezone.utc))

    def test_records_in_any_order_give_the_runs_oldest_first(self):
        a, b = dish("2026-10-01T10:00:00+00:00"), dish("2026-10-03T10:00:00+00:00")
        self.assertEqual(rl.finished_runs([b, a]), rl.finished_runs([a, b]))
        self.assertLess(rl.finished_runs([b, a])[0].end, rl.finished_runs([b, a])[1].end)

    def test_an_interrupted_wash_is_no_run_but_a_suspect_one_is(self):
        runs = rl.finished_runs([wash("2026-10-10T10:00:00+00:00", completion="interrupted", minutes=12.0),
                                 wash("2026-10-11T10:00:00+00:00", completion="suspect"),
                                 wash("2026-10-12T10:00:00+00:00")])
        self.assertEqual(len(runs), 2)

    def test_the_dryers_finish_uld_is_no_run(self):
        cycles = [{"ts": "2026-08-12T16:55:00+02:00", "duration_min": 5.1, "energy_kwh": 0.003,
                   "confirmed": "finish_uld", "programme_confirmed_by_human": False},
                  {"ts": "2026-08-12T17:40:00+02:00", "duration_min": 5.1, "energy_kwh": 0.002,
                   "confirmed": "finish_uld", "programme_confirmed_by_human": False},
                  {"ts": "2026-08-12T18:30:00+02:00", "duration_min": 80.6, "energy_kwh": 0.79,
                   "confirmed": "ekspres__skabstoert", "programme_confirmed_by_human": False}]
        self.assertEqual(len(rl.finished_runs(cycles)), 1)
        self.assertEqual(rl.finished_runs(cycles)[0].end, datetime(2026, 8, 12, 16, 30, tzinfo=timezone.utc))

    def test_junk_records_are_skipped(self):
        junk = [None, "x", 5, {}, {"ts": "yesterday"}, {"ts": 17}, {"ts": ""}, dish("2026-10-01T10:00:00+00:00")]
        self.assertEqual(len(rl.finished_runs(junk)), 1)
        self.assertEqual(rl.finished_runs(None), [])
        self.assertEqual(rl.finished_runs([]), [])
        self.assertEqual(rl.ledger(None), rl.Ledger([], []))

    def test_a_missing_or_bad_duration_makes_a_run_of_no_length(self):
        for minutes in (None, "long", True, -5.0):
            rec = dish("2026-10-01T10:00:00+00:00")
            rec["duration_min"] = minutes
            run = rl.finished_runs([rec])[0]
            self.assertEqual(run.start, run.end, minutes)

    def test_a_naive_time_reads_as_utc(self):
        self.assertEqual(rl.parse_time("2026-10-01T10:00:00"), datetime(2026, 10, 1, 10, tzinfo=timezone.utc))
        self.assertEqual(rl.parse_time("2026-10-01T10:00:00Z"), datetime(2026, 10, 1, 10, tzinfo=timezone.utc))
        self.assertIsNone(rl.parse_time(None))


class OneRunSavedSeveralTimesTests(unittest.TestCase):
    """A finish the monitor takes back leaves a record, and the run it resumes leaves another."""

    def test_the_dishwasher_run_saved_four_times_on_10_08(self):
        saved = [dish("2026-08-10T08:14:31+00:00", 161.3), dish("2026-08-10T09:24:43+00:00", 231.5),
                 dish("2026-08-10T09:26:49+00:00", 233.6), dish("2026-08-10T09:28:58+00:00", 235.7)]
        runs = rl.finished_runs(saved)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].end, datetime(2026, 8, 10, 9, 28, 58, tzinfo=timezone.utc))

    def test_a_dishwasher_run_saved_again_while_it_dried(self):
        self.assertEqual(len(rl.finished_runs([dish("2026-08-20T05:00:08+00:00", 196.8),
                                               dish("2026-08-20T05:51:12+00:00", 230.0)])), 1)

    def test_a_washer_record_inside_the_record_of_the_wash_it_was_taken_from(self):
        inner = wash("2026-08-19T14:46:40+02:00", 37.7, completion="suspect")
        outer = wash("2026-08-19T14:55:00+02:00", 163.0, end="2026-08-19T14:44:00+02:00")
        self.assertEqual(len(rl.finished_runs([inner, outer])), 1)
        self.assertEqual(len(rl.finished_runs([outer, inner])), 1)

    def test_a_short_record_between_two_that_a_long_one_covers(self):
        long_one = dish("2026-10-01T12:00:00+00:00", 600.0)
        covered = [dish("2026-10-01T04:00:00+00:00", 60.0), dish("2026-10-01T07:00:00+00:00", 60.0)]
        self.assertEqual(len(rl.finished_runs(covered + [long_one])), 1)


class TwoRunsTests(unittest.TestCase):
    def test_a_second_wash_started_right_after_the_first_is_another_run(self):
        first = wash("2026-03-18T12:00:00+01:00", 156.0)
        second = wash("2026-03-18T14:43:00+01:00", 152.0)
        self.assertEqual(len(rl.finished_runs([first, second])), 2)

    def test_dishwasher_runs_hours_apart_are_two_runs(self):
        self.assertEqual(len(rl.finished_runs([dish("2026-05-07T13:07:53+00:00", 214.0),
                                               dish("2026-05-07T16:44:52+00:00", 214.0)])), 2)

    def test_a_run_started_a_few_minutes_before_the_last_ended_by_the_estimate_is_still_another(self):
        first = dish("2026-10-01T10:00:00+00:00", 200.0)
        second = dish("2026-10-01T13:25:00+00:00", 205.0)
        self.assertEqual(len(rl.finished_runs([first, second])), 2)


class CountSinceTests(unittest.TestCase):
    """For a task an anchor closes (a tapped Done, or the moment a task was first seen): the runs that finished
    after it."""

    RUNS = rl.finished_runs([dish(iso(at(day, 10)), 230.0) for day in (1, 3, 5, 7)])

    def test_none_counts_every_run(self):
        self.assertEqual(rl.count_since(self.RUNS, None), 4)
        self.assertEqual(rl.count_since([], None), 0)

    def test_the_runs_that_finished_after_the_moment(self):
        self.assertEqual(rl.count_since(self.RUNS, at(5, 12)), 1)
        self.assertEqual(rl.count_since(self.RUNS, at(3, 12)), 2)
        self.assertEqual(rl.count_since(self.RUNS, at(8, 0)), 0)
        self.assertEqual(rl.count_since(self.RUNS, at(1, 0)), 4)

    def test_a_run_still_going_at_the_moment_counts_once_it_has_finished(self):
        self.assertEqual(rl.count_since(self.RUNS, at(5, 8)), 2)
        run = rl.finished_runs([wash("2026-10-15T14:00:00+00:00", 120.0)])
        self.assertEqual(rl.count_since(run, datetime(2026, 10, 15, 13, tzinfo=timezone.utc)), 1)
        self.assertEqual(rl.count_since(run, datetime(2026, 10, 15, 14, tzinfo=timezone.utc)), 0)

    def test_the_run_that_ended_at_the_moment_is_not_counted_and_the_next_one_is(self):
        self.assertEqual(rl.count_since(self.RUNS, self.RUNS[1].end), 2)
        self.assertEqual(rl.count_since(self.RUNS, self.RUNS[1].end - timedelta(seconds=1)), 3)

    def test_a_run_that_began_before_the_moment_and_ended_after_it_is_never_lost(self):
        for moment in (at(5, 6, 11), at(5, 8), at(5, 9, 59)):
            self.assertEqual(rl.count_since(self.RUNS, moment), 2, moment)


class CountAfterRunTests(unittest.TestCase):
    """For a task a confirmed run closes: the runs after that run, which does not count itself."""

    RUNS = rl.finished_runs([dish(iso(at(day, 10)), 230.0) for day in (1, 3, 5, 7)])

    def test_none_counts_every_run(self):
        self.assertEqual(rl.count_after_run(self.RUNS, None), 4)
        self.assertEqual(rl.count_after_run([], None), 0)

    def test_the_confirmed_run_is_not_counted_and_the_runs_after_it_are(self):
        self.assertEqual(rl.count_after_run(self.RUNS, self.RUNS[0].end), 3)
        self.assertEqual(rl.count_after_run(self.RUNS, self.RUNS[2].end), 1)
        self.assertEqual(rl.count_after_run(self.RUNS, self.RUNS[3].end), 0)

    def test_the_confirmed_run_saved_again_ending_later_does_not_count_itself(self):
        runs = rl.finished_runs([dish(iso(at(5, 10)), 230.0), dish(iso(at(5, 10) + timedelta(minutes=40)), 270.0),
                                 dish(iso(at(7, 10)), 230.0)])
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0].end, at(5, 10) + timedelta(minutes=40))
        self.assertEqual(rl.count_after_run(runs, at(5, 10)), 1)

    def test_the_confirmed_run_saved_again_hours_later_does_not_count_itself_either(self):
        runs = rl.finished_runs([dish(iso(at(5, 10)), 230.0), dish(iso(at(5, 14)), 470.0), dish(iso(at(7, 10)), 230.0)])
        self.assertEqual(len(runs), 2)
        self.assertEqual(rl.count_after_run(runs, at(5, 10)), 1)

    def test_a_run_started_a_few_minutes_before_the_confirmed_one_ended_by_the_estimate_counts(self):
        runs = rl.finished_runs([dish(iso(at(5, 10)), 230.0), dish(iso(at(5, 13, 5)), 190.0)])
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[1].start, at(5, 9, 55))
        self.assertEqual(rl.count_after_run(runs, at(5, 10)), 1)

    def test_a_run_that_finished_before_the_confirmed_one_never_counts(self):
        self.assertEqual(rl.count_after_run(self.RUNS, at(7, 10)), 0)
        self.assertEqual(rl.count_after_run(self.RUNS, at(4, 0)), 2)


class PerWeekTests(unittest.TestCase):
    def runs_every(self, days, count, last=None):
        last = last or NOW - DAY
        return rl.finished_runs([dish(iso(last - i * days * DAY), 200.0) for i in range(count)])

    def test_the_last_ninety_days_set_the_pace(self):
        runs = self.runs_every(2, 100)
        self.assertAlmostEqual(rl.per_week(runs, NOW, 9.9), 3.5 * 90 / 90, places=1)

    def test_a_ledger_shorter_than_the_window_is_measured_over_its_own_span(self):
        runs = self.runs_every(3, 11)
        span_weeks = (NOW - runs[0].start) / timedelta(days=7)
        self.assertAlmostEqual(rl.per_week(runs, NOW, 9.9), 11 / span_weeks)

    def test_less_than_two_weeks_or_no_run_in_the_window_is_the_default(self):
        self.assertEqual(rl.per_week([], NOW, 3.5), 3.5)
        self.assertEqual(rl.per_week(self.runs_every(1, 5), NOW, 3.5), 3.5)
        self.assertEqual(rl.per_week(self.runs_every(2, 5, last=NOW - 120 * DAY), NOW, 3.5), 3.5)

    def test_a_quiet_stretch_lowers_the_pace(self):
        busy = self.runs_every(2, 30, last=NOW - DAY)
        quiet = self.runs_every(2, 30, last=NOW - 31 * DAY)
        self.assertGreater(rl.per_week(busy, NOW, 3.5), rl.per_week(quiet, NOW, 3.5))


class LastConfirmedTests(unittest.TestCase):
    def test_the_newest_run_a_person_confirmed_as_the_programme(self):
        cycles = [dish("2026-09-01T10:00:00+00:00", confirmed="intensive", programme_confirmed_by_human=True),
                  dish("2026-10-01T10:00:00+00:00", confirmed="intensive", programme_confirmed_by_human=True),
                  dish("2026-10-05T10:00:00+00:00", confirmed="eco", programme_confirmed_by_human=True)]
        self.assertEqual(rl.last_confirmed(cycles, "intensive"), datetime(2026, 10, 1, 10, tzinfo=timezone.utc))

    def test_a_guess_is_no_confirmation(self):
        self.assertIsNone(rl.last_confirmed([dish("2026-10-01T10:00:00+00:00", confirmed="intensive")], "intensive"))
        self.assertIsNone(rl.last_confirmed([], "intensive"))
        self.assertIsNone(rl.last_confirmed(None, "intensive"))

    def test_the_older_flag_of_the_other_monitors_counts(self):
        rec = dish("2026-10-01T10:00:00+00:00", confirmed="intensive", programme_user_confirmed=True)
        del rec["programme_confirmed_by_human"]
        self.assertEqual(rl.last_confirmed([rec], "intensive"), datetime(2026, 10, 1, 10, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
