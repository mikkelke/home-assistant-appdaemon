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
    def test_turns_on_while_someone_sleeps(self):
        app = bathroom_app(presence="off")
        app.states["input_boolean.sleep"] = "on"
        app.states["sensor.room_state"] = "Occupied (Dark)"
        presence_event(app, "on")
        app.turn_on.assert_called()
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
        self.assertIsNone(bathroom_lights.action_reason("off_vacant", False))

    def test_manual_override(self):
        app = bathroom_app(presence="off")
        app.states["input_boolean.manual"] = "on"
        presence_event(app, "on")
        self.assertIn("manual override", reason(app))

    def test_same_reason_is_not_republished(self):
        app = bathroom_app(presence="off")
        app.states["input_boolean.manual"] = "on"
        presence_event(app, "on")
        presence_event(app, "off")
        presence_event(app, "on")
        self.assertEqual(app.set_state.call_count, 1)



if __name__ == "__main__":
    unittest.main()
