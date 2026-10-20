# tests/test_coffee_cup.py - whether a cup stands on the scale, replayed from the scale frames recorded on 20.10:
# a machine woken at rest and readied, then the cup put on; woken with the scale empty and the magnet cup and the
# coffee cup put on and off; woken with the coffee cup on and it lifted; glitches, the sleep reading, a brew and its
# pickup.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_cup as cc  # noqa: E402

# Recorder, sensor.xbloom_studio_scale_weight, 20.10 08:59:15-09:06:47Z: the machine zeroed at rest with the magnet
# cup and the dripper on the scale, both lifted off to ready the beans and the filter (the first frame on the link),
# a touch, then the coffee cup put on. The hold to brew finished at 09:06:52.
AT_REST = [(0.0, -113.64), (404.712, -114.09), (404.923, -113.64),
           (450.590, -105.72), (450.792, 13.91), (450.995, 220.41), (451.176, 163.35), (451.584, 163.22)]

# 11:49:38-11:59:37Z: woken by the Connect with the scale empty, the magnet cup (39.6 g) put on, the coffee cup put
# on beside it (310.3 g both), the magnet cup lifted, the coffee cup lifted, put back, one bad frame, asleep.
EMPTY_WAKE = [
    (21.005, 1.71), (21.409, 39.71), (21.996, 39.64),
    (75.145, 38.21), (75.275, 42.8), (75.480, 38.51), (75.680, 39.61), (75.894, 41.04), (76.295, 192.01),
    (76.477, 310.31), (109.343, 310.44), (113.158, 310.31), (113.748, 310.44), (115.803, 310.31), (117.365, 310.44),
    (119.788, 310.31),
    (124.192, 324.71), (124.401, 276.0), (124.601, 270.8),
    (153.636, 125.84), (153.887, 0.0),
    (261.436, 5.33), (261.615, 313.7), (261.820, 270.9), (262.023, 270.6), (262.228, 270.8),
    (464.193, -963.87), (464.393, 270.8),
    (577.825, 50.0),
]

# 12:14:51-12:24:50Z: woken with the coffee cup on (reads 0.0), the cup lifted, touched for a moment, asleep.
CUP_WAKE = [(153.883, 30.94), (154.087, -222.83), (154.320, -270.6), (290.792, 38.03), (290.922, -270.68),
            (599.632, 50.0)]

MAGNET_G, DRIPPER_G, CUP_G = 39.6, 74.0, 270.8


def play(cup, frames, until=None):
    """Feeds `frames` (seconds, grams) and the settles due between them; [(seconds, state)] at every change."""
    seen = []
    times = [t for t, _ in frames] + ([until] if until is not None else [])
    for i, (t, grams) in enumerate(frames):
        if cup.reading(t, grams):
            seen.append((t, cup.state))
        upto = times[i + 1] if i + 1 < len(times) else t + cc.HOLD_S
        due = cup.due()
        if due is not None and due <= upto and cup.settle(due):
            seen.append((round(due, 3), cup.state))
    return seen


class MeasuredTests(unittest.TestCase):
    def test_woken_at_rest_and_readied_without_the_cup_is_absent_then_the_cup_makes_it_present(self):
        cup = cc.Cup()
        cup.zero()
        self.assertEqual(play(cup, AT_REST), [(2.0, "absent"), (452.995, "present")])

    def test_the_cup_put_on_before_the_dripper_and_the_magnet_cup_come_off_is_present_all_along(self):
        cup = cc.Cup()
        cup.zero()
        frames = [(10, CUP_G), (30, CUP_G - MAGNET_G), (50, CUP_G - MAGNET_G - DRIPPER_G)]
        self.assertEqual(play(cup, frames, until=60), [(12, "present")])

    def test_woken_empty_the_magnet_cup_never_counts_the_coffee_cup_does(self):
        cup = cc.Cup()
        cup.zero()
        self.assertEqual(play(cup, EMPTY_WAKE), [(78.295, "present"), (155.887, "unknown"), (263.615, "present")])
        self.assertEqual(cup.state, "present")

    def test_the_magnet_cup_alone(self):
        cup = cc.Cup()
        cup.zero()
        self.assertEqual(play(cup, EMPTY_WAKE[:3], until=70), [])
        self.assertEqual(play(cup, [(80, -MAGNET_G)], until=120), [])
        self.assertEqual(cup.state, "unknown")

    def test_woken_with_the_cup_on_it_reads_nothing_until_the_cup_is_lifted(self):
        cup = cc.Cup()
        cup.zero()
        self.assertEqual(play(cup, CUP_WAKE), [(156.087, "absent")])
        self.assertEqual(cup.state, "absent")

    def test_the_sleep_reading_and_a_bad_frame_are_no_readings(self):
        cup = cc.Cup()
        cup.zero()
        play(cup, AT_REST[:1])
        self.assertFalse(cup.reading(600, 50.0))
        self.assertIsNone(cup.due())
        self.assertEqual(play(cup, [(700.429, 4321.24), (700.684, -113.6)], until=800), [])
        self.assertEqual(cup.state, "absent")
        cup.asleep()
        self.assertEqual(cup.state, "unknown")


class RuleTests(unittest.TestCase):
    def test_a_reading_must_hold_two_seconds(self):
        cup = cc.Cup()
        self.assertFalse(cup.reading(0, 270.8))
        self.assertEqual(cup.due(), 2.0)
        self.assertFalse(cup.settle(1.9))
        self.assertTrue(cup.settle(2.0))
        self.assertEqual(cup.state, "present")

    def test_a_reading_that_falls_back_is_dropped(self):
        cup = cc.Cup()
        cup.reading(0, 270.8)
        cup.reading(1, 20.0)
        self.assertIsNone(cup.due())
        self.assertFalse(cup.settle(5))
        self.assertEqual(cup.state, "unknown")

    def test_the_line_is_a_hundred_grams_either_way(self):
        self.assertEqual([cc.side(g) for g in (99.9, 100.0, -99.9, -100.0, 0.0)],
                         ["unknown", "present", "unknown", "absent", "unknown"])

    def test_a_cup_lifted_off_where_the_scale_was_zeroed_without_it_says_nothing(self):
        cup = cc.Cup()
        cup.zero()
        play(cup, [(0, CUP_G)])
        self.assertEqual(play(cup, [(10, 0.4)], until=20), [(12, "unknown")])

    def test_a_brew_its_zero_its_coffee_and_its_pickup(self):
        cup = cc.Cup()
        play(cup, [(0, 163.35)])
        self.assertEqual(cup.state, "present")
        self.assertTrue(cup.zero())
        self.assertEqual(cup.state, "unknown")
        play(cup, [(t, 0.85 * t) for t in range(1, 246)])
        self.assertEqual(cup.state, "present")
        self.assertFalse(cup.put_on())
        self.assertEqual(play(cup, [(330.0, 221.22), (330.2, -72.72), (330.4, -268.57), (330.6, -276.63)]),
                         [(332.4, "absent")])
        self.assertFalse(cup.lifted())

    def test_a_brew_left_on_the_scale_is_present_even_without_frames(self):
        cup = cc.Cup()
        cup.zero()
        self.assertTrue(cup.put_on())
        self.assertTrue(cup.lifted())
        self.assertEqual(cup.state, "absent")

    def test_unseen_forgets_the_state_until_the_next_frame(self):
        cup = cc.Cup()
        cup.zero()
        play(cup, AT_REST[:1])
        self.assertTrue(cup.unseen())
        self.assertEqual(cup.state, "unknown")
        self.assertEqual(play(cup, [(400.0, -113.64 + CUP_G)], until=410), [(402.0, "present")])
        self.assertEqual(cc.Cup().state, "unknown")


if __name__ == "__main__":
    unittest.main()
