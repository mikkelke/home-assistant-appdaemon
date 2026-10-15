# tests/test_upcoming_maint.py - maintenance rows for sensor.house_upcoming: Rober2 parts and their
# dates before they are due, battery rules, replace-by days, offline devices and the daily push.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "appliances"))
import upcoming_core as core  # noqa: E402
import upcoming_guides as guides  # noqa: E402
import upcoming_maint as maint  # noqa: E402

T0 = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def model(**cfg):
    return maint.parse_inventory({"models": {"m": cfg}})["models"]["m"]


SNZB = model(cell="CR2477", action="replace", low=20)
WS90 = model(cell="2x AA", action="replace", low=20, days=2)
BLIND = model(action="charge", low=25)
YALE = model(cell="lithium AA (rechargeable)", action="recharge", drop=15, trend=False)
PARASOLL = model(cell="1x LADDA AAA NiMH", action="recharge", trend=False)
SALUS = model(cell="built-in rechargeable", action="charge", low=40, pushed_by="salus_health")
DEVICE = {"model": "m", "name": "Dining room window 1", "area": "dining_room", "history_from": None}
ENTITY = "sensor.dining_room_window_1_battery"


def feed(battery_model, readings, st=None):
    for when, value in readings:
        st = maint.step_battery(st, battery_model, value, when)
    return st


def row_of(battery_model, st, value, now, facts=None):
    return maint.battery_row(ENTITY, DEVICE, battery_model, st, value, facts, now)


def rober2(states, left=None, rate=maint.ROBER2_RATE_DEFAULT, now=T0):
    return maint.rober2_rows(states, "kitchen", left or {}, rate, now)


class LowRuleTests(unittest.TestCase):
    def test_a_low_reading_opens_once_it_has_held_two_hours(self):
        st = feed(SNZB, [(T0, 20), (T0 + HOUR, 20)])
        self.assertNotIn("low_since", st)
        self.assertIsNone(row_of(SNZB, st, 20, T0 + HOUR))
        st = feed(SNZB, [(T0 + 2 * HOUR, 20)], st)
        row, key = row_of(SNZB, st, 20, T0 + 2 * HOUR)
        self.assertEqual((row["title"], row["detail"], row["icon"]),
                         ("Dining room window 1 battery", "replace, CR2477", "mdi:battery-alert-variant-outline"))
        self.assertNotIn("at", row)
        self.assertEqual((row["audience"], row["area"], row["kind"]), ("admin", "dining_room", "maint"))
        self.assertEqual(key, f"battery:{ENTITY}:low:{st['low_since']}")

    def test_a_motor_sag_for_a_minute_never_opens(self):
        st = feed(BLIND, [(T0, 30), (T0 + MIN, 20), (T0 + 2 * MIN, 30), (T0 + 3 * HOUR, 30)])
        self.assertNotIn("low_since", st)

    def test_swapped_cells_close_only_on_a_rise_of_thirty_held_two_hours(self):
        st = feed(SNZB, [(T0, 18), (T0 + 2 * HOUR, 18)])
        self.assertIn("low_since", st)
        st = feed(SNZB, [(T0 + 3 * HOUR, 40), (T0 + 6 * HOUR, 40)], st)
        self.assertIn("low_since", st)
        st = feed(SNZB, [(T0 + 7 * HOUR, 100), (T0 + 8 * HOUR, 100)], st)
        self.assertIn("low_since", st)
        st = feed(SNZB, [(T0 + 9 * HOUR, 100)], st)
        self.assertNotIn("low_since", st)
        self.assertIsNone(row_of(SNZB, st, 100, T0 + 9 * HOUR))

    def test_a_one_poll_spike_up_does_not_close(self):
        st = feed(SNZB, [(T0, 18), (T0 + 2 * HOUR, 18), (T0 + 3 * HOUR, 100), (T0 + 3 * HOUR + 5 * MIN, 18),
                         (T0 + 6 * HOUR, 18)])
        self.assertIn("low_since", st)

    def test_built_in_cells_close_on_a_partial_charge(self):
        st = feed(BLIND, [(T0, 25), (T0 + 2 * HOUR, 24)])
        row, _ = row_of(BLIND, st, 24, T0 + 2 * HOUR)
        self.assertEqual((row["detail"], row["icon"]), ("charge", "mdi:battery-charging-low"))
        st = feed(BLIND, [(T0 + 3 * HOUR, 34), (T0 + 6 * HOUR, 34)], st)
        self.assertIn("low_since", st)
        st = feed(BLIND, [(T0 + 7 * HOUR, 50), (T0 + 9 * HOUR, 50)], st)
        self.assertNotIn("low_since", st)


class DayRuleTests(unittest.TestCase):
    def test_the_second_house_day_in_a_row_opens(self):
        night1 = datetime(2026, 10, 13, 20, 17, tzinfo=timezone.utc)
        st = feed(WS90, [(night1, 20), (night1 + 10 * MIN, 40), (night1 + 20 * HOUR, 40)])
        self.assertNotIn("low_since", st)
        st = feed(WS90, [(night1 + DAY, 20)], st)
        self.assertIn("low_since", st)
        st = feed(WS90, [(night1 + DAY + 10 * MIN, 40), (night1 + 2 * DAY - HOUR, 40)], st)
        self.assertIn("low_since", st)
        row, _ = row_of(WS90, st, 40, night1 + 2 * DAY - HOUR)
        self.assertEqual(row["detail"], "replace, 2x AA")

    def test_days_are_house_days(self):
        late = datetime(2026, 10, 13, 21, 30, tzinfo=timezone.utc)
        st = feed(WS90, [(late, 20), (late + HOUR, 20)])
        self.assertEqual(st["low_days"], ["2026-10-13", "2026-10-14"])
        self.assertIn("low_since", st)

    def test_days_apart_do_not_open(self):
        st = feed(WS90, [(T0 - 2 * DAY, 20), (T0, 20)])
        self.assertNotIn("low_since", st)

    def test_history_low_days_open_on_the_next_reading(self):
        st = maint.bootstrap_battery(None, WS90, low_dates=["2026-10-13", "2026-10-14"])
        st = feed(WS90, [(T0, 40)], st)
        self.assertIn("low_since", st)


class DropRuleTests(unittest.TestCase):
    def test_flat_cells_and_a_one_minute_glitch_never_open(self):
        readings = [(T0 + i * 7 * HOUR, 92 + i % 2) for i in range(40)]
        st = feed(YALE, readings)
        st = feed(YALE, [(T0 + 300 * HOUR, 0), (T0 + 300 * HOUR + MIN, 92), (T0 + 303 * HOUR, 92)], st)
        self.assertNotIn("low_since", st)
        self.assertEqual(st["peak"], 93)

    def test_a_sudden_drop_held_two_hours_says_recharge_and_new_cells_close_it(self):
        st = feed(YALE, [(T0, 93), (T0 + DAY, 75), (T0 + DAY + 2 * HOUR, 75)])
        row, _ = row_of(YALE, st, 75, T0 + DAY + 2 * HOUR)
        self.assertEqual(row["detail"], "recharge, lithium AA (rechargeable)")
        self.assertEqual(row["icon"], "mdi:battery-sync-outline")
        st = feed(YALE, [(T0 + 2 * DAY, 92), (T0 + 2 * DAY + 2 * HOUR, 92)], st)
        self.assertNotIn("low_since", st)
        self.assertEqual(st["peak"], 92)


class OfflineTests(unittest.TestCase):
    def test_a_live_transition_is_dated_after_a_day(self):
        onset = datetime(2026, 7, 12, 12, 36, 39, tzinfo=timezone.utc)
        st = feed(PARASOLL, [(onset - DAY, 52)])
        st = maint.step_battery(st, PARASOLL, None, onset + MIN, offline_at=onset)
        self.assertIsNone(row_of(PARASOLL, st, None, onset + 23 * HOUR))
        row, key = row_of(PARASOLL, st, None, onset + DAY)
        self.assertEqual(row["detail"], "no data since 12.07, recharge, 1x LADDA AAA NiMH")
        self.assertEqual((row["icon"], row["title"]), ("mdi:lan-disconnect", "Dining room window 1"))
        self.assertEqual(key, f"battery:{ENTITY}:offline:2026-07-12T12:36:39+00:00")

    def test_an_ha_restart_blip_is_no_outage_and_leaves_nothing_behind(self):
        st = feed(SNZB, [(T0, 100), (T0 + HOUR, None), (T0 + HOUR + MIN, 100)])
        self.assertEqual(st, {})

    def test_a_restart_during_an_outage_keeps_the_date(self):
        onset = datetime(2026, 7, 12, 12, 36, 39, tzinfo=timezone.utc)
        st = maint.step_battery(feed(PARASOLL, [(onset - DAY, 52)]), PARASOLL, None, onset, offline_at=onset)
        restored = dict(st)
        for when in (T0, T0 + HOUR, T0 + 5 * HOUR):
            restored = maint.step_battery(restored, PARASOLL, None, when)
        self.assertEqual(restored["off_since"], st["off_since"])
        self.assertEqual(row_of(PARASOLL, restored, None, T0 + 5 * HOUR)[0]["detail"][:19], "no data since 12.07")

    def test_an_unseen_onset_is_undated_and_waits_a_day_from_first_sight(self):
        st = maint.step_battery(None, SNZB, None, T0)
        self.assertIsNone(row_of(SNZB, st, None, T0 + 23 * HOUR))
        row, _ = row_of(SNZB, st, None, T0 + DAY)
        self.assertEqual(row["detail"], "offline, CR2477")

    def test_history_dates_an_unseen_onset_but_never_overrides_a_live_one(self):
        onset = datetime(2026, 7, 12, 12, 36, 39, tzinfo=timezone.utc)
        st = maint.bootstrap_battery(maint.step_battery(None, SNZB, None, T0), SNZB, onset=onset)
        self.assertEqual(row_of(SNZB, st, None, T0)[0]["detail"], "no data since 12.07, CR2477")
        live = maint.step_battery(None, SNZB, None, T0, offline_at=T0 - HOUR)
        self.assertEqual(maint.bootstrap_battery(live, SNZB, onset=onset)["off_since"], live["off_since"])

    def test_a_window_without_any_reading_is_undated_and_shows_at_once(self):
        st = maint.bootstrap_battery(maint.step_battery(None, SNZB, None, T0), SNZB,
                                     outage_from=datetime(2025, 1, 26, tzinfo=timezone.utc))
        self.assertEqual(row_of(SNZB, st, None, T0)[0]["detail"], "offline, CR2477")

    def test_back_online_with_new_cells_shows_nothing(self):
        # Dining room window 1, 14.10.2026: offline since 12.07, back at 06:12 UTC reading the
        # stale 52, then 81 from the fresh LADDA cell an hour later.
        onset = datetime(2026, 7, 12, 12, 36, 39, tzinfo=timezone.utc)
        st = maint.step_battery(feed(PARASOLL, [(onset - DAY, 52)]), PARASOLL, None, onset, offline_at=onset)
        back = datetime(2026, 10, 14, 6, 12, 44, tzinfo=timezone.utc)
        st = feed(PARASOLL, [(back, 52), (back + HOUR, 81), (back + 4 * HOUR, 81)], st)
        self.assertEqual(st, {})
        self.assertIsNone(row_of(PARASOLL, st, 81, back + 4 * HOUR))

    def test_offline_outranks_an_open_low_condition(self):
        st = feed(SNZB, [(T0, 15), (T0 + 2 * HOUR, 15)])
        st = maint.step_battery(st, SNZB, None, T0 + 3 * HOUR, offline_at=T0 + 3 * HOUR)
        self.assertEqual(row_of(SNZB, st, None, T0 + 4 * HOUR)[0]["detail"], "replace, CR2477")
        self.assertEqual(row_of(SNZB, st, None, T0 + 28 * HOUR)[0]["detail"][:19], "no data since 14.10")


class RowTextTests(unittest.TestCase):
    def test_age_facts_follow_the_cell_type_within_sixty_characters(self):
        st = feed(SNZB, [(T0, 18), (T0 + 2 * HOUR, 18)])
        facts = {"in_since": "2026-06-02", "last_set_days": 101}
        row, _ = row_of(SNZB, st, 18, T0 + 2 * HOUR, facts)
        self.assertEqual(row["detail"], "replace, CR2477, in since 02.06, last set 101 d")
        self.assertTrue(core.is_valid_row(row))

    def test_an_offline_row_puts_the_age_of_the_cells_before_their_type(self):
        onset = datetime(2026, 7, 12, 12, 36, 39, tzinfo=timezone.utc)
        st = maint.step_battery(None, PARASOLL, None, onset, offline_at=onset)
        facts = {"in_since": "2025-10-15", "last_set_days": 270}
        self.assertEqual(row_of(PARASOLL, st, None, T0, facts)[0]["detail"],
                         "no data since 12.07, recharge, in since 15.10.25")
        self.assertEqual(row_of(SNZB, maint.step_battery(None, SNZB, None, onset, offline_at=onset), None, T0,
                                {"in_since": "2026-06-02"})[0]["detail"],
                         "no data since 12.07, in since 02.06, CR2477")

    def test_an_open_condition_is_timed_at_its_replace_by_day(self):
        st = feed(WS90, [(T0 - DAY, 20), (T0, 20)])
        facts = {"empty_on": "2026-11-16", "replace_by": "2026-11-16", "basis": "trend"}
        row, key = row_of(WS90, st, 20, T0, facts)
        self.assertEqual((row["title"], row["detail"]),
                         ("Dining room window 1 batteries", "replace by 16.11 - 33 days, 2x AA"))
        self.assertEqual((row["at"], row["icon"]), ("2026-11-16T08:00:00+00:00", "mdi:battery-alert-variant-outline"))
        self.assertEqual(key, f"battery:{ENTITY}:low:{st['low_since']}")
        self.assertTrue(core.is_valid_row(row))

    def test_a_replace_by_day_alone_is_a_timed_row_that_never_pushes(self):
        facts = {"in_since": "2026-06-19", "last_set_days": 191, "replace_by": "2026-12-26", "basis": "trend"}
        row, key = row_of(BLIND, {}, 44, T0, facts)
        self.assertEqual((row["title"], row["detail"]), ("Dining room window 1 battery",
                                                         "charge by 26.12 - 73 days, in since 19.06, last set 191 d"))
        self.assertEqual((row["at"], row["icon"], key),
                         ("2026-12-26T08:00:00+00:00", "mdi:battery-clock-outline", None))
        self.assertIsNone(maint.digest([(row["title"], key)], {}))

    def test_a_model_lifetime_reads_as_approximate_and_the_year_shows_when_it_differs(self):
        facts = {"in_since": "2026-09-15", "lifetimes": [335], "replace_by": "2027-01-30", "basis": "model"}
        row, _ = row_of(PARASOLL, {}, 75, T0, facts)
        self.assertEqual(row["detail"], "recharge by ~30.01.27 - 108 days, 1x LADDA AAA NiMH")
        self.assertEqual(row["at"], "2027-01-30T08:00:00+00:00")

    def test_one_day_and_today(self):
        tomorrow = {"replace_by": "2026-10-15", "basis": "own"}
        self.assertEqual(row_of(SNZB, {}, 60, T0, tomorrow)[0]["detail"], "replace by 15.10 - 1 day, CR2477")
        self.assertEqual(row_of(SNZB, {}, 60, T0 + DAY, tomorrow)[0]["detail"], "replace by 15.10 - today, CR2477")

    def test_a_day_that_has_passed_shows_nothing_and_an_open_condition_goes_untimed(self):
        facts = {"replace_by": "2026-10-13", "basis": "own"}
        self.assertIsNone(row_of(SNZB, {}, 60, T0, facts))
        st = feed(SNZB, [(T0, 18), (T0 + 2 * HOUR, 18)])
        row, _ = row_of(SNZB, st, 18, T0 + 2 * HOUR, facts)
        self.assertEqual(row["detail"], "replace, CR2477")
        self.assertNotIn("at", row)

    def test_an_offline_row_stays_untimed_under_the_device_name(self):
        onset = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        st = maint.step_battery(None, SNZB, None, onset, offline_at=onset)
        row, _ = row_of(SNZB, st, None, T0, {"replace_by": "2026-12-01", "basis": "own"})
        self.assertEqual((row["title"], row["detail"]), ("Dining room window 1", "no data since 12.10, CR2477"))
        self.assertNotIn("at", row)

    def test_a_model_without_an_action_gets_no_replace_by_row(self):
        self.assertIsNone(row_of(model(cell=""), {}, 30, T0, {"replace_by": "2026-12-01", "basis": "own"}))

    def test_the_title_counts_the_cells(self):
        self.assertEqual(maint.battery_title("Roof weather station", "2x AA"), "Roof weather station batteries")
        self.assertEqual(maint.battery_title("Window", "1x LADDA AAA NiMH"), "Window battery")
        self.assertEqual(maint.battery_title("Bedroom blind", ""), "Bedroom blind battery")

    def test_an_older_year_is_written_out_and_long_details_drop_trailing_facts(self):
        self.assertEqual(maint.ddmm(date(2025, 10, 15), T0), "15.10.25")
        self.assertEqual(maint.fit_parts(["a" * 50, "b" * 9, "c"]), "a" * 50 + ", c")
        self.assertEqual(maint.fit_parts(["a", "", None, "b"]), "a, b")

    def test_another_apps_conditions_get_rows_but_no_push_key(self):
        st = feed(SALUS, [(T0, 40), (T0 + 2 * HOUR, 40)])
        row, key = row_of(SALUS, st, 40, T0 + 2 * HOUR)
        self.assertEqual(row["detail"], "charge, built-in rechargeable")
        self.assertIsNone(key)
        st = maint.step_battery(st, SALUS, None, T0 + 3 * HOUR)
        self.assertIsNone(row_of(SALUS, st, None, T0 + 30 * HOUR)[1])


class Rober2Tests(unittest.TestCase):
    def step(self, st, seconds, when):
        return maint.step_rober2(st, seconds, when)

    def test_a_part_at_zero_is_due_with_the_robot_word(self):
        states = {"filter": self.step(None, -19902, T0), "sensor": self.step(None, 0, T0),
                  "side_brush": self.step(None, 716910, T0)}
        rows = rober2(states)
        self.assertEqual([(r["title"], r["detail"], r["area"]) for r, _ in rows],
                         [("Rober2 filter", "replace", "kitchen"), ("Rober2 sensors", "clean", "kitchen")])
        self.assertEqual(rows[0][1], f"rober2:filter:{states['filter']['due_since']}")

    def test_a_replayed_one_poll_jump_to_150_h_does_not_clear(self):
        st = self.step(None, -19902, T0)
        key = rober2({"filter": st})[0][1]
        st = self.step(st, 540000, T0 + HOUR)
        st = self.step(st, -20000, T0 + HOUR + 15 * MIN)
        st = self.step(st, -20000, T0 + 4 * HOUR)
        rows = rober2({"filter": st})
        self.assertEqual(rows[0][1], key)

    def test_a_reset_that_holds_two_hours_clears(self):
        st = self.step(None, -19902, T0)
        st = self.step(st, 540000, T0 + HOUR)
        st = self.step(st, 540000, T0 + 2 * HOUR + 59 * MIN)
        self.assertIn("due_since", st)
        st = self.step(st, 539734, T0 + 3 * HOUR)
        self.assertEqual(st, {})

    def test_no_reading_changes_nothing(self):
        st = self.step(None, -1, T0)
        self.assertEqual(self.step(st, None, T0 + DAY), st)

    def test_every_row_of_a_part_carries_its_guide_due_or_not(self):
        states = {"filter": self.step(None, -19902, T0)}
        rows = rober2(states, {"main_brush": 514101, "side_brush": 714003, "sensor": 102003})
        self.assertEqual(len(rows), 4)
        for row, _ in rows:
            part = row["id"].removeprefix("maint:rober2:")
            self.assertEqual(row["steps"], list(guides.GUIDES[f"rober2:{part}"].steps), part)
            self.assertNotIn("warn", row)
            self.assertNotIn("done", row)
            self.assertTrue(core.is_valid_row(row), row)


class Rober2EstimateTests(unittest.TestCase):
    def test_a_part_with_time_left_is_dated_by_the_cleaning_rate(self):
        [(row, key)] = rober2({}, {"sensor": 102003}, rate=3000)
        self.assertEqual((row["id"], row["title"], row["detail"]),
                         ("maint:rober2:sensor", "Rober2 sensors", "clean by ~17.11 - 34 days"))
        self.assertEqual((row["at"], row["icon"], row["area"]), ("2026-11-17T08:00:00+00:00", "mdi:robot-vacuum", "kitchen"))
        self.assertEqual((row["kind"], row["audience"]), ("maint", "admin"))
        self.assertIsNone(key)
        self.assertTrue(core.is_valid_row(row))

    def test_every_part_gets_its_own_row_in_its_own_words(self):
        left = {"filter": 534003, "main_brush": 514101, "side_brush": 714003, "sensor": 102003}
        shown = [(r["title"], r["detail"]) for r, _ in rober2({}, left)]
        self.assertEqual(shown, [("Rober2 filter", "replace by ~16.04.27 - 184 days"),
                                 ("Rober2 main brush", "replace by ~09.04.27 - 177 days"),
                                 ("Rober2 side brush", "replace by ~17.06.27 - 246 days"),
                                 ("Rober2 sensors", "clean by ~18.11 - 35 days")])

    def test_the_day_is_the_house_day_the_time_runs_out(self):
        evening = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
        [(row, _)] = rober2({}, {"sensor": 290}, now=evening)
        self.assertEqual((row["detail"], row["at"]), ("clean by ~15.10 - 1 day", "2026-10-15T07:00:00+00:00"))
        [(row, _)] = rober2({}, {"sensor": 1}, now=evening)
        self.assertEqual(row["detail"], "clean by ~14.10 - today")

    def test_a_part_without_a_reading_or_beyond_the_horizon_has_no_row(self):
        rate = 3000.0
        edge = maint.ROBER2_HORIZON_DAYS * rate
        left = {"filter": None, "main_brush": edge, "side_brush": edge + 60, "sensor": 0}
        self.assertEqual([r["id"] for r, _ in rober2({}, left, rate)], ["maint:rober2:main_brush"])
        self.assertEqual(rober2({}, {}), [])

    def test_a_due_part_takes_over_the_row_of_its_estimate(self):
        estimate, _ = rober2({}, {"filter": 534003})[0]
        due = maint.step_rober2(None, 0, T0)
        rows = rober2({"filter": due}, {"filter": 0, "sensor": 102003})
        self.assertEqual([r["id"] for r, _ in rows], [estimate["id"], "maint:rober2:sensor"])
        row, key = rows[0]
        self.assertEqual((row["title"], row["detail"]), (estimate["title"], "replace"))
        self.assertNotIn("at", row)
        self.assertEqual(key, f"rober2:filter:{due['due_since']}")

    def test_a_part_held_due_by_a_reset_that_has_not_held_keeps_its_due_row(self):
        due = maint.step_rober2(None, -19902, T0)
        held = maint.step_rober2(due, 540000, T0 + HOUR)
        [(row, key)] = rober2({"filter": held}, {"filter": 540000}, now=T0 + HOUR)
        self.assertEqual(row["detail"], "replace")
        self.assertEqual(key, f"rober2:filter:{due['due_since']}")
        cleared = maint.step_rober2(held, 540000, T0 + 3 * HOUR)
        [(row, key)] = rober2({"filter": cleared}, {"filter": 540000}, now=T0 + 3 * HOUR)
        self.assertEqual(row["detail"], "replace by ~18.04.27 - 186 days")
        self.assertIsNone(key)

    def test_an_estimate_is_never_pushed_and_a_due_row_is_pushed_once(self):
        due = maint.step_rober2(None, 0, T0)
        rows = rober2({"sensor": due}, {"filter": 534003, "main_brush": 514101, "sensor": 0})
        self.assertEqual([key is not None for _, key in rows], [False, False, True])
        conditions = [(row["title"], key) for row, key in rows]
        found = maint.digest(conditions, {})
        self.assertEqual((found["message"], found["keys"]), ("Rober2 sensors", [rows[2][1]]))
        self.assertIsNone(maint.digest(conditions[:2], {}))
        self.assertIsNone(maint.digest(conditions, {rows[2][1]: core.utc_iso(T0)}))


class Rober2RateTests(unittest.TestCase):
    START = T0 - 30 * DAY

    def test_the_rate_is_the_cleaning_time_added_a_day_over_the_window(self):
        series = [(self.START, 1041807.0), (self.START + 2 * DAY, 1041871.0), (self.START + 20 * DAY, 1100000.0),
                  (T0 - HOUR, 1131245.0)]
        self.assertAlmostEqual(maint.rober2_rate(series, T0), 89438 / 30)

    def test_unavailable_readings_and_a_count_that_falls_back_add_nothing(self):
        series = [(self.START, 1000.0), (self.START + DAY, None), (self.START + 2 * DAY, 1000.0),
                  (self.START + 5 * DAY, 4000.0), (self.START + 9 * DAY, 100.0), (self.START + 12 * DAY, 400.0)]
        self.assertAlmostEqual(maint.rober2_rate(series, T0), 3300 / 30)

    def test_a_series_shorter_than_the_window_is_averaged_over_what_it_covers(self):
        series = [(T0 - 10 * DAY, 5000.0), (T0 - DAY, 35000.0)]
        self.assertAlmostEqual(maint.rober2_rate(series, T0), 3000.0)

    def test_without_cleaning_or_a_week_of_readings_the_default_applies(self):
        flat = [(self.START, 7000.0), (T0 - HOUR, 7000.0)]
        short = [(T0 - 6 * DAY, 7000.0), (T0 - DAY, 17000.0)]
        for series in ([], [(self.START, 7000.0)], [(self.START, None), (T0 - DAY, None)], flat, short):
            self.assertEqual(maint.rober2_rate(series, T0), 2900.0, series)
        self.assertEqual(maint.ROBER2_RATE_DEFAULT, 2900.0)


class DigestTests(unittest.TestCase):
    def test_three_names_then_a_count_and_only_new_episodes(self):
        conditions = [("Rober2 filter", "a"), ("Washer drum clean", None), ("Kitchen smoke alarm", None),
                      ("Bathroom presence", "b"), ("Hallway presence", "c"), ("Dining room window 1", "d"),
                      ("Bedroom blind", "e")]
        found = maint.digest(conditions, {"b": "2026-10-13T07:00:00+00:00"})
        self.assertEqual(found["title"], "Maintenance due")
        self.assertEqual(found["message"], "Rober2 filter, Hallway presence, Dining room window 1 +1")
        self.assertEqual(found["keys"], ["a", "c", "d", "e"])
        pushed = {key: "2026-10-14T07:00:00+00:00" for key in found["keys"]}
        self.assertIsNone(maint.digest(conditions, {**pushed, "b": "x"}))

    def test_pushed_keys_are_kept_half_a_year(self):
        pushed = {"old": core.utc_iso(T0 - 181 * DAY), "new": core.utc_iso(T0 - 179 * DAY)}
        self.assertEqual(set(maint.prune_pushed(pushed, T0)), {"new"})


class InventoryTests(unittest.TestCase):
    def test_typos_fail_at_start(self):
        bad = [
            {"models": {"m": {"action": "swap"}}},
            {"models": {"m": {"low": 20}}},
            {"models": {"m": {"action": "replace", "low": 20}}, "devices": [{"entity": "sensor.a", "model": "x"}]},
            {"models": {"m": {}}, "devices": [{"entity": "sensor.a", "model": "m"}, {"entity": "sensor.a", "model": "m"}]},
            {"models": {"m": {}}, "devices": [{"entity": "sensor.a", "model": "m"}], "excluded": ["sensor.a"]},
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=raw):
                maint.parse_inventory(raw)

    def test_unlisted_battery_sensors_are_named(self):
        inv = maint.parse_inventory({"models": {"m": {}}, "devices": [{"entity": "sensor.a", "model": "m"}],
                                     "excluded": ["sensor.b"]})
        self.assertEqual(maint.unlisted_batteries(["sensor.a", "sensor.b", "sensor.c"], inv), ["sensor.c"])
        self.assertEqual(inv["devices"]["sensor.a"]["history_from"], None)

    def test_a_model_is_empty_at_zero_unless_it_says_otherwise(self):
        self.assertEqual(SNZB["empty"], 0)
        self.assertEqual(model(action="replace", empty=15)["empty"], 15)


if __name__ == "__main__":
    unittest.main()
