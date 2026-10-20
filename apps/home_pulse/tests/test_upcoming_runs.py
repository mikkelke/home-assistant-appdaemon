# tests/test_upcoming_runs.py - the run-based appliance tasks of sensor.house_upcoming: counting runs since
# the last Done or confirmation, the seed, Done and undo, the estimate, the rows and the dryer's log row.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

APPS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPS / "home_pulse"))
sys.path.insert(0, str(APPS / "appliances"))
import run_ledger  # noqa: E402
import upcoming_core as core  # noqa: E402
import upcoming_guides as guides  # noqa: E402
import upcoming_runs as runs  # noqa: E402

NOW = datetime(2026, 10, 15, 10, 30, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
AREAS = {"washer": "guest_bathroom", "dishwasher": "kitchen", "dryer": "guest_bathroom", "coffee": "kitchen"}


def dish(end, **extra):
    return {"ts": end.isoformat(), "duration_min": 230.0, "energy_kwh": 0.75, "predicted": "eco", "confirmed": "eco",
            "programme_confirmed_by_human": False, **extra}


def wash(end, **extra):
    return {"ts": end.isoformat(), "duration_min": 150.0, "energy_kwh": 1.2, "confirmed": "bomuld",
            "confirmed_temperature": "60", "programme_user_confirmed": False, "completion_class": "completed", **extra}


def dry(end, **extra):
    return {"ts": end.isoformat(), "duration_min": 90.0, "energy_kwh": 1.0, "predicted": "bomuld__skabstoert",
            "confirmed": "bomuld__skabstoert", "programme_confirmed_by_human": False, **extra}


def drum_clean_run(end):
    return wash(end, confirmed="drum_clean", confirmed_temperature=None, programme_user_confirmed=True)


def every_two_days(make, count, last_end=NOW - DAY):
    """`count` runs, the newest ending at `last_end`, one every two days: 3.5 runs a week once the
    ledger reaches back ninety days, which 45 do."""
    return [make(last_end - 2 * DAY * i) for i in range(count - 1, -1, -1)]


def replace(cycles, runs_ago, record):
    """`cycles` with the run `runs_ago` runs before the newest swapped for `record`."""
    out = list(cycles)
    out[len(out) - 1 - runs_ago] = record
    return out


def ledgers(washer=None, dishwasher=None, dryer=None):
    return {
        "washer": run_ledger.ledger(every_two_days(wash, 60) if washer is None else washer),
        "dishwasher": run_ledger.ledger(every_two_days(dish, 50) if dishwasher is None else dishwasher),
        "dryer": run_ledger.ledger(every_two_days(dry, 50) if dryer is None else dryer),
    }


def seeded(led, at=NOW, tasks=runs.TASKS):
    return runs.seed({}, led, at, tasks)


def rows_of(led, anchors, now=NOW, start=None, tasks=runs.TASKS):
    found = runs.statuses(led, anchors, now, tasks)
    return {row["id"]: (row, key) for row, key in runs.rows(found, now, AREAS, start)}


class TaskTests(unittest.TestCase):
    def test_the_tasks_with_their_intervals_and_how_each_is_closed(self):
        table = {task.key: (task.appliance, task.interval, task.confirmed is None) for task in runs.TASKS}
        self.assertEqual(table, {
            "dishwasher:hygiene": ("dishwasher", 15, False),
            "dishwasher:filters": ("dishwasher", 30, True),
            "dishwasher:check": ("dishwasher", 60, True),
            "washer:drum": ("washer", 15, False),
            "washer:inlet": ("washer", 90, True),
            "dryer:plinth": ("dryer", None, True),
            "coffee:rinse": ("coffee", 10, True),
            "coffee:tank": ("coffee", 20, True),
            "coffee:burrs": ("coffee", 40, True),
            "coffee:descale": ("coffee", 60, True),
        })

    def test_the_coffee_tasks_count_brews_and_the_others_runs(self):
        self.assertEqual({task.key for task in runs.TASKS if task.unit == "brew"},
                         {"coffee:rinse", "coffee:tank", "coffee:burrs", "coffee:descale"})
        self.assertEqual({task.unit for task in runs.TASKS if task.appliance != "coffee"}, {"run"})

    def test_the_drum_clean_interval_is_the_washers_own(self):
        task = next(task for task in runs.TASKS if task.key == runs.DRUM_KEY)
        self.assertEqual(task.interval, 15)

    def test_the_dryers_plinth_filter_takes_an_interval_from_the_config_and_nothing_else_does(self):
        self.assertIs(runs.configured(None), runs.TASKS)
        tasks = runs.configured(40)
        self.assertEqual({task.key: task.interval for task in tasks},
                         {**{task.key: task.interval for task in runs.TASKS}, "dryer:plinth": 40})

    def test_a_configured_interval_is_a_whole_number_above_zero(self):
        self.assertEqual([runs.interval_from(v) for v in (40, 40.0, 1, 12.9)], [40, 40, 1, 12])
        for bad in (None, 0, -3, 0.5, True, False, "40", [], {}):
            self.assertIsNone(runs.interval_from(bad), bad)

    def test_a_tapped_task_is_found_by_its_row_id_and_a_confirmed_one_is_not(self):
        for key in ("dishwasher:filters", "dishwasher:check", "washer:inlet", "dryer:plinth"):
            self.assertEqual(runs.tapped_task(f"maint:{key}").key, key)
        for other in ("maint:washer:drum", "maint:dishwasher:hygiene", "maint:rober2:filter", "", None, 5):
            self.assertIsNone(runs.tapped_task(other), other)

    def test_every_task_has_a_guide_and_text_that_fits_a_row(self):
        for task in runs.TASKS:
            self.assertIn(task.key, guides.GUIDES)
            for text in (task.title, task.how, task.closes):
                self.assertEqual(core.clean_text(text), text, text)


class SeedTests(unittest.TestCase):
    def test_every_tapped_task_is_seeded_at_now_with_the_run_count_of_the_moment(self):
        anchors = runs.seed({}, ledgers(), NOW)
        self.assertEqual(set(anchors), {"dishwasher:filters", "dishwasher:check", "washer:inlet", "dryer:plinth"})
        self.assertEqual(anchors["dishwasher:filters"], {"at": core.utc_iso(NOW), "runs": 50, "seed": True})
        self.assertEqual(anchors["washer:inlet"], {"at": core.utc_iso(NOW), "runs": 60, "seed": True})

    def test_the_dryers_finish_uld_is_no_run(self):
        cycles = every_two_days(dry, 10) + [dry(NOW - HOUR, confirmed="finish_uld", duration_min=5.1, energy_kwh=0.002)]
        self.assertEqual(runs.seed({}, ledgers(dryer=cycles), NOW)["dryer:plinth"]["runs"], 10)

    def test_a_task_that_has_an_anchor_keeps_it(self):
        kept = {"dishwasher:filters": {"at": "2026-09-01T10:00:00+00:00", "runs": 10}}
        anchors = runs.seed(kept, ledgers(), NOW)
        self.assertEqual(anchors["dishwasher:filters"], kept["dishwasher:filters"])
        self.assertEqual(anchors["dishwasher:check"]["at"], core.utc_iso(NOW))

    def test_an_appliance_without_a_ledger_is_not_seeded(self):
        anchors = runs.seed({}, {"washer": run_ledger.ledger([])}, NOW)
        self.assertEqual(set(anchors), {"washer:inlet"})

    def test_seeding_twice_changes_nothing(self):
        once = runs.seed({}, ledgers(), NOW)
        self.assertEqual(runs.seed(once, ledgers(), NOW + DAY), once)

    def test_saved_anchors_that_are_not_for_a_tapped_task_or_have_no_time_are_dropped(self):
        raw = {"dishwasher:filters": {"at": "2026-09-01T10:00:00+00:00", "runs": 10},
               "washer:drum": {"at": "2026-09-01T10:00:00+00:00"}, "washer:inlet": {"at": "soon"},
               "dishwasher:check": "x", "gone:task": {"at": "2026-09-01T10:00:00+00:00"},
               "dryer:plinth": {"at": "2026-09-01T10:00:00+00:00", "runs": 3}}
        self.assertEqual(set(runs.valid_anchors(raw)), {"dishwasher:filters", "dryer:plinth"})
        self.assertEqual(runs.valid_anchors(None), {})
        self.assertEqual(runs.valid_anchors([1]), {})


class CountingTests(unittest.TestCase):
    def test_a_seeded_task_counts_the_runs_after_the_seed_not_the_ones_before(self):
        led = ledgers()
        found = runs.statuses(led, seeded(led), NOW)
        self.assertEqual({key: (s.since, s.left, s.seeded) for key, s in found.items() if s.task.confirmed is None},
                         {"dishwasher:filters": (0, 30, True), "dishwasher:check": (0, 60, True),
                          "washer:inlet": (0, 90, True), "dryer:plinth": (0, None, True)})

    def test_runs_that_finish_after_the_seed_count(self):
        led = ledgers()
        found = runs.statuses(led, seeded(led, NOW - 11 * DAY), NOW)
        self.assertEqual((found["dishwasher:filters"].since, found["dishwasher:filters"].left), (5, 25))
        self.assertEqual(found["dryer:plinth"].since, 5)

    def test_the_hygiene_run_counts_from_the_last_run_confirmed_as_intensive(self):
        cycles = replace(every_two_days(dish, 50), 5, dish(NOW - 11 * DAY, confirmed="intensive",
                                                           programme_confirmed_by_human=True))
        status = runs.statuses(ledgers(dishwasher=cycles), {}, NOW)["dishwasher:hygiene"]
        self.assertEqual((status.since, status.left), (5, 10))
        self.assertEqual(status.when, NOW - 11 * DAY)

    def test_a_run_going_when_a_task_was_seeded_counts_once_it_has_finished(self):
        going = dish(NOW + 2 * HOUR, duration_min=240.0)
        led = ledgers(dishwasher=every_two_days(dish, 50) + [going])
        found = runs.statuses(led, seeded(led), NOW + 3 * HOUR)
        self.assertEqual((found["dishwasher:filters"].since, found["dishwasher:filters"].left), (1, 29))
        self.assertEqual(found["dishwasher:check"].since, 1)

    def test_a_run_going_when_done_was_tapped_counts_once_it_has_finished(self):
        going = wash(NOW + 2 * HOUR, duration_min=180.0)
        led = ledgers(washer=every_two_days(wash, 60) + [going])
        done = runs.mark_done(seeded(led, NOW - 30 * DAY), "washer:inlet", NOW, 60)
        self.assertEqual(runs.statuses(led, done, NOW + 3 * HOUR)["washer:inlet"].since, 1)

    def test_the_confirmed_run_saved_again_later_does_not_count_against_its_own_task(self):
        cycles = every_two_days(dish, 50)
        confirmed = dish(NOW - 11 * DAY, confirmed="intensive", programme_confirmed_by_human=True)
        again = dish(NOW - 11 * DAY + 40 * MIN, duration_min=270.0)
        cycles = [c for c in cycles if c["ts"] != confirmed["ts"]] + [confirmed, again]
        status = runs.statuses(ledgers(dishwasher=cycles), {}, NOW)["dishwasher:hygiene"]
        self.assertEqual(status.when, NOW - 11 * DAY)
        self.assertEqual(status.since, 5)

    def test_a_drum_clean_saved_again_later_does_not_count_against_its_own_task(self):
        cycles = every_two_days(wash, 60)
        clean = drum_clean_run(NOW - 9 * DAY)
        again = wash(NOW - 9 * DAY + 50 * MIN, duration_min=200.0, completion_class="suspect")
        cycles = [c for c in cycles if c["ts"] != clean["ts"]] + [clean, again]
        status = runs.statuses(ledgers(washer=cycles), {}, NOW)["washer:drum"]
        self.assertEqual(status.since, 4)

    def test_a_guess_at_intensive_is_not_a_confirmation(self):
        cycles = replace(every_two_days(dish, 50), 5, dish(NOW - 11 * DAY, confirmed="intensive"))
        status = runs.statuses(ledgers(dishwasher=cycles), {}, NOW)["dishwasher:hygiene"]
        self.assertEqual((status.when, status.since), (None, 50))

    def test_the_drum_clean_counts_from_the_last_confirmed_clean(self):
        cycles = replace(every_two_days(wash, 60), 4, drum_clean_run(NOW - 9 * DAY))
        status = runs.statuses(ledgers(washer=cycles), {}, NOW)["washer:drum"]
        self.assertEqual((status.since, status.left), (4, 11))

    def test_a_confirmed_task_with_none_on_record_has_run_every_run(self):
        status = runs.statuses(ledgers(), {}, NOW)["dishwasher:hygiene"]
        self.assertEqual((status.when, status.since, status.left), (None, 50, 0))

    def test_a_tapped_task_without_an_anchor_and_an_appliance_without_a_ledger_are_left_out(self):
        self.assertEqual(set(runs.statuses(ledgers(), {}, NOW)), {"dishwasher:hygiene", "washer:drum"})
        self.assertEqual(set(runs.statuses({"washer": ledgers()["washer"]}, {}, NOW)), {"washer:drum"})

    def test_the_pace_is_the_runs_of_the_last_ninety_days(self):
        status = runs.statuses(ledgers(), {}, NOW)["dishwasher:hygiene"]
        self.assertAlmostEqual(status.per_week, 3.5)
        few = runs.statuses(ledgers(dishwasher=[dish(NOW - 2 * HOUR)], dryer=[dry(NOW - 2 * HOUR)]), seeded(ledgers()),
                            NOW)
        self.assertEqual(few["dishwasher:hygiene"].per_week, 3.5)
        self.assertEqual(few["dryer:plinth"].per_week, 2.4)


class DoneTests(unittest.TestCase):
    def test_done_starts_the_count_over_and_keeps_the_anchor_it_replaced(self):
        led = ledgers()
        seed = seeded(led, NOW - 30 * DAY)
        done = runs.mark_done(seed, "dishwasher:filters", NOW, 50)
        self.assertEqual(done["dishwasher:filters"],
                         {"at": core.utc_iso(NOW), "runs": 50, "prev": seed["dishwasher:filters"]})
        self.assertEqual(runs.statuses(led, done, NOW)["dishwasher:filters"].since, 0)
        self.assertEqual(done["dishwasher:check"], seed["dishwasher:check"])

    def test_a_second_done_keeps_only_the_one_before_it(self):
        first = runs.mark_done({}, "washer:inlet", NOW - DAY, 29)
        second = runs.mark_done(first, "washer:inlet", NOW, 30)
        self.assertEqual(second["washer:inlet"]["prev"], {"at": core.utc_iso(NOW - DAY), "runs": 29})

    def test_undo_brings_the_seed_back_and_a_second_undo_does_nothing(self):
        seed = seeded(ledgers())
        done = runs.mark_done(seed, "dishwasher:filters", NOW + HOUR, 50)
        undone = runs.undo_done(done, "dishwasher:filters")
        self.assertEqual(undone, seed)
        self.assertEqual(runs.undo_done(undone, "dishwasher:filters"), undone)

    def test_undo_without_anything_to_undo_changes_nothing(self):
        self.assertEqual(runs.undo_done({}, "washer:inlet"), {})
        seed = seeded(ledgers())
        self.assertEqual(runs.undo_done(seed, "washer:inlet"), seed)

    def test_the_anchors_given_are_never_changed_in_place(self):
        seed = seeded(ledgers())
        before = {key: dict(value) for key, value in seed.items()}
        runs.undo_done(runs.mark_done(seed, "washer:inlet", NOW, 30), "washer:inlet")
        self.assertEqual(seed, before)

    def test_the_count_ignores_the_run_count_stored_with_the_anchor(self):
        led = ledgers()
        anchors = {"dishwasher:filters": {"at": core.utc_iso(NOW - 11 * DAY), "runs": 9999}}
        self.assertEqual(runs.statuses(led, anchors, NOW)["dishwasher:filters"].since, 5)


class RowTests(unittest.TestCase):
    def test_a_seeded_tapped_task_counts_down_to_a_dated_estimate(self):
        led = ledgers()
        row, key = rows_of(led, seeded(led))["maint:dishwasher:filters"]
        self.assertEqual((row["title"], row["detail"]),
                         ("Dishwasher filters + spray arms", "rinse filter + arms, 30 runs left, ~14.12"))
        self.assertEqual((row["at"], row["area"], row["icon"], row["audience"], row["done"]),
                         ("2026-12-14T08:00:00+00:00", "kitchen", "mdi:dishwasher", "admin", "tap"))
        self.assertIsNone(key)
        self.assertNotIn("next", row)
        self.assertTrue(core.is_valid_row(row))

    def test_the_other_intervals_give_their_dates(self):
        led = ledgers()
        found = rows_of(led, seeded(led))
        self.assertEqual(found["maint:dishwasher:check"][0]["detail"], "seal, sides, inside, 60 runs left, ~12.02.27")
        self.assertEqual(found["maint:washer:inlet"][0]["detail"], "hose + valve filters, 90 runs left, ~13.04.27")
        self.assertEqual(found["maint:washer:inlet"][0]["at"], "2027-04-13T07:00:00+00:00")
        self.assertEqual(found["maint:washer:inlet"][0]["area"], "guest_bathroom")

    def test_the_estimate_follows_the_measured_pace(self):
        slow = ledgers()
        busy = ledgers(dishwasher=[dish(NOW - DAY - HOUR * 12 * i) for i in range(200)])
        slow_at = rows_of(slow, seeded(slow))["maint:dishwasher:check"][0]["at"]
        busy_at = rows_of(busy, seeded(busy))["maint:dishwasher:check"][0]["at"]
        self.assertGreater(slow_at, busy_at)

    def test_the_sheet_lines_say_the_interval_and_the_last_done(self):
        led = ledgers()
        found = rows_of(led, seeded(led))
        self.assertEqual(found["maint:dishwasher:filters"][0]["info"], ["Every 30 runs (~2 months)", "Counting from 15.10"])
        self.assertEqual(found["maint:dishwasher:check"][0]["info"][0], "Every 60 runs (~4 months)")
        self.assertEqual(found["maint:washer:inlet"][0]["info"][0], "Every 90 runs (~6 months)")
        done = runs.mark_done(seeded(led, NOW - 30 * DAY), "dishwasher:check", NOW - 11 * DAY, 45)
        self.assertEqual(rows_of(led, done)["maint:dishwasher:check"][0]["info"][1], "Done 04.10, 5 runs ago")
        done = runs.mark_done(done, "washer:inlet", NOW, 60)
        self.assertEqual(rows_of(led, done)["maint:washer:inlet"][0]["info"][1], "Done 15.10, no runs since")
        done = runs.mark_done(done, "washer:inlet", NOW - 3 * DAY, 58)
        self.assertEqual(rows_of(led, done)["maint:washer:inlet"][0]["info"][1], "Done 12.10, 1 run ago")

    def test_one_run_left_is_singular(self):
        led = ledgers(dishwasher=every_two_days(dish, 70))
        anchors = {"dishwasher:check": {"at": core.utc_iso(NOW - 118 * DAY)}}
        self.assertEqual(runs.statuses(led, anchors, NOW)["dishwasher:check"].left, 1)
        row, _ = rows_of(led, anchors)["maint:dishwasher:check"]
        self.assertEqual(row["detail"], "seal, sides, inside, 1 run left, ~17.10")
        self.assertEqual(row["at"], "2026-10-17T07:00:00+00:00")

    def test_once_due_a_tapped_task_is_untimed_and_pushed_once_per_anchor(self):
        led = ledgers()
        anchors = seeded(led, NOW - 61 * DAY)
        row, key = rows_of(led, anchors)["maint:dishwasher:filters"]
        self.assertEqual(row["detail"], "rinse filter + arms, due")
        self.assertNotIn("at", row)
        self.assertEqual(key, f"dishwasher:filters:{core.utc_iso(NOW - 61 * DAY)}")
        self.assertEqual(row["info"][1], f"Counting from {NOW - 61 * DAY:%d.%m}")
        done = runs.mark_done(anchors, "dishwasher:filters", NOW - HOUR, 50)
        row, key = rows_of(led, done)["maint:dishwasher:filters"]
        self.assertTrue(row["detail"].startswith("rinse filter + arms, 30 runs left, ~"), row["detail"])
        self.assertIsNone(key)

    def test_a_count_past_the_interval_stays_due_and_never_goes_negative(self):
        led = ledgers(dishwasher=every_two_days(dish, 100))
        status = runs.statuses(led, seeded(led, NOW - 300 * DAY), NOW)["dishwasher:check"]
        self.assertEqual((status.since, status.left), (100, 0))
        row, key = rows_of(led, seeded(led, NOW - 300 * DAY))["maint:dishwasher:check"]
        self.assertEqual(row["detail"], "seal, sides, inside, due")
        self.assertEqual(row["info"][1], "Counting from 19.12.25")
        self.assertIsNotNone(key)

    def test_every_row_has_its_guide(self):
        led = ledgers()
        for row_id, (row, _) in rows_of(led, seeded(led)).items():
            guide = guides.GUIDES[row_id.removeprefix("maint:")]
            self.assertEqual(row["steps"], list(guide.steps), row_id)
            self.assertEqual(row.get("warn", ""), guide.warn, row_id)

    def test_the_confirmed_tasks_have_no_done_button_but_say_how_they_close(self):
        led = ledgers()
        found = rows_of(led, seeded(led))
        hygiene, drum = found["maint:dishwasher:hygiene"][0], found["maint:washer:drum"][0]
        for row in (hygiene, drum):
            self.assertNotIn("done", row)
        self.assertEqual(hygiene["info"], ["Every 15 runs (~1 month)", "None logged yet",
                                           "Logged when you pick Intensive on the dishwasher card"])
        self.assertEqual(drum["info"][2], "Logged when you pick Drum clean on the washer card")

    def test_the_hygiene_run_with_none_confirmed_is_due_and_never_pushed(self):
        row, key = rows_of(ledgers(), {})["maint:dishwasher:hygiene"]
        self.assertEqual((row["title"], row["detail"], row["area"]),
                         ("Dishwasher hygiene run", "Intensive 75, due, last unknown", "kitchen"))
        self.assertNotIn("at", row)
        self.assertIsNone(key)

    def test_the_hygiene_run_is_pushed_once_the_runs_since_the_last_one_are_fifteen(self):
        cycles = replace(every_two_days(dish, 50), 15, dish(NOW - 31 * DAY, confirmed="intensive",
                                                            programme_confirmed_by_human=True))
        row, key = rows_of(ledgers(dishwasher=cycles), {})["maint:dishwasher:hygiene"]
        self.assertEqual(row["detail"], "Intensive 75, due, last 14.09")
        self.assertEqual(key, f"dishwasher:hygiene:{core.utc_iso(NOW - 31 * DAY)}")

    def test_the_hygiene_run_counts_down_before_that_and_is_not_pushed(self):
        cycles = replace(every_two_days(dish, 50), 5, dish(NOW - 11 * DAY, confirmed="intensive",
                                                           programme_confirmed_by_human=True))
        row, key = rows_of(ledgers(dishwasher=cycles), {})["maint:dishwasher:hygiene"]
        self.assertEqual((row["detail"], row["at"]), ("Intensive 75, 10 runs left, ~04.11", "2026-11-04T08:00:00+00:00"))
        self.assertIsNone(key)
        self.assertEqual(row["info"][1], "Done 04.10, 5 runs ago")

    def test_the_drum_clean_keeps_its_cheapest_start_once_due(self):
        start = (datetime(2026, 10, 16, 0, 15, tzinfo=timezone.utc), "saves 10,40 kr vs now", False)
        row, key = rows_of(ledgers(), {}, start=start)["maint:washer:drum"]
        self.assertEqual((row["title"], row["at"], row["detail"], row["area"]),
                         ("Washer drum + drawer clean", "2026-10-16T00:15:00+00:00",
                          "Bomuld 90 + drain filter, door seal, saves 10,40 kr vs now", "guest_bathroom"))
        self.assertNotIn("next", row)
        self.assertIsNone(key)

    def test_a_start_now_stays_out_of_the_next_chip(self):
        cycles = replace(every_two_days(wash, 60), 20, drum_clean_run(NOW - 40 * DAY))
        row, key = rows_of(ledgers(washer=cycles), {}, start=(NOW, "cheapest now", True))["maint:washer:drum"]
        self.assertEqual((row["at"], row["next"]), (core.utc_iso(NOW), "skip"))
        self.assertEqual(row["detail"], "Bomuld 90 + drain filter, door seal, cheapest now, due")
        self.assertEqual(key, f"washer:drum:{core.utc_iso(NOW - 40 * DAY)}")
        self.assertTrue(core.is_valid_row(row))

    def test_a_clean_that_is_not_due_is_a_dated_countdown_and_ignores_a_start(self):
        cycles = replace(every_two_days(wash, 60), 4, drum_clean_run(NOW - 9 * DAY))
        row, key = rows_of(ledgers(washer=cycles), {}, start=(NOW, "cheapest now", True))["maint:washer:drum"]
        self.assertEqual(row["detail"], "Bomuld 90 + drain filter, door seal, 11 runs left, ~06.11")
        self.assertEqual(row["at"], "2026-11-06T08:00:00+00:00")
        self.assertNotIn("next", row)
        self.assertIsNone(key)

    def test_beyond_the_horizon_a_row_counts_runs_without_a_date(self):
        slow = ledgers(dishwasher=[dish(NOW - DAY - 10 * DAY * i) for i in range(20)])
        self.assertAlmostEqual(runs.statuses(slow, {}, NOW)["dishwasher:hygiene"].per_week, 0.7)
        row, _ = rows_of(slow, seeded(slow))["maint:dishwasher:check"]
        self.assertEqual(row["detail"], "seal, sides, inside, 60 runs left")
        self.assertNotIn("at", row)
        self.assertTrue(core.is_valid_row(row))
        row, _ = rows_of(slow, seeded(slow))["maint:dishwasher:filters"]
        self.assertEqual(row["detail"], "rinse filter + arms, 30 runs left, ~11.08.27")

    def test_every_row_is_a_valid_admin_maintenance_row(self):
        led = ledgers()
        for row, _ in rows_of(led, seeded(led)).values():
            self.assertTrue(core.is_valid_row(row), row)
            self.assertEqual((row["kind"], row["audience"]), ("maint", "admin"))
            self.assertLessEqual(len(row["detail"]), core.TEXT_MAX)


class DryerLogTests(unittest.TestCase):
    def row(self, led, anchors, tasks=runs.TASKS):
        return rows_of(led, anchors, tasks=tasks)["maint:dryer:plinth"]

    def test_with_nothing_logged_it_counts_from_today_and_stays_low_key(self):
        led = ledgers()
        row, key = self.row(led, seeded(led))
        self.assertEqual((row["title"], row["detail"], row["area"], row["icon"]),
                         ("Dryer plinth filter", "0 runs since cleaned, counting from 15.10", "guest_bathroom",
                          "mdi:tumble-dryer"))
        self.assertEqual((row["audience"], row["done"], row["next"]), ("admin", "tap", "skip"))
        self.assertNotIn("at", row)
        self.assertIsNone(key)
        self.assertEqual(row["info"], ["No interval yet. Log each clean to learn it.", "Counting from 15.10"])
        self.assertTrue(core.is_valid_row(row))

    def test_it_shows_the_runs_since_and_the_date_of_the_last_log(self):
        led = ledgers()
        logged = runs.mark_done(seeded(led, NOW - 30 * DAY), "dryer:plinth", NOW - 11 * DAY, 45)
        row, key = self.row(led, logged)
        self.assertEqual(row["detail"], "5 runs since cleaned, last 04.10")
        self.assertEqual(row["info"][1], "Logged 04.10, 5 runs ago")
        self.assertIsNone(key)
        row, _ = self.row(led, runs.mark_done(logged, "dryer:plinth", NOW - 3 * DAY, 49))
        self.assertEqual((row["detail"], row["info"][1]), ("1 run since cleaned, last 12.10", "Logged 12.10, 1 run ago"))
        row, _ = self.row(led, runs.mark_done(logged, "dryer:plinth", NOW, 50))
        self.assertEqual((row["detail"], row["info"][1]),
                         ("0 runs since cleaned, last 15.10", "Logged 15.10, no runs since"))

    def test_it_is_never_due_timed_or_pushed_however_many_runs_pass(self):
        led = ledgers(dryer=every_two_days(dry, 500, NOW - DAY))
        status = runs.statuses(led, seeded(led, NOW - 900 * DAY), NOW)["dryer:plinth"]
        self.assertGreater(status.since, 100)
        self.assertIsNone(status.left)
        row, key = self.row(led, seeded(led, NOW - 900 * DAY))
        self.assertNotIn("at", row)
        self.assertEqual(row["next"], "skip")
        self.assertNotIn("due", row["detail"])
        self.assertIsNone(key)

    def test_the_finish_uld_runs_are_not_counted(self):
        extra = [dry(NOW - HOUR * i, confirmed="finish_uld", duration_min=5.1, energy_kwh=0.003) for i in range(1, 6)]
        led = ledgers(dryer=every_two_days(dry, 10) + extra)
        row, _ = self.row(led, seeded(led, NOW - 30 * DAY))
        self.assertEqual(row["detail"], "10 runs since cleaned, counting from 15.09")

    def test_the_guide_opens_from_its_row(self):
        led = ledgers()
        row, _ = self.row(led, seeded(led))
        self.assertEqual(row["steps"], list(guides.GUIDES["dryer:plinth"].steps))

    def test_with_an_interval_in_the_config_it_becomes_a_normal_countdown(self):
        tasks = runs.configured(40)
        led = ledgers()
        row, key = self.row(led, seeded(led, tasks=tasks), tasks=tasks)
        self.assertEqual(row["detail"], "plinth filter + air path, 40 runs left, ~03.01.27")
        self.assertEqual((row["at"], row["done"]), ("2027-01-03T08:00:00+00:00", "tap"))
        self.assertNotIn("next", row)
        self.assertEqual(row["info"][0], "Every 40 runs (~3 months)")
        self.assertIsNone(key)

    def test_with_an_interval_it_is_due_untimed_and_pushed_once_per_log(self):
        tasks = runs.configured(40)
        led = ledgers()
        anchors = seeded(led, NOW - 81 * DAY, tasks=tasks)
        row, key = self.row(led, anchors, tasks=tasks)
        self.assertEqual(row["detail"], "plinth filter + air path, due")
        self.assertNotIn("at", row)
        self.assertEqual(key, f"dryer:plinth:{core.utc_iso(NOW - 81 * DAY)}")
        logged = runs.mark_done(anchors, "dryer:plinth", NOW, 50)
        row, key = self.row(led, logged, tasks=tasks)
        self.assertTrue(row["detail"].startswith("plinth filter + air path, 40 runs left"), row["detail"])
        self.assertIsNone(key)


def brew(end, **extra):
    return {"id": f"c{int(end.timestamp()) - 240}", "ts": end.isoformat(), "duration_min": 4.0,
            "completion_class": "complete", "outcome": "complete", **extra}


class CoffeeTests(unittest.TestCase):
    """The xBloom's four tapped tasks, counted in brews from the coffee app's ledger."""

    def ledgers(self, cycles):
        return {"coffee": run_ledger.ledger(cycles)}

    def test_seeded_at_first_sight_with_brews_wording(self):
        led = self.ledgers([brew(NOW - DAY - 2 * DAY * i) for i in range(45)])
        anchors = runs.seed({}, led, NOW)
        self.assertEqual(set(anchors), {"coffee:rinse", "coffee:tank", "coffee:burrs", "coffee:descale"})
        found = rows_of(led, anchors)
        row, key = found["maint:coffee:rinse"]
        self.assertEqual((row["title"], row["area"], row["icon"], row["audience"], row["done"]),
                         ("xBloom brew head", "kitchen", "mdi:coffee-maker", "admin", "tap"))
        self.assertEqual(row["detail"], "shower screen + scale plate, 10 brews left, ~04.11")
        self.assertEqual(row["info"], ["Every 10 brews (~3 weeks)", "Counting from 15.10",
                                       "Provisional interval: check the xBloom manual"])
        self.assertIsNone(key)
        self.assertEqual(found["maint:coffee:descale"][0]["detail"], "descaler, 2 rounds, 60 brews left, ~11.02.27")
        for row, _ in found.values():
            self.assertTrue(core.is_valid_row(row), row)
            self.assertNotIn("run", " ".join([row.get("detail", "")] + row.get("info", [])))

    def test_only_complete_brews_count(self):
        cycles = [brew(NOW - DAY * i) for i in range(1, 4)]
        cycles += [brew(NOW - DAY * i - HOUR, completion_class="interrupted", outcome="stopped") for i in range(1, 4)]
        anchors = {"coffee:rinse": {"at": core.utc_iso(NOW - 10 * DAY)}}
        self.assertEqual(runs.statuses(self.ledgers(cycles), anchors, NOW)["coffee:rinse"].since, 3)

    def test_done_and_the_brews_since(self):
        cycles = [brew(NOW - DAY - 2 * DAY * i) for i in range(45)]
        led = self.ledgers(cycles)
        done = runs.mark_done(runs.seed({}, led, NOW - 30 * DAY), "coffee:tank", NOW - 11 * DAY, 39)
        row, _ = rows_of(led, done)["maint:coffee:tank"]
        self.assertEqual(row["info"][1], "Done 04.10, 5 brews ago")
        self.assertTrue(row["detail"].startswith("empty, rinse, air-dry, 15 brews left"), row["detail"])
        done = runs.mark_done(done, "coffee:tank", NOW, 45)
        self.assertEqual(rows_of(led, done)["maint:coffee:tank"][0]["info"][1], "Done 15.10, no brews since")

    def test_due_after_its_interval_and_pushed_once(self):
        cycles = [brew(NOW - DAY - DAY * i) for i in range(25)]
        led = self.ledgers(cycles)
        anchors = {"coffee:rinse": {"at": core.utc_iso(NOW - 30 * DAY), "seed": True}}
        row, key = rows_of(led, anchors)["maint:coffee:rinse"]
        self.assertEqual(row["detail"], "shower screen + scale plate, due")
        self.assertEqual(key, f"coffee:rinse:{core.utc_iso(NOW - 30 * DAY)}")

    def test_the_pace_until_the_ledger_covers_two_weeks(self):
        led = self.ledgers([brew(NOW - DAY)])
        status = runs.statuses(led, runs.seed({}, led, NOW), NOW)["coffee:burrs"]
        self.assertEqual(status.per_week, 3.4)


class EstimateTests(unittest.TestCase):
    def test_the_day_the_runs_run_out(self):
        self.assertEqual(runs.estimate_day(7, 7.0, NOW).isoformat(), "2026-10-22")
        self.assertEqual(runs.estimate_day(30, 3.5, NOW).isoformat(), "2026-12-14")

    def test_none_beyond_the_horizon(self):
        self.assertIsNone(runs.estimate_day(90, 0.5, NOW))
        self.assertIsNotNone(runs.estimate_day(1, 0.5, NOW))

    def test_the_calendar_equivalent(self):
        self.assertEqual([runs.calendar_text(w) for w in (0.4, 1.0, 2.6, 3.9, 4.3, 5.2, 6.5, 10.4, 17.1, 21.4)],
                         ["~1 week", "~1 week", "~3 weeks", "~4 weeks", "~1 month", "~1 month", "~1 month", "~2 months",
                          "~4 months", "~5 months"])


if __name__ == "__main__":
    unittest.main()
