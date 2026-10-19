# tests/test_coffee_beans.py - the beans container: adds, switching beans, debits (a stopped brew that
# ground included), undo within ten minutes with the debits since kept, and what the entity shows.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_beans as cbeans  # noqa: E402

NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)


class AddTests(unittest.TestCase):
    def test_unknown_until_the_first_add(self):
        beans = cbeans.Beans()
        view = beans.view(15.0, NOW)
        self.assertIsNone(view["remaining"])
        self.assertNotIn("brews_left", view)
        self.assertTrue(beans.debit("c1", 15.0, NOW))
        self.assertIsNone(beans.remaining)

    def test_the_first_add_names_the_beans(self):
        beans = cbeans.Beans()
        op = beans.add(250, NOW, new_name="Kenya Kiambu")
        self.assertTrue(op.startswith("o"))
        self.assertEqual(beans.bean(), {"id": "b1", "name": "Kenya Kiambu"})
        self.assertEqual(beans.remaining, 250.0)

    def test_a_top_up_adds_to_what_is_left(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya Kiambu")
        beans.debit("c1", 15, NOW)
        beans.add(250, NOW + MIN)
        self.assertEqual(beans.remaining, 485.0)
        self.assertEqual(beans.bean_id, "b1")

    def test_other_beans_switch_the_container_and_keep_the_leftover(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya Kiambu")
        for i in range(10):
            beans.debit(f"c{i}", 15, NOW)
        beans.add(250, NOW + MIN, new_name="Ethiopia Guji")
        self.assertEqual(beans.bean(), {"id": "b2", "name": "Ethiopia Guji"})
        self.assertEqual(beans.remaining, 350.0)
        beans.add(100, NOW + 2 * MIN, bean_id="b1")
        self.assertEqual((beans.bean_id, beans.remaining), ("b1", 450.0))

    def test_a_name_that_is_known_picks_those_beans(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya Kiambu")
        beans.add(250, NOW + MIN, new_name="kenya kiambu")
        self.assertEqual(len(beans.known), 1)

    def test_invalid_adds_change_nothing(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya")
        before = json.dumps(beans.to_dict(), sort_keys=True)
        for grams, bean_id in ((0, None), (-5, None), ("x", None), (2500, None), (100, "b9")):
            self.assertIsNone(beans.add(grams, NOW, bean_id=bean_id))
        self.assertEqual(json.dumps(beans.to_dict(), sort_keys=True), before)

    def test_an_add_without_beans_named_makes_unnamed_beans(self):
        beans = cbeans.Beans()
        beans.add(250, NOW)
        self.assertEqual(beans.bean()["name"], cbeans.DEFAULT_NAME)


class DebitTests(unittest.TestCase):
    def test_a_brew_debits_once(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya")
        self.assertTrue(beans.debit("c1", 15, NOW))
        self.assertFalse(beans.debit("c1", 15, NOW))
        self.assertEqual(beans.remaining, 235.0)

    def test_below_zero_is_kept_inside_and_shown_as_zero(self):
        beans = cbeans.Beans()
        beans.add(20, NOW, new_name="Kenya")
        beans.debit("c1", 15, NOW)
        beans.debit("c2", 15, NOW)
        self.assertEqual(beans.remaining, -10.0)
        view = beans.view(15.0, NOW + 20 * MIN)
        self.assertEqual((view["remaining"], view["brews_left"], view["low"]), (0, 0, True))
        beans.add(250, NOW + 30 * MIN)
        self.assertEqual(beans.remaining, 250.0)


class UndoTests(unittest.TestCase):
    def test_undo_restores_exactly_what_was_there(self):
        beans = cbeans.Beans()
        first = beans.add(250, NOW, new_name="Kenya")
        beans.debit("c1", 15, NOW + MIN)
        before = beans.container.copy(), list(beans.known)
        op = beans.add(250, NOW + 2 * MIN, new_name="Ethiopia")
        self.assertTrue(beans.undo(op, NOW + 5 * MIN))
        self.assertEqual((beans.container, beans.known), before)
        self.assertNotEqual(op, first)

    def test_undo_keeps_the_brews_made_since(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya")
        op = beans.add(250, NOW + MIN)
        beans.debit("c2", 15, NOW + 2 * MIN)
        self.assertTrue(beans.undo(op, NOW + 3 * MIN))
        self.assertEqual(beans.remaining, 235.0)
        self.assertFalse(beans.debit("c2", 15, NOW + 4 * MIN))

    def test_undo_only_the_newest_add_and_only_within_ten_minutes(self):
        beans = cbeans.Beans()
        first = beans.add(250, NOW, new_name="Kenya")
        second = beans.add(100, NOW + MIN)
        self.assertFalse(beans.undo(first, NOW + 2 * MIN))
        self.assertFalse(beans.undo(second, NOW + 12 * MIN))
        self.assertFalse(beans.undo("nope", NOW + 2 * MIN))
        self.assertEqual(beans.remaining, 350.0)

    def test_the_view_offers_the_undo_until_its_deadline(self):
        beans = cbeans.Beans()
        op = beans.add(250, NOW, new_name="Kenya")
        view = beans.view(15.0, NOW + MIN)
        self.assertEqual((view["last_op_id"], view["undo_until"]), (op, (NOW + 10 * MIN).isoformat(timespec="seconds")))
        self.assertNotIn("last_op_id", beans.view(15.0, NOW + 11 * MIN))

    def test_the_container_survives_a_round_trip(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya")
        beans.debit("c1", 15, NOW)
        again = cbeans.Beans(json.loads(json.dumps(beans.to_dict())))
        self.assertEqual(again.to_dict(), beans.to_dict())
        self.assertEqual(again.view(15.0, NOW), beans.view(15.0, NOW))


class ViewTests(unittest.TestCase):
    def test_brews_left_and_low(self):
        beans = cbeans.Beans()
        beans.add(112, NOW, new_name="Kenya Kiambu")
        view = beans.view(15.0, NOW)
        self.assertEqual((view["remaining"], view["brews_left"], view["low"]), (112, 7, False))
        beans.debit("c1", 80, NOW)
        self.assertTrue(beans.view(15.0, NOW)["low"])
        self.assertEqual(view["known_beans"], [{"id": "b1", "name": "Kenya Kiambu"}])


if __name__ == "__main__":
    unittest.main()
