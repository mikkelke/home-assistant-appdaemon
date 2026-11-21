"""Bright auto-off keeps the shower spot only while someone is in the shower cabin."""

import unittest

from test_lighting_presence_unknown import bathroom_app, presence_event


class BathroomBrightOff(unittest.TestCase):
    def _bright_with_lights_on(self, shower):
        app = bathroom_app(presence="off", light="on", shower=shower)
        app.states["sensor.darkness"] = "bright"
        app.states["sensor.room_state"] = "Occupied (Bright)"
        presence_event(app, "on")
        return [c.args[0] for c in app.turn_off.call_args_list]

    def test_nobody_in_shower_turns_off_all_six(self):
        self.assertEqual(self._bright_with_lights_on("off"), ["light.all"])

    def test_someone_in_shower_keeps_shower_spot(self):
        self.assertEqual(self._bright_with_lights_on("on"), ["light.main"])


if __name__ == "__main__":
    unittest.main()
