"""Real lighting entrypoints preserve state across semantic presence evidence loss."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# AppDaemon exposes sibling app directories; make this suite independent of test order.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds"))

from test_family_room_robot_run_hold import (
    HoldTestCase, ShippedConfigInitialises, shipped_config,
    KITCHEN, DINING, COUNTER, ISLAND, epoch, TRIP,
)
from test_dishwasher_island_signal import (
    make_app as signal_app, dark_solo_states, states_for, PIR, DISHWASHER,
)

import bathroom_lights
import guest_bathroom_lights


def bathroom_app(guest=False, presence="unknown", light="off", shower="off"):
    cls = guest_bathroom_lights.GuestBathroomLights if guest else bathroom_lights.BathroomLights
    app = cls.__new__(cls)
    app.args = {
        "light_entity": "light.main", "all_spots_group": "light.all",
        "main_5_spots_group": "light.main", "bath_spot_light": "light.shower",
        "raw_pir_sensor": "binary_sensor.room_active", "door_sensor": "binary_sensor.door",
        "bathroom_door_sensor": "binary_sensor.door", "switch_event_entity": "event.switch",
        "switch_device_id": "switch", "specific_bath_presence_entity": "binary_sensor.shower",
        "adaptive_lighting_switch": "switch.adaptive", "mikkel_sleep_entity": "input_boolean.sleep",
        "manual_override_boolean": "input_boolean.manual",
        "room_state_text_entity": "sensor.room_state", "darkness_confirmed_sensor_entity": "sensor.darkness",
    }
    app.states = {
        "binary_sensor.room_active": presence, "binary_sensor.shower": shower,
        "light.main": light, "light.shower": light, "light.all": light,
        "input_boolean.sleep": "off", "input_boolean.manual": "off",
        "sensor.darkness": "dark", "sensor.room_state": "Empty (Dark)",
    }
    app.get_state = lambda entity, attribute=None, **kw: app.states.get(entity) if attribute is None else None
    app.turn_on = MagicMock()
    app.turn_off = MagicMock()
    app.call_service = MagicMock()
    app.toggle = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_in = MagicMock()
    app.log = MagicMock()
    app.error = MagicMock()
    app.initialize()
    return app


def presence_event(app, new):
    entity = "binary_sensor.room_active"
    old = app.states[entity]
    app.states[entity] = new
    for call in app.listen_state.call_args_list:
        callback, target = call.args[:2]
        opts = call.kwargs
        if target != entity or opts.get("old", old) != old or opts.get("new", new) != new:
            continue
        callback(entity, None, old, new, {})


class BathroomUnknownTests(unittest.TestCase):
    def test_unknown_does_not_change_on_or_off_lights_on_darkness_push(self):
        for guest in (False, True):
            for light in ("on", "off"):
                for presence in ("unknown", "unavailable", None):
                    with self.subTest(guest=guest, light=light, presence=presence):
                        app = bathroom_app(guest, light=light, presence=presence)
                        if guest:
                            app._check_lights()
                        else:
                            app._on_room_state_push(None, None, None, None, {})
                        app.turn_on.assert_not_called()
                        app.turn_off.assert_not_called()

    def test_recovery_listeners_accept_unknown_to_on_and_unknown_to_off(self):
        for guest in (False, True):
            for new, light in (("on", "off"), ("off", "on")):
                with self.subTest(guest=guest, new=new):
                    app = bathroom_app(guest, light=light)
                    presence_event(app, new)
                    if new == "on":
                        app.turn_on.assert_called()
                        app.turn_off.assert_not_called()
                    else:
                        app.turn_off.assert_called()
                        app.turn_on.assert_not_called()

    def test_manual_override_and_its_expiry_preserve_unknown_room(self):
        for guest in (False, True):
            app = bathroom_app(guest, light="on")
            app._on_manual_override_change("input_boolean.manual", None, "on", "off", {})
            app.turn_off.assert_not_called()
            app.turn_on.assert_not_called()
            app.states["input_boolean.manual"] = "on"
            presence_event(app, "off")
            app.turn_off.assert_not_called()

    def test_bath_door_and_positive_shower_remain_positive_evidence(self):
        app = bathroom_app()
        app._on_bathroom_door_open("binary_sensor.door", None, "off", "on", {})
        app.turn_on.assert_called_once_with("light.all")
        app.turn_on.reset_mock()
        app.states["binary_sensor.shower"] = "on"
        app._evaluate_main_lights()
        app.turn_on.assert_called_once_with("light.all")

    def test_unknown_shower_does_not_turn_its_spot_off_in_bright_room(self):
        app = bathroom_app(presence="off", shower="unknown")
        app.states.update({"sensor.darkness": "bright", "sensor.room_state": "Empty (Bright)",
                           "light.shower": "on"})
        app._on_bath_presence_change(None, None, "on", "unknown", {})
        app._evaluate_main_lights()
        app.turn_off.assert_not_called()
        app.turn_on.assert_not_called()
        app.states["binary_sensor.shower"] = "off"
        app._on_bath_presence_change(None, None, "unknown", "off", {})
        app.turn_off.assert_called_once_with("light.shower")


class FamilyUnknownTests(HoldTestCase):
    def test_bright_signal_survives_kitchen_outage_while_other_room_is_present(self):
        app = self.app(zone="1", darkness="bright", pir="on", lamps_on=(ISLAND,), hold=False)
        app._dishwasher_state_entity = DISHWASHER
        app.states[DISHWASHER] = {"state": "Unemptied"}
        app.states[KITCHEN]["state"] = "on"
        self.evaluate(app)
        app.turn_off.assert_not_called()
        app.states[KITCHEN]["state"] = "unknown"
        app._on_room_state_push(None, None, None, "Occupied (Bright)", {})
        self.evaluate(app)
        app.turn_off.assert_not_called()
        app.turn_on.assert_not_called()
        self.assertEqual(app.states[ISLAND]["state"], "on")

    def test_unknown_kitchen_does_not_create_an_island_signal(self):
        app = self.app(zone="1", darkness="bright", pir="on", hold=False)
        app._dishwasher_state_entity = DISHWASHER
        app.states[DISHWASHER] = {"state": "Unemptied"}
        app.states[KITCHEN]["state"] = "unknown"
        self.evaluate(app)
        app.turn_on.assert_not_called()
        app.turn_off.assert_not_called()
        self.assertEqual(app.states[ISLAND]["state"], "off")

    def test_known_kitchen_absence_or_completed_task_releases_bright_signal(self):
        for kitchen, dishwasher in (("off", "Unemptied"), ("unknown", "Emptied")):
            with self.subTest(kitchen=kitchen, dishwasher=dishwasher):
                app = self.app(zone="1", darkness="bright", pir="on", lamps_on=(ISLAND,), hold=False)
                app._dishwasher_state_entity = DISHWASHER
                app.states[DISHWASHER] = {"state": "Unemptied"}
                app.states[KITCHEN]["state"] = "unknown"
                self.evaluate(app)
                app.turn_off.assert_not_called()
                app.states[KITCHEN]["state"] = kitchen
                app.states[DISHWASHER]["state"] = dishwasher
                self.evaluate(app)
                app.turn_off.assert_called_once_with(ISLAND)
                app.turn_on.assert_not_called()

    def test_unknown_holds_lights_latch_and_session_through_other_triggers(self):
        for lamps in ((), (COUNTER,)):
            app = self.app(zone="1", pir="off", lamps_on=lamps, hold=False)
            app.states[KITCHEN]["state"] = "unknown"
            app._door_arrival_latch = True
            app._presence_session_started_at = epoch(TRIP - 100)
            app._sleep_activated_during_presence = True
            app._on_raw_pir_off(DINING, None, "unknown", "off", {"room": "dining_room"})
            app._on_room_state_push(None, None, "Occupied (Dark)", "Empty (Bright)", {})
            app._on_door_open(None, None, "off", "on", {"doorroom": "bedroom"})
            self.evaluate(app)
            app.turn_on.assert_not_called()
            app.turn_off.assert_not_called()
            self.assertTrue(app._door_arrival_latch)
            self.assertTrue(app._sleep_activated_during_presence)
            self.assertEqual(app._presence_session_started_at, epoch(TRIP - 100))
            self.assertFalse(app._guest_exit_satisfied())

    def test_positive_room_still_acts_when_other_room_is_unknown(self):
        app = self.app(zone="1", pir="on", hold=False)
        app.states[KITCHEN]["state"] = "unknown"
        self.evaluate(app)
        app.turn_on.assert_called()
        app.turn_off.assert_not_called()

    def test_recovery_evaluates_presence_and_then_confirmed_absence(self):
        app = self.app(zone="1", pir="off", hold=False)
        app.states[KITCHEN]["state"] = "unknown"
        self.evaluate(app)
        app.turn_on.assert_not_called()
        app.states[KITCHEN]["state"] = "on"
        app._on_raw_pir_on(KITCHEN, None, "unknown", "on", {"room": "kitchen"})
        self.evaluate(app)
        app.turn_on.assert_called()
        app.turn_on.reset_mock()
        app.states[KITCHEN]["state"] = "off"
        app._on_raw_pir_off(KITCHEN, None, "unknown", "off", {"room": "kitchen"})
        self.evaluate(app)
        app.turn_off.assert_called()
        self.assertIsNone(app._presence_session_started_at)

    def test_unknown_interval_prevents_robot_quiet_claim_even_after_recovery(self):
        app = self.app(vacuum="docked", pir="off", armed=False)
        app.states[KITCHEN]["state"] = "unknown"
        app._on_quiet_sensor_change(KITCHEN, None, "off", "unknown", {})
        self.tick(1)
        app.states[KITCHEN]["state"] = "off"
        app._on_quiet_sensor_change(KITCHEN, None, "unknown", "off", {})
        self.vacuum_event(app, "docked", "cleaning")
        self.assertIsNone(app._robot_quiet_for)

    def test_current_unknown_sensor_prevents_robot_quiet_claim(self):
        app = self.app(vacuum="docked", pir="off", armed=False)
        app.states[KITCHEN]["state"] = "unknown"
        self.vacuum_event(app, "docked", "cleaning")
        self.assertIsNone(app._robot_quiet_for)

    def test_semantic_listeners_accept_recovery_and_track_quiet_gaps(self):
        app = ShippedConfigInitialises().initialised(shipped_config())
        calls = app.listen_state.call_args_list
        for sensor in app._family_presence_sensors.values():
            on = [c for c in calls if c.args[1] == sensor and c.args[0].__name__ == "_on_raw_pir_on"]
            off = [c for c in calls if c.args[1] == sensor and c.args[0].__name__ == "_on_raw_pir_off"]
            self.assertEqual(len(on), 1)
            self.assertEqual(len(off), 1)
            self.assertNotIn("old", on[0].kwargs)
            self.assertNotIn("old", off[0].kwargs)
        for sensor in app._robot_quiet_sensors:
            self.assertTrue(any(c.args[1] == sensor and c.args[0].__name__ == "_on_quiet_sensor_change"
                                and not c.kwargs for c in calls))


class DishwasherUnknownTests(unittest.TestCase):
    def test_unknown_preserves_applied_signal_and_does_not_apply_a_new_one(self):
        for states in (dark_solo_states(pir="unknown"), states_for(pir="unknown")):
            app = signal_app(states)
            app._sync_signal()
            app._restore_normal_layout()
            app.turn_on.assert_not_called()
            app.turn_off.assert_not_called()
            app.call_service.assert_not_called()
            app.run_in.assert_not_called()

    def test_real_task_completion_releases_even_when_presence_unknown(self):
        app = signal_app(dark_solo_states(pir="unknown", dishwasher="Emptied"))
        app._on_dishwasher_state(DISHWASHER, None, "Unemptied", "Emptied", {})
        app.run_in.assert_called_once()
        app._restore_normal_layout()
        app.call_service.assert_called()

    def test_known_absence_recovers_handback(self):
        app = signal_app(dark_solo_states(pir="unknown"))
        app.states[PIR] = "off"
        app._on_pir(PIR, None, "unknown", "off", {})
        app.run_in.assert_called_once()


if __name__ == "__main__":
    unittest.main()
