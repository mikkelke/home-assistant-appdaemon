# tests/test_upcoming_core.py - the sensor.house_upcoming row schema: strings only, ASCII, order,
# overflow and lease. Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

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
