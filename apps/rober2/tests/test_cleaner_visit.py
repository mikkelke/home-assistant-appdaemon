import asyncio
import sys
import types
import unittest
from datetime import datetime, timedelta
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


class CleanerVisitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.states = {
            "sensor.untracked_visitor": "none",
            "zone.home": "0",
            "person.mikkel": "not_home",
            "person.kristine": "Office",
            "input_boolean.clean_while_home": "on",
            "input_boolean.rober2_enabled": "on",
            "input_boolean.rober2_automation_paused": "off",
            "input_boolean.rober2_clean_hallway": "on",
            "input_boolean.rober2_clean_kitchen": "on",
            "vacuum.rober2": "docked",
            "sensor.rober2_status": "charging",
            "sensor.rober2_battery": "100",
        }
        self.app = control.Rober2Control.__new__(control.Rober2Control)
        self.app.cleaning = False
        self.app.current_room = None
        self.app.room_start_time = None
        self.app.peak_progress = 0
        self.app.person_entities = ["person.mikkel", "person.kristine"]
        self.app.room_config = {"17": "hallway", "22": "kitchen"}
        self.app.room_order = ["17", "22"]
        self.app.door_sensors = {}
        self.app.SEND_RETRY_SEC = 25
        self.app._segment_send_lock = asyncio.Lock()
        self.app._cleaning_session_active = False
        self.app._use_segment_clean_script = False
        self.app.get_state = AsyncMock(side_effect=lambda entity, **kwargs: self.states.get(entity))
        self.app.call_service = AsyncMock()
        self.app._safe_call_service = AsyncMock(return_value=True)
        self.app.run_in = Mock()
        self.app.log = Mock()
        self.app.log_state_conclusion = Mock()
        self.app._set_narrative = AsyncMock()
        self.app._report_house_event = AsyncMock()
        self.app.is_within_cleaning_hours = AsyncMock(return_value=True)

    def tearDown(self):
        errors = [call for call in self.app.log.call_args_list if call.kwargs.get("level") == "ERROR"]
        self.assertEqual(errors, [])

    async def visitor_changed(self, old, new):
        self.states["sensor.untracked_visitor"] = new
        await self.app.handle_visitor_state("sensor.untracked_visitor", "state", old, new, {})

    async def run_evaluation(self):
        callback, delay = self.app.run_in.call_args.args
        self.assertEqual(callback, self.app.evaluate_cleaning_conditions)
        kwargs = self.app.run_in.call_args.kwargs
        self.app.run_in.reset_mock()
        await callback(kwargs)

    async def test_cleaner_arrival_stops_at_95_percent_with_override_on(self):
        self.app.cleaning = True
        self.app.current_room = "17"
        self.app.room_start_time = datetime.now() - timedelta(minutes=5)
        self.app.peak_progress = 95

        await self.visitor_changed("none", "cleaner")

        self.app._safe_call_service.assert_awaited_once_with(
            "vacuum/return_to_base", entity_id="vacuum.rober2"
        )
        self.app._report_house_event.assert_awaited_once_with("Cleaner arrived", "Vacuum went to dock")
        self.assertEqual(self.app._set_narrative.await_args.args, ("Paused - cleaner visiting",))
        self.assertFalse(self.app.cleaning)
        self.assertEqual(self.app.current_room, "17")
        self.app.call_service.assert_not_awaited()

    async def test_cleaner_interruption_does_not_complete_a_95_percent_room(self):
        self.app.cleaning = True
        self.app.current_room = "17"
        self.app.room_start_time = datetime.now() - timedelta(minutes=5)
        self.app.peak_progress = 95
        self.app.mark_room_cleaned = AsyncMock()

        await self.visitor_changed("none", "cleaner")
        await self.app.handle_vacuum_state("vacuum.rober2", "state", "cleaning", "returning", {})

        self.app.call_service.assert_not_awaited()
        self.app.mark_room_cleaned.assert_not_awaited()
        self.app.run_in.assert_not_called()
        self.assertEqual(self.app.current_room, "17")

    async def test_cleaner_blocks_away_when_all_residents_are_away(self):
        self.states["sensor.untracked_visitor"] = "cleaner"
        self.assertFalse(await self.app.is_away())

    async def test_only_cleaner_blocks_away(self):
        for state in ("guest", "none", "unknown", "unavailable", None):
            with self.subTest(state=state):
                self.states["sensor.untracked_visitor"] = state
                self.assertTrue(await self.app.is_away())

    async def test_configured_visitor_entity_is_used(self):
        self.app._visitor_entity = "sensor.other_visitor"
        self.states["sensor.other_visitor"] = "cleaner"
        self.assertFalse(await self.app.is_away())

    async def test_cleaner_departure_schedules_evaluation(self):
        await self.visitor_changed("cleaner", "none")
        self.app.run_in.assert_called_once_with(
            self.app.evaluate_cleaning_conditions, 1, trigger="cleaner_left"
        )
        self.app._safe_call_service.assert_not_awaited()

    async def test_guest_changes_do_nothing(self):
        self.app.cleaning = True
        self.app.peak_progress = 95
        for old, new in (("none", "guest"), ("guest", "none")):
            with self.subTest(old=old, new=new):
                await self.visitor_changed(old, new)
        self.app._safe_call_service.assert_not_awaited()
        self.app._set_narrative.assert_not_awaited()
        self.app._report_house_event.assert_not_awaited()
        self.app.run_in.assert_not_called()
        self.assertTrue(self.app.cleaning)

    async def test_evaluation_blocks_cleaner_despite_override(self):
        self.states["sensor.untracked_visitor"] = "cleaner"
        await self.app.evaluate_cleaning_conditions({"trigger": "clean_while_home_on"})
        self.app._safe_call_service.assert_not_awaited()
        self.app.run_in.assert_not_called()
        self.app._set_narrative.assert_awaited_once_with("Paused - cleaner visiting")

    async def test_next_room_is_not_queued_during_visit_despite_override(self):
        self.states["sensor.untracked_visitor"] = "cleaner"
        await self.app.queue_next_room("room_completed")
        self.app._safe_call_service.assert_not_awaited()
        self.app.run_in.assert_not_called()

    async def test_direct_start_is_blocked_during_visit(self):
        self.states["sensor.untracked_visitor"] = "cleaner"
        await self.app.start_cleaning("17")
        self.app._safe_call_service.assert_not_awaited()
        self.app.run_in.assert_not_called()

    async def test_pending_segment_command_is_blocked_if_cleaner_arrives(self):
        await self.app.start_cleaning("17")
        dispatch = next(call for call in self.app.run_in.call_args_list
                        if call.args[0] == self.app._do_send_segment_clean)
        self.app._safe_call_service.reset_mock()
        self.states["sensor.untracked_visitor"] = "cleaner"
        for use_script in (False, True):
            with self.subTest(use_script=use_script):
                self.app._use_segment_clean_script = use_script
                await dispatch.args[0](dispatch.kwargs)
        self.app._safe_call_service.assert_not_awaited()

    async def test_battery_auto_resume_is_blocked_during_visit(self):
        self.states["sensor.untracked_visitor"] = "cleaner"
        await self.app.handle_battery("sensor.rober2_battery", "state", "79", "80", {})
        await self.run_evaluation()
        self.app._safe_call_service.assert_not_awaited()
        self.app.run_in.assert_not_called()

    async def test_manual_override_is_blocked_during_visit(self):
        self.states["sensor.untracked_visitor"] = "cleaner"
        await self.app.handle_clean_while_home("input_boolean.clean_while_home", "state", "off", "on", {})
        await self.run_evaluation()
        self.app._safe_call_service.assert_not_awaited()
        self.app.run_in.assert_not_called()

    async def test_quiet_hours_end_is_blocked_during_visit(self):
        self.states["sensor.untracked_visitor"] = "cleaner"
        await self.app.check_quiet_hours_end({})
        await self.run_evaluation()
        self.app._safe_call_service.assert_not_awaited()
        self.app.run_in.assert_not_called()

    async def test_cleaner_departure_does_not_resume_when_resident_home_even_with_override(self):
        self.states["zone.home"] = "1"
        self.states["person.mikkel"] = "home"
        await self.visitor_changed("cleaner", "none")
        await self.run_evaluation()
        self.app._safe_call_service.assert_not_awaited()
        self.app.run_in.assert_not_called()

    async def test_cleaner_departure_uses_normal_start_when_away(self):
        await self.visitor_changed("cleaner", "none")
        await self.run_evaluation()
        self.app.run_in.assert_any_call(self.app._do_send_segment_clean, 0, room_id="17")

    async def test_guest_does_not_block_resident_override(self):
        self.states["sensor.untracked_visitor"] = "guest"
        self.states["zone.home"] = "1"
        await self.app.evaluate_cleaning_conditions({"trigger": "clean_while_home_on"})
        self.app.run_in.assert_any_call(self.app._do_send_segment_clean, 0, room_id="17")


if __name__ == "__main__":
    unittest.main()
