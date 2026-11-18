# tests/test_coffee_beans.py - open bags, legacy state, attribution, debits, undo, and the sensor view.
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
        self.assertFalse(beans.debit("c1", 15.0, NOW))
        self.assertEqual(beans.bags, [])

    def test_the_first_add_names_the_beans(self):
        beans = cbeans.Beans()
        op = beans.add(250, NOW, new_name="Kenya Kiambu")
        self.assertTrue(op.startswith("o"))
        self.assertEqual(beans.bean("b1"), {"id": "b1", "name": "Kenya Kiambu"})
        self.assertEqual(beans.bag("b1")["remaining_g"], 250.0)

    def test_a_top_up_adds_to_what_is_left(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya Kiambu")
        beans.debit("c1", 15, NOW, bean_id="b1")
        beans.add(250, NOW + MIN)
        self.assertEqual(beans.bag("b1")["remaining_g"], 485.0)
        self.assertEqual(len(beans.bags), 1)

    def test_other_beans_open_a_separate_bag(self):
        beans = cbeans.Beans()
        beans.add(214, NOW, new_name="Daybreak")
        beans.add(500, NOW + MIN, new_name="Signature Brew", dose_g=18)
        self.assertEqual([(bag["bean_id"], bag["remaining_g"]) for bag in beans.bags],
                         [("b2", 500.0), ("b1", 214.0)])
        beans.add(100, NOW + 2 * MIN, bean_id="b1")
        self.assertEqual([(bag["bean_id"], bag["remaining_g"]) for bag in beans.bags],
                         [("b1", 314.0), ("b2", 500.0)])

    def test_add_closes_other_bags_below_their_dose(self):
        beans = cbeans.Beans()
        recipes = [{"id": "Daybreak", "name": "Daybreak", "dose_g": 18}]
        beans.add(16, NOW, new_name="Daybreak")
        beans.add(15, NOW, new_name="Backup")
        beans.add(10, NOW, new_name="Signature Brew")
        op = beans.add(4, NOW + MIN, bean_id="b3", dose_g=15, recipes=recipes)
        self.assertEqual([(bag["bean_id"], bag["remaining_g"]) for bag in beans.bags],
                         [("b3", 14.0), ("b2", 15.0)])
        self.assertTrue(beans.undo(op, NOW + 2 * MIN))
        self.assertEqual(beans.bag("b1")["remaining_g"], 16.0)

    def test_add_without_a_name_can_default_to_the_primary_bag(self):
        beans = cbeans.Beans()
        beans.add(214, NOW, new_name="Daybreak")
        beans.add(500, NOW + MIN, new_name="Signature Brew")
        beans.add(50, NOW + 2 * MIN, new_name="  ", default_bean_id="b1")
        self.assertEqual(beans.bag("b1")["remaining_g"], 264.0)
        self.assertEqual(beans.bag("b2")["remaining_g"], 500.0)

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
        self.assertEqual(beans.bean("b1")["name"], cbeans.DEFAULT_NAME)

    def test_empty_closes_the_chosen_bag_and_keeps_the_name(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya")
        beans.add(300, NOW + MIN, new_name="Ethiopia")
        self.assertTrue(beans.empty("b1", NOW + 2 * MIN))
        self.assertFalse(beans.empty("b1", NOW + 3 * MIN))
        self.assertEqual((beans.ops[-1]["kind"], beans.ops[-1]["bean_id"]), ("empty", "b1"))
        self.assertEqual(beans.bag("b2")["remaining_g"], 300.0)
        beans.add(100, NOW + 4 * MIN, bean_id="b1")
        self.assertEqual(beans.bag("b1")["remaining_g"], 100.0)


class DebitTests(unittest.TestCase):
    def test_a_brew_debits_once(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya")
        beans.add(250, NOW + MIN, new_name="Ethiopia")
        self.assertTrue(beans.debit("c1", 15, NOW, bean_id="b1"))
        self.assertFalse(beans.debit("c1", 15, NOW, bean_id="b2"))
        self.assertEqual(beans.bag("b1")["remaining_g"], 235.0)
        self.assertEqual(beans.bag("b2")["remaining_g"], 250.0)
        self.assertFalse(beans.debit("c2", 15, NOW))
        self.assertEqual(beans.bag("b2")["remaining_g"], 250.0)

    def test_below_zero_is_kept_inside_and_shown_as_zero(self):
        beans = cbeans.Beans()
        beans.add(20, NOW, new_name="Kenya")
        beans.debit("c1", 15, NOW, bean_id="b1")
        beans.debit("c2", 15, NOW, bean_id="b1")
        self.assertEqual(beans.bag("b1")["remaining_g"], -10.0)
        view = beans.view(15.0, NOW + 20 * MIN)
        self.assertEqual((view["remaining"], view["brews_left"], view["low"]), (0, 0, True))
        beans.add(250, NOW + 30 * MIN)
        self.assertEqual(beans.bag("b1")["remaining_g"], 250.0)


class RetagTests(unittest.TestCase):
    def test_retag_credits_a_topped_up_bag(self):
        beans = cbeans.Beans()
        beans.add(100, NOW, new_name="Kenya")
        beans.add(100, NOW, new_name="Ethiopia")
        beans.debit("c1", 18, NOW + MIN, bean_id="b1")
        beans.add(50, NOW + 2 * MIN, bean_id="b1")

        self.assertEqual(beans.retag("c1", "b1", 18, "b2", 18, NOW + 3 * MIN), 18)
        self.assertEqual((beans.bag("b1")["remaining_g"], beans.bag("b2")["remaining_g"]), (150.0, 82.0))

    def test_retag_does_not_credit_a_reopened_bag(self):
        beans = cbeans.Beans()
        beans.add(100, NOW, new_name="Kenya")
        beans.add(100, NOW, new_name="Ethiopia")
        beans.debit("c1", 18, NOW + MIN, bean_id="b1")
        beans.empty("b1", NOW + 2 * MIN)
        beans.add(150, NOW + 3 * MIN, bean_id="b1")

        self.assertEqual(beans.retag("c1", "b1", 18, "b2", 18, NOW + 4 * MIN), 18)
        self.assertEqual((beans.bag("b1")["remaining_g"], beans.bag("b2")["remaining_g"]), (150.0, 82.0))

    def test_retag_uses_the_ledger_charge_for_a_bag_opened_before_the_finish(self):
        for reopened in (False, True):
            with self.subTest(reopened=reopened):
                beans = cbeans.Beans()
                beans.add(100, NOW, new_name="Kenya")
                beans.add(100, NOW, new_name="Ethiopia")
                beans.debit("c1", 18, NOW + MIN, bean_id="b1")
                if reopened:
                    beans.empty("b1", NOW + 2 * MIN)
                    beans.add(150, NOW + 3 * MIN, bean_id="b1")
                beans.ops = [op for op in beans.ops if op["kind"] != "debit"]

                beans.retag("c1", "b1", 18, "b2", 18, NOW + 4 * MIN, finished_at=(NOW + MIN).timestamp())

                self.assertEqual(beans.bag("b1")["remaining_g"], 150.0 if reopened else 100.0)
                self.assertEqual(beans.bag("b2")["remaining_g"], 82.0)

    def test_recent_accounting_survives_more_than_the_usual_operation_limit(self):
        beans = cbeans.Beans()
        beans.add(100, NOW, new_name="Kenya")
        beans.add(100, NOW, new_name="Ethiopia")
        beans.debit("c1", 18, NOW + MIN, bean_id="b1")
        beans.empty("b1", NOW + 2 * MIN)
        beans.add(150, NOW + 3 * MIN, bean_id="b1")
        op_ids = {beans.add(1, NOW + 4 * MIN, bean_id="b2") for _ in range(105)}
        self.assertEqual(len(op_ids), 105)
        beans = cbeans.Beans(json.loads(json.dumps(beans.to_dict())))

        beans.retag("c1", "b1", 18, "b2", 18, NOW + 5 * MIN)
        self.assertEqual(beans.bag("b1")["remaining_g"], 150.0)
        beans.add(1, NOW + timedelta(days=2), bean_id="b2")
        self.assertEqual(len(beans.ops), cbeans.MAX_OPS)


class UndoTests(unittest.TestCase):
    def test_undo_restores_exactly_what_was_there(self):
        beans = cbeans.Beans()
        first = beans.add(250, NOW, new_name="Kenya")
        beans.debit("c1", 15, NOW + MIN, bean_id="b1")
        before = [dict(bag) for bag in beans.bags], list(beans.known)
        op = beans.add(250, NOW + 2 * MIN, new_name="Ethiopia")
        self.assertTrue(beans.undo(op, NOW + 5 * MIN))
        self.assertEqual((beans.bags, beans.known), before)
        self.assertNotEqual(op, first)

    def test_undo_reapplies_later_debits_to_both_bags(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya")
        beans.add(400, NOW + MIN, new_name="Ethiopia")
        op = beans.add(250, NOW + 2 * MIN, bean_id="b1")
        beans.debit("c1", 15, NOW + 3 * MIN, bean_id="b1")
        beans.debit("c2", 18, NOW + 4 * MIN, bean_id="b2")
        self.assertTrue(beans.undo(op, NOW + 5 * MIN))
        self.assertEqual([(bag["bean_id"], bag["remaining_g"]) for bag in beans.bags],
                         [("b2", 382.0), ("b1", 235.0)])
        self.assertFalse(beans.debit("c2", 18, NOW + 6 * MIN, bean_id="b2"))

    def test_undo_does_not_charge_the_wrong_bag(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya")
        op = beans.add(500, NOW + MIN, new_name="Ethiopia")
        beans.debit("c1", 18, NOW + 2 * MIN, bean_id="b2")
        self.assertTrue(beans.undo(op, NOW + 3 * MIN))
        self.assertEqual(beans.bag("b1")["remaining_g"], 250.0)
        self.assertIsNone(beans.bag("b2"))
        self.assertEqual((beans.ops[-1]["bean_id"], beans.ops[-1]["applied"]), ("b2", False))

    def test_a_bag_id_from_an_undone_add_is_never_reused(self):
        beans = cbeans.Beans()
        op = beans.add(250, NOW, new_name="Kenya")
        self.assertTrue(beans.undo(op, NOW + MIN))
        restored = cbeans.Beans(beans.to_dict())
        restored.add(250, NOW + 2 * MIN, new_name="Ethiopia")
        self.assertFalse(restored.debit("c1", 15, NOW + 3 * MIN, bean_id="b1"))
        self.assertEqual(restored.bag("b2")["remaining_g"], 250.0)

    def test_undo_keeps_a_later_manual_empty(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya")
        beans.add(500, NOW + MIN, new_name="Ethiopia")
        op = beans.add(250, NOW + 2 * MIN, bean_id="b1")
        beans.empty("b2", NOW + 3 * MIN)
        self.assertTrue(beans.undo(op, NOW + 4 * MIN))
        self.assertEqual(beans.bag("b1")["remaining_g"], 250.0)
        self.assertIsNone(beans.bag("b2"))

    def test_undo_retag_credits_an_old_bag_closed_by_the_add(self):
        beans = cbeans.Beans()
        beans.add(30, NOW, new_name="Kenya")
        beans.add(100, NOW, new_name="Ethiopia")
        beans.debit("c1", 18, NOW + MIN, bean_id="b1")
        op = beans.add(250, NOW + 2 * MIN, new_name="Colombia", dose_g=18)
        self.assertIsNone(beans.bag("b1"))
        self.assertEqual(beans.retag("c1", "b1", 18, "b2", 18, NOW + 3 * MIN), 18)

        self.assertTrue(beans.undo(op, NOW + 4 * MIN))
        self.assertEqual([(bag["bean_id"], bag["remaining_g"]) for bag in beans.bags],
                         [("b2", 82.0), ("b1", 30.0)])

    def test_undo_retag_credits_a_debit_that_only_applied_during_replay(self):
        beans = cbeans.Beans()
        beans.add(12, NOW, new_name="Kenya")
        beans.add(100, NOW, new_name="Ethiopia")
        op = beans.add(250, NOW + MIN, new_name="Colombia", dose_g=18)
        self.assertFalse(beans.debit("c1", 18, NOW + 2 * MIN, bean_id="b1"))
        self.assertEqual(beans.retag("c1", "b1", 0, "b2", 18, NOW + 3 * MIN), 18)

        self.assertTrue(beans.undo(op, NOW + 4 * MIN))
        self.assertEqual([(bag["bean_id"], bag["remaining_g"]) for bag in beans.bags],
                         [("b2", 82.0), ("b1", 12.0)])

    def test_retag_after_undo_does_not_credit_a_debit_that_no_longer_applies(self):
        beans = cbeans.Beans()
        beans.add(40, NOW, new_name="Kenya")
        beans.add(100, NOW, new_name="Ethiopia")
        beans.empty("b2", NOW)
        beans.debit("c1", 18, NOW + MIN, bean_id="b1")
        op = beans.add(100, NOW + 2 * MIN, bean_id="b2")
        beans.retag("c1", "b1", 18, "b2", 18, NOW + 3 * MIN)
        self.assertTrue(beans.undo(op, NOW + 4 * MIN))
        self.assertEqual(beans.bag("b1")["remaining_g"], 40.0)
        self.assertIsNone(beans.bag("b2"))

        beans.add(100, NOW + 5 * MIN, bean_id="b2")
        beans.retag("c1", "b2", 18, "b1", 18, NOW + 6 * MIN)
        self.assertEqual([(bag["bean_id"], bag["remaining_g"]) for bag in beans.bags],
                         [("b2", 100.0), ("b1", 22.0)])

    def test_undo_only_the_newest_add_and_only_within_ten_minutes(self):
        beans = cbeans.Beans()
        first = beans.add(250, NOW, new_name="Kenya")
        second = beans.add(100, NOW + MIN)
        self.assertFalse(beans.undo(first, NOW + 2 * MIN))
        self.assertFalse(beans.undo(second, NOW + 12 * MIN))
        self.assertFalse(beans.undo("nope", NOW + 2 * MIN))
        self.assertEqual(beans.bag("b1")["remaining_g"], 350.0)

    def test_the_view_offers_the_undo_until_its_deadline(self):
        beans = cbeans.Beans()
        op = beans.add(250, NOW, new_name="Kenya")
        view = beans.view(15.0, NOW + MIN)
        self.assertEqual((view["last_op_id"], view["undo_until"]), (op, (NOW + 10 * MIN).isoformat(timespec="seconds")))
        self.assertNotIn("last_op_id", beans.view(15.0, NOW + 11 * MIN))

    def test_bags_survive_a_round_trip(self):
        beans = cbeans.Beans()
        beans.add(250, NOW, new_name="Kenya")
        beans.add(500, NOW + MIN, new_name="Ethiopia")
        beans.debit("c1", 15, NOW + 2 * MIN, bean_id="b1")
        again = cbeans.Beans(json.loads(json.dumps(beans.to_dict())))
        self.assertEqual(again.to_dict(), beans.to_dict())
        self.assertEqual(again.view(15.0, NOW + 2 * MIN), beans.view(15.0, NOW + 2 * MIN))


class MigrationTests(unittest.TestCase):
    def test_legacy_container_and_undo_log_survive_reload(self):
        at = NOW.isoformat(timespec="seconds")
        daybreak = {"bean_id": "b1", "remaining_g": 217.0, "added_at": at}
        known = [{"id": "b1", "name": "Daybreak"}]
        legacy = {
            "container": dict(daybreak, remaining_g=298.0), "known": known, "seq": 3,
            "ops": [{"id": "o2", "kind": "add", "at": at, "grams": 100.0,
                     "before": {"container": daybreak, "known": known}},
                    {"id": "o3", "kind": "debit", "at": at, "brew_id": "c1", "grams": 19.0, "applied": True}],
        }
        beans = cbeans.Beans(legacy)
        self.assertEqual(beans.bags, [legacy["container"]])
        self.assertNotIn("container", beans.to_dict())
        again = cbeans.Beans(json.loads(json.dumps(beans.to_dict())))
        self.assertTrue(again.undo("o2", NOW + MIN))
        self.assertEqual(again.bags, [dict(daybreak, remaining_g=198.0)])

    def test_undo_of_legacy_switch_does_not_charge_the_previous_bean(self):
        at = NOW.isoformat(timespec="seconds")
        daybreak = {"bean_id": "b1", "remaining_g": 217.0, "added_at": at}
        known = [{"id": "b1", "name": "Daybreak"}]
        beans = cbeans.Beans({
            "container": dict(daybreak, bean_id="b2", remaining_g=698.0),
            "known": known + [{"id": "b2", "name": "Signature Brew"}], "seq": 3,
            "ops": [{"id": "o2", "kind": "add", "at": at, "grams": 500.0,
                     "before": {"container": daybreak, "known": known}},
                    {"id": "o3", "kind": "debit", "at": at, "brew_id": "c1", "grams": 19.0, "applied": True}],
        })
        self.assertTrue(beans.undo("o2", NOW + MIN))
        self.assertEqual(beans.bags, [daybreak])


class RecipeTests(unittest.TestCase):
    def test_named_recipe_and_the_single_bag_fallback(self):
        beans = cbeans.Beans()
        beans.add(214, NOW, new_name="Daybreak")
        self.assertEqual(beans.for_recipe("4 Pour Recipe"), beans.bean("b1"))
        self.assertEqual(beans.for_recipe(None), beans.bean("b1"))
        beans.add(500, NOW + MIN, new_name="Signature Brew")
        self.assertEqual(beans.for_recipe(" SIGNATURE   BRÉEW "), beans.bean("b2"))
        self.assertIsNone(beans.for_recipe("4 Pour Recipe"))
        self.assertIsNone(beans.for_recipe(None))
        self.assertEqual(beans.primary("Daybreak"), beans.bean("b1"))
        self.assertEqual(beans.primary("4 Pour Recipe"), beans.bean("b2"))
        recipes = [{"id": "Signature BrÉew", "name": "Signature BrÉew"}, {"id": "4", "name": "4 Pour Recipe"}]
        self.assertEqual(beans.recipe_for("b2", recipes), recipes[0])
        self.assertIsNone(beans.recipe_for("b1", recipes))


class ViewTests(unittest.TestCase):
    def test_brews_left_and_low(self):
        beans = cbeans.Beans()
        beans.add(112, NOW, new_name="Kenya Kiambu")
        view = beans.view(15.0, NOW)
        self.assertEqual((view["remaining"], view["brews_left"], view["low"]), (112, 7, False))
        beans.debit("c1", 80, NOW, bean_id="b1")
        self.assertTrue(beans.view(15.0, NOW)["low"])
        self.assertEqual(view["known_beans"], [{"id": "b1", "name": "Kenya Kiambu"}])


if __name__ == "__main__":
    unittest.main()
