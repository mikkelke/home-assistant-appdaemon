# tests/test_coffee_who.py - who made a brew and who is told: the order of the evidence and the
# provisional Bermuda rule (near the plug around the start, and moved before it), and who filled the water tank.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_who as cw  # noqa: E402
from coffee_fsm import iso  # noqa: E402

T0 = 1_800_000_000.0
MIN = 60.0


def track(points):
    t = cw.Track()
    for ts, state in points:
        t.add(T0 + ts, state)
    return t


class PickTests(unittest.TestCase):
    def test_the_one_confident_phone_wins(self):
        phones = {"mikkel": (True, {"closest_m": "1.8"}), "claudia": (False, {"closest_m": "0.5"})}
        self.assertEqual(cw.pick(phones), ("mikkel", "closest"))

    def test_of_several_the_clearly_closest_wins(self):
        phones = {"mikkel": (True, {"closest_m": "1.9"}), "claudia": (True, {"closest_m": "0.8"})}
        self.assertEqual(cw.pick(phones), ("claudia", "closest"))

    def test_two_phones_equally_near_name_nobody(self):
        phones = {"mikkel": (True, {"closest_m": "1.2"}), "claudia": (True, {"closest_m": "0.9"})}
        self.assertEqual(cw.pick(phones), (None, "tie"))

    def test_no_confident_phone_names_nobody(self):
        self.assertEqual(cw.pick({"mikkel": (False, {})}), (None, "none_near"))
        self.assertEqual(cw.pick({}), (None, "none_near"))


class BermudaTests(unittest.TestCase):
    def test_near_and_moving_is_confident(self):
        distance = track([(-600, "6.0"), (-120, "3.1"), (-30, "1.6"), (10, "1.4")])
        area = track([(-800, "Bedroom"), (-200, "Kitchen")])
        confident, evidence = cw.bermuda("home", distance, area, T0)
        self.assertTrue(confident)
        self.assertEqual((evidence["closest_m"], evidence["moved"], evidence["updates"]), ("1.4", "area", "3"))

    def test_a_phone_lying_still_is_no_evidence(self):
        distance = track([(-700, "1.5"), (-300, "1.5"), (-20, "1.6")])
        area = track([(-1000, "Kitchen")])
        confident, evidence = cw.bermuda("home", distance, area, T0)
        self.assertFalse(confident)
        self.assertEqual(evidence["moved"], "no")

    def test_a_distance_change_counts_as_moving(self):
        distance = track([(-500, "4.0"), (-60, "1.8")])
        confident, evidence = cw.bermuda("home", distance, track([]), T0)
        self.assertTrue(confident)
        self.assertEqual(evidence["moved"], "distance")

    def test_too_far_too_early_too_late_or_away_is_not_confident(self):
        area = track([(-800, "Bedroom"), (-200, "Kitchen")])
        self.assertFalse(cw.bermuda("home", track([(-500, "5.0"), (-30, "2.5")]), area, T0)[0])
        self.assertFalse(cw.bermuda("home", track([(-500, "5.0"), (-200, "1.0"), (-190, "3.0")]), area, T0)[0])
        self.assertFalse(cw.bermuda("home", track([(-500, "5.0"), (45, "1.0")]), area, T0)[0])
        self.assertFalse(cw.bermuda("not_home", track([(-500, "5.0"), (-30, "1.0")]), area, T0)[0])
        self.assertFalse(cw.bermuda("home", track([(-500, "unknown"), (-30, "unavailable")]), area, T0)[0])

    def test_the_track_keeps_twenty_minutes_and_the_reading_before(self):
        t = track([(-3000, "a"), (-2000, "b"), (-100, "c")])
        self.assertEqual([s for _, s in t.points], ["b", "c"])
        self.assertEqual(t.at(T0 - 900), "b")


class ResolveTests(unittest.TestCase):
    def test_the_order(self):
        self.assertEqual(cw.resolve("kristine", "claudia", "mikkel", "mikkel"), ("kristine", "dashboard_action"))
        self.assertEqual(cw.resolve(None, "claudia", "mikkel", "mikkel"), ("claudia", "chip"))
        self.assertEqual(cw.resolve(None, None, "mikkel", "kristine"), ("mikkel", "bermuda"))
        self.assertEqual(cw.resolve(None, None, None, "kristine"), ("kristine", "sole_occupancy"))
        self.assertEqual(cw.resolve(None, None, None, "kristine", sole_enabled=False), ("none", None))
        self.assertEqual(cw.resolve(), ("none", None))

    def test_a_chip_of_nobody_is_no_person(self):
        self.assertEqual(cw.resolve(None, "none", None, "claudia"), ("claudia", "sole_occupancy"))

    def test_who_is_told(self):
        self.assertEqual(cw.notify_target("mikkel", None), "mikkel")
        self.assertEqual(cw.notify_target("mikkel", "kristine"), "kristine")
        self.assertEqual(cw.notify_target("mikkel", "none"), "none")
        self.assertEqual(cw.notify_target("none", None), "none")

    def test_display_names(self):
        self.assertEqual([cw.display_name(p) for p in ("mikkel", "kristine", "claudia", "none", None)],
                         ["Mikkel", "Kristine", "Claudia", None, None])


def tap(at=0.0, user=None):
    return {"at": iso(T0 + at), "source": "tap", "user": user}


def brew(start, who, brew_id="c1", **extra):
    return {"id": brew_id, "started_at": iso(T0 + start), "ts": iso(T0 + start + 240), "who": who, **extra}


class FillByTests(unittest.TestCase):
    def test_a_tap_followed_by_a_named_brew_is_that_persons(self):
        records = [brew(30, "mikkel")]
        self.assertEqual(cw.fill_by(tap(), records, T0 + 5 * MIN), "mikkel")
        self.assertEqual(cw.fill_by(tap(user="claudia"), records, T0 + 5 * MIN), "mikkel")

    def test_a_shared_account_tap_and_a_brew_nobody_made_is_nobody(self):
        for records in ([brew(30, "none")], [{"id": "c1", "started_at": iso(T0 + 30)}], [brew(30, "bob")]):
            self.assertEqual(cw.fill_by(tap(), records, T0 + 5 * MIN), "none")

    def test_a_personal_account_tap_and_a_brew_nobody_made_is_the_account(self):
        self.assertEqual(cw.fill_by(tap(user="claudia"), [brew(30, "none")], T0 + 5 * MIN), "claudia")
        self.assertEqual(cw.fill_by(tap(user="bob"), [brew(30, "none")], T0 + 5 * MIN), "none")

    def test_with_no_brew_yet_it_is_pending_for_fifteen_minutes(self):
        self.assertIsNone(cw.fill_by(tap(user="claudia"), [], T0))
        self.assertIsNone(cw.fill_by(tap(user="claudia"), [], T0 + 15 * MIN - 1))
        self.assertEqual(cw.fill_by(tap(user="claudia"), [], T0 + 15 * MIN), "claudia")
        self.assertEqual(cw.fill_by(tap(), [], T0 + 15 * MIN), "none")

    def test_a_brew_in_the_ledger_tells_at_once(self):
        self.assertEqual(cw.fill_by(tap(), [brew(20, "kristine")], T0 + 25), "kristine")

    def test_the_first_brew_within_fifteen_minutes_tells(self):
        records = [brew(120, "mikkel", "c2"), brew(60, "none", "c1")]
        self.assertEqual(cw.fill_by(tap(user="claudia"), records, T0 + 30 * MIN), "claudia")
        self.assertEqual(cw.fill_by(tap(), [brew(60, "kristine", "c1"), brew(120, "mikkel", "c2")], T0 + 30 * MIN),
                         "kristine")

    def test_a_brew_that_started_before_the_tap_or_after_the_window_does_not_tell(self):
        records = [brew(-30, "mikkel", "c1"), brew(15 * MIN + 1, "kristine", "c2")]
        self.assertEqual(cw.fill_by(tap(user="claudia"), records, T0 + 30 * MIN), "claudia")
        self.assertIsNone(cw.fill_by(tap(user="claudia"), records, T0 + 5 * MIN))

    def test_the_window_includes_both_ends(self):
        self.assertEqual(cw.fill_by(tap(), [brew(0, "mikkel")], T0 + 30 * MIN), "mikkel")
        self.assertEqual(cw.fill_by(tap(), [brew(15 * MIN, "mikkel")], T0 + 30 * MIN), "mikkel")

    def test_a_record_without_a_start_is_placed_by_its_end(self):
        records = [{"id": "c1", "ts": iso(T0 + 60), "who": "mikkel"}]
        self.assertEqual(cw.fill_by(tap(), records, T0 + 30 * MIN), "mikkel")

    def test_an_inferred_refill_is_the_brew_that_showed_it(self):
        fill = {"at": iso(T0 + 240), "source": "inferred", "user": None, "brew": "c2"}
        records = [brew(0, "mikkel", "c2"), brew(300, "claudia", "c3")]
        self.assertEqual(cw.fill_by(fill, records, T0 + 10 * MIN), "mikkel")
        self.assertEqual(cw.fill_by(fill, [brew(0, "none", "c2")], T0 + 10 * MIN), "none")

    def test_an_inferred_refill_waits_for_its_brew_to_be_written(self):
        fill = {"at": iso(T0 + 240), "source": "inferred", "user": None, "brew": "c2"}
        self.assertIsNone(cw.fill_by(fill, [brew(300, "claudia", "c3")], T0 + 240))
        self.assertEqual(cw.fill_by(fill, [brew(300, "claudia", "c3")], T0 + 240 + 15 * MIN), "none")


class CountTests(unittest.TestCase):
    def test_fills_are_counted_by_who_from_the_start_of_the_window_and_not_while_pending(self):
        fills = [tap(-100 * 86400, "mikkel"), tap(-10 * MIN, "claudia"), tap(-2 * 86400), tap(-86400, "mikkel"),
                 tap(-1 * MIN, "mikkel")]
        records = [brew(-2 * 86400 + 10, "kristine"), brew(-10 * MIN + 10, "none")]
        counts = cw.fill_counts(fills, records, T0, T0 - 60 * 86400)
        self.assertEqual(counts, {"kristine": 1, "claudia": 1, "mikkel": 1})

    def test_the_csv_is_the_most_first_and_nobody_last(self):
        self.assertEqual(cw.counts_csv({"mikkel": 18, "claudia": 2, "none": 15}), "mikkel:18,claudia:2,none:15")
        self.assertEqual(cw.counts_csv({"none": 3, "kristine": 1, "claudia": 1, "mikkel": 1}),
                         "claudia:1,kristine:1,mikkel:1,none:3")

    def test_the_csv_leaves_out_what_is_not_counted(self):
        self.assertEqual(cw.counts_csv({"mikkel": 0, "none": 2}), "none:2")
        self.assertEqual(cw.counts_csv({}), "")

    def test_a_resident_or_nobody(self):
        self.assertEqual([cw.named(w) for w in ("mikkel", "kristine", "claudia", "none", "bob", None)],
                         ["mikkel", "kristine", "claudia", "none", "none", "none"])


if __name__ == "__main__":
    unittest.main()
