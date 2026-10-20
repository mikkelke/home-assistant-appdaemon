# tests/test_upcoming_plants.py - watering reminders for sensor.house_upcoming: the dry latch and its
# hysteresis, the row with the litres to pour and its push key, the plants watered by the calendar, and
# the plants list of house_upcoming.yaml.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import upcoming_core as core  # noqa: E402
import upcoming_maint as maint  # noqa: E402
import upcoming_plants as plants  # noqa: E402

T0 = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
ENTITY = "sensor.strelitzia_moisture"
RAW = [{"moisture": ENTITY, "name": "Strelitzia", "area": "dining_room", "dry": 15}]
PLANT = plants.parse_plants(RAW)[ENTITY]
CAL = [{"name": "Monstera", "area": "kitchen", "water": 1, "every_days": 10}]
MONSTERA = plants.parse_timed(CAL)["monstera"]


def feed(readings, st=None, plant=PLANT):
    for when, value in readings:
        st = plants.step_plant(st, plant, value, when)
    return st


def row_of(st, value, plant=PLANT, entity=ENTITY):
    return plants.plant_row(entity, plant, st, value)


class LatchTests(unittest.TestCase):
    def test_a_reading_at_or_under_dry_opens_it_and_one_above_does_not(self):
        self.assertEqual(feed([(T0, 16)]), {})
        self.assertEqual(feed([(T0, 15)]), {"since": core.utc_iso(T0)})
        self.assertEqual(feed([(T0, 14.9)]), {"since": core.utc_iso(T0)})
        self.assertEqual(feed([(T0, 0)]), {"since": core.utc_iso(T0)})

    def test_it_closes_only_five_points_above_dry(self):
        st = feed([(T0, 15)])
        for value in (16, 17, 19, 19.9, 15, 12):
            st = feed([(T0 + HOUR, value)], st)
            self.assertEqual(st, {"since": core.utc_iso(T0)}, value)
        self.assertEqual(feed([(T0 + HOUR, 20)], st), {})

    def test_a_reading_wobbling_around_dry_is_one_episode(self):
        st = feed([(T0 + i * HOUR, value) for i, value in enumerate((15, 16, 15, 16, 14, 18, 15))])
        self.assertEqual(st, {"since": core.utc_iso(T0)})
        st = feed([(T0 + 8 * HOUR, 20), (T0 + 9 * HOUR, 16), (T0 + 10 * HOUR, 17)], st)
        self.assertEqual(st, {})

    def test_watering_closes_it_in_one_reading_and_the_next_dry_spell_is_a_new_episode(self):
        st = feed([(T0, 12), (T0 + HOUR, 55)])
        self.assertEqual(st, {})
        st = feed([(T0 + 9 * DAY, 15)], st)
        self.assertEqual(st, {"since": core.utc_iso(T0 + 9 * DAY)})

    def test_an_unknown_reading_changes_nothing_open_or_closed(self):
        closed, opened = feed([(T0, 40)]), feed([(T0, 10)])
        self.assertEqual(feed([(T0 + DAY, None)], closed), closed)
        self.assertEqual(feed([(T0 + DAY, None)], opened), opened)
        self.assertEqual(feed([(T0 + DAY, None), (T0 + 2 * DAY, 11)], opened), opened)

    def test_the_state_it_is_given_is_never_changed(self):
        opened = {"since": core.utc_iso(T0)}
        plants.step_plant(opened, PLANT, 50, T0 + HOUR)
        self.assertEqual(opened, {"since": core.utc_iso(T0)})
        closed = {}
        plants.step_plant(closed, PLANT, 5, T0)
        self.assertEqual(closed, {})

    def test_each_plant_has_its_own_level(self):
        thirsty = plants.parse_plants([{"moisture": "sensor.fern", "name": "Fern", "dry": 30}])["sensor.fern"]
        opened = feed([(T0, 28)], plant=thirsty)
        self.assertEqual(opened, {"since": core.utc_iso(T0)})
        self.assertEqual(feed([(T0, 28)]), {})
        self.assertEqual(feed([(T0 + HOUR, 34)], opened, thirsty), opened)
        self.assertEqual(feed([(T0 + HOUR, 35)], opened, thirsty), {})


class RowTests(unittest.TestCase):
    def test_a_dry_plant_has_one_admin_row_that_names_the_reading_and_the_level(self):
        st = feed([(T0, 12)])
        row, _ = row_of(st, 12.0)
        self.assertEqual((row["id"], row["kind"], row["title"], row["detail"]),
                         ("maint:plant:strelitzia", "maint", "Water the Strelitzia", "soil 12%, water at 15%"))
        self.assertEqual((row["icon"], row["area"], row["audience"]),
                         ("mdi:watering-can-outline", "dining_room", "admin"))
        self.assertTrue(core.is_valid_row(row))

    def test_the_row_is_untimed_and_has_no_sheet_since_the_moisture_rising_closes_it(self):
        row, _ = row_of(feed([(T0, 12)]), 12.0)
        self.assertEqual(set(row), {"id", "kind", "icon", "title", "detail", "area", "audience"})

    def test_the_row_follows_the_reading_while_the_latch_is_open(self):
        st = feed([(T0, 12)])
        self.assertEqual(row_of(st, 17.0)[0]["detail"], "soil 17%, water at 15%")
        self.assertEqual(row_of(st, 12.5)[0]["detail"], "soil 12.5%, water at 15%")
        self.assertEqual(row_of(st, 12.34)[0]["detail"], "soil 12.3%, water at 15%")
        half = plants.parse_plants([{"moisture": "sensor.fern", "name": "Fern", "dry": 17.5}])["sensor.fern"]
        self.assertEqual(row_of(feed([(T0, 10)], plant=half), 10.0, half, "sensor.fern")[0]["detail"],
                         "soil 10%, water at 17.5%")

    def test_the_title_says_how_many_litres_to_pour_without_trailing_zeros(self):
        amounts = [(3, "3"), (3.0, "3"), (0.5, "0.5"), (0.4, "0.4"), (0.3, "0.3"), (0.25, "0.25"), (1.5, "1.5"),
                   (2.50, "2.5"), (12, "12")]
        for litres, shown in amounts:
            plant = plants.parse_plants([{**RAW[0], "water": litres}])[ENTITY]
            row, _ = row_of(feed([(T0, 12)], plant=plant), 12.0, plant)
            self.assertEqual(row["title"], f"Water the Strelitzia: {shown} L", litres)
            self.assertEqual(row["detail"], "soil 12%, water at 15%", litres)
            self.assertTrue(core.is_valid_row(row), litres)

    def test_without_an_amount_the_title_is_as_it_was(self):
        for cfg in (RAW[0], {**RAW[0], "water": None}):
            plant = plants.parse_plants([cfg])[ENTITY]
            self.assertEqual(row_of(feed([(T0, 12)], plant=plant), 12.0, plant)[0]["title"], "Water the Strelitzia")

    def test_a_long_name_is_cut_before_the_amount_is(self):
        name = "Monstera deliciosa variegata (the big one by the window)"
        plant = plants.parse_plants([{"moisture": "sensor.p", "name": name, "dry": 20, "water": 0.3}])["sensor.p"]
        row, _ = row_of(feed([(T0, 5)], plant=plant), 5.0, plant, "sensor.p")
        self.assertEqual(len(row["title"]), core.TEXT_MAX)
        self.assertTrue(row["title"].startswith("Water the Monstera deliciosa variegata (the big"), row["title"])
        self.assertTrue(row["title"].endswith(": 0.3 L"), row["title"])
        self.assertTrue(core.is_valid_row(row))

    def test_no_row_while_wet_or_while_the_reading_is_unknown(self):
        self.assertIsNone(row_of({}, 40.0))
        self.assertIsNone(row_of(None, 12.0))
        self.assertIsNone(row_of(feed([(T0, 12)]), None))
        self.assertIsNone(row_of({}, None))

    def test_a_room_is_optional(self):
        plant = plants.parse_plants([{"moisture": "sensor.fern", "name": "Fern", "dry": 30}])["sensor.fern"]
        row, _ = row_of(feed([(T0, 12)], plant=plant), 12.0, plant, "sensor.fern")
        self.assertNotIn("area", row)
        self.assertTrue(core.is_valid_row(row))

    def test_awkward_names_still_make_a_valid_row(self):
        names = ("Monstera deliciosa variegata (the big one by the window)", "Ficus  \u00e9lastica", "x" * 60, "A&B")
        for name in names:
            for litres in (None, 0.3, 12.5):
                cfg = {"moisture": "sensor.p", "name": name, "dry": 20, "water": litres}
                plant = plants.parse_plants([cfg])["sensor.p"]
                row, _ = row_of(feed([(T0, 5)], plant=plant), 5.0, plant, "sensor.p")
                self.assertTrue(core.is_valid_row(row), name)
                self.assertLessEqual(len(row["title"]), core.TEXT_MAX)
                self.assertTrue(row["title"].startswith("Water the "), name)
                self.assertEqual(row["title"].endswith(" L"), litres is not None, row["title"])


class PushKeyTests(unittest.TestCase):
    def test_the_key_names_the_sensor_and_the_time_the_latch_opened(self):
        _, key = row_of(feed([(T0, 12)]), 12.0)
        self.assertEqual(key, f"plant:{ENTITY}:2026-10-14T08:00:00+00:00")

    def test_the_key_is_the_same_all_episode_and_new_for_the_next_one(self):
        st = feed([(T0, 12)])
        later = feed([(T0 + DAY, 14), (T0 + 2 * DAY, 18)], st)
        self.assertEqual(row_of(later, 18.0)[1], row_of(st, 12.0)[1])
        watered = feed([(T0 + 3 * DAY, 40)], later)
        next_episode = feed([(T0 + 30 * DAY, 14)], watered)
        self.assertNotEqual(row_of(next_episode, 14.0)[1], row_of(st, 12.0)[1])

    def test_changing_the_amount_does_not_change_the_key(self):
        st = feed([(T0, 12)])
        poured = plants.parse_plants([{**RAW[0], "water": 3}])[ENTITY]
        self.assertEqual(row_of(st, 12.0, poured)[1], row_of(st, 12.0)[1])

    def test_the_digest_names_it_once_an_episode(self):
        row, key = row_of(feed([(T0, 12)]), 12.0)
        conditions = [(row["title"], key)]
        found = maint.digest(conditions, {})
        self.assertEqual((found["title"], found["message"], found["keys"]),
                         ("Maintenance due", "Water the Strelitzia", [key]))
        pushed = {key: core.utc_iso(T0 + DAY)}
        self.assertIsNone(maint.digest(conditions, pushed))
        again_row, again_key = row_of(feed([(T0 + 30 * DAY, 13)]), 13.0)
        self.assertEqual(maint.digest([(again_row["title"], again_key)], pushed)["keys"], [again_key])

    def test_two_plants_have_two_keys_and_two_rows(self):
        fern = plants.parse_plants([{"moisture": "sensor.fern", "name": "Boston fern", "dry": 30}])["sensor.fern"]
        one, other = row_of(feed([(T0, 12)]), 12.0), row_of(feed([(T0, 12)], plant=fern), 12.0, fern, "sensor.fern")
        self.assertNotEqual(one[0]["id"], other[0]["id"])
        self.assertNotEqual(one[1], other[1])
        self.assertEqual(maint.digest([(one[0]["title"], one[1]), (other[0]["title"], other[1])], {})["message"],
                         "Water the Strelitzia, Water the Boston fern")


class ParseTests(unittest.TestCase):
    def test_a_plant_is_its_sensor_name_room_dry_level_and_optional_litres(self):
        self.assertEqual(plants.parse_plants(RAW),
                         {ENTITY: {"name": "Strelitzia", "slug": "strelitzia", "area": "dining_room", "dry": 15.0,
                                   "water": None}})
        self.assertEqual(plants.parse_plants([{**RAW[0], "water": 0.3}])[ENTITY]["water"], 0.3)

    def test_no_plants_is_fine(self):
        for raw in (None, [], ()):
            self.assertEqual(plants.parse_plants(raw), {})

    def test_the_order_of_the_lines_is_kept(self):
        raw = [{"moisture": f"sensor.{name}", "name": name.title(), "dry": 10} for name in ("zz", "aa", "mm")]
        self.assertEqual(list(plants.parse_plants(raw)), ["sensor.zz", "sensor.aa", "sensor.mm"])

    def test_the_row_id_is_the_name_in_lowercase_ascii(self):
        names = {"Strelitzia": "strelitzia", "Monstera Deliciosa": "monstera_deliciosa", "Ficus (big)": "ficus_big",
                 "  Caf\u00e9-Palme  ": "cafe_palme", "Fern #2": "fern_2", "Aloe": "aloe"}
        for name, slug in names.items():
            self.assertEqual(plants.slug(name), slug, name)
            [plant] = plants.parse_plants([{"moisture": "sensor.p", "name": name, "dry": 10}]).values()
            self.assertEqual(plant["slug"], slug)

    def test_typos_fail_at_start(self):
        ok = RAW[0]
        bad = [
            "sensor.strelitzia_moisture",
            None,
            {**ok, "moisture": None},
            {**ok, "moisture": ""},
            {**ok, "moisture": 7},
            {**ok, "name": None},
            {**ok, "name": "   "},
            {**ok, "name": "!!!"},
            {**ok, "name": "\u00e6\u00f8"},
            {**ok, "dry": None},
            {**ok, "dry": "wet"},
            {**ok, "dry": True},
            {**ok, "dry": float("nan")},
            {k: v for k, v in ok.items() if k != "dry"},
            {k: v for k, v in ok.items() if k != "moisture"},
        ]
        for cfg in bad:
            with self.assertRaises(ValueError, msg=cfg):
                plants.parse_plants([cfg])

    def test_water_is_left_out_or_a_positive_number_of_litres(self):
        for litres, expected in ((None, None), (3, 3.0), (0.3, 0.3), (0.01, 0.01), (250, 250.0)):
            self.assertEqual(plants.parse_plants([{**RAW[0], "water": litres}])[ENTITY]["water"], expected, litres)
        for bad in (0, 0.0, -1, -0.5, "lots", "", True, False, float("nan"), float("inf"), [], {}, [1]):
            with self.assertRaises(ValueError, msg=bad):
                plants.parse_plants([{**RAW[0], "water": bad}])

    def test_a_plant_listed_twice_by_sensor_or_by_name_fails_at_start(self):
        same_sensor = [RAW[0], {**RAW[0], "name": "Other"}]
        same_name = [RAW[0], {**RAW[0], "moisture": "sensor.other_moisture", "name": "strelitzia"}]
        same_row = [RAW[0], {**RAW[0], "moisture": "sensor.other_moisture", "name": "Strelitzia!"}]
        for raw in (same_sensor, same_name, same_row):
            with self.assertRaises(ValueError, msg=raw):
                plants.parse_plants(raw)

    def test_dry_may_be_a_fraction_or_zero(self):
        for dry, expected in ((15, 15.0), (17.5, 17.5), (0, 0.0)):
            [plant] = plants.parse_plants([{**RAW[0], "dry": dry}]).values()
            self.assertEqual(plant["dry"], expected, dry)


class CalendarParseTests(unittest.TestCase):
    def test_a_calendar_plant_is_its_name_room_litres_and_interval(self):
        self.assertEqual(plants.parse_timed(CAL),
                         {"monstera": {"name": "Monstera", "slug": "monstera", "area": "kitchen", "water": 1.0,
                                       "every_days": 10}})
        self.assertEqual(plants.parse_timed([{"name": "Aloe", "every_days": 35}])["aloe"],
                         {"name": "Aloe", "slug": "aloe", "area": None, "water": None, "every_days": 35})

    def test_each_kind_of_plant_is_found_in_a_mixed_list_in_the_order_of_the_lines(self):
        raw = [CAL[0], RAW[0], {"name": "Aloe", "every_days": 35}]
        self.assertEqual(list(plants.parse_plants(raw)), [ENTITY])
        self.assertEqual(list(plants.parse_timed(raw)), ["monstera", "aloe"])
        self.assertEqual(plants.parse_plants(CAL), {})
        self.assertEqual(plants.parse_timed(RAW), {})

    def test_a_plant_has_exactly_one_of_moisture_and_every_days(self):
        both = {**RAW[0], "every_days": 10}
        neither = {"name": "Monstera", "area": "kitchen", "water": 1}
        for cfg in (both, neither, {**CAL[0], "moisture": None, "every_days": None}):
            for parse in (plants.parse_plants, plants.parse_timed):
                with self.assertRaises(ValueError, msg=cfg):
                    parse([cfg])

    def test_every_days_is_a_whole_number_of_days_above_zero(self):
        for days, expected in ((1, 1), (7, 7), (35, 35), (10.0, 10)):
            self.assertEqual(plants.parse_timed([{**CAL[0], "every_days": days}])["monstera"]["every_days"], expected)
        for bad in (0, -1, 0.5, 3.5, "weekly", "", True, False, float("nan"), float("inf"), [], {}):
            for parse in (plants.parse_plants, plants.parse_timed):
                with self.assertRaises(ValueError, msg=bad):
                    parse([{**CAL[0], "every_days": bad}])

    def test_dry_goes_with_a_sensor_and_water_is_checked_for_both_kinds(self):
        for cfg in ({**CAL[0], "dry": 20}, {**CAL[0], "water": 0}, {**CAL[0], "water": "lots"},
                    {**CAL[0], "water": -1}):
            with self.assertRaises(ValueError, msg=cfg):
                plants.parse_timed([cfg])
        self.assertIsNone(plants.parse_timed([{**CAL[0], "water": None}])["monstera"]["water"])

    def test_a_renamed_plant_names_its_old_name_in_was(self):
        renamed = {**CAL[0], "name": "Monstera big", "was": "Monstera"}
        self.assertEqual(plants.parse_timed([renamed])["monstera_big"]["was"], "monstera")
        self.assertNotIn("was", plants.parse_timed(CAL)["monstera"])

    def test_was_needs_a_name_goes_with_every_days_and_names_no_plant_that_is_listed(self):
        for cfg in ([{**CAL[0], "was": "!!!"}], [{**CAL[0], "was": ""}], [{**RAW[0], "was": "Old"}],
                    [{**CAL[0], "name": "Monstera big", "was": "Monstera"}, CAL[0]],
                    [{**CAL[0], "name": "Fern", "was": "Strelitzia"}, RAW[0]]):
            for parse in (plants.parse_plants, plants.parse_timed):
                with self.assertRaises(ValueError, msg=cfg):
                    parse(cfg)

    def test_a_name_is_unique_across_both_kinds_because_the_row_id_is(self):
        for raw in ([RAW[0], {**CAL[0], "name": "strelitzia"}], [CAL[0], {**CAL[0], "name": "MONSTERA!"}],
                    [CAL[0], {**RAW[0], "name": "Monstera"}]):
            for parse in (plants.parse_plants, plants.parse_timed):
                with self.assertRaises(ValueError, msg=raw):
                    parse(raw)


class CalendarRowTests(unittest.TestCase):
    """Dates in house time: 22:00 on 20.10.2026 (summer time) and, from 25.10, winter time."""

    NOW = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)

    @staticmethod
    def anchor(at, **extra):
        return {"at": core.utc_iso(at), **extra}

    def row(self, anchor, now=None, plant=MONSTERA):
        return plants.timed_row(plant, anchor, now or self.NOW)

    def test_seeding_adds_a_seed_at_now_for_each_plant_without_an_anchor_and_keeps_the_others(self):
        aloe = plants.parse_timed([{"name": "Aloe", "every_days": 35}])["aloe"]
        kept = {"plant:aloe": self.anchor(self.NOW - 3 * DAY)}
        seeded = plants.seed_timed(kept, {"monstera": MONSTERA, "aloe": aloe}, self.NOW)
        self.assertEqual(seeded, {"plant:aloe": kept["plant:aloe"], "plant:monstera": self.anchor(self.NOW, seed=True)})
        self.assertEqual(plants.seed_timed(seeded, {"monstera": MONSTERA}, self.NOW + DAY), seeded)
        self.assertEqual(kept, {"plant:aloe": self.anchor(self.NOW - 3 * DAY)})

    def test_a_renamed_plants_anchor_moves_to_its_new_key_unless_it_has_one(self):
        big = plants.parse_timed([{**CAL[0], "name": "Monstera big", "was": "Monstera"}])
        old, other = self.anchor(self.NOW - 3 * DAY, prev={"at": "x"}), self.anchor(self.NOW)
        saved = {"plant:monstera": old, "plant:fern": other}
        self.assertEqual(plants.migrate_anchors(saved, big), {"plant:monstera_big": old, "plant:fern": other})
        self.assertEqual(saved, {"plant:monstera": old, "plant:fern": other})
        has_one = {"plant:monstera": old, "plant:monstera_big": other}
        self.assertEqual(plants.migrate_anchors(has_one, big), has_one)
        self.assertEqual(plants.migrate_anchors(saved, {"monstera": MONSTERA}), saved)
        for raw in (None, [], "x", 5):
            self.assertEqual(plants.migrate_anchors(raw, big), {})

    def test_a_plant_is_found_by_its_row_id_and_nothing_else_is(self):
        timed = {"monstera": MONSTERA}
        self.assertIs(plants.timed_by_row("maint:plant:monstera", timed), MONSTERA)
        for other in ("maint:plant:strelitzia", "maint:monstera", "plant:monstera", "", None, 5):
            self.assertIsNone(plants.timed_by_row(other, timed), other)

    def test_a_plant_has_no_row_until_the_day_before_it_is_due(self):
        seed = self.anchor(self.NOW, seed=True)
        for days in range(9):
            self.assertIsNone(self.row(seed, self.NOW + days * DAY), days)
        self.assertEqual(plants.LEAD_DAYS, 1)
        for days in (9, 10, 11):
            self.assertIsNotNone(self.row(seed, self.NOW + days * DAY), days)

    def test_the_day_before_the_row_is_dated_at_nine_house_time_and_not_pushed(self):
        row, key = self.row(self.anchor(self.NOW, seed=True), self.NOW + 9 * DAY)
        self.assertEqual((row["id"], row["kind"], row["title"], row["detail"]),
                         ("maint:plant:monstera", "maint", "Water the Monstera: 1 L", "1 day left, 30.10"))
        self.assertEqual((row["at"], row["icon"], row["area"], row["audience"], row["done"]),
                         ("2026-10-30T08:00:00+00:00", "mdi:watering-can-outline", "kitchen", "admin", "tap"))
        self.assertEqual(row["info"], ["Every 10 days", "Counting from 20.10"])
        self.assertNotIn("next", row)
        self.assertNotIn("steps", row)
        self.assertIsNone(key)
        self.assertTrue(core.is_valid_row(row))

    def test_summer_time_and_winter_time_move_the_utc_time_not_the_house_time(self):
        week = plants.parse_timed([{"name": "Lily", "every_days": 4}])["lily"]
        for started, shown in ((0, "2026-10-24T07:00:00+00:00"), (1, "2026-10-25T08:00:00+00:00"),
                               (2, "2026-10-26T08:00:00+00:00")):
            anchor = self.anchor(self.NOW + started * DAY)
            self.assertEqual(self.row(anchor, self.NOW + (started + 3) * DAY, week)[0]["at"], shown)

    def test_the_days_are_house_days_not_twenty_four_hours(self):
        daily = plants.parse_timed([{"name": "Fern", "every_days": 1}])["fern"]
        late = datetime(2026, 10, 20, 21, 30, tzinfo=timezone.utc)
        row, key = self.row(self.anchor(late), late + timedelta(minutes=40), daily)
        self.assertEqual(row["detail"], "due today")
        self.assertEqual(row["info"][0], "Every day")
        self.assertIsNotNone(key)
        row, key = self.row(self.anchor(late), late - timedelta(hours=2), daily)
        self.assertEqual((row["detail"], row["at"]), ("1 day left, 21.10", "2026-10-21T07:00:00+00:00"))

    def test_from_the_due_day_the_row_is_untimed_due_and_pushed_once_per_anchor(self):
        seed = self.anchor(self.NOW, seed=True)
        today, key = self.row(seed, self.NOW + 10 * DAY)
        self.assertEqual(today["detail"], "due today")
        self.assertNotIn("at", today)
        self.assertEqual(key, f"plant:monstera:{core.utc_iso(self.NOW)}")
        late, same = self.row(seed, self.NOW + 13 * DAY)
        self.assertEqual(late["detail"], "due since 30.10")
        self.assertEqual(same, key)
        self.assertEqual(late["done"], "tap")
        self.assertTrue(core.is_valid_row(late))

    def test_a_done_takes_the_row_away_until_the_day_before_the_next_one(self):
        watered = self.anchor(self.NOW + 13 * DAY)
        self.assertIsNone(self.row(watered, self.NOW + 13 * DAY))
        self.assertIsNone(self.row(watered, self.NOW + 21 * DAY))
        row, key = self.row(watered, self.NOW + 22 * DAY)
        self.assertEqual((row["detail"], row["info"][1]), ("1 day left, 12.11", "Done 02.11, 9 days ago"))
        self.assertIsNone(key)
        self.assertNotEqual(self.row(watered, self.NOW + 24 * DAY)[1], f"plant:monstera:{core.utc_iso(self.NOW)}")

    def test_the_sheet_says_when_it_was_done_and_how_long_ago(self):
        nine_days_ago = self.row(self.anchor(self.NOW - 9 * DAY))[0]
        self.assertEqual(nine_days_ago["info"], ["Every 10 days", "Done 11.10, 9 days ago"])
        daily = plants.parse_timed([{"name": "Fern", "every_days": 1}])["fern"]
        self.assertEqual(self.row(self.anchor(self.NOW - DAY), plant=daily)[0]["info"][1], "Done 19.10, 1 day ago")
        self.assertEqual(self.row(self.anchor(self.NOW), plant=daily)[0]["info"][1], "Done 20.10, today")
        seeded_again = self.anchor(self.NOW, seed=True, prev={"at": "x"})
        self.assertEqual(self.row(seeded_again, self.NOW + 9 * DAY)[0]["info"][1], "Counting from 20.10")

    def test_a_date_in_another_year_carries_it(self):
        row, _ = self.row(self.anchor(datetime(2026, 12, 22, 12, 0, tzinfo=timezone.utc)),
                          datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(row["detail"], "1 day left, 01.01.27")
        self.assertEqual(row["at"], "2027-01-01T08:00:00+00:00")

    def test_without_an_anchor_or_with_a_broken_one_there_is_no_row(self):
        for anchor in (None, {}, {"at": "soon"}, {"at": 5}):
            self.assertIsNone(self.row(anchor, self.NOW + 11 * DAY), anchor)

    def test_the_title_carries_the_litres_when_there_are_some(self):
        bare = plants.parse_timed([{"name": "Aloe", "every_days": 35}])["aloe"]
        small = plants.parse_timed([{"name": "Aloe", "water": 0.25, "every_days": 35}])["aloe"]
        eve = self.NOW + 34 * DAY
        self.assertEqual(self.row(self.anchor(self.NOW), eve, bare)[0]["title"], "Water the Aloe")
        self.assertEqual(self.row(self.anchor(self.NOW), eve, small)[0]["title"], "Water the Aloe: 0.25 L")

    def test_the_digest_names_a_due_plant_once_an_episode(self):
        seed = self.anchor(self.NOW, seed=True)
        row, key = self.row(seed, self.NOW + 11 * DAY)
        found = maint.digest([(row["title"], key)], {})
        self.assertEqual((found["message"], found["keys"]), ("Water the Monstera: 1 L", [key]))
        self.assertIsNone(maint.digest([(row["title"], key)], {key: core.utc_iso(self.NOW + 11 * DAY)}))


class SavedLatchTests(unittest.TestCase):
    OPEN = {"since": "2026-10-14T08:00:00+00:00"}

    def test_a_latch_with_its_time_for_a_listed_plant_is_kept(self):
        raw = {ENTITY: {**self.OPEN, "extra": "dropped"}}
        self.assertEqual(plants.valid_latches(raw, {ENTITY: PLANT}), {ENTITY: self.OPEN})

    def test_a_latch_for_a_plant_that_is_no_longer_listed_is_dropped(self):
        self.assertEqual(plants.valid_latches({"sensor.gone_moisture": self.OPEN}, {ENTITY: PLANT}), {})

    def test_a_latch_without_a_time_that_parses_is_dropped(self):
        raw = {ENTITY: {"since": "yesterday"}, "sensor.a": {}, "sensor.b": self.OPEN["since"], "sensor.c": None,
               "sensor.d": {"since": 5}, "sensor.e": {"since": ""}}
        self.assertEqual(plants.valid_latches(raw, {entity: PLANT for entity in raw}), {})

    def test_a_missing_or_broken_section_is_no_latches(self):
        for raw in (None, [], "x", 5):
            self.assertEqual(plants.valid_latches(raw, {ENTITY: PLANT}), {})


if __name__ == "__main__":
    unittest.main()
