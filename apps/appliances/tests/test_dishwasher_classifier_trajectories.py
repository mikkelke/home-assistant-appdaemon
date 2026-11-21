"""Programme classification replayed over cumulative-energy trajectories measured from the plug
on real, human-confirmed cycles (minute from start -> kWh used)."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_dishwasher_audit_2026_09 import make_classify_app  # noqa: E402

QUICK_A = {10: 0.14, 20: 0.46, 30: 0.69, 40: 0.93, 50: 1.21, 60: 1.21, 70: 1.21, 82: 1.21}
QUICK_B = {10: 0.24, 20: 0.56, 30: 0.79, 40: 1.02, 50: 1.22, 60: 1.22, 75: 1.22}
ECO_TYPICAL = {10: 0.01, 20: 0.01, 30: 0.15, 40: 0.27, 60: 0.28, 80: 0.30, 90: 0.61,
               100: 0.75, 150: 0.75, 230: 0.76}
ECO_HEAVIEST = {10: 0.17, 20: 0.28, 40: 0.33, 60: 0.34, 65: 0.47, 70: 0.64, 90: 0.77,
                110: 0.87, 230: 0.875}
INTENSIVE = {10: 0.01, 30: 0.13, 40: 0.31, 70: 0.54, 80: 0.85, 120: 0.89, 130: 1.02, 139: 1.27}


def interpolate(points, minute):
    keys = sorted(points)
    if minute <= keys[0]:
        return points[keys[0]]
    for a, b in zip(keys, keys[1:]):
        if a <= minute <= b:
            return points[a] + (points[b] - points[a]) * (minute - a) / (b - a)
    return points[keys[-1]]


def replay(points):
    """Classify every minute of one cycle on one monitor object, as the live tick does."""
    start = datetime(2026, 11, 21, 8, 48, tzinfo=timezone.utc)
    app = make_classify_app(start + timedelta(minutes=10), elapsed_min=10,
                            energy_used_kwh=interpolate(points, 10), power_w="30.0")
    out = {}
    for minute in range(10, max(points) + 1):
        app.now = start + timedelta(minutes=minute)
        app.states[app.energy_sensor] = f"{10.0 + interpolate(points, minute):.3f}"
        out[minute] = app._classify_programme()
    return out


class QuickIsRecognisedFromItsEarlyHeating(unittest.TestCase):
    def _assert_quick_from(self, points, first_minute):
        guesses = replay(points)
        self.assertTrue(all(g == "quick" for m, g in guesses.items() if m >= first_minute),
                        {m: g for m, g in guesses.items() if m >= first_minute and g != "quick"})
        self.assertNotIn("quick", [g for m, g in guesses.items() if m < 15])

    def test_quick_a(self):
        self._assert_quick_from(QUICK_A, 22)

    def test_quick_b(self):
        self._assert_quick_from(QUICK_B, 20)


class EcoIsNeverCalledAnythingElse(unittest.TestCase):
    def _assert_eco(self, points):
        guesses = replay(points)
        self.assertEqual({g for g in guesses.values()} - {"unknown"}, {"eco"}, guesses)

    def test_typical_eco(self):
        self._assert_eco(ECO_TYPICAL)

    def test_heaviest_eco_on_record(self):
        self._assert_eco(ECO_HEAVIEST)


class IntensiveIsNeverCalledQuick(unittest.TestCase):
    def test_intensive_ends_as_intensive(self):
        guesses = replay(INTENSIVE)
        self.assertNotIn("quick", guesses.values())
        self.assertEqual(guesses[139], "intensive")


class LatchResetsForTheNextCycle(unittest.TestCase):
    def test_a_new_cycle_starts_unlatched(self):
        now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
        app = make_classify_app(now, elapsed_min=30, energy_used_kwh=0.7)
        self.assertEqual(app._classify_programme(), "quick")
        app.quick_shape_seen = False
        app.start_time = now - timedelta(minutes=40)
        app.states[app.energy_sensor] = "10.270"
        self.assertEqual(app._classify_programme(), "eco")


if __name__ == "__main__":
    unittest.main()
