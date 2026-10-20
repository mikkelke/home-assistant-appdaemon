# tests/test_upcoming_plants.py - watering reminders for sensor.house_upcoming: the dry latch and its
# hysteresis, the row and its push key, and the plants list of house_upcoming.yaml.
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
            plant = plants.parse_plants([{"moisture": "sensor.p", "name": name, "dry": 20}])["sensor.p"]
            row, _ = row_of(feed([(T0, 5)], plant=plant), 5.0, plant, "sensor.p")
            self.assertTrue(core.is_valid_row(row), name)
            self.assertLessEqual(len(row["title"]), core.TEXT_MAX)
            self.assertTrue(row["title"].startswith("Water the "), name)


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
    def test_a_plant_is_its_sensor_name_room_and_dry_level(self):
        self.assertEqual(plants.parse_plants(RAW),
                         {ENTITY: {"name": "Strelitzia", "slug": "strelitzia", "area": "dining_room", "dry": 15.0}})

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
