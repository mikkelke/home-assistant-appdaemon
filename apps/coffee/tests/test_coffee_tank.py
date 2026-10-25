# tests/test_coffee_tank.py - the water tank: unknown until the first fill, a brew's water debited once, a fill or a
# run-dry in the middle of a brew, the last ten run-dry moments, and the store.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_tank as ctank  # noqa: E402

NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)


def filled(**kw):
    tank = ctank.Tank(**kw)
    tank.fill(NOW)
    return tank


class LevelTests(unittest.TestCase):
    def test_unknown_until_the_first_fill(self):
        tank = ctank.Tank()
        self.assertIsNone(tank.left)
        self.assertTrue(tank.brew_done(255.0, "c1"))
        self.assertIsNone(tank.left)
        self.assertIsNone(tank.empty(NOW, 100.0))
        self.assertEqual((tank.empties, tank.used_ml, tank.offset_ml), ([], 0.0, None))
        self.assertEqual(tank.view(), {"capacity": 946.0, "left": None, "filled_at": None})

    def test_a_fill_makes_it_full(self):
        tank = filled()
        self.assertEqual(tank.left, 946.0)
        self.assertEqual(tank.view(), {"capacity": 946.0, "left": 946.0, "filled_at": "2026-10-20T06:00:00+00:00"})

    def test_the_capacity_comes_from_the_caller(self):
        self.assertEqual(filled(capacity=1000).left, 1000.0)

    def test_a_fill_starts_the_count_again(self):
        tank = filled()
        tank.brew_done(255.0, "c1")
        tank.fill(NOW + MIN)
        self.assertEqual((tank.left, tank.filled_at), (946.0, "2026-10-20T06:01:00+00:00"))


class DebitTests(unittest.TestCase):
    def test_a_brew_takes_its_water_out_once(self):
        tank = filled()
        self.assertTrue(tank.brew_done(255.0, "c1"))
        self.assertFalse(tank.brew_done(255.0, "c1"))
        self.assertEqual(tank.left, 691.0)
        self.assertTrue(tank.brew_done(272.0, "c2"))
        self.assertEqual(tank.left, 419.0)

    def test_below_zero_is_kept_inside_and_shown_as_zero(self):
        tank = filled(capacity=300)
        tank.brew_done(255.0, "c1")
        tank.brew_done(255.0, "c2")
        self.assertEqual((tank.left, tank.used_ml), (0.0, 510.0))
        tank.fill(NOW + MIN)
        self.assertEqual(tank.left, 300.0)

    def test_a_brew_without_a_measure_takes_nothing_out(self):
        tank = filled()
        self.assertTrue(tank.brew_done(None, "c1"))
        self.assertEqual(tank.left, 946.0)

    def test_a_reading_below_the_offset_takes_nothing_out(self):
        tank = filled()
        tank.fill(NOW + MIN, 200.0)
        tank.brew_done(150.0, "c1")
        self.assertEqual((tank.left, tank.offset_ml), (946.0, None))


class MidBrewTests(unittest.TestCase):
    def test_a_fill_in_the_middle_of_a_brew_leaves_the_water_poured_before_it(self):
        tank = filled()
        tank.fill(NOW + MIN, 100.0)
        self.assertEqual(tank.offset_ml, 100.0)
        tank.brew_done(255.0, "c1")
        self.assertEqual((tank.left, tank.offset_ml), (791.0, None))
        tank.brew_done(255.0, "c2")
        self.assertEqual(tank.left, 536.0)

    def test_empty_then_fill_then_the_end_of_the_brew_takes_only_the_water_after_the_fill(self):
        tank = filled()
        tank.brew_done(800.0, "c0")
        self.assertEqual(tank.empty(NOW + MIN, 150.0), 950.0)
        self.assertEqual((tank.left, tank.offset_ml), (0.0, 150.0))
        tank.fill(NOW + 2 * MIN, 200.0)
        self.assertEqual((tank.left, tank.offset_ml), (946.0, 200.0))
        tank.brew_done(272.0, "c1")
        self.assertEqual((tank.left, tank.offset_ml), (874.0, None))

    def test_a_brew_that_ends_after_the_run_dry_takes_only_the_water_after_it(self):
        tank = filled()
        tank.brew_done(800.0, "c0")
        tank.empty(NOW + MIN, 150.0)
        tank.brew_done(272.0, "c1")
        self.assertEqual((tank.left, tank.used_ml), (0.0, 946.0 + 122.0))

    def test_a_fill_while_nothing_has_poured_yet_has_no_offset(self):
        tank = filled()
        tank.fill(NOW + MIN, None)
        tank.brew_done(255.0, "c1")
        self.assertEqual(tank.left, 691.0)


class EmptiesTests(unittest.TestCase):
    def test_each_run_dry_is_kept_with_the_water_used(self):
        tank = filled()
        tank.brew_done(930.0, "c1")
        self.assertEqual(tank.empty(NOW + MIN, 40.0), 970.0)
        self.assertEqual(tank.empties, [{"at": "2026-10-20T06:01:00+00:00", "used_ml": 970.0}])

    def test_between_brews_it_is_the_water_used_so_far(self):
        tank = filled()
        tank.brew_done(900.0, "c1")
        self.assertEqual(tank.empty(NOW + MIN), 900.0)
        self.assertEqual((tank.left, tank.offset_ml), (0.0, None))

    def test_the_last_ten_are_kept(self):
        tank = ctank.Tank()
        for i in range(12):
            tank.fill(NOW + i * MIN)
            tank.brew_done(100.0 + i, f"c{i}")
            tank.empty(NOW + i * MIN)
        self.assertEqual(len(tank.empties), ctank.EMPTIES_MAX)
        self.assertEqual([e["used_ml"] for e in tank.empties], [102.0 + i for i in range(10)])


class StoreTests(unittest.TestCase):
    def test_a_round_trip(self):
        tank = filled()
        tank.brew_done(255.0, "c1")
        tank.fill(NOW + MIN, 100.0)
        tank.empty(NOW + 2 * MIN, 130.0)
        again = ctank.Tank(json.loads(json.dumps(tank.to_dict())))
        self.assertEqual(again.to_dict(), tank.to_dict())
        self.assertEqual(again.view(), tank.view())
        self.assertFalse(again.brew_done(1.0, "c1"))

    def test_nothing_usable_is_no_state(self):
        for data in (None, "x", [], {"used_ml": "x", "filled_at": 5, "offset_ml": True, "empties": "x"},
                     {"empties": [None, {"at": 1}, {"at": "a", "used_ml": "b"}]}):
            tank = ctank.Tank(data)
            self.assertEqual((tank.left, tank.used_ml, tank.offset_ml, tank.empties), (None, 0.0, None, []), data)

    def test_the_saved_level_follows_a_new_capacity(self):
        tank = filled()
        tank.brew_done(200.0, "c1")
        self.assertEqual(ctank.Tank(tank.to_dict(), capacity=1000).left, 800.0)


if __name__ == "__main__":
    unittest.main()
