"""Airing allowance follows the opening, independent of floor-heating cycles."""

import asyncio
from datetime import datetime, timedelta, timezone
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
THERMOSTAT = "climate.family_room_thermostat"
SEASON = "input_boolean.heating_season"
ACTIVE = "binary_sensor.living_room_heating_active"


class OpeningReminder(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc)
        self.states = {DOOR: "off", WINDOW: "off", THERMOSTAT: "heat", SEASON: "on", ACTIVE: "off"}
        self.changed = {}
        self.pending = {}
        self.notifier = MagicMock()
        self.notifier.notify = AsyncMock()
        self.app = OpenWhileHeatingGuard.__new__(OpenWhileHeatingGuard)
        self.app.args = yaml.safe_load((APPS / "open_while_heating_guard.yaml").read_text())[
            "open_while_heating_guard"
        ]
        self.app.get_state = self.get_state
        self.app.get_now = lambda: self.now
        self.app.get_history = MagicMock(return_value=[[
            self.history_row("on", self.now - timedelta(hours=1)),
            self.history_row("off", self.now - timedelta(minutes=30)),
        ]])
        self.app.get_app = lambda name: self.notifier
        self.app.create_task = asyncio.run
        self.app.listen_state = MagicMock()
        self.app.log = MagicMock()
        self.app.fire_event = MagicMock()
        self.app.run_in = self.schedule
        self.app.timer_running = lambda handle: handle in self.pending
        self.app.cancel_timer = lambda handle: self.pending.pop(handle, None)
        self.app.initialize()
        self.app.get_history.assert_not_called()
        self.app._startup_recheck({})

    @staticmethod
    def history_row(state, when):
        return {"state": state, "last_changed": when.isoformat()}

    def get_state(self, entity, attribute=None):
        if attribute == "all":
            return {"state": self.states.get(entity), "last_changed": self.changed.get(entity)}
        return self.states.get(entity)

    def schedule(self, callback, delay, **kwargs):
        handle = object()
        self.pending[handle] = callback, delay, kwargs
        return handle

    def change(self, entity, value):
        old = self.states.get(entity)
        self.states[entity] = value
        self.changed[entity] = self.now.isoformat()
        kwargs = {} if entity == SEASON else {"room": "living_room"}
        self.app._on_change(entity, "state", old, value, kwargs)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def elapse(self, expected_delay=12 * 60):
        handle = self.app._timers["living_room"]
        callback, delay, kwargs = self.pending.pop(handle)
        self.assertEqual(delay, expected_delay)
        self.advance(delay)
        callback(kwargs)

    def test_shipped_rooms_use_their_actual_zone_thermostat(self):
        controller = yaml.safe_load((APPS / "heating_controller.yaml").read_text())["HeatingController"]
        family = controller["zones"]["family"]
        self.assertIn(DOOR, family["openings"])
        for room in ("kitchen", "living_room", "dining_room"):
            self.assertEqual(self.app.rooms[room]["thermostat"], family["thermostat"])
        for room in ("bedroom", "bathroom", "kristines_room", "claudias_room"):
            self.assertEqual(self.app.rooms[room]["thermostat"], f"climate.{room}_thermostat")
        self.assertEqual(self.app.season_entity, controller["season_entity"])
        self.assertEqual(self.app.heating_lookback_s, 24 * 3600)
        for room, cfg in self.app.rooms.items():
            self.assertEqual(cfg["heating"], f"binary_sensor.{room}_heating_active")
        self.app.listen_state.assert_any_call(self.app._on_change, ACTIVE, room="living_room")
        self.app.listen_state.assert_any_call(self.app._on_change, DOOR, room="living_room")
        self.app.listen_state.assert_any_call(self.app._on_change, THERMOSTAT, room="living_room")
        self.app.listen_state.assert_any_call(self.app._on_change, SEASON)

    def test_idle_heat_warns_after_twelve_minutes_and_names_the_door(self):
        self.change(DOOR, "on")
        self.app.fire_event.assert_not_called()
        self.elapse()
        self.assertEqual(self.states[ACTIVE], "off")
        event = self.app.fire_event.call_args.kwargs
        self.assertEqual(event["cause"], "Living room rooftop door open 12+ min in a recently heated room")
        self.assertIn("heated recently", event["effect"])
        push = self.notifier.notify.await_args.kwargs
        self.assertEqual(push["target"], ["mikkel", "kristine", "claudia"])
        self.assertIn("rooftop door", push["message"])
        self.assertNotIn("window", push["message"])
        self.assertEqual(self.app.rooms["claudias_room"]["notify"], ["mikkel", "claudia"])

    def test_heat_stopping_does_not_reset_pending_allowance(self):
        self.change(ACTIVE, "on")
        self.change(DOOR, "on")
        handle = self.app._timers["living_room"]
        self.change(ACTIVE, "off")
        self.assertIs(self.app._timers["living_room"], handle)
        self.elapse()
        self.app.fire_event.assert_called_once()

    def test_short_airing_cancels_and_reopening_gets_a_new_allowance(self):
        self.change(DOOR, "on")
        handle = self.app._timers["living_room"]
        self.advance(300)
        self.change(DOOR, "off")
        self.assertNotIn(handle, self.pending)
        self.assertNotIn("living_room", self.app._opened_at)
        self.change(DOOR, "on")
        self.elapse()
        self.app.fire_event.assert_called_once()

    def test_disabled_or_unavailable_heating_is_silent(self):
        for entity, value in ((SEASON, "off"), (THERMOSTAT, "off"), (THERMOSTAT, "cool"),
                              (THERMOSTAT, "unavailable"), (SEASON, "unknown")):
            with self.subTest(entity=entity, value=value):
                self.change(DOOR, "off")
                self.states[SEASON], self.states[THERMOSTAT] = "on", "heat"
                self.change(entity, value)
                self.change(DOOR, "on")
                self.elapse()
                self.app.fire_event.assert_not_called()

    def test_heating_enabled_after_overdue_opening_warns_immediately(self):
        for entity in (THERMOSTAT, SEASON):
            with self.subTest(entity=entity):
                self.change(DOOR, "off")
                self.change(entity, "off")
                self.change(DOOR, "on")
                self.elapse()
                before = self.app.fire_event.call_count
                self.change(entity, "heat" if entity == THERMOSTAT else "on")
                self.assertEqual(self.app.fire_event.call_count, before + 1)
                self.assertNotIn("living_room", self.app._timers)

    def test_disabling_and_reenabling_does_not_restart_pending_time(self):
        self.change(DOOR, "on")
        handle = self.app._timers["living_room"]
        self.change(THERMOSTAT, "off")
        self.change(SEASON, "off")
        self.change(THERMOSTAT, "heat")
        self.change(SEASON, "on")
        self.assertIs(self.app._timers["living_room"], handle)
        self.elapse()
        self.app.fire_event.assert_called_once()

    def test_window_and_door_share_one_episode_across_heat_cycles(self):
        self.change(DOOR, "on")
        self.elapse()
        self.change(WINDOW, "on")
        self.change(DOOR, "off")
        self.change(THERMOSTAT, "off")
        self.change(THERMOSTAT, "heat")
        self.assertNotIn("living_room", self.app._timers)
        self.app.fire_event.assert_called_once()
        self.change(WINDOW, "off")
        self.change(WINDOW, "on")
        self.elapse()
        self.assertEqual(self.app.fire_event.call_count, 2)
        self.assertIn("Living room window", self.app.fire_event.call_args.kwargs["cause"])
        self.assertEqual(self.notifier.notify.await_count, 2)

    def test_overlapping_openings_keep_original_allowance(self):
        self.change(DOOR, "on")
        started = self.app._opened_at["living_room"]
        self.advance(300)
        self.change(WINDOW, "on")
        self.change(DOOR, "off")
        self.assertEqual(self.app._opened_at["living_room"], started)

    def test_both_openings_are_named_without_splitting_the_episode(self):
        self.change(DOOR, "on")
        self.change(WINDOW, "on")
        self.elapse()
        self.assertIn("window and rooftop door", self.notifier.notify.await_args.kwargs["message"])
        self.app.fire_event.assert_called_once()

    def test_restart_uses_remaining_open_time(self):
        self.states[DOOR] = "on"
        self.changed[DOOR] = (self.now - timedelta(minutes=8)).isoformat()
        self.app._recheck_all()
        self.elapse(expected_delay=4 * 60)
        self.app.fire_event.assert_called_once()

    def test_restart_with_already_overdue_opening_warns_immediately(self):
        self.states[DOOR] = "on"
        self.changed[DOOR] = (self.now - timedelta(minutes=20)).isoformat()
        self.app._recheck_all()
        self.assertNotIn("living_room", self.app._timers)
        self.app.fire_event.assert_called_once()

    def test_unknown_contact_recovery_does_not_duplicate_report(self):
        self.change(DOOR, "on")
        self.elapse()
        started = self.app._opened_at["living_room"]
        self.change(DOOR, "unavailable")
        self.assertEqual(self.app._opened_at["living_room"], started)
        self.change(DOOR, "on")
        self.app.fire_event.assert_called_once()
        self.assertNotIn("living_room", self.app._timers)

    def test_unknown_contact_recovery_keeps_elapsed_allowance(self):
        self.change(DOOR, "on")
        self.advance(300)
        self.change(DOOR, "unknown")
        self.assertNotIn("living_room", self.app._timers)
        self.advance(500)
        self.change(DOOR, "on")
        self.app.fire_event.assert_called_once()

    def test_restart_without_valid_timestamp_grants_full_allowance(self):
        for stamp in (None, "unknown", "2026-10-27T19:00:00", (self.now + timedelta(hours=1)).isoformat()):
            with self.subTest(stamp=stamp):
                self.change(DOOR, "off")
                self.states[DOOR] = "on"
                self.changed[DOOR] = stamp
                self.app._recheck_all()
                before = self.app.fire_event.call_count
                self.elapse()
                self.assertEqual(self.app.fire_event.call_count, before + 1)

    def test_heat_mode_without_actual_use_is_silent(self):
        self.app._last_heated.clear()
        self.change(DOOR, "on")
        self.elapse()
        self.app.fire_event.assert_not_called()
        self.assertNotIn("living_room", self.app._reported)

    def test_confirmed_recent_heat_expires_after_twenty_four_hours(self):
        self.app._last_heated["living_room"] = self.now.timestamp() - 24 * 3600 + 1
        self.assertTrue(self.app._heating_enabled("living_room"))
        self.advance(2)
        self.assertFalse(self.app._heating_enabled("living_room"))

    def test_current_heat_qualifies_even_when_run_started_over_a_day_ago(self):
        self.app._last_heated["living_room"] = self.now.timestamp() - 30 * 3600
        self.states[ACTIVE] = "on"
        self.assertTrue(self.app._heating_enabled("living_room"))
        self.change(ACTIVE, "off")
        ended = self.now.timestamp()
        self.assertEqual(self.app._last_heated["living_room"], ended)
        self.advance(24 * 3600 - 1)
        self.assertTrue(self.app._heating_enabled("living_room"))
        self.advance(2)
        self.assertFalse(self.app._heating_enabled("living_room"))

    def test_heat_begins_while_opening_overdue_warns_immediately(self):
        self.app._last_heated.clear()
        self.change(DOOR, "on")
        self.elapse()
        self.app.fire_event.assert_not_called()
        self.change(ACTIVE, "on")
        self.app.fire_event.assert_called_once()

    def test_history_initial_off_is_not_proof_of_heating(self):
        self.app._last_heated.clear()
        self.app.get_history.return_value = [[self.history_row("off", self.now - timedelta(hours=1))]]
        self.app._startup_recheck({})
        self.assertFalse(self.app._heating_enabled("living_room"))
        self.assertEqual(self.app._last_heated, {})

    def test_history_long_run_uses_recent_end_not_old_start(self):
        self.app._last_heated.clear()
        self.app.get_history.return_value = [[
            self.history_row("on", self.now - timedelta(hours=30)),
            self.history_row("off", self.now - timedelta(hours=2)),
        ]]
        self.app._startup_recheck({})
        self.assertTrue(self.app._heating_enabled("living_room"))
        self.assertEqual(self.app._last_heated["living_room"], self.now.timestamp() - 2 * 3600)
        call = self.app.get_history.call_args.kwargs
        self.assertEqual(call["start_time"], self.now - timedelta(hours=24))
        self.assertTrue(call["minimal_response"])
        self.assertTrue(call["no_attributes"])

    def test_history_unknown_gap_does_not_extend_confirmed_heat(self):
        self.app._last_heated.clear()
        self.app.get_history.return_value = [[
            self.history_row("on", self.now - timedelta(hours=30)),
            self.history_row("unavailable", self.now - timedelta(hours=2)),
            self.history_row("off", self.now - timedelta(hours=1)),
        ]]
        self.app._startup_recheck({})
        self.assertFalse(self.app._heating_enabled("living_room"))
        self.assertEqual(self.app._last_heated["living_room"], self.now.timestamp() - 30 * 3600)

    def test_live_unknown_to_off_is_not_proof_of_recent_heat(self):
        self.app._last_heated.clear()
        self.change(ACTIVE, "unknown")
        self.change(ACTIVE, "off")
        self.assertFalse(self.app._heating_enabled("living_room"))

    def test_history_cannot_overwrite_newer_live_heat(self):
        self.app._last_heated.clear()

        def history_during_live_event(**kwargs):
            self.change(ACTIVE, "on")
            self.change(ACTIVE, "off")
            return [[self.history_row("on", self.now - timedelta(hours=2)),
                     self.history_row("off", self.now - timedelta(hours=1))]]

        self.app.get_history.side_effect = history_during_live_event
        self.app._startup_recheck({})
        self.assertEqual(self.app._last_heated["living_room"], self.now.timestamp())

    def test_history_failure_is_silent_until_actual_heat_observed(self):
        self.app._last_heated.clear()
        self.app.get_history.side_effect = RuntimeError("recorder unavailable")
        self.app._startup_recheck({})
        self.change(DOOR, "on")
        self.elapse()
        self.app.fire_event.assert_not_called()
        self.app.log.assert_any_call("living_room: recent heating history unavailable: recorder unavailable", level="WARNING")
        self.change(ACTIVE, "on")
        self.app.fire_event.assert_called_once()

    def test_heat_starts_after_two_minutes_warns_once_and_cancels_grace(self):
        self.app._last_heated.clear()
        self.change(DOOR, "on")
        handle = self.app._timers["living_room"]
        callback, delay, kwargs = self.pending[handle]
        self.advance(120)
        self.change(ACTIVE, "on")
        self.app.fire_event.assert_called_once()
        self.assertNotIn(handle, self.pending)
        self.assertNotIn("living_room", self.app._timers)
        event = self.app.fire_event.call_args.kwargs
        self.assertEqual(event["cause"], "Living room heating started while the rooftop door was open")
        push = self.notifier.notify.await_args.kwargs
        self.assertIn("heating has started", push["message"])
        self.assertNotIn("12+", push["message"])
        self.assertNotIn("12+", event["cause"])
        self.assertEqual(push["target"], ["mikkel", "kristine", "claudia"])
        self.advance(delay)
        callback(kwargs)  # Even an already-dispatched grace callback must stay silent.
        self.change(ACTIVE, "off")
        self.change(ACTIVE, "on")
        self.app.fire_event.assert_called_once()
        self.assertEqual(self.notifier.notify.await_count, 1)

    def test_heat_start_with_closed_openings_is_silent(self):
        self.change(ACTIVE, "on")
        self.app.fire_event.assert_not_called()
        self.assertNotIn("living_room", self.app._reported)

    def test_opening_while_heat_already_on_keeps_twelve_minute_allowance(self):
        self.change(ACTIVE, "on")
        self.change(DOOR, "on")
        self.app.fire_event.assert_not_called()
        self.elapse()
        self.app.fire_event.assert_called_once()
        self.assertIn("12+ min", self.notifier.notify.await_args.kwargs["message"])

    def test_heat_availability_recovery_does_not_warn_early(self):
        for previous in (None, "unknown", "unavailable"):
            with self.subTest(previous=previous):
                self.change(DOOR, "off")
                self.states[ACTIVE] = previous
                self.change(DOOR, "on")
                handle = self.app._timers["living_room"]
                self.advance(120)
                self.change(ACTIVE, "on")
                self.assertIs(self.app._timers["living_room"], handle)
                self.app.fire_event.assert_not_called()

    def test_heat_start_requires_enabled_season_and_thermostat(self):
        for entity, value in ((SEASON, "off"), (THERMOSTAT, "off"), (THERMOSTAT, "cool")):
            with self.subTest(entity=entity, value=value):
                self.change(DOOR, "off")
                self.states[ACTIVE], self.states[SEASON], self.states[THERMOSTAT] = "off", "on", "heat"
                self.change(entity, value)
                self.change(DOOR, "on")
                handle = self.app._timers["living_room"]
                self.change(ACTIVE, "on")
                self.assertIs(self.app._timers["living_room"], handle)
                self.app.fire_event.assert_not_called()

    def test_closure_rearms_early_heat_start_warning(self):
        self.change(DOOR, "on")
        self.change(ACTIVE, "on")
        self.app.fire_event.assert_called_once()
        self.change(DOOR, "off")
        self.change(ACTIVE, "off")
        self.change(DOOR, "on")
        self.change(ACTIVE, "on")
        self.assertEqual(self.app.fire_event.call_count, 2)
        self.assertEqual(self.notifier.notify.await_count, 2)


if __name__ == "__main__":
    unittest.main()
