"""Unit tests for room_state_darkness (no AppDaemon runtime)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_LIGHTS_DIR = Path(__file__).resolve().parents[1]
if str(_LIGHTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTS_DIR))

import room_state_darkness as rsd  # noqa: E402


class FakeHass:
  def __init__(
    self,
    state: str = "Empty (Dark)",
    attrs: dict | None = None,
    pending=None,
    darkness_state: str | None = None,
  ):
    self._state = state
    self._attrs = dict(attrs or {})
    self._pending = pending
    self._darkness_state = darkness_state

  def get_state(self, entity_id, attribute=None):
    if entity_id == "sensor.darkness_test" and attribute is None:
      return self._darkness_state
    if attribute == "pending_target":
      return self._pending
    if attribute == "all":
      return {"state": self._state, "attributes": self._attrs}
    if attribute is None:
      return self._state
    return self._attrs.get(attribute)


class TestRoomStateDarkness(unittest.TestCase):
  def test_sunny_morning_label_dark_auto_on(self):
    """172 lx + high outdoor but committed (Dark) -> auto-on (no daylit block)."""
    hass = FakeHass(
      "Occupied (Dark)",
      {
        "indoor_lux": 172.0,
        "outdoor_lux": 9800.0,
        "bright_threshold": 250.0,
        "dark_threshold": 160.0,
      },
      darkness_state="dark",
    )
    d = rsd.evaluate_auto_on(
      hass,
      "sensor.room_state_bedroom_bathroom",
      darkness_sensor="sensor.darkness_test",
    )
    self.assertTrue(d.is_dark)
    self.assertEqual(d.rule, "confirmed_dark")

  def test_true_dark_evening_auto_on(self):
    hass = FakeHass(
      "Occupied (Dark)",
      {"indoor_lux": 14.0, "outdoor_lux": 30.0, "bright_threshold": 250.0},
      darkness_state="dark",
    )
    d = rsd.evaluate_auto_on(
      hass,
      "sensor.room_state_bedroom_bathroom",
      darkness_sensor="sensor.darkness_test",
    )
    self.assertTrue(d.is_dark)
    self.assertEqual(d.rule, "confirmed_dark")

  def test_pending_bright_is_ignored_for_auto_off(self):
    """Unconfirmed bright must NOT turn lights off (anti-flap contract)."""
    hass = FakeHass(
      "Occupied (Dark)",
      {"indoor_lux": 200.0, "outdoor_lux": 1000.0, "bright_threshold": 250.0},
      pending="bright",
    )
    d = rsd.evaluate_auto_off(hass, "sensor.room_state_bedroom_bathroom")
    self.assertTrue(d.is_dark)
    self.assertEqual(d.rule, "confirmed_dark")

  def test_pending_dark_blocks_auto_off(self):
    """Confirmed bright but trending dark -> keep lights on (early ON / no hard OFF)."""
    hass = FakeHass(
      "Occupied (Bright)",
      {"indoor_lux": 300.0, "outdoor_lux": 3000.0, "bright_threshold": 250.0},
      pending="dark",
      darkness_state="bright",
    )
    d = rsd.evaluate_auto_off(
      hass,
      "sensor.room_state_x",
      darkness_sensor="sensor.darkness_test",
    )
    self.assertTrue(d.is_dark)
    self.assertEqual(d.rule, "pending_dark")

  def test_confirmed_bright_auto_off(self):
    hass = FakeHass(
      "Occupied (Bright)",
      {"indoor_lux": 360.0, "outdoor_lux": 1000.0, "bright_threshold": 250.0},
      darkness_state="bright",
    )
    d = rsd.evaluate_auto_off(
      hass,
      "sensor.room_state_x",
      darkness_sensor="sensor.darkness_test",
    )
    self.assertFalse(d.is_dark)
    self.assertEqual(d.rule, "confirmed_bright")

  def test_label_dark_when_no_darkness_sensor(self):
    hass = FakeHass(
      "Occupied (Dark)",
      {"indoor_lux": 255.0, "outdoor_lux": 1000.0, "bright_threshold": 250.0},
    )
    d = rsd.evaluate_auto_on(hass, "sensor.room_state_x")
    self.assertTrue(d.is_dark)
    self.assertEqual(d.rule, "confirmed_dark")

  def test_is_dark_for_lights_alias(self):
    hass = FakeHass(
      "Occupied (Bright)",
      {"indoor_lux": 400.0, "bright_threshold": 250.0},
      darkness_state="bright",
    )
    self.assertFalse(
      rsd.is_dark_for_lights(
        hass,
        "sensor.room_state_x",
        darkness_sensor="sensor.darkness_test",
      )
    )


if __name__ == "__main__":
  unittest.main()
