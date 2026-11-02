# tests/test_washer_user_bar.py - the user's programme and temperature set expected_dur_at_start in either direction, so
# a programme picked before its temperature does not leave the cold fallback's duration behind.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import unittest

from test_washer_tail_eta import CONFIRM, TEMPERATURE, running_app


class UserChoiceSetsTheBar(unittest.TestCase):
    def choose(self, app, entity, value):
        old = app.states.get(entity)
        app.states[entity] = value
        app._on_confirm_changed(entity, "state", old, value, {})

    def test_programme_then_a_shorter_temperature_lowers_the_bar(self):
        app, _ = running_app()
        app.states[TEMPERATURE] = ""
        app.expected_dur_at_start = None
        self.choose(app, CONFIRM, "Bomuld")
        cold = app.expected_dur_at_start
        self.choose(app, TEMPERATURE, "60°C")
        self.assertLess(app.expected_dur_at_start, cold)
        self.assertEqual(app.expected_dur_at_start, app._get_programme_duration("bomuld", app._read_temperature_selector(), use_learned=False))
        self.assertEqual(app._guard_bar_class, ("bomuld", app._read_temperature_selector()))

    def test_a_shorter_programme_replaces_a_longer_one(self):
        app, _ = running_app()
        self.choose(app, CONFIRM, "ECO")
        self.choose(app, TEMPERATURE, "60°C")
        self.choose(app, CONFIRM, "Bomuld")
        self.assertEqual(app._guard_bar_class[0], "bomuld")
        self.assertEqual(app.expected_dur_at_start, app._get_programme_duration("bomuld", app._read_temperature_selector(), use_learned=False))


if __name__ == "__main__":
    unittest.main()
