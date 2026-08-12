"""Unit tests for the shared brightness-ramp mechanics (no AppDaemon runtime).

The service call emitted here is the one wakeup_bedroom used to make inline. Its
exact shape is pinned on purpose: the wake ramp's own tests assert on it, so a change
to these kwargs is a change to the alarm, not a refactor.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_LIGHTS_DIR = Path(__file__).resolve().parents[1]
if str(_LIGHTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTS_DIR))

import light_ramp  # noqa: E402


class SetBrightness(unittest.TestCase):
    def test_emits_the_exact_light_turn_on_the_wake_ramp_expects(self):
        hass = MagicMock()
        returned = light_ramp.set_brightness(hass, "light.bedroom_bed_lights", 56, transition=60)
        hass.call_service.assert_called_once_with(
            "light/turn_on",
            entity_id="light.bedroom_bed_lights",
            brightness_pct=56,
            transition=60,
        )
        self.assertEqual(returned, 56)

    def test_transition_is_omitted_when_not_asked_for(self):
        hass = MagicMock()
        light_ramp.set_brightness(hass, "light.x", 40)
        self.assertNotIn("transition", hass.call_service.call_args.kwargs)

    def test_a_junk_transition_is_dropped_rather_than_raised(self):
        hass = MagicMock()
        light_ramp.set_brightness(hass, "light.x", 40, transition="soon")
        self.assertNotIn("transition", hass.call_service.call_args.kwargs)
        self.assertEqual(hass.call_service.call_args.kwargs["brightness_pct"], 40)

    def test_never_commands_zero_percent(self):
        hass = MagicMock()
        self.assertEqual(light_ramp.set_brightness(hass, "light.x", 0), 1)

    def test_clamps_above_one_hundred(self):
        hass = MagicMock()
        self.assertEqual(light_ramp.set_brightness(hass, "light.x", 140), 100)


class RampArithmetic(unittest.TestCase):
    def test_start_pct_never_below_one(self):
        self.assertEqual(light_ramp.start_pct(0), 1)
        self.assertEqual(light_ramp.start_pct(-5), 1)
        self.assertEqual(light_ramp.start_pct(1), 1)
        self.assertEqual(light_ramp.start_pct(12), 12)

    def test_next_pct_steps_by_the_configured_amount(self):
        self.assertEqual(light_ramp.next_pct(50, 6, 90), 56)

    def test_next_pct_never_overshoots_the_target(self):
        self.assertEqual(light_ramp.next_pct(88, 6, 90), 90)
        self.assertEqual(light_ramp.next_pct(90, 6, 90), 90)

    def test_next_pct_survives_junk(self):
        self.assertEqual(light_ramp.next_pct(None, 6, 90), 7)
        self.assertEqual(light_ramp.next_pct(50, None, 90), 51)
        self.assertEqual(light_ramp.next_pct(50, 6, None), 50)

    def test_resolve_target_caps_adaptive_lighting_at_the_ramp_ceiling(self):
        self.assertEqual(light_ramp.resolve_target(100, 90), 90)
        self.assertEqual(light_ramp.resolve_target(45, 90), 45)

    def test_resolve_target_falls_back_to_the_ceiling_without_a_reading(self):
        self.assertEqual(light_ramp.resolve_target(None, 90), 90)
        self.assertEqual(light_ramp.resolve_target("n/a", 90), 90)


class AdaptiveLightingHandover(unittest.TestCase):
    def test_pause_turns_the_switch_off(self):
        hass = MagicMock()
        light_ramp.pause_adaptive_brightness(hass, "switch.al_brightness")
        hass.turn_off.assert_called_once_with("switch.al_brightness")

    def test_resume_turns_the_switch_on_and_logs(self):
        hass = MagicMock()
        lines = []
        light_ramp.resume_adaptive_brightness(hass, "switch.al_brightness", log_fn=lines.append)
        hass.turn_on.assert_called_once_with("switch.al_brightness")
        self.assertEqual(len(lines), 1)

    def test_no_switch_configured_is_a_noop(self):
        hass = MagicMock()
        light_ramp.pause_adaptive_brightness(hass, None)
        light_ramp.resume_adaptive_brightness(hass, None)
        hass.turn_off.assert_not_called()
        hass.turn_on.assert_not_called()

    def test_neither_ever_raises(self):
        """Both run from paths where an exception strands the switch - resume especially,
        which is the only thing that ever gives brightness back to Adaptive Lighting."""
        hass = MagicMock()
        hass.turn_off.side_effect = RuntimeError("boom")
        hass.turn_on.side_effect = RuntimeError("boom")
        light_ramp.pause_adaptive_brightness(hass, "switch.al")
        light_ramp.resume_adaptive_brightness(hass, "switch.al")


if __name__ == "__main__":
    unittest.main()
