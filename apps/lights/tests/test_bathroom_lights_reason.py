"""BathroomLights publishes why its lights are as they are."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds"))

from test_lighting_presence_unknown import bathroom_app, presence_event

import bathroom_lights


def reason(app):
    return app.set_state.call_args.kwargs["state"] if app.set_state.call_args else None


class BathroomReason(unittest.TestCase):
    def test_sleep_mode_holds_back_and_says_so(self):
        app = bathroom_app(presence="off")
        app.states["input_boolean.sleep"] = "on"
        app.states["sensor.room_state"] = "Occupied (Dark)"
        presence_event(app, "on")
        app.turn_on.assert_not_called()
        self.assertIn("Mikkel's sleep mode is on", reason(app))
        self.assertIn("wall switch", reason(app))
        self.assertTrue(app.set_state.call_args.kwargs["attributes"]["at"])

    def test_dark_entry_turns_on(self):
        app = bathroom_app(presence="off")
        app.states["sensor.room_state"] = "Occupied (Dark)"
        presence_event(app, "on")
        app.turn_on.assert_called()
        self.assertEqual(reason(app), "Turned on because someone came in and it is dark.")

    def test_vacancy_off_only_when_lights_were_on(self):
        app = bathroom_app(presence="on", light="on")
        presence_event(app, "off")
        self.assertEqual(reason(app), "Turned off because nobody is in the bathroom.")
        self.assertIsNone(bathroom_lights.action_reason("off_vacant", False, None))

    def test_manual_override(self):
        app = bathroom_app(presence="off")
        app.states["input_boolean.manual"] = "on"
        presence_event(app, "on")
        self.assertIn("manual override", reason(app))

    def test_same_reason_is_not_republished(self):
        app = bathroom_app(presence="off")
        app.states["input_boolean.sleep"] = "on"
        presence_event(app, "on")
        presence_event(app, "off")
        presence_event(app, "on")
        self.assertEqual(app.set_state.call_count, 1)

    def test_blind_down_is_named(self):
        text = bathroom_lights.action_reason("blocked", False, "the bedroom blind is down")
        self.assertIn("the bedroom blind is down", text)
        self.assertIsNone(bathroom_lights.action_reason("blocked", True, "x"))


if __name__ == "__main__":
    unittest.main()
