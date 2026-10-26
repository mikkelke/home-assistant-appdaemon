"""The shipped openings, actual heating signal, grace and shared open-episode reminder."""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import yaml

APPS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APPS))
if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
        sys.modules[name] = types.ModuleType(name)
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

from open_while_heating_guard import OpenWhileHeatingGuard  # noqa: E402

DOOR = "binary_sensor.rooftop_door_1_contact"
WINDOW = "binary_sensor.living_room_window_contact"
HEATING = "binary_sensor.living_room_heating_active"


class OpeningReminder(unittest.TestCase):
    def setUp(self):
        self.states = {DOOR: "off", WINDOW: "off", HEATING: "off"}
        self.pending = {}
        self.notifier = MagicMock()
        self.notifier.notify = AsyncMock()
        self.app = OpenWhileHeatingGuard.__new__(OpenWhileHeatingGuard)
        self.app.args = yaml.safe_load((APPS / "open_while_heating_guard.yaml").read_text())[
            "open_while_heating_guard"
        ]
        self.app.get_state = lambda entity: self.states.get(entity)
        self.app.get_app = lambda name: self.notifier
        self.app.create_task = asyncio.run
        self.app.listen_state = MagicMock()
        self.app.log = MagicMock()
        self.app.fire_event = MagicMock()
        self.app.run_in = self.schedule
        self.app.timer_running = lambda handle: handle in self.pending
        self.app.cancel_timer = lambda handle: self.pending.pop(handle, None)
        self.app.initialize()

    def schedule(self, callback, delay, **kwargs):
        handle = object()
        self.pending[handle] = callback, delay, kwargs
        return handle

    def change(self, entity, value):
        old = self.states.get(entity)
        self.states[entity] = value
        self.app._on_change(entity, "state", old, value, {"room": "living_room"})

    def elapse(self):
        handle = self.app._timers["living_room"]
        callback, delay, kwargs = self.pending.pop(handle)
        self.assertEqual(delay, 15 * 60)
        callback(kwargs)

    def test_door_matches_the_living_zone_and_living_room_feel(self):
        controller = yaml.safe_load((APPS / "heating_controller.yaml").read_text())["HeatingController"]
        zone = controller["zones"]["living"]
        self.assertIn(DOOR, zone["openings"])
        self.assertNotIn(DOOR, controller["zones"]["family"]["openings"])
        self.assertEqual(zone["thermostat"], "climate.living_room_thermostat")
        feel = next(iter(yaml.safe_load((APPS / "room_feel.yaml").read_text()).values()))
        living = feel["rooms"]["living_room"]
        self.assertIn(DOOR, [c if isinstance(c, str) else c["entity"] for c in living["window_contacts"]])
        self.app.listen_state.assert_any_call(self.app._on_change, DOOR, room="living_room")
        self.assertEqual(self.app.rooms["living_room"]["heating"], HEATING)

    def test_door_alone_reports_after_grace_and_names_the_door(self):
        self.change(DOOR, "on")
        self.assertNotIn("living_room", self.app._timers)
        self.change(HEATING, "on")
        self.app.fire_event.assert_not_called()
        self.elapse()
        event = self.app.fire_event.call_args.kwargs
        self.assertEqual(event["cause"], "Living room rooftop door open 15+ min with heating on")
        push = self.notifier.notify.await_args.kwargs
        self.assertEqual(push["target"], ["mikkel", "kristine", "claudia"])
        self.assertIn("rooftop door", push["message"])
        self.assertNotIn("window", push["message"])

    def test_shadow_plan_and_monitor_cannot_trigger_actual_heating_reminder(self):
        self.states["sensor.heating_zone_family"] = "hold"
        self.states["climate.living_room_thermostat"] = "heat"
        self.change(DOOR, "on")
        self.assertNotIn("living_room", self.app._timers)
        self.app.fire_event.assert_not_called()
        self.states["sensor.heating_zone_family"] = "off_season"
        self.states["climate.living_room_thermostat"] = "off"
        self.change(HEATING, "on")
        self.elapse()
        self.app.fire_event.assert_called_once()

    def test_short_airing_or_heating_stopping_cancels_grace(self):
        self.change(HEATING, "on")
        self.change(DOOR, "on")
        self.change(DOOR, "off")
        self.assertNotIn("living_room", self.app._timers)
        self.change(DOOR, "on")
        self.change(HEATING, "off")
        self.assertNotIn("living_room", self.app._timers)
        self.app.fire_event.assert_not_called()

    def test_window_and_door_share_one_episode_across_heat_cycles(self):
        self.change(HEATING, "on")
        self.change(DOOR, "on")
        self.elapse()
        self.change(WINDOW, "on")
        self.change(DOOR, "off")
        self.change(HEATING, "off")
        self.change(HEATING, "on")
        self.assertNotIn("living_room", self.app._timers)
        self.app.fire_event.assert_called_once()
        self.change(WINDOW, "off")
        self.change(WINDOW, "on")
        self.elapse()
        self.assertEqual(self.app.fire_event.call_count, 2)
        self.assertIn("Living room window", self.app.fire_event.call_args.kwargs["cause"])
        self.assertEqual(self.notifier.notify.await_count, 2)

    def test_both_openings_are_named_without_splitting_the_episode(self):
        self.change(HEATING, "on")
        self.change(DOOR, "on")
        self.change(WINDOW, "on")
        self.elapse()
        self.assertIn("window and rooftop door", self.notifier.notify.await_args.kwargs["message"])
        self.app.fire_event.assert_called_once()


if __name__ == "__main__":
    unittest.main()
