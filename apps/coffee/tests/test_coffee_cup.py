# tests/test_coffee_cup.py - whether a cup stands on the scale, replayed from the scale frames recorded on 20.10:
# a wake with the scale empty, the magnet cup, the coffee cup on and off, a wake with the coffee cup on and it
# lifted, glitches, the sleep reading, a brew and its pickup.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_cup as cc  # noqa: E402

# Recorder, sensor.xbloom_studio_scale_weight, 20.10 11:49:38-11:59:37Z: woken by the Connect with the scale empty,
# the magnet cup (39.6 g) put on, the coffee cup put on beside it (310.3 g both), the magnet cup lifted, the coffee
# cup lifted, put back, one bad frame, then the machine falling asleep.
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
    def test_woken_empty_the_magnet_cup_never_counts_the_coffee_cup_does(self):
        cup = cc.Cup()
        cup.zero()
        self.assertEqual(play(cup, EMPTY_WAKE), [(78.295, "present"), (155.636, "absent"), (263.615, "present")])
        self.assertEqual((cup.state, cup.level), ("present", 270.8))

    def test_the_magnet_cup_alone(self):
        cup = cc.Cup()
        cup.zero()
        self.assertEqual(play(cup, EMPTY_WAKE[:3], until=70), [])
        self.assertEqual(cup.state, "unknown")
        cup = cc.Cup()
        cup.zero()
        self.assertEqual(play(cup, [(10, -39.64)], until=60), [])
        self.assertEqual(cup.state, "unknown")

    def test_woken_with_the_cup_on_it_reads_nothing_until_the_cup_is_lifted(self):
        cup = cc.Cup()
        cup.zero()
        self.assertEqual(cup.state, "unknown")
        self.assertEqual(play(cup, CUP_WAKE), [(156.087, "absent")])
        self.assertEqual((cup.state, cup.level), ("absent", -270.6))

    def test_putting_it_back_after_a_lift_is_present_again(self):
        cup = cc.Cup()
        cup.zero()
        play(cup, CUP_WAKE[:3])
        self.assertEqual(play(cup, [(200.0, -150.0), (200.2, 0.2)], until=210), [(202.0, "present")])

    def test_the_sleep_reading_is_no_reading(self):
        cup = cc.Cup()
        cup.zero()
        play(cup, CUP_WAKE[:3])
        self.assertFalse(cup.reading(600, 50.0))
        self.assertIsNone(cup.due())
        self.assertFalse(cup.settle(700))
        self.assertEqual(cup.state, "absent")
        cup.asleep()
        self.assertEqual((cup.state, cup.level), ("unknown", 0.0))


class RuleTests(unittest.TestCase):
    def test_a_step_must_hold_two_seconds(self):
        cup = cc.Cup()
        self.assertFalse(cup.reading(0, 270.8))
        self.assertEqual(cup.due(), 2.0)
        self.assertFalse(cup.settle(1.9))
        self.assertTrue(cup.settle(2.0))
        self.assertEqual(cup.state, "present")

    def test_a_step_that_falls_back_is_dropped(self):
        cup = cc.Cup()
        cup.reading(0, 270.8)
        cup.reading(1, 20.0)
        self.assertIsNone(cup.due())
        self.assertFalse(cup.settle(5))
        self.assertEqual(cup.state, "unknown")

    def test_ninety_nine_grams_is_no_cup(self):
        cup = cc.Cup()
        cup.reading(0, 99.9)
        self.assertFalse(cup.settle(10))
        cup.reading(11, 100.0)
        self.assertTrue(cup.settle(13))
        self.assertEqual(cup.state, "present")

    def test_a_brew_its_zero_its_coffee_and_its_pickup(self):
        cup = cc.Cup()
        cup.reading(0, 163.35)
        cup.settle(2)
        self.assertEqual(cup.state, "present")
        self.assertTrue(cup.zero())
        self.assertEqual(cup.state, "unknown")
        play(cup, [(t, 0.85 * t) for t in range(1, 246)])
        self.assertEqual(cup.state, "present")
        self.assertFalse(cup.put_on())
        self.assertEqual(play(cup, [(330.0, 221.22), (330.2, -72.72), (330.4, -268.57), (330.6, -276.63)]),
                         [(332.2, "absent")])
        self.assertFalse(cup.lifted())
        self.assertEqual(cup.level, -276.63)

    def test_a_brew_left_on_the_scale_is_present_even_without_frames(self):
        cup = cc.Cup()
        cup.zero()
        self.assertTrue(cup.put_on())
        self.assertEqual((cup.state, cup.level), ("present", 0.0))
        self.assertTrue(cup.lifted())
        self.assertEqual(cup.state, "absent")

    def test_unseen_forgets_the_state_and_keeps_the_level(self):
        cup = cc.Cup()
        cup.zero()
        play(cup, CUP_WAKE[:3])
        self.assertTrue(cup.unseen())
        self.assertEqual((cup.state, cup.level), ("unknown", -270.6))
        self.assertEqual(play(cup, [(400.0, -0.4)], until=410), [(402.0, "present")])

    def test_the_level_survives_a_restart(self):
        cup = cc.Cup()
        cup.zero()
        play(cup, CUP_WAKE[:3])
        again = cc.Cup.from_dict(cup.to_dict())
        self.assertEqual((again.state, again.level), ("unknown", -270.6))
        self.assertEqual((cc.Cup.from_dict(None).level, cc.Cup.from_dict({"level": "x"}).level), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
