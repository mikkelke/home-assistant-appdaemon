# tests/test_upcoming_humidifier.py - the bedroom humidifier's refill row on the shipped settings.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import unittest
from pathlib import Path

import yaml

HOME_PULSE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOME_PULSE))
import upcoming_core as core  # noqa: E402
import upcoming_humidifier as humid  # noqa: E402

APP = yaml.safe_load((HOME_PULSE / "house_upcoming.yaml").read_text())["HouseUpcoming"]
CFG = humid.parse(APP)


class RefillRow(unittest.TestCase):
    def test_shipped_settings(self):
        self.assertEqual(CFG, {"entity": "sensor.evaporative_humidifier_271a_water_level", "area": "bedroom"})

    def test_low_and_empty_give_one_valid_bedroom_row(self):
        for level, detail in (("low", "Tank low"), ("empty", "Tank empty, it cannot run")):
            [row] = humid.rows(CFG, level)
            self.assertTrue(core.is_valid_row(row), row)
            self.assertEqual((row["id"], row["kind"], row["area"], row["title"], row["detail"]),
                             ("maint:humidifier:water", "maint", "bedroom", "Refill the humidifier", detail))
            self.assertEqual(row["audience"], "admin")
            self.assertNotIn("at", row)

    def test_other_levels_give_none(self):
        for level in ("medium", "high", "unavailable", "unknown", None):
            self.assertEqual(humid.rows(CFG, level), [])

    def test_unset_turns_it_off_and_a_bad_value_fails(self):
        self.assertIsNone(humid.parse({}))
        self.assertEqual(humid.rows(None, "empty"), [])
        with self.assertRaises(ValueError):
            humid.parse({"humidifier_water": "humidifier.x", "humidifier_area": "bedroom"})
        with self.assertRaises(ValueError):
            humid.parse({"humidifier_water": "sensor.x"})


if __name__ == "__main__":
    unittest.main()
