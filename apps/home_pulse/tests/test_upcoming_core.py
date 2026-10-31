# tests/test_upcoming_core.py - the sensor.house_upcoming row schema: strings only, ASCII, order,
# overflow, lease and the records of a row that stands in for several plants.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import upcoming_core as core  # noqa: E402

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)


def strings_only(value):
    if isinstance(value, str):
        return value.isascii()
    if isinstance(value, list):
        return all(isinstance(item, str) and item.isascii() for item in value)
    return False


def battery(i):
    return core.make_row(f"maint:battery:sensor.device_{i:02d}_battery", "maint", f"Device {i}", "replace",
                         audience="admin")


class MakeRowTests(unittest.TestCase):
    def test_every_value_is_an_ascii_string_or_a_list_of_them(self):
        row = core.make_row("train:kristine", "train", "Train to Odense", "07:46 +5, 08:02", at=NOW,
                            icon="mdi:train", chip="S-tog delayed", audience_users=["Kristine"])
        self.assertTrue(all(strings_only(v) for v in row.values()))
        self.assertEqual(row["at"], "2026-10-14T10:00:00+00:00")
        self.assertTrue(core.is_valid_row(row))

    def test_text_is_one_ascii_line_and_capped(self):
        row = core.make_row("rain:terrace", "rain", "  Rain–forecast\n", "x" * 80)
        self.assertEqual(row["title"], "Rainforecast")
        self.assertEqual(len(row["detail"]), core.TEXT_MAX)

    def test_empty_optional_fields_are_left_out(self):
        row = core.make_row("rain:terrace", "rain", "Rain forecast", "", chip="", audience_users=["", " "])
        self.assertEqual(set(row), {"id", "kind", "icon", "title"})

    def test_a_row_the_next_chip_skips(self):
        row = core.make_row("maint:washer:drum", "maint", "Washer drum clean", at=NOW, audience="admin", skip_next=True)
        self.assertEqual(row["next"], "skip")
        self.assertTrue(core.is_valid_row(row))


class ItemsTests(unittest.TestCase):
    """items: one plain line each for a maintenance row that summarises several things."""

    def make(self, **extra):
        return core.make_row("maint:plants", "maint", "Plants: next 27.10", "17 plants", audience="admin", **extra)

    def test_a_row_may_list_lines_that_are_ascii_one_line_and_capped(self):
        row = self.make(items=["Monstera: 30.10, watered 20.10", "", "x" * 90, "Caf\u00e9: 01.11"])
        self.assertEqual(row["items"], ["Monstera: 30.10, watered 20.10", "x" * core.TEXT_MAX, "Caf: 01.11"])
        self.assertTrue(core.is_valid_row(row))
        self.assertEqual(len(self.make(items=[f"Plant {i}" for i in range(30)])["items"]), core.ITEMS_MAX)

    def test_a_row_without_lines_has_no_field(self):
        for items in (None, [], ["", " "]):
            self.assertNotIn("items", self.make(items=items))

    def test_the_contract_is_a_list_of_non_empty_ascii_lines_on_a_maintenance_row(self):
        good = {**self.make(items=["a"])}
        self.assertTrue(core.is_valid_row(good))
        too_many = [f"Plant {i}" for i in range(21)]
        for bad in ([], "a", ["a", ""], [5], ["x" * 61], ["caf\u00e9"], ["a\nb"], too_many, None):
            self.assertFalse(core.is_valid_row({**good, "items": bad}), bad)
        self.assertFalse(core.is_valid_row({**core.make_row("rain:terrace", "rain", "Rain forecast"), "items": ["a"]}))


class PlantsTests(unittest.TestCase):
    """plants: the records of one maintenance row that stands in for several plants."""

    RECORD = {"id": "maint:plant:monstera", "name": "Monstera", "amount": "1 L + 1 tsp fertiliser",
              "detail": "due since 30.10", "where": "Kitchen floor", "aka": "Big leaves with holes",
              "photo": "monstera", "done": "tap"}

    def make(self, **extra):
        return core.make_row("maint:plants", "maint", "Water 2 plants", "Monstera, Ivy", audience="admin", **extra)

    def test_a_row_may_list_records_of_ascii_strings_and_stays_valid(self):
        other = {"id": "maint:plant:ivy", "name": "Ivy", "detail": "soil 20%, water at 20%"}
        row = self.make(plants=[self.RECORD, other])
        self.assertEqual(row["plants"], [self.RECORD, other])
        self.assertTrue(all(isinstance(value, str) and value.isascii() for each in row["plants"]
                            for value in each.values()))
        self.assertTrue(core.is_valid_row(row))

    def test_every_text_is_one_ascii_line_and_capped_and_an_id_may_be_as_long_as_a_row_id(self):
        row = self.make(plants=[{**self.RECORD, "name": "Caf\u00e9\n  Palme", "aka": "x" * 90,
                                 "id": "maint:plant:" + "y" * 100}])
        [record] = row["plants"]
        self.assertEqual((record["name"], len(record["aka"]), len(record["id"])), ("Caf Palme", core.TEXT_MAX, 112))
        self.assertTrue(core.is_valid_row(row))
        long_id = self.make(plants=[{**self.RECORD, "id": "z" * 150}])
        self.assertEqual(len(long_id["plants"][0]["id"]), core.ID_MAX)

    def test_a_field_with_nothing_in_it_is_left_out_of_the_record(self):
        record = {"id": "maint:plant:ivy", "name": "Ivy", "amount": "", "detail": None, "where": " ", "aka": 5,
                  "photo": [], "done": False, "stray": "dropped"}
        self.assertEqual(self.make(plants=[record])["plants"], [{"id": "maint:plant:ivy", "name": "Ivy"}])

    def test_a_record_without_an_id_or_a_name_or_that_is_no_dict_is_dropped(self):
        records = [{"name": "Ivy"}, {"id": "maint:plant:ivy"}, {"id": " ", "name": "Ivy"}, "Ivy", None, 5, [], {},
                   self.RECORD]
        self.assertEqual(self.make(plants=records)["plants"], [self.RECORD])

    def test_a_row_without_records_has_no_field(self):
        for plants in (None, [], [{}], [{"name": "Ivy"}]):
            self.assertNotIn("plants", self.make(plants=plants))

    def test_at_most_twenty_records_fit(self):
        records = [{"id": f"maint:plant:p{i:02d}", "name": f"Plant {i:02d}"} for i in range(30)]
        row = self.make(plants=records)
        self.assertEqual([record["name"] for record in row["plants"]], [f"Plant {i:02d}" for i in range(20)])
        self.assertTrue(core.is_valid_row(row))

    def test_the_contract_is_a_list_of_records_of_non_empty_ascii_strings_on_a_maintenance_row(self):
        good = self.make(plants=[self.RECORD])
        too_many = [{"id": f"maint:plant:p{i:02d}", "name": "Plant"} for i in range(21)]
        bad_lists = ([], "a", {}, None, [None], ["Ivy"], [{"id": "maint:plant:ivy"}], [{"name": "Ivy"}], too_many,
                     [{**self.RECORD, "stray": "x"}], [{**self.RECORD, "amount": ""}], [{**self.RECORD, "amount": 5}],
                     [{**self.RECORD, "where": None}], [{**self.RECORD, "name": "x" * 61}],
                     [{**self.RECORD, "aka": "Caf\u00e9"}], [{**self.RECORD, "detail": "a\nb"}],
                     [{**self.RECORD, "id": "z" * 121}], [{**self.RECORD, "photo": ["monstera"]}])
        for bad in bad_lists:
            self.assertFalse(core.is_valid_row({**good, "plants": bad}), bad)
        self.assertTrue(core.is_valid_row({**good, "plants": [{**self.RECORD, "id": "z" * 120}]}))
        self.assertFalse(core.is_valid_row({**core.make_row("rain:terrace", "rain", "Rain forecast"),
                                            "plants": [self.RECORD]}))

    def test_done_in_a_record_is_tap_or_absent(self):
        good = self.make(plants=[{k: v for k, v in self.RECORD.items() if k != "done"}])
        self.assertTrue(core.is_valid_row(good))
        for bad in ("true", "Tap", "yes", True):
            self.assertFalse(core.is_valid_row({**good, "plants": [{**self.RECORD, "done": bad}]}), bad)

    def test_the_canonical_form_ignores_the_order_of_a_records_keys_but_not_of_the_records(self):
        one, two = {"id": "maint:plant:a", "name": "A"}, {"id": "maint:plant:b", "name": "B"}
        flipped = {"name": "A", "id": "maint:plant:a"}
        same, flipped_keys, swapped = (core.canonical([self.make(plants=records)])
                                       for records in ([one, two], [flipped, two], [two, one]))
        self.assertEqual(same, flipped_keys)
        self.assertNotEqual(same, swapped)

    def test_a_row_with_records_goes_through_finalize_untouched(self):
        row = self.make(plants=[self.RECORD])
        self.assertEqual(core.finalize_rows([battery(1), row])[1], row)


class SheetFieldTests(unittest.TestCase):
    """done, info, steps and warn: what the sheet of a maintenance row shows."""

    def make(self, **extra):
        return core.make_row("maint:dishwasher:filters", "maint", "Dishwasher filters", "30 runs left",
                             audience="admin", **extra)

    def test_a_sheet_row_carries_a_done_button_info_lines_steps_and_a_note(self):
        row = self.make(tap_done=True, info=["Every 30 runs (~2 months)", "Counting from 15.10"],
                        steps=["Switch it off.", "Rinse the filter."], warn="Never run it without the filter.")
        self.assertEqual((row["done"], row["info"], row["steps"], row["warn"]),
                         ("tap", ["Every 30 runs (~2 months)", "Counting from 15.10"],
                          ["Switch it off.", "Rinse the filter."], "Never run it without the filter."))
        self.assertTrue(all(strings_only(v) for v in row.values()))
        self.assertTrue(core.is_valid_row(row))

    def test_a_row_without_them_has_none_of_the_fields(self):
        row = self.make(info=["", " "], steps=[None, ""], warn="")
        self.assertEqual(set(row), {"id", "kind", "icon", "title", "detail", "audience"})
        self.assertEqual(set(self.make(tap_done=False, info=None, steps=None, warn=None)), set(row))

    def test_empty_lines_are_dropped_and_the_lists_are_capped(self):
        row = self.make(info=["a", "", "b", "c", "d", "e"], steps=[f"Step {i}." for i in range(12)])
        self.assertEqual(row["info"], ["a", "b", "c", "d"])
        self.assertEqual(len(row["steps"]), core.STEPS_MAX)

    def test_the_lines_are_ascii_one_line_and_capped(self):
        row = self.make(info=["x" * 90], steps=["Rinse \u2013 the\nfilter " + "y" * 200], warn="z" * 300)
        self.assertEqual(len(row["info"][0]), core.TEXT_MAX)
        self.assertEqual(len(row["steps"][0]), core.STEP_MAX)
        self.assertTrue(row["steps"][0].startswith("Rinse the filter y"))
        self.assertEqual(len(row["warn"]), core.STEP_MAX)

    def test_a_note_without_steps_is_dropped(self):
        self.assertNotIn("warn", self.make(warn="Careful."))

    def test_done_is_tap_on_a_maintenance_row_only(self):
        row = self.make(tap_done=True)
        self.assertTrue(core.is_valid_row(row))
        for bad in ("true", "Tap", "", True, 1):
            self.assertFalse(core.is_valid_row({**row, "done": bad}), bad)
        rain = core.make_row("rain:terrace", "rain", "Rain forecast")
        self.assertFalse(core.is_valid_row({**rain, "done": "tap"}))
        self.assertFalse(core.is_valid_row({**rain, "audience": "admin", "done": "tap"}))

    def test_info_is_a_short_list_of_ascii_lines(self):
        row = self.make()
        self.assertTrue(core.is_valid_row({**row, "info": ["one", "two"]}))
        for bad in ("one", [], [""], ["x" * 61], ["a", "b", "c", "d", "e"], [1], ["a", None], ["Regn \u00e6"]):
            self.assertFalse(core.is_valid_row({**row, "info": bad}), bad)

    def test_steps_are_a_list_of_ascii_lines_on_a_maintenance_row_only(self):
        row = self.make()
        self.assertTrue(core.is_valid_row({**row, "steps": ["Switch it off."], "warn": "Careful."}))
        for bad in ("Switch it off.", [], [""], ["x" * 151], [f"Step {i}." for i in range(9)], [1], ["Regn \u00e6"]):
            self.assertFalse(core.is_valid_row({**row, "steps": bad}), bad)
        rain = {**core.make_row("rain:terrace", "rain", "Rain forecast"), "steps": ["Close the door."]}
        self.assertFalse(core.is_valid_row(rain))

    def test_a_note_needs_steps_and_a_string(self):
        row = self.make(steps=["Switch it off."])
        self.assertTrue(core.is_valid_row({**row, "warn": "Careful."}))
        for bad in ("", "x" * 151, 5, ["Careful."], True):
            self.assertFalse(core.is_valid_row({**row, "warn": bad}), bad)
        without = {k: v for k, v in row.items() if k != "steps"}
        self.assertFalse(core.is_valid_row({**without, "warn": "Careful."}))


class ValidRowTests(unittest.TestCase):
    def valid(self, **extra):
        row = {"id": "rain:terrace", "kind": "rain", "icon": "mdi:weather-pouring", "title": "Rain forecast"}
        row.update(extra)
        return core.is_valid_row(row)

    def test_numbers_booleans_and_nulls_are_invalid(self):
        self.assertTrue(self.valid())
        for bad in (0, 1.5, True, False, None, {"a": "b"}, ["ok", 3]):
            self.assertFalse(self.valid(detail=bad), bad)

    def test_non_ascii_unknown_kind_and_unknown_fields_are_invalid(self):
        self.assertFalse(self.valid(detail="Regn æ"))
        self.assertFalse(self.valid(kind="storm"))
        self.assertFalse(self.valid(style="card"))
        self.assertFalse(self.valid(title=""))

    def test_audience_must_be_admin_or_a_name_list_never_both(self):
        self.assertTrue(self.valid(audience="admin"))
        self.assertFalse(self.valid(audience="Admin"))
        self.assertTrue(self.valid(audience_users=["Kristine"]))
        self.assertFalse(self.valid(audience_users=[]))
        self.assertFalse(self.valid(audience="admin", audience_users=["Kristine"]))

    def test_next_is_skip_or_absent(self):
        self.assertTrue(self.valid(next="skip"))
        for bad in ("true", "", "Skip", True):
            self.assertFalse(self.valid(next=bad), bad)

    def test_maintenance_rows_must_be_admin(self):
        row = {"id": "maint:rober2:filter", "kind": "maint", "icon": "mdi:robot-vacuum", "title": "Rober2 filter"}
        self.assertFalse(core.is_valid_row(row))
        self.assertTrue(core.is_valid_row({**row, "audience": "admin"}))


class FinalizeTests(unittest.TestCase):
    def test_timed_rows_by_time_then_id_and_untimed_last(self):
        rows = [
            battery(1),
            core.make_row("rain:terrace", "rain", "Rain forecast", at=NOW + timedelta(hours=2)),
            core.make_row("train:kristine", "train", "Train", at=NOW, audience_users=["Kristine"]),
            core.make_row("maint:washer:drum", "maint", "Washer drum clean", audience="admin"),
        ]
        ids = [row["id"] for row in core.finalize_rows(rows)]
        self.assertEqual(ids, ["train:kristine", "rain:terrace", "maint:battery:sensor.device_01_battery",
                               "maint:washer:drum"])

    def test_invalid_rows_are_dropped(self):
        rows = [battery(1), {"id": "x", "kind": "maint", "title": 3}]
        self.assertEqual(len(core.finalize_rows(rows)), 1)

    def test_overflow_folds_the_batteries_that_do_not_fit_into_one_row(self):
        rows = [battery(i) for i in range(40)]
        rows.append(core.make_row("maint:rober2:filter", "maint", "Rober2 filter", "replace", audience="admin"))
        out = core.finalize_rows(rows)
        self.assertEqual(len(out), core.MAX_ROWS)
        more = [row for row in out if row["id"] == core.OVERFLOW_ID]
        self.assertEqual(len(more), 1)
        shown = sum(1 for row in out if row["id"].startswith(core.BATTERY_ID_PREFIX))
        self.assertEqual(more[0]["title"], f"{40 - shown} more devices")
        self.assertIn("maint:rober2:filter", [row["id"] for row in out])
        self.assertTrue(all(core.is_valid_row(row) for row in out))

    def test_thirty_rows_need_no_overflow(self):
        out = core.finalize_rows([battery(i) for i in range(core.MAX_ROWS)])
        self.assertEqual(len(out), core.MAX_ROWS)
        self.assertNotIn(core.OVERFLOW_ID, [row["id"] for row in out])

    def test_canonical_json_ignores_key_order(self):
        a = [{"id": "a", "kind": "rain", "title": "t", "icon": "mdi:x"}]
        b = [{"icon": "mdi:x", "title": "t", "kind": "rain", "id": "a"}]
        self.assertEqual(core.canonical(a), core.canonical(b))
        self.assertEqual(json.loads(core.canonical(a)), a)

    def test_the_lease_runs_fifteen_minutes(self):
        self.assertEqual(core.lease_until(NOW), "2026-10-14T10:15:00+00:00")


if __name__ == "__main__":
    unittest.main()
