# tests/test_upcoming_maint.py - maintenance rows for sensor.house_upcoming: Rober2 parts, the washer
# drum clean, battery rules, offline devices and the daily push.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import upcoming_core as core  # noqa: E402
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


class LowRuleTests(unittest.TestCase):
    def test_a_low_reading_opens_once_it_has_held_two_hours(self):
        st = feed(SNZB, [(T0, 20), (T0 + HOUR, 20)])
        self.assertNotIn("low_since", st)
        self.assertIsNone(row_of(SNZB, st, 20, T0 + HOUR))
        st = feed(SNZB, [(T0 + 2 * HOUR, 20)], st)
        row, key = row_of(SNZB, st, 20, T0 + 2 * HOUR)
        self.assertEqual((row["title"], row["detail"], row["icon"]),
                         ("Dining room window 1", "replace, CR2477", "mdi:battery-alert-variant-outline"))
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

    def test_a_trend_shows_only_while_above_twenty(self):
        st = feed(BLIND, [(T0, 25), (T0 + 2 * HOUR, 25)])
        facts = {"in_since": "2026-06-19", "last_set_days": 191, "eta_weeks": 1}
        self.assertEqual(row_of(BLIND, st, 25, T0 + 2 * HOUR, facts)[0]["detail"],
                         "charge, about 1 week to 20 %, in since 19.06, last set 191 d")
        self.assertEqual(row_of(BLIND, st, 19, T0 + 2 * HOUR, facts)[0]["detail"],
                         "charge, in since 19.06, last set 191 d")

    def test_an_older_year_is_written_out_and_long_details_drop_trailing_facts(self):
        self.assertEqual(maint.ddmm(date(2025, 10, 15), T0), "15.10.25")
        self.assertEqual(maint.fit_parts(["a" * 50, "b" * 9, "c"]), "a" * 50)
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
        rows = maint.rober2_rows(states, "kitchen")
        self.assertEqual([(r["title"], r["detail"], r["area"]) for r, _ in rows],
                         [("Rober2 filter", "replace", "kitchen"), ("Rober2 sensors", "clean", "kitchen")])
        self.assertEqual(rows[0][1], f"rober2:filter:{states['filter']['due_since']}")

    def test_a_replayed_one_poll_jump_to_150_h_does_not_clear(self):
        st = self.step(None, -19902, T0)
        key = maint.rober2_rows({"filter": st}, "kitchen")[0][1]
        st = self.step(st, 540000, T0 + HOUR)
        st = self.step(st, -20000, T0 + HOUR + 15 * MIN)
        st = self.step(st, -20000, T0 + 4 * HOUR)
        rows = maint.rober2_rows({"filter": st}, "kitchen")
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


class DrumTests(unittest.TestCase):
    def test_unknown_shows_without_a_push_key(self):
        row, key = maint.drum_row("", T0, "guest_bathroom")
        self.assertEqual((row["title"], row["detail"], row["area"]),
                         ("Washer drum clean", "Bomuld 90 monthly, last unknown", "guest_bathroom"))
        self.assertIsNone(key)
        self.assertEqual(maint.drum_row(None, T0, "guest_bathroom")[1], None)

    def test_due_thirty_days_after_the_last_clean(self):
        self.assertIsNone(maint.drum_row(core.utc_iso(T0 - 29 * DAY), T0, "guest_bathroom"))
        row, key = maint.drum_row("2026-09-12T11:21:30+00:00", T0, "guest_bathroom")
        self.assertEqual(row["detail"], "Bomuld 90 monthly, last 12.09")
        self.assertEqual(key, "washer:drum:2026-09-12T11:21:30+00:00")


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


if __name__ == "__main__":
    unittest.main()
