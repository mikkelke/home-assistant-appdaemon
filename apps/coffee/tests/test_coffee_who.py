# tests/test_coffee_who.py - who made a brew and who is told: the order of the evidence and the
# provisional Bermuda rule (near the plug around the start, and moved before it).
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_who as cw  # noqa: E402

T0 = 1_800_000_000.0


def track(points):
    t = cw.Track()
    for ts, state in points:
        t.add(T0 + ts, state)
    return t


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


if __name__ == "__main__":
    unittest.main()
