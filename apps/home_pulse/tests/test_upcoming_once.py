# tests/test_upcoming_once.py - the one-off tasks of sensor.house_upcoming: the config, the seed, the row
# from the day before its day, and what a Done anchor does to it.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import upcoming_core as core  # noqa: E402
import upcoming_maint as maint  # noqa: E402
import upcoming_once as once  # noqa: E402

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)
RAW = [{"due": date(2026, 11, 17), "title": "Check the Alocasia for spider mites",
        "detail": "new leaves dark and clean? then back to the living room", "area": "bathroom",
        "icon": "mdi:bug-outline"}]
SLUG = "check_the_alocasia_for_spider_mites"
TASK = once.parse_once(RAW)[SLUG]
SEED = {"at": core.utc_iso(NOW), "seed": True}


def row_of(anchor, now, task=TASK):
    return once.once_row(task, anchor, now)


class ParseTests(unittest.TestCase):
    def test_a_task_is_its_day_title_detail_room_and_icon(self):
        self.assertEqual(once.parse_once(RAW), {SLUG: {
            "slug": SLUG, "title": "Check the Alocasia for spider mites",
            "detail": "new leaves dark and clean? then back to the living room", "area": "bathroom",
            "icon": "mdi:bug-outline", "due": date(2026, 11, 17), "info": []}})

    def test_detail_room_and_icon_are_optional_and_the_day_may_be_written_as_text(self):
        [task] = once.parse_once([{"due": "2026-11-17", "title": "Call the sweep"}]).values()
        self.assertEqual((task["detail"], task["area"], task["icon"], task["due"]),
                         ("", None, once.ICON, date(2026, 11, 17)))

    def test_no_tasks_is_fine_and_the_order_of_the_lines_is_kept(self):
        for raw in (None, [], ()):
            self.assertEqual(once.parse_once(raw), {})
        raw = [{"due": date(2026, 11, 17), "title": title} for title in ("Zinc", "Aloe", "Moss")]
        self.assertEqual(list(once.parse_once(raw)), ["zinc", "aloe", "moss"])

    def test_typos_fail_at_start(self):
        ok = RAW[0]
        bad = ["Check it", None, {**ok, "title": None}, {**ok, "title": "  "}, {**ok, "title": "!!!"},
               {k: v for k, v in ok.items() if k != "title"}, {**ok, "due": None}, {**ok, "due": "soon"},
               {**ok, "due": 20261117}, {**ok, "due": "17.11.2026"}, {**ok, "due": datetime(2026, 11, 17, 9, 0)},
               {k: v for k, v in ok.items() if k != "due"}]
        for cfg in bad:
            with self.assertRaises(ValueError, msg=cfg):
                once.parse_once([cfg])

    def test_a_title_listed_twice_fails_at_start(self):
        with self.assertRaises(ValueError):
            once.parse_once([RAW[0], {**RAW[0], "title": "check the alocasia for SPIDER mites!"}])


class AnchorTests(unittest.TestCase):
    def test_a_task_is_seeded_at_now_once_and_keeps_an_anchor_it_has(self):
        seeded = once.seed({}, {SLUG: TASK}, NOW)
        self.assertEqual(seeded, {f"once:{SLUG}": SEED})
        self.assertEqual(once.seed(seeded, {SLUG: TASK}, NOW + DAY), seeded)
        kept = {f"once:{SLUG}": {"at": core.utc_iso(NOW - DAY), "prev": SEED}}
        self.assertEqual(once.seed(kept, {SLUG: TASK}, NOW), kept)
        self.assertEqual(once.seed({}, {}, NOW), {})

    def test_the_seed_is_open_and_anything_else_is_done(self):
        self.assertFalse(once.is_done(SEED))
        self.assertTrue(once.is_done({"at": core.utc_iso(NOW), "prev": SEED}))
        self.assertTrue(once.is_done({"at": core.utc_iso(NOW)}))
        for anchor in (None, {}, "x", 5):
            self.assertFalse(once.is_done(anchor), anchor)

    def test_a_task_is_found_by_its_row_id_and_nothing_else_is(self):
        self.assertIs(once.by_row(f"maint:once:{SLUG}", {SLUG: TASK}), TASK)
        for other in (f"maint:plant:{SLUG}", f"once:{SLUG}", SLUG, "", None, 5):
            self.assertIsNone(once.by_row(other, {SLUG: TASK}), other)


class RowTests(unittest.TestCase):
    def test_no_row_until_the_day_before_the_day(self):
        for days in range(6):
            self.assertIsNone(row_of(SEED, NOW + days * DAY), days)
        self.assertIsNotNone(row_of(SEED, NOW + 6 * DAY))

    def test_the_day_before_the_row_is_dated_at_nine_house_time_and_not_pushed(self):
        row, key = row_of(SEED, NOW + 6 * DAY)
        self.assertEqual((row["id"], row["kind"], row["title"], row["detail"]),
                         (f"maint:once:{SLUG}", "maint", "Check the Alocasia for spider mites",
                          "new leaves dark and clean? then back to the living room"))
        self.assertEqual((row["at"], row["icon"], row["area"], row["audience"], row["done"]),
                         ("2026-11-17T08:00:00+00:00", "mdi:bug-outline", "bathroom", "admin", "tap"))
        self.assertEqual(row["info"], ["Due 17.11"])
        self.assertNotIn("next", row)
        self.assertIsNone(key)
        self.assertTrue(core.is_valid_row(row))

    def test_from_the_day_the_row_is_untimed_and_pushed_once_per_task(self):
        today, key = row_of(SEED, NOW + 7 * DAY)
        self.assertNotIn("at", today)
        self.assertEqual(key, f"once:{SLUG}:{core.utc_iso(NOW)}")
        late, same = row_of(SEED, NOW + 30 * DAY)
        self.assertEqual((late["info"], same), (["Due 17.11"], key))
        self.assertTrue(core.is_valid_row(late))
        found = maint.digest([(today["title"], key)], {})
        self.assertEqual((found["message"], found["keys"]), ("Check the Alocasia for spider mites", [key]))
        self.assertIsNone(maint.digest([(today["title"], key)], {key: core.utc_iso(NOW + 7 * DAY)}))

    def test_a_done_task_has_no_row_on_any_day(self):
        done = {"at": core.utc_iso(NOW + 6 * DAY), "prev": SEED}
        for days in (0, 6, 7, 8, 100):
            self.assertIsNone(row_of(done, NOW + days * DAY), days)

    def test_without_an_anchor_or_with_a_broken_one_there_is_no_row(self):
        for anchor in (None, {}, {"at": "soon"}, {"at": 5}):
            self.assertIsNone(row_of(anchor, NOW + 7 * DAY), anchor)

    def test_a_task_with_only_a_title_has_no_detail_and_the_plain_icon(self):
        bare = once.parse_once([{"due": date(2026, 11, 17), "title": "Call the sweep"}])["call_the_sweep"]
        row, _ = row_of(SEED, NOW + 6 * DAY, bare)
        self.assertEqual((row["icon"], "detail" in row, "area" in row), (once.ICON, False, False))
        self.assertTrue(core.is_valid_row(row))

    def test_info_lines_lead_the_sheet_before_the_due_day(self):
        task = once.parse_once([{"due": date(2026, 11, 17), "title": "Spray", "info": ["Mix it", " Spray it "]}])
        row, _ = row_of(SEED, NOW + 6 * DAY, task["spray"])
        self.assertEqual(row["info"], ["Mix it", "Spray it", "Due 17.11"])
        self.assertTrue(core.is_valid_row(row))

    def test_a_day_in_another_year_carries_it(self):
        new_year = once.parse_once([{"due": date(2027, 1, 1), "title": "Toast"}])["toast"]
        eve = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc)
        row, _ = row_of({"at": core.utc_iso(eve - 20 * DAY), "seed": True}, eve, new_year)
        self.assertEqual((row["info"], row["at"]), (["Due 01.01.27"], "2027-01-01T08:00:00+00:00"))


if __name__ == "__main__":
    unittest.main()
