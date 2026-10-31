# tests/test_upcoming_plants.py - watering reminders for sensor.house_upcoming: the dry latch and its
# hysteresis, the row with the litres to pour and its push key, the plants watered by the calendar, the
# one row for the plants due now, and the plants list of house_upcoming.yaml.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import upcoming_core as core  # noqa: E402
import upcoming_maint as maint  # noqa: E402
import upcoming_plants as plants  # noqa: E402

T0 = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
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
                                   "water": None, "info": [], "aka": None, "where": None, "photo": None,
                                   "conductivity": None}})
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


class FeedTitleTests(unittest.TestCase):
    def test_the_dose_is_a_teaspoon_a_litre_to_the_nearest_quarter_and_at_least_a_quarter(self):
        table = {0.05: "1/4", 0.1: "1/4", 0.2: "1/4", 0.3: "1/4", 0.4: "1/2", 0.5: "1/2", 0.6: "1/2", 0.7: "3/4",
                 0.8: "3/4", 1: "1", 1.5: "1 1/2", 2.25: "2 1/4", 3: "3", 3.0: "3", 10: "10"}
        for litres, expected in table.items():
            self.assertEqual(plants.tsp_text(litres), expected, litres)

    def test_a_dry_plant_due_for_feed_says_so_in_the_title_at_its_dose(self):
        for water, title in ((3, "Water the Strelitzia: 3 L + 3 tsp fertiliser"),
                             (0.3, "Water the Strelitzia: 0.3 L + 1/4 tsp fertiliser"),
                             (0.5, "Water the Strelitzia: 0.5 L + 1/2 tsp fertiliser"),
                             (0.7, "Water the Strelitzia: 0.7 L + 3/4 tsp fertiliser"),
                             (1, "Water the Strelitzia: 1 L + 1 tsp fertiliser")):
            plant = plants.parse_plants([{**RAW[0], "water": water}])[ENTITY]
            row, _ = plants.plant_row(ENTITY, plant, feed([(T0, 12)], plant=plant), 12.0, 90)
            self.assertEqual(row["title"], title, water)
            self.assertEqual(row["detail"], "soil 12%, water at 15%")
            self.assertTrue(core.is_valid_row(row), water)
    def test_a_long_name_gives_up_the_word_before_it_gives_up_the_name_and_the_dose_before_neither(self):
        def title(name, water=1):
            plant = plants.parse_plants([{"moisture": "sensor.p", "name": name, "dry": 20, "water": water}])["sensor.p"]
            return plants.plant_row("sensor.p", plant, {"since": "x"}, 5.0, 10)[0]["title"]

        self.assertEqual(title("Fiddle leaf fig bedroom"), "Water the Fiddle leaf fig bedroom: 1 L + 1 tsp fertiliser")
        medium = title("Monstera deliciosa variegata big")
        self.assertEqual(medium, "Water the Monstera deliciosa variegata big: 1 L + 1 tsp feed")
        long_name = title("Monstera deliciosa variegata (the big one by the window)", 0.5)
        self.assertEqual(len(long_name), core.TEXT_MAX)
        self.assertTrue(long_name.startswith("Water the Monstera deliciosa"), long_name)
        self.assertTrue(long_name.endswith(": 0.5 L + 1/2 tsp feed"), long_name)

    def test_without_an_amount_the_feed_is_named_but_not_dosed(self):
        plant = plants.parse_plants([{"moisture": "sensor.p", "name": "Fern", "dry": 20}])["sensor.p"]
        self.assertEqual(plants.plant_row("sensor.p", plant, {"since": "x"}, 5.0, 10)[0]["title"],
                         "Water the Fern + fertiliser")

    def test_a_sensor_plant_is_due_for_feed_under_350_microsiemens_and_not_at_it_or_without_a_reading(self):
        st = feed([(T0, 12)])
        for conductivity, fed in ((90, True), (349.9, True), (0, True), (350, False), (807, False), (None, False)):
            row, _ = plants.plant_row(ENTITY, PLANT, st, 12.0, conductivity)
            self.assertEqual("fertiliser" in row["title"] or "feed" in row["title"], fed, conductivity)
        self.assertEqual(plants.FEED_BELOW, 350)

    def test_the_predicate_is_the_same_under_350_and_says_nothing_without_a_reading(self):
        for conductivity, expected in ((0, True), (90, True), (349.9, True), (350, False), (807, False), (None, False)):
            self.assertIs(plants.soil_feeds(conductivity), expected, conductivity)

    def test_the_title_without_feed_is_as_it_was(self):
        row, _ = plants.plant_row(ENTITY, PLANT, feed([(T0, 12)]), 12.0)
        self.assertEqual(row["title"], "Water the Strelitzia")


class SensorParseTests(unittest.TestCase):
    def test_a_sensor_plant_may_name_its_conductivity_sensor(self):
        cfg = {**RAW[0], "conductivity": "sensor.strelitzia_conductivity"}
        self.assertEqual(plants.parse_plants([cfg])[ENTITY]["conductivity"], "sensor.strelitzia_conductivity")
        self.assertIsNone(plants.parse_plants(RAW)[ENTITY]["conductivity"])

    def test_the_conductivity_sensor_is_an_entity_id_listed_once_and_only_on_a_sensor_plant(self):
        other = {"moisture": "sensor.other_moisture", "name": "Other", "dry": 10}
        for cfgs in ([{**RAW[0], "conductivity": ""}], [{**RAW[0], "conductivity": 5}],
                     [{**RAW[0], "conductivity": ENTITY}],
                     [{**RAW[0], "conductivity": "sensor.ec"}, {**other, "conductivity": "sensor.ec"}],
                     [{**RAW[0], "conductivity": "sensor.other_moisture"}, other],
                     [{**CAL[0], "conductivity": "sensor.ec"}]):
            for parse in (plants.parse_plants, plants.parse_timed):
                with self.assertRaises(ValueError, msg=cfgs):
                    parse(cfgs)

    def test_feed_is_summer_or_left_out_and_only_on_a_calendar_plant(self):
        self.assertEqual(plants.parse_timed([{**CAL[0], "feed": "summer"}])["monstera"]["feed"], "summer")
        for cfg in ({**CAL[0], "feed": "winter"}, {**CAL[0], "feed": True}, {**CAL[0], "feed": ""},
                    {**RAW[0], "feed": "summer"}):
            for parse in (plants.parse_plants, plants.parse_timed):
                with self.assertRaises(ValueError, msg=cfg):
                    parse([cfg])


class CalendarFeedTests(unittest.TestCase):
    """The watering a row is for is on its due day, or today once that has passed."""

    def plant(self, **extra):
        return plants.parse_timed([{**CAL[0], **extra}])["monstera"]

    @staticmethod
    def anchor(at, **extra):
        return {"at": core.utc_iso(at), **extra}

    def feeds(self, plant, anchor, now):
        return plants.feeds(plant, anchor, now)

    def test_from_march_to_september_every_watering_comes_with_feed(self):
        june = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        anchor = self.anchor(june, seed=True)
        self.assertTrue(self.feeds(self.plant(), anchor, june + 9 * DAY))
        fed_yesterday = self.anchor(june, fed_at=core.utc_iso(june + 8 * DAY))
        self.assertTrue(self.feeds(self.plant(), fed_yesterday, june + 9 * DAY))

    def test_in_the_other_months_a_watering_comes_with_feed_after_twenty_eight_days_or_if_never_fed(self):
        fed = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
        anchor = self.anchor(fed, seed=True)
        for days, expected in ((9, False), (10, False), (26, False), (27, False), (28, True), (35, True)):
            self.assertEqual(self.feeds(self.plant(), anchor, fed + days * DAY), expected, days)
        never = self.anchor(fed)
        self.assertTrue(self.feeds(self.plant(), never, fed + 9 * DAY))
        self.assertFalse(self.feeds(self.plant(), self.anchor(fed, fed_at=core.utc_iso(fed + 8 * DAY)), fed + 9 * DAY))

    def test_the_row_for_the_eve_is_for_the_due_day_so_the_title_does_not_flip_when_it_is_reached(self):
        fed = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
        exactly = self.anchor(fed + 18 * DAY, fed_at=core.utc_iso(fed))   # watered 07.11, due 17.11: 28 days after
        for days in (24, 26, 27, 30):
            self.assertTrue(self.feeds(self.plant(), exactly, fed + days * DAY), days)
        short = self.anchor(fed + 17 * DAY, fed_at=core.utc_iso(fed))      # watered 06.11, due 16.11: 27 days after
        for days in (24, 25, 26):
            self.assertFalse(self.feeds(self.plant(), short, fed + days * DAY), days)
        self.assertTrue(self.feeds(self.plant(), short, fed + 28 * DAY))   # two days late: the watering is today
    def test_a_succulent_is_fed_only_from_march_to_september_and_then_at_most_every_twenty_eight_days(self):
        cactus = self.plant(feed="summer", every_days=42)
        winter = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
        self.assertFalse(self.feeds(cactus, self.anchor(winter, seed=True), winter + 41 * DAY))
        self.assertFalse(self.feeds(cactus, self.anchor(winter), winter + 41 * DAY))
        spring = datetime(2027, 3, 1, 10, 0, tzinfo=timezone.utc)
        old = self.anchor(spring, fed_at=core.utc_iso(spring - 60 * DAY))
        self.assertTrue(self.feeds(cactus, old, spring + 41 * DAY))
        self.assertTrue(self.feeds(cactus, self.anchor(spring), spring + 41 * DAY))
        recent = self.anchor(spring, fed_at=core.utc_iso(spring + 20 * DAY))
        self.assertFalse(self.feeds(cactus, recent, spring + 41 * DAY))
        week = self.plant(feed="summer", every_days=7)
        june = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        for watered, expected in ((0, False), (14, False), (20, False), (21, True)):
            # watered `watered` days after the feeding on 01.06, so due 7 days later
            anchor = self.anchor(june + watered * DAY, fed_at=core.utc_iso(june))
            self.assertEqual(self.feeds(week, anchor, june + (watered + 7) * DAY), expected, watered)
    def test_the_seasons_switch_on_the_first_of_march_and_the_first_of_october(self):
        daily = self.plant(every_days=1)
        for due, expected in ((datetime(2027, 2, 28, 10, 0, tzinfo=timezone.utc), False),
                              (datetime(2027, 3, 1, 10, 0, tzinfo=timezone.utc), True),
                              (datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc), True),
                              (datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc), False)):
            watered = self.anchor(due - DAY, fed_at=core.utc_iso(due - DAY))
            self.assertEqual(self.feeds(daily, watered, due), expected, due)
    def test_no_anchor_means_no_feed(self):
        self.assertFalse(self.feeds(self.plant(), None, datetime(2026, 6, 1, tzinfo=timezone.utc)))
        self.assertFalse(self.feeds(self.plant(), {"at": "soon"}, datetime(2026, 6, 1, tzinfo=timezone.utc)))

    def test_a_done_records_the_feeding_when_the_row_said_to_feed_and_carries_the_last_one_over_when_it_did_not(self):
        fed = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
        seed = self.anchor(fed, seed=True)
        self.assertEqual(plants.fed_on(seed), fed)
        self.assertEqual(plants.done_fields(self.plant(), seed, fed + 10 * DAY), {"fed_at": core.utc_iso(fed)})
        later = fed + 28 * DAY
        self.assertEqual(plants.done_fields(self.plant(), seed, later), {"fed_at": core.utc_iso(later)})
        carried = self.anchor(fed + 10 * DAY, fed_at=core.utc_iso(fed))
        self.assertEqual(plants.fed_on(carried), fed)
        self.assertEqual(plants.done_fields(self.plant(), carried, fed + 20 * DAY), {"fed_at": core.utc_iso(fed)})
        never = self.anchor(fed)
        self.assertIsNone(plants.fed_on(never))
        self.assertEqual(plants.done_fields(self.plant(feed="summer"), never, fed + 10 * DAY), {})

    def test_the_calendar_row_title_says_the_feed(self):
        now = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
        plant = self.plant()
        quiet, _ = plants.timed_row(plant, self.anchor(now, seed=True), now + 9 * DAY)
        self.assertEqual(quiet["title"], "Water the Monstera: 1 L")
        fed, _ = plants.timed_row(plant, self.anchor(now - 20 * DAY, seed=True), now + 9 * DAY)
        self.assertEqual(fed["title"], "Water the Monstera: 1 L + 1 tsp fertiliser")
        self.assertTrue(core.is_valid_row(fed))


class NoteTests(unittest.TestCase):
    LOOK, STANDS = "Bird of paradise, the big banana-leaf plant", "Floor by the facade, between kitchen and dining"

    def test_what_a_plant_looks_like_and_where_it_stands_become_its_info_lines_in_that_order(self):
        both = {**RAW[0], "where": self.STANDS, "aka": self.LOOK}
        self.assertEqual(plants.parse_plants([both])[ENTITY]["info"], [self.LOOK, self.STANDS])
        self.assertEqual(plants.parse_timed([{**CAL[0], "where": self.STANDS, "aka": self.LOOK}])["monstera"]["info"],
                         [self.LOOK, self.STANDS])
        self.assertEqual(plants.parse_plants([{**RAW[0], "where": self.STANDS}])[ENTITY]["info"], [self.STANDS])
        self.assertEqual(plants.parse_plants([{**RAW[0], "aka": self.LOOK}])[ENTITY]["info"], [self.LOOK])
        self.assertEqual(plants.parse_plants([{**RAW[0], "aka": None, "where": None}])[ENTITY]["info"], [])

    def test_the_notes_and_the_photo_are_also_kept_apart_for_the_record_of_the_plant(self):
        raw = {"where": self.STANDS, "aka": self.LOOK, "photo": "bird_of_paradise"}
        for parse, cfg, found in ((plants.parse_plants, RAW[0], lambda parsed: parsed[ENTITY]),
                                  (plants.parse_timed, CAL[0], lambda parsed: parsed["monstera"])):
            plant = found(parse([{**cfg, **raw}]))
            self.assertEqual((plant["aka"], plant["where"], plant["photo"]),
                             (self.LOOK, self.STANDS, "bird_of_paradise"))
            bare = found(parse([cfg]))
            self.assertEqual((bare["aka"], bare["where"], bare["photo"], bare["info"]), (None, None, None, []))
            only = found(parse([{**cfg, "where": self.STANDS}]))
            self.assertEqual((only["aka"], only["where"], only["info"]), (None, self.STANDS, [self.STANDS]))

    def test_a_note_is_plain_text_on_one_line_of_at_most_sixty_characters(self):
        self.assertEqual(plants.parse_plants([{**RAW[0], "aka": "x" * 60}])[ENTITY]["info"], ["x" * 60])
        for bad in ("x" * 61, "", " ", " padded", "two  spaces", "line\nbreak", "Br\u00f8nd", 5, True, ["a"], {}):
            for key in ("aka", "where"):
                for parse, cfg in ((plants.parse_plants, RAW[0]), (plants.parse_timed, CAL[0])):
                    with self.assertRaises(ValueError, msg=(key, bad)):
                        parse([{**cfg, key: bad}])

    def test_the_sensor_row_leads_with_the_notes_and_has_none_without_them(self):
        plant = plants.parse_plants([{**RAW[0], "aka": self.LOOK, "where": self.STANDS}])[ENTITY]
        row, _ = row_of(feed([(T0, 12)], plant=plant), 12.0, plant)
        self.assertEqual(row["info"], [self.LOOK, self.STANDS])
        self.assertTrue(core.is_valid_row(row))
        self.assertNotIn("info", row_of(feed([(T0, 12)]), 12.0)[0])

    def test_the_calendar_row_leads_with_the_notes_before_the_interval_and_the_last_watering(self):
        plant = plants.parse_timed([{**CAL[0], "aka": self.LOOK, "where": self.STANDS}])["monstera"]
        now = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
        row, _ = plants.timed_row(plant, {"at": core.utc_iso(now), "seed": True}, now + 9 * DAY)
        self.assertEqual(row["info"], [self.LOOK, self.STANDS, "Every 10 days", "Counting from 20.10"])
        self.assertTrue(core.is_valid_row(row))
        bare, _ = plants.timed_row(MONSTERA, {"at": core.utc_iso(now), "seed": True}, now + 9 * DAY)
        self.assertEqual(bare["info"], ["Every 10 days", "Counting from 20.10"])


class PhotoTests(unittest.TestCase):
    def test_a_photo_is_a_key_of_lowercase_letters_digits_and_underscores(self):
        for key in ("strelitzia", "snake_plant_moonshine", "peace_lily", "fern2", "2", "_", "a" * 60):
            self.assertEqual(plants.parse_plants([{**RAW[0], "photo": key}])[ENTITY]["photo"], key, key)
            self.assertEqual(plants.parse_timed([{**CAL[0], "photo": key}])["monstera"]["photo"], key, key)

    def test_it_is_optional(self):
        for cfg in (RAW[0], {**RAW[0], "photo": None}):
            self.assertIsNone(plants.parse_plants([cfg])[ENTITY]["photo"])
        for cfg in (CAL[0], {**CAL[0], "photo": None}):
            self.assertIsNone(plants.parse_timed([cfg])["monstera"]["photo"])

    def test_anything_else_fails_at_start_in_the_words_of_the_other_fields(self):
        bad = ("", " ", "Strelitzia", "snake-plant", "snake plant", "peace.lily", "caf\u00e9", "fern\n", " fern",
               "a" * 61, 5, True, False, ["fern"], {}, 0)
        for key in bad:
            for parse, cfg in ((plants.parse_plants, RAW[0]), (plants.parse_timed, CAL[0])):
                with self.assertRaisesRegex(ValueError, r"^plant '[^']+': photo must be a key of lowercase letters",
                                            msg=repr(key)):
                    parse([{**cfg, "photo": key}])

    def test_a_row_does_not_carry_the_photo(self):
        plant = plants.parse_plants([{**RAW[0], "photo": "strelitzia"}])[ENTITY]
        row, _ = row_of(feed([(T0, 12)], plant=plant), 12.0, plant)
        self.assertNotIn("photo", row)
        self.assertTrue(core.is_valid_row(row))


class CalendarParseTests(unittest.TestCase):
    def test_a_calendar_plant_is_its_name_room_litres_and_interval(self):
        self.assertEqual(plants.parse_timed(CAL),
                         {"monstera": {"name": "Monstera", "slug": "monstera", "area": "kitchen", "water": 1.0,
                                       "every_days": 10, "info": [], "aka": None, "where": None, "photo": None,
                                       "feed": None}})
        self.assertEqual(plants.parse_timed([{"name": "Aloe", "every_days": 35}])["aloe"],
                         {"name": "Aloe", "slug": "aloe", "area": None, "water": None, "every_days": 35, "info": [],
                          "aka": None, "where": None, "photo": None, "feed": None})

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
        self.assertNotIn("items", row)
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
        bare = plants.parse_timed([{"name": "Aloe", "feed": "summer", "every_days": 35}])["aloe"]
        small = plants.parse_timed([{"name": "Aloe", "feed": "summer", "water": 0.25, "every_days": 35}])["aloe"]
        eve = self.NOW + 34 * DAY
        self.assertEqual(self.row(self.anchor(self.NOW), eve, bare)[0]["title"], "Water the Aloe")
        self.assertEqual(self.row(self.anchor(self.NOW), eve, small)[0]["title"], "Water the Aloe: 0.25 L")

    def test_the_digest_names_a_due_plant_once_an_episode(self):
        seed = self.anchor(self.NOW, seed=True)
        row, key = self.row(seed, self.NOW + 11 * DAY)
        found = maint.digest([(row["title"], key)], {})
        self.assertEqual((found["message"], found["keys"]), ("Water the Monstera: 1 L", [key]))
        self.assertIsNone(maint.digest([(row["title"], key)], {key: core.utc_iso(self.NOW + 11 * DAY)}))


class RoundRowTests(unittest.TestCase):
    """The one row for the plants due now: 18:00 on 31.10.2026 in the house."""

    NOW = datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc)
    NOTES = {"where": "Kitchen floor by the dock", "aka": "Big leaves with holes", "photo": "monstera"}

    def sensor(self, name="Strelitzia", reading=12, conductivity=None, **cfg):
        entity = f"sensor.{plants.slug(name)}_moisture"
        [plant] = plants.parse_plants([{"moisture": entity, "name": name, "dry": 15, **cfg}]).values()
        row, _ = plants.plant_row(entity, plant, {"since": core.utc_iso(self.NOW)}, float(reading), conductivity)
        return row, plant, plants.soil_feeds(conductivity)

    def calendar(self, name="Monstera", over=0, fed=1, **cfg):
        """A calendar plant `over` days past its due day, last fed `fed` days ago (None: never)."""
        [plant] = plants.parse_timed([{"name": name, "every_days": 10, **cfg}]).values()
        anchor = {"at": core.utc_iso(self.NOW - (10 + over) * DAY)}
        if fed is not None:
            anchor["fed_at"] = core.utc_iso(self.NOW - fed * DAY)
        row, _ = plants.timed_row(plant, anchor, self.NOW)
        return row, plant, plants.feeds(plant, anchor, self.NOW)

    def names(self, row):
        return [record["name"] for record in row["plants"]]

    def test_nothing_due_is_no_row(self):
        self.assertIsNone(plants.round_row([]))
        self.assertIsNone(plants.round_row(()))

    def test_one_due_plant_gives_a_row_with_the_plants_own_title_and_one_record(self):
        row = plants.round_row([self.sensor(water=3)])
        self.assertEqual((row["id"], row["kind"], row["icon"], row["audience"], row["title"], row["detail"]),
                         ("maint:plants", "maint", "mdi:watering-can-outline", "admin", "Water the Strelitzia: 3 L",
                          "Strelitzia"))
        self.assertEqual(row["plants"], [{"id": "maint:plant:strelitzia", "name": "Strelitzia", "amount": "3 L",
                                          "detail": "soil 12%, water at 15%"}])
        self.assertEqual(set(row), {"id", "kind", "icon", "title", "detail", "audience", "plants"})
        self.assertTrue(core.is_valid_row(row))

    def test_the_plants_own_title_is_kept_whole_with_its_feed_or_its_cut_name(self):
        row = plants.round_row([self.sensor(water=3, conductivity=90)])
        self.assertEqual(row["title"], "Water the Strelitzia: 3 L + 3 tsp fertiliser")
        name = "Monstera deliciosa variegata (the big one by the window)"
        row = plants.round_row([self.sensor(name, water=0.5, conductivity=90)])
        self.assertEqual(len(row["title"]), core.TEXT_MAX)
        self.assertTrue(row["title"].endswith(": 0.5 L + 1/2 tsp feed"), row["title"])
        self.assertTrue(core.is_valid_row(row))

    def test_several_due_plants_are_counted_in_the_title_and_named_in_the_detail(self):
        row = plants.round_row([self.calendar("Monstera", area="kitchen", water=1), self.sensor(area="dining_room"),
                                self.calendar("Alocasia", over=2, area="bathroom")])
        self.assertEqual((row["title"], row["detail"]), ("Water 3 plants", "Monstera, Strelitzia, Alocasia"))
        self.assertEqual(self.names(row), ["Monstera", "Strelitzia", "Alocasia"])
        self.assertEqual(set(row), {"id", "kind", "icon", "title", "detail", "audience", "plants"})
        self.assertTrue(core.is_valid_row(row))

    def test_the_row_has_no_time_no_lines_no_room_and_no_sheet(self):
        row = plants.round_row([self.calendar(area="kitchen"), self.sensor(area="dining_room")])
        for field in ("at", "items", "area", "info", "steps", "warn", "done", "next", "chip"):
            self.assertNotIn(field, row)

    def test_the_records_go_in_the_order_of_a_walk_through_the_flat_and_then_by_name(self):
        self.assertEqual(plants.WALK, ("kitchen", "living_room", "dining_room", "hallway", "bedroom", "bathroom"))
        spec = [("Bath", "bathroom"), ("Zzz", "guest_bathroom"), ("Bed", "bedroom"), ("Hall", "hallway"),
                ("banana", "kitchen"), ("Dining", "dining_room"), ("Aaa", "office"), ("Living", "living_room"),
                ("Cherry", "kitchen"), ("Nowhere", None), ("Apple", "kitchen")]
        row = plants.round_row([self.calendar(name, area=area) for name, area in spec])
        self.assertEqual(self.names(row), ["Apple", "banana", "Cherry", "Living", "Dining", "Hall", "Bed", "Bath",
                                           "Aaa", "Nowhere", "Zzz"])
        reverse = plants.round_row([self.calendar(name, area=area) for name, area in reversed(spec)])
        self.assertEqual(reverse["plants"], row["plants"])
        self.assertEqual(row["detail"], maint.fit_parts(self.names(row)))
        self.assertTrue(row["detail"].startswith("Apple, banana, Cherry, Living, Dining"), row["detail"])

    def test_a_record_has_the_plants_name_its_rows_id_and_its_rows_reason(self):
        row = plants.round_row([self.calendar(area="kitchen", **self.NOTES),
                                self.calendar("Fern", over=2, area="hallway")])
        self.assertEqual(row["plants"][0], {"id": "maint:plant:monstera", "name": "Monstera", "detail": "due today",
                                            "done": "tap", **self.NOTES})
        self.assertEqual((row["plants"][1]["id"], row["plants"][1]["detail"]), ("maint:plant:fern", "due since 29.10"))

    def test_a_calendar_plant_can_be_tapped_done_and_a_sensor_plant_cannot(self):
        row = plants.round_row([self.calendar(area="kitchen"), self.sensor(area="dining_room")])
        self.assertEqual([record.get("done") for record in row["plants"]], ["tap", None])
        self.assertTrue(core.is_valid_row(row))

    def test_the_notes_and_the_photo_are_in_the_record_when_the_plant_has_them(self):
        row = plants.round_row([self.sensor(**self.NOTES), self.sensor("Ivy", where="Top of the wine fridge"),
                                self.sensor("Pothos")])
        by_name = {record["name"]: record for record in row["plants"]}
        self.assertEqual({key: by_name["Strelitzia"][key] for key in self.NOTES}, self.NOTES)
        self.assertEqual({key for key in by_name["Ivy"] if key in self.NOTES}, {"where"})
        self.assertEqual({key for key in by_name["Pothos"] if key in self.NOTES}, set())

    def test_the_amount_is_the_part_of_the_title_after_the_name(self):
        for water in (0.1, 0.3, 0.5, 1, 2.5, 3):
            for conductivity in (None, 90):
                row = plants.round_row([self.sensor(water=water, conductivity=conductivity)])
                [record] = row["plants"]
                self.assertEqual(row["title"], f"Water the Strelitzia: {record['amount']}", (water, conductivity))
                self.assertEqual("fertiliser" in record["amount"], conductivity is not None)
        self.assertEqual(plants.round_row([self.sensor(water=0.3, conductivity=90)])["plants"][0]["amount"],
                         "0.3 L + 1/4 tsp fertiliser")
        self.assertEqual(plants.round_row([self.calendar(water=1, fed=None)])["plants"][0]["amount"],
                         "1 L + 1 tsp fertiliser")
        self.assertEqual(plants.round_row([self.calendar(water=1, fed=1)])["plants"][0]["amount"], "1 L")

    def test_a_long_name_does_not_shorten_the_amount_the_title_had_to_shorten(self):
        name = "Monstera deliciosa variegata big"
        row = plants.round_row([self.sensor(name, water=1, conductivity=90)])
        self.assertTrue(row["title"].endswith("1 L + 1 tsp feed"), row["title"])
        self.assertEqual(row["plants"][0]["amount"], "1 L + 1 tsp fertiliser")

    def test_a_plant_without_an_amount_has_no_amount_even_when_it_is_due_for_feed(self):
        sensor, calendar = self.sensor(conductivity=90), self.calendar("Aloe", fed=None)
        self.assertEqual(sensor[0]["title"], "Water the Strelitzia + fertiliser")
        self.assertEqual((sensor[2], calendar[2]), (True, True))
        row = plants.round_row([sensor, calendar])
        self.assertEqual(self.names(row), ["Aloe", "Strelitzia"])
        self.assertFalse(any("amount" in record for record in row["plants"]))
        self.assertTrue(core.is_valid_row(row))

    def test_every_text_goes_through_clean_text(self):
        row = plants.round_row([self.calendar("Caf\u00e9  Palme", area="kitchen")])
        self.assertEqual(row["plants"][0]["name"], "Caf Palme")
        self.assertEqual(row["plants"][0]["id"], "maint:plant:cafe_palme")
        long = plants.round_row([self.calendar("x" * 60, **self.NOTES), self.calendar("y" * 60)])
        self.assertTrue(all(len(record["name"]) == core.TEXT_MAX for record in long["plants"]))
        self.assertTrue(len(long["detail"]) <= core.TEXT_MAX)
        self.assertTrue(core.is_valid_row(long))

    def test_a_plant_with_a_long_name_still_has_its_whole_row_id_in_the_record(self):
        name = "Monstera deliciosa variegata (the big one by the window)"
        row = plants.round_row([self.calendar(name)])
        self.assertEqual(row["plants"][0]["id"], "maint:plant:monstera_deliciosa_variegata_the_big_one_by_the_window")
        self.assertTrue(core.is_valid_row(row))

    def test_the_detail_names_as_many_plants_as_fit(self):
        due = [self.calendar(f"Plant number {i:02d}", area="kitchen") for i in range(12)]
        row = plants.round_row(due)
        self.assertEqual(row["title"], "Water 12 plants")
        self.assertEqual(row["detail"], maint.fit_parts(self.names(row)))
        self.assertLessEqual(len(row["detail"]), core.TEXT_MAX)
        self.assertTrue(row["detail"].startswith("Plant number 00, Plant number 01"))

    def test_the_list_is_capped_like_the_items_but_the_title_counts_every_plant(self):
        due = [self.calendar(f"Plant {i:02d}", area="kitchen") for i in range(25)]
        row = plants.round_row(due)
        self.assertEqual(row["title"], "Water 25 plants")
        self.assertEqual(self.names(row), [f"Plant {i:02d}" for i in range(core.ITEMS_MAX)])
        self.assertTrue(core.is_valid_row(row))

    def test_the_rows_it_is_given_are_left_as_they_are(self):
        due = [self.calendar(**self.NOTES), self.sensor(conductivity=90)]
        before = copy.deepcopy(due)
        plants.round_row(due)
        self.assertEqual(due, before)


class RiseTests(unittest.TestCase):
    """A watering is a rise of ten points or more within half an hour that has stopped climbing for fifteen
    minutes."""

    T = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)

    def feed(self, readings, until):
        """Looks at the soil every minute up to minute `until`: the reading where `readings` (minute, value)
        has one, else the last one again, as a sensor that has not changed. Returns ([(minute, event)], state)."""
        steps, st, found, value = dict(readings), None, [], None
        for minute in range(until + 1):
            value = steps.get(minute, value)
            st, event = plants.step_rise(st, value, self.T + minute * MIN)
            if event:
                found.append((minute, event))
        return found, st

    def test_a_rise_that_has_stopped_climbing_is_one_watering_told_with_its_lowest_and_highest_reading(self):
        found, _ = self.feed([(0, 15), (5, 28), (10, 40), (15, 42)], until=60)
        self.assertEqual(found, [(30, {"at": core.utc_iso(self.T + 5 * MIN), "low": 15, "peak": 42})])

    def test_it_is_told_only_when_fifteen_minutes_have_passed_without_a_higher_reading(self):
        found, _ = self.feed([(0, 15), (5, 28), (10, 40), (15, 42)], until=29)
        self.assertEqual(found, [])
        found, _ = self.feed([(0, 15), (5, 28), (10, 40), (15, 42), (20, 43)], until=34)
        self.assertEqual(found, [])
        found, _ = self.feed([(0, 15), (5, 28), (10, 40), (15, 42), (20, 43)], until=35)
        self.assertEqual([minute for minute, _ in found], [35])
        self.assertEqual(found[0][1]["peak"], 43)

    def test_less_than_ten_points_or_a_climb_slower_than_the_window_is_no_watering(self):
        self.assertEqual(self.feed([(0, 15), (5, 24)], until=60)[0], [])
        self.assertEqual(self.feed([(0, 15), (20, 20), (40, 25), (60, 30)], until=120)[0], [])
        self.assertEqual(self.feed([(0, 15), (5, 25)], until=60)[0][0][1]["peak"], 25)
        self.assertEqual(self.feed([(0, 25), (5, 15)], until=60)[0], [])

    def test_a_reading_that_never_changes_or_a_sensor_that_drops_out_changes_nothing(self):
        self.assertEqual(self.feed([(0, 15)], until=120)[0], [])
        found, _ = self.feed([(0, 15), (5, 30), (10, None), (15, None)], until=60)
        self.assertEqual([event["peak"] for _, event in found], [30])

    def test_a_watering_is_told_once_and_the_next_one_later_is_another(self):
        readings = [(0, 15), (5, 40), (60, 30), (65, 20), (120, 12), (125, 35)]
        found, _ = self.feed(readings, until=180)
        self.assertEqual([(minute, event["low"], event["peak"]) for minute, event in found],
                         [(20, 15, 40), (140, 12, 35)])

    def test_the_state_given_is_never_changed_in_place(self):
        st, _ = plants.step_rise(None, 15, self.T)
        before = {"recent": [list(each) for each in st["recent"]], "rise": st["rise"]}
        plants.step_rise(st, 40, self.T + 5 * MIN)
        self.assertEqual(st, before)

    def test_a_moisture_history_gives_its_waterings_oldest_first(self):
        series = [(self.T + minute * MIN, value) for minute, value in (
            (0, 15), (5, 28), (10, 41), (15, 42), (200, 30), (400, 12), (405, 12), (410, 35), (415, 36))]
        found = plants.rises(series, self.T + 600 * MIN)
        self.assertEqual([(event["at"], event["low"], event["peak"]) for event in found], [
            (core.utc_iso(self.T + 5 * MIN), 15, 42), (core.utc_iso(self.T + 410 * MIN), 12, 36)])
        self.assertEqual(plants.rises(series[:3], self.T + 11 * MIN), [])
        self.assertEqual(plants.rises([], self.T), [])

    def test_the_saved_times_are_kept_for_the_sensor_plants_that_are_listed_and_parse(self):
        raw = {"strelitzia": "2026-10-21T14:05:00+00:00", "gone": "2026-10-21T14:05:00+00:00", "ivy": "soon",
               "pothos": None}
        self.assertEqual(plants.valid_watered(raw, plants.parse_plants([
            RAW[0], {"moisture": "sensor.ivy_moisture", "name": "Ivy", "dry": 20},
            {"moisture": "sensor.pothos_moisture", "name": "Pothos", "dry": 15}])),
            {"strelitzia": "2026-10-21T14:05:00+00:00"})
        for bad in (None, [], "x", 5):
            self.assertEqual(plants.valid_watered(bad, {ENTITY: PLANT}), {})


class OverviewTests(unittest.TestCase):
    """The row that lists every plant: 17:00 on 22.10.2026 in the house."""

    NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
    SEEDED = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)

    @staticmethod
    def timed(name, days):
        return plants.parse_timed([{"name": name, "every_days": days}])[plants.slug(name)]

    def anchor(self, **extra):
        return {"at": core.utc_iso(self.SEEDED), "seed": True, **extra}

    def sensor(self, value=31, dry=False, watered="2026-10-21T10:00:00+00:00", plant=PLANT):
        return {"plant": plant, "value": value, "dry": dry, "watered": watered}

    def row(self, sensor=(), timed=(), now=None):
        return plants.overview_row(list(sensor), list(timed), now or self.NOW)

    def test_each_plant_is_a_line_with_its_due_day_or_its_soil_and_when_it_was_last_watered(self):
        row = self.row([self.sensor()], [(self.timed("Peace lily kitchen", 7), self.anchor()),
                                         (self.timed("Aloe", 35), self.anchor())])
        self.assertEqual(row["items"], ["Peace lily kitchen: 27.10, watered 20.10", "Aloe: 24.11, watered 20.10",
                                        "Strelitzia: soil 31%, at 15%, watered 21.10"])
        self.assertEqual((row["id"], row["kind"], row["icon"], row["audience"]),
                         ("maint:plants", "maint", "mdi:sprout", "admin"))
        self.assertNotIn("done", row)
        self.assertNotIn("area", row)
        self.assertTrue(core.is_valid_row(row))

    def test_the_title_gives_the_next_day_and_the_row_sits_at_nine_that_morning(self):
        row = self.row([self.sensor()], [(self.timed("Aloe", 35), self.anchor()),
                                         (self.timed("Peace lily kitchen", 7), self.anchor())])
        self.assertEqual((row["title"], row["detail"], row["at"]),
                         ("Plants: next 27.10", "3 plants", "2026-10-27T08:00:00+00:00"))

    def test_when_something_is_due_the_title_counts_it_the_detail_names_it_and_the_row_has_no_time(self):
        late = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
        timed = [(self.timed("Peace lily kitchen", 7), self.anchor()), (self.timed("Aloe", 35), self.anchor())]
        row = self.row(timed=timed, now=late)
        self.assertEqual((row["title"], row["detail"]), ("Plants: 1 of 2 due", "Peace lily kitchen"))
        self.assertNotIn("at", row)
        self.assertEqual(row["items"][0], "Peace lily kitchen: due 27.10, watered 20.10")
        today = self.row(timed=[(self.timed("Peace lily kitchen", 7), self.anchor())],
                         now=datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(today["title"], "Plants: 1 of 1 due")

    def test_a_dry_sensor_plant_is_due_and_leads_with_the_other_due_plants(self):
        late = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
        row = self.row([self.sensor(12, dry=True)], [(self.timed("Peace lily kitchen", 7), self.anchor())], now=late)
        self.assertEqual(row["title"], "Plants: 2 of 2 due")
        self.assertEqual(row["items"], ["Peace lily kitchen: due 27.10, watered 20.10",
                                        "Strelitzia: dry, soil 12%, at 15%, watered 21.10"])
        only = self.row([self.sensor(12, dry=True)], [(self.timed("Aloe", 35), self.anchor())])
        self.assertEqual((only["title"], only["items"][0], "at" in only),
                         ("Plants: 1 of 2 due", "Strelitzia: dry, soil 12%, at 15%, watered 21.10", False))

    def test_the_sensor_plants_that_are_not_dry_come_last_the_nearest_to_dry_first(self):
        ivy = plants.parse_plants([{"moisture": "sensor.ivy_moisture", "name": "Ivy", "dry": 20}])
        far = ivy["sensor.ivy_moisture"]
        row = self.row([self.sensor(60, plant=far), self.sensor(31)], [(self.timed("Aloe", 35), self.anchor())])
        self.assertEqual([line.split(":")[0] for line in row["items"]], ["Aloe", "Strelitzia", "Ivy"])

    def test_a_reading_that_is_unknown_and_a_watering_nobody_has_seen_are_said_so(self):
        row = self.row([self.sensor(None, watered=None)])
        self.assertEqual(row["items"], ["Strelitzia: soil unknown, at 15%"])
        self.assertEqual(row["title"], "Plants: none due")
        self.assertNotIn("at", row)

    def test_only_twenty_lines_fit_and_the_rest_is_counted(self):
        timed = [(self.timed(f"Plant {i:02d}", 10 + i), self.anchor()) for i in range(25)]
        for now in (self.NOW, self.NOW + 20 * DAY):
            row = self.row(timed=timed, now=now)
            self.assertEqual(len(row["items"]), core.ITEMS_MAX)
            self.assertEqual(row["items"][-1], "+6 more")
            self.assertTrue(row["items"][0].startswith("Plant 00"))
            self.assertTrue(core.is_valid_row(row))

    def test_no_plants_no_row_and_a_plant_without_a_usable_anchor_has_no_line(self):
        self.assertIsNone(self.row())
        row = self.row(timed=[(self.timed("Aloe", 35), None), (self.timed("Fern", 7), {"at": "soon"}),
                              (self.timed("Lily", 7), self.anchor())])
        self.assertEqual([line.split(":")[0] for line in row["items"]], ["Lily"])

    def test_a_done_after_the_seed_shows_as_the_last_watering(self):
        row = self.row(timed=[(self.timed("Aloe", 35), {"at": "2026-10-22T10:00:00+00:00", "prev": self.anchor()})])
        self.assertEqual(row["items"], ["Aloe: 26.11, watered 22.10"])

    def test_a_long_name_is_cut_to_a_line_that_fits(self):
        name = "Monstera deliciosa variegata (the big one by the window)"
        row = self.row(timed=[(self.timed(name, 7), self.anchor())])
        self.assertTrue(all(len(line) <= core.TEXT_MAX for line in row["items"]))
        self.assertTrue(core.is_valid_row(row))


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
