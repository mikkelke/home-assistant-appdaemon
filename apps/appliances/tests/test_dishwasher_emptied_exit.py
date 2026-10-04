# tests/test_dishwasher_emptied_exit.py - the door closing on an Emptied dishwasher must land Off
# even inside the cooling period.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# _transition_to_emptied stamps the cooling clock itself (it forces its own transition), and
# _handle_door_closed cancels emptied_timeout_timer before it asks for Off. A door close between
# min_emptying_seconds (a shorter one is a peek) and cooling_period therefore has no backstop left
# if that Off is refused - the real _handle_door_closed, _transition_to_emptied and
# _transition_to_off run here, only the AppDaemon I/O boundary is faked. Same contract as
# test_dryer_emptied_exit.py.

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hassmod = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hassmod
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

from test_dishwasher_audit_2026_09 import make_live_app  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_emptied_app():
    """Dishwasher that entered Emptied through the real _transition_to_emptied, so the cooling
    clock, emptied_at and the emptied timeout timer are exactly what a door open leaves behind."""
    app = make_live_app(NOW, state="Unemptied", start_minutes_ago=240, power_w="0.0")
    app._transition_to_emptied("Door opened - emptying")
    assert app.states[app.state_entity] == "Emptied"
    return app


def close_door_after(app, seconds):
    app.now = app.now + timedelta(seconds=seconds)
    app.states[app.door_sensor] = "off"
    app._handle_door_closed(app.states[app.state_entity])


class DoorCloseFromEmptiedBypassesCoolingPeriod(unittest.TestCase):
    def test_door_close_inside_cooling_period_lands_off(self):
        """45 s is the first close that is not a peek, 299 s the last one inside cooling_period."""
        for seconds in (45, 60, 299):
            with self.subTest(seconds=seconds):
                app = make_emptied_app()
                self.assertLess(seconds, app.cooling_period)
                close_door_after(app, seconds)
                self.assertEqual(app.state, "Off")
                self.assertEqual(app.states[app.state_entity], "Off")

    def test_door_close_cancels_the_emptied_timer_and_still_arms_fast_start(self):
        app = make_emptied_app()
        emptied_timer = app.emptied_timeout_timer
        self.assertIsNotNone(emptied_timer)
        close_door_after(app, 60)
        self.assertEqual(app.states[app.state_entity], "Off")
        self.assertIn(emptied_timer, app.canceled_timers)
        self.assertIsNone(app.emptied_timeout_timer)
        self.assertEqual(
            app.door_fast_start_armed_until,
            app.now + timedelta(seconds=app.door_close_fast_start_window_s),
        )

    def test_door_close_under_the_peek_threshold_still_reverts_to_unemptied(self):
        app = make_emptied_app()
        close_door_after(app, app.min_emptying_seconds - 1)
        self.assertEqual(app.state, "Unemptied")

    def test_unforced_transition_to_off_is_still_refused_inside_cooling_period(self):
        """force stays opt-in: only the door-close call site bypasses cooling."""
        app = make_emptied_app()
        app.now = app.now + timedelta(seconds=60)
        app._transition_to_off("some other reason")
        self.assertEqual(app.states[app.state_entity], "Emptied")


if __name__ == "__main__":
    unittest.main()
