import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import control  # noqa: E402


class DoorSkipRowTests(unittest.IsolatedAsyncioTestCase):
    """A closed door gives a timeline row only when the vacuum would actually have gone in."""

    def setUp(self):
        self.states = {
            "sensor.untracked_visitor": "none",
            "person.mikkel": "home",
            "input_boolean.clean_while_home": "off",
            "input_boolean.rober2_enabled": "on",
            "input_boolean.rober2_automation_paused": "off",
            "input_boolean.rober2_clean_bedroom": "on",
            "input_boolean.rober2_clean_bathroom": "on",
            "binary_sensor.bedroom_door_contact": "off",
            "binary_sensor.bathroom_door_contact": "off",
            "vacuum.rober2": "docked",
            "sensor.rober2_status": "charging",
            "sensor.rober2_battery": "100",
        }
        self.app = control.Rober2Control.__new__(control.Rober2Control)
        self.app.cleaning = False
        self.app.current_room = None
        self.app.person_entities = ["person.mikkel"]
        self.app.room_config = {"30": "bedroom", "31": "bathroom"}
        self.app.room_order = ["30", "31"]
        self.app.door_sensors = {"30": "binary_sensor.bedroom_door_contact", "31": "binary_sensor.bathroom_door_contact"}
        self.app._segment_send_lock = asyncio.Lock()
        self.app._cleaning_session_active = False
        self.app._last_blocked_report = None
        self.app.get_state = AsyncMock(side_effect=self.state)
        self.app.get_rooms_to_clean = AsyncMock(return_value=["30", "31"])
        self.app.call_service = AsyncMock()
        self.app._safe_call_service = AsyncMock(return_value=True)
        self.app.run_in = Mock()
        self.app.log = Mock()
        self.app.log_state_conclusion = Mock()
        self.app._set_narrative = AsyncMock()
        self.app._report_house_event = AsyncMock()
        self.app.is_within_cleaning_hours = AsyncMock(return_value=True)
        self.app.start_cleaning = AsyncMock()

    def state(self, entity, **kwargs):
        if entity == "zone.home":
            return "1" if self.states["person.mikkel"] == "home" else "0"
        return self.states.get(entity)

    def rows(self):
        return [call.args for call in self.app._report_house_event.call_args_list]

    async def evaluate(self):
        await self.app.evaluate_cleaning_conditions({"trigger": "test"})

    async def test_no_row_while_someone_is_home(self):
        await self.evaluate()
        await self.evaluate()
        self.assertEqual(self.rows(), [])

    async def test_one_row_when_the_house_empties_with_doors_closed(self):
        self.states["person.mikkel"] = "not_home"
        await self.evaluate()
        await self.evaluate()
        self.assertEqual(self.rows(), [("Nobody home", "Vacuum didn't start: Bedroom, Bathroom - doors closed")])
        self.app.start_cleaning.assert_not_called()

    async def test_no_row_when_a_door_opens_before_the_house_empties(self):
        self.app.get_rooms_to_clean.return_value = ["30", "31"]
        await self.evaluate()
        self.states["binary_sensor.bedroom_door_contact"] = "on"
        self.states["binary_sensor.bathroom_door_contact"] = "on"
        self.states["person.mikkel"] = "not_home"
        await self.evaluate()
        self.assertEqual(self.rows(), [])
        self.app.start_cleaning.assert_awaited_once_with("30")

    async def test_next_away_period_reports_again(self):
        self.states["person.mikkel"] = "not_home"
        await self.evaluate()
        self.states["person.mikkel"] = "home"
        await self.evaluate()
        self.states["person.mikkel"] = "not_home"
        await self.evaluate()
        self.assertEqual(len(self.rows()), 2)

    async def test_round_ending_at_closed_doors_names_the_skipped_rooms(self):
        self.app._cleaning_session_active = True
        self.app.has_active_error = AsyncMock(return_value=(False, ""))
        self.app._is_cleaner_visiting = AsyncMock(return_value=False)
        await self.app.queue_next_room(trigger="room_completed")
        await self.app.queue_next_room(trigger="vacuum_docked")
        self.assertEqual(self.rows(), [("Vacuum finished the open rooms", "Vacuum skipped Bedroom, Bathroom - doors closed")])
        self.assertFalse(self.app._cleaning_session_active)

    async def test_no_skip_row_outside_a_round(self):
        self.app.has_active_error = AsyncMock(return_value=(False, ""))
        self.app._is_cleaner_visiting = AsyncMock(return_value=False)
        await self.app.queue_next_room(trigger="room_flag_change")
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
