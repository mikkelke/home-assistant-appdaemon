"""Unit tests for the bedroom_lights bed-light session state machine."""

from __future__ import annotations

import datetime
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_LIGHTS_DIR = Path(__file__).resolve().parents[1]
if str(_LIGHTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTS_DIR))

# bedroom_lights imports cover_util, which lives in apps/blinds
_BLINDS_DIR = Path(__file__).resolve().parents[2] / "blinds"
if str(_BLINDS_DIR) not in sys.path:
    sys.path.insert(0, str(_BLINDS_DIR))

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

import bedroom_lights  # noqa: E402

LEFT = "binary_sensor.left_bedside"
RIGHT = "binary_sensor.right_bedside"
FP300 = "binary_sensor.bedroom_presence_presence"
SLEEP = "input_boolean.mikkel_sleep_mode"
SESSION = "input_boolean.bedroom_bed_session"
BED = "light.bedroom_bed_lights"
CEILING = "light.bedroom_ceiling_lights"
BLIND = "cover.bedroom_blind"
DARK_SENSOR = "sensor.darkness_bedroom_bathroom"
ROOM_STATE = "sensor.room_state_bedroom_bathroom"
BATH_PIR = "binary_sensor.bathroom_pir_presence"
BATH_DOOR = "binary_sensor.bathroom_door_contact"
TV_POWER = "sensor.bedroom_tv_relay_power"


def make_session_app(session=False, dark=True, session_exit_timer=None, session_exit_armed_at=None):
    """BedroomLights with just the state the session-machinery / decision / occupancy /
    blind methods need, without running AppDaemon's initialize()."""
    app = bedroom_lights.BedroomLights.__new__(bedroom_lights.BedroomLights)
    app.bed_lights = BED
    app.ceiling_lights = CEILING
    app.withings_in_bed_entities = [LEFT, RIGHT]
    app.bedroom_presence_sensor = FP300
    app.bedroom_presence_extra = []
    app.bed_session_entity = SESSION
    app.session_exit_debounce_sec = 90
    app.bathroom_door_close_grace_sec = 5
    app._vacancy_grace_timer = None
    app.blind_closed_threshold = 95
    app.bedroom_blind_entity = BLIND
    app.mikkel_sleep_entity = SLEEP
    app.raw_bathroom_pir_sensor = BATH_PIR
    app.bathroom_door_sensor = BATH_DOOR
    app.room_state_text_entity = ROOM_STATE
    app.darkness_confirmed_sensor = DARK_SENSOR
    app.manual_override_entity = None
    app.log_level = "normal"
    app._session = session
    app._session_exit_timer = session_exit_timer
    app._last_off_is_dark = None
    app._session_exit_armed_at = session_exit_armed_at
    app.session_hold_bed_max_sec = 1800
    app.session_hold_power_entity = "sensor.bedroom_tv_relay_power"
    app.session_hold_power_watts = 20
    app.session_hold_power_max_sec = 21600

    app.states = {
        (FP300, None): "off",
        (LEFT, None): "off",
        (RIGHT, None): "off",
        (SLEEP, None): "off",
        (SESSION, None): "on" if session else "off",
        (BATH_PIR, None): "off",
        (BATH_DOOR, None): "off",
        (BLIND, "current_position"): 0,
        (DARK_SENSOR, None): "dark" if dark else "bright",
        (TV_POWER, None): "0",
    }

    def get_state(entity, **kw):
        return app.states.get((entity, kw.get("attribute")))

    app.get_state = get_state
    app.log = lambda *a, **kw: None
    app.run_in = MagicMock(return_value="exit-timer-handle")
    app.cancel_timer = MagicMock()
    app.call_service = MagicMock()
    app.turn_on = MagicMock()
    app.turn_off = MagicMock()
    return app


BASE_ARGS = {
    "bed_lights": BED,
    "ceiling_lights": CEILING,
    "withings_in_bed_entities": [LEFT, RIGHT],
    "switch_device_id": "switch-device-id",
    "remote_device_id": "remote-device-id",
    "raw_bathroom_pir_sensor": BATH_PIR,
    "bathroom_door_sensor": BATH_DOOR,
    "mikkel_sleep_entity": SLEEP,
    "bedroom_blind_entity": BLIND,
    "room_state_text_entity": ROOM_STATE,
    "darkness_confirmed_sensor_entity": DARK_SENSOR,
    "bed_session_entity": SESSION,
    "bedroom_presence_sensor": FP300,
    "verbosity_level": "normal",
}


def make_full_app(overrides=None):
    """BedroomLights with initialize() actually run (AppDaemon primitives stubbed only) -
    used solely to verify listener *registration*, since that is the real mechanism that
    keeps e.g. a Withings OFF edge from ever reaching its handler (see
    SessionListenerRegistration below)."""
    app = bedroom_lights.BedroomLights.__new__(bedroom_lights.BedroomLights)
    app.args = dict(BASE_ARGS)
    if overrides:
        app.args.update(overrides)
    app.log = MagicMock()
    app.error = MagicMock()
    app.listen_event = MagicMock()
    app.listen_state = MagicMock(return_value="listener-handle")
    app.run_in = MagicMock(return_value="timer-handle")
    app.initialize()
    return app


class SessionEnter(unittest.TestCase):
    def test_enter_on_withings_on_edge(self):
        app = make_session_app(session=False, dark=True)
        app.states[(FP300, None)] = "on"  # you're in the room when the mat reports weight
        app._on_withings_in_bed_on(LEFT, "state", "off", "on", {})
        self.assertTrue(app._session)
        app.call_service.assert_called_once_with("input_boolean/turn_on", entity_id=SESSION)
        app.turn_on.assert_called_once_with(BED)

    def test_enter_on_withings_unknown_to_on_edge(self):
        # Withings is cloud: the real getting-in-bed edge is often unknown->on (or
        # unavailable->on), which an old="off" listener filter silently missed
        # (user-reported 2026-07-20: in bed, ceiling stayed on, session never started).
        app = make_session_app(session=False, dark=True)
        app.states[(FP300, None)] = "on"
        app._on_withings_in_bed_on(LEFT, "state", "unknown", "on", {})
        self.assertTrue(app._session)

    def test_no_enter_on_withings_when_room_empty(self):
        # A stale cloud reconnect reporting "on" after you've left must not relight an
        # empty bed - the FP300 presence guard blocks it.
        app = make_session_app(session=False, dark=True)
        app.states[(FP300, None)] = "off"
        app._on_withings_in_bed_on(LEFT, "state", "unavailable", "on", {})
        self.assertFalse(app._session)
        app.call_service.assert_not_called()

    def test_enter_on_sleep_mode_on_edge(self):
        app = make_session_app(session=False, dark=True)
        app._on_sleep_mode_on(SLEEP, "state", "off", "on", {})
        self.assertTrue(app._session)
        app.call_service.assert_called_once_with("input_boolean/turn_on", entity_id=SESSION)

    def test_enter_on_manual_bed_on_while_dark(self):
        app = make_session_app(session=False, dark=True)
        app._on_bed_lights_on_manual(BED, "state", "off", "on", {})
        self.assertTrue(app._session)
        app.call_service.assert_called_once_with("input_boolean/turn_on", entity_id=SESSION)

    def test_no_enter_on_manual_bed_on_while_bright(self):
        app = make_session_app(session=False, dark=False)
        app._on_bed_lights_on_manual(BED, "state", "off", "on", {})
        self.assertFalse(app._session)
        app.call_service.assert_not_called()

    def test_no_enter_on_manual_bed_on_while_already_in_session(self):
        app = make_session_app(session=True, dark=True)
        app.call_service.reset_mock()
        app._on_bed_lights_on_manual(BED, "state", "off", "on", {})
        self.assertTrue(app._session)
        app.call_service.assert_not_called()  # already on -> _set_session's no-op guard

    def test_withings_off_alone_does_not_end_active_session(self):
        """Withings' OFF edge is ignored forever - nothing in the running state machine
        reacts to it. FP300 still "on" holds occupancy independent of the session too."""
        app = make_session_app(session=True, dark=True)
        app.states[(LEFT, None)] = "off"
        app.states[(RIGHT, None)] = "off"
        app.states[(FP300, None)] = "on"
        app.call_service.reset_mock()
        app._evaluate_lights("NEUTRAL")
        self.assertTrue(app._session)
        app.call_service.assert_not_called()


class SessionListenerRegistration(unittest.TestCase):
    """The session-START listeners register on new="on" ONLY (no old="off" filter): Withings
    is a cloud sensor that constantly passes through unknown/unavailable, so the real
    getting-in-bed edge is usually unknown->on or unavailable->on. An old="off" filter would
    silently drop it (user-reported 2026-07-20). This verifies the registration."""

    def setUp(self):
        self.app = make_full_app()

    def _reg(self, callback):
        return [
            c for c in self.app.listen_state.call_args_list
            if c.args and c.args[0] == callback
        ]

    def test_withings_registered_to_on_no_old_filter(self):
        for ent in (LEFT, RIGHT):
            calls = [c for c in self._reg(self.app._on_withings_in_bed_on) if c.args[1] == ent]
            self.assertEqual(len(calls), 1)
            self.assertNotIn("old", calls[0].kwargs)  # any -> on, not just off -> on
            self.assertEqual(calls[0].kwargs.get("new"), "on")

    def test_sleep_mode_registered_to_on_no_old_filter(self):
        calls = self._reg(self.app._on_sleep_mode_on)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1], SLEEP)
        self.assertNotIn("old", calls[0].kwargs)
        self.assertEqual(calls[0].kwargs.get("new"), "on")

    def test_bed_lights_manual_registered_to_on_no_old_filter(self):
        calls = self._reg(self.app._on_bed_lights_on_manual)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1], BED)
        self.assertNotIn("old", calls[0].kwargs)
        self.assertEqual(calls[0].kwargs.get("new"), "on")

    def test_presence_registered_both_edges(self):
        calls = self._reg(self.app._on_presence_change_session)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1], FP300)
        self.assertNotIn("old", calls[0].kwargs)
        self.assertNotIn("new", calls[0].kwargs)


class SessionExit(unittest.TestCase):
    def test_fp300_off_arms_exit_timer_when_session_active(self):
        app = make_session_app(session=True)
        app._on_presence_change_session(FP300, "state", "on", "off", {})
        app.run_in.assert_called_once_with(app._session_exit_fire, 90)
        self.assertEqual(app._session_exit_timer, "exit-timer-handle")

    def test_fp300_off_no_timer_when_session_inactive(self):
        app = make_session_app(session=False)
        app._on_presence_change_session(FP300, "state", "on", "off", {})
        app.run_in.assert_not_called()
        self.assertIsNone(app._session_exit_timer)

    def test_fp300_off_while_sleep_on_no_timer(self):
        app = make_session_app(session=True)
        app.states[(SLEEP, None)] = "on"
        app._on_presence_change_session(FP300, "state", "on", "off", {})
        app.run_in.assert_not_called()
        self.assertIsNone(app._session_exit_timer)

    def test_fp300_unavailable_no_timer(self):
        app = make_session_app(session=True)
        app._on_presence_change_session(FP300, "state", "on", "unavailable", {})
        app.run_in.assert_not_called()
        self.assertIsNone(app._session_exit_timer)

    def test_timer_fire_ends_session_when_still_clear(self):
        app = make_session_app(session=True, session_exit_timer="handle")
        app.states[(FP300, None)] = "off"
        app._session_exit_fire({})
        self.assertIsNone(app._session_exit_timer)
        self.assertFalse(app._session)
        app.call_service.assert_called_once_with("input_boolean/turn_off", entity_id=SESSION)

    def test_presence_returns_before_fire_cancels_and_holds(self):
        app = make_session_app(session=True, session_exit_timer="handle")
        app._on_presence_change_session(FP300, "state", "off", "on", {})
        app.cancel_timer.assert_called_once_with("handle")
        self.assertIsNone(app._session_exit_timer)
        self.assertTrue(app._session)

    def test_fire_is_noop_if_presence_already_returned(self):
        app = make_session_app(session=True, session_exit_timer="handle")
        app.states[(FP300, None)] = "on"  # presence came back, timer just never got cancelled
        app._session_exit_fire({})
        self.assertTrue(app._session)
        app.call_service.assert_not_called()

    def test_fire_is_noop_if_sleep_mode_started(self):
        app = make_session_app(session=True, session_exit_timer="handle")
        app.states[(FP300, None)] = "off"
        app.states[(SLEEP, None)] = "on"
        app._session_exit_fire({})
        self.assertTrue(app._session)
        app.call_service.assert_not_called()


class SessionExitHold(unittest.TestCase):
    """_session_hold_reason: capped, independent witnesses that extend the exit debounce
    instead of ending the session outright when the timer fires."""

    def setUp(self):
        self._real_time = bedroom_lights.time
        bedroom_lights.time = types.SimpleNamespace(time=lambda: FIXED_NOW)
        self.addCleanup(self._restore_time)

    def _restore_time(self):
        bedroom_lights.time = self._real_time

    def test_no_hold_when_armed_at_is_none(self):
        # Existing behaviour preserved: a test that never went through _arm_session_exit
        # (armed_at defaults to None) must not hold, even with a mat "on".
        app = make_session_app(session=True, session_exit_timer="handle")
        app.states[(LEFT, None)] = "on"
        app.states[(FP300, None)] = "off"
        app._session_exit_fire({})
        self.assertFalse(app._session)
        app.call_service.assert_called_once_with("input_boolean/turn_off", entity_id=SESSION)

    def test_bed_witness_holds_and_rearms(self):
        app = make_session_app(
            session=True, session_exit_timer="handle", session_exit_armed_at=FIXED_NOW - 100
        )
        app.states[(FP300, None)] = "off"
        app.states[(LEFT, None)] = "on"  # live witness despite FP300 being off
        app._session_exit_fire({})
        self.assertTrue(app._session)
        app.call_service.assert_not_called()
        app.run_in.assert_called_once_with(app._session_exit_fire, app.session_exit_debounce_sec)
        self.assertIsNotNone(app._session_exit_timer)

    def test_bed_witness_hold_expires_past_cap(self):
        app = make_session_app(
            session=True,
            session_exit_timer="handle",
            session_exit_armed_at=FIXED_NOW - 1801,
        )
        app.states[(FP300, None)] = "off"
        app.states[(LEFT, None)] = "on"  # witness still "on", but past the 1800s cap
        app._session_exit_fire({})
        self.assertFalse(app._session)
        app.call_service.assert_called_once_with("input_boolean/turn_off", entity_id=SESSION)

    def test_tv_power_holds_and_rearms(self):
        app = make_session_app(
            session=True, session_exit_timer="handle", session_exit_armed_at=FIXED_NOW - 100
        )
        app.states[(FP300, None)] = "off"
        app.states[(TV_POWER, None)] = "45.0"  # above the 20W threshold
        app._session_exit_fire({})
        self.assertTrue(app._session)
        app.call_service.assert_not_called()

    def test_tv_power_at_or_below_threshold_does_not_hold(self):
        app = make_session_app(
            session=True, session_exit_timer="handle", session_exit_armed_at=FIXED_NOW - 100
        )
        app.states[(FP300, None)] = "off"
        app.states[(TV_POWER, None)] = "20.0"
        app._session_exit_fire({})
        self.assertFalse(app._session)

    def test_tv_power_hold_expires_past_cap(self):
        app = make_session_app(
            session=True,
            session_exit_timer="handle",
            session_exit_armed_at=FIXED_NOW - 21600 - 1,
        )
        app.states[(FP300, None)] = "off"
        app.states[(TV_POWER, None)] = "45.0"
        app._session_exit_fire({})
        self.assertFalse(app._session)

    def test_unreadable_power_sensor_does_not_hold(self):
        app = make_session_app(
            session=True, session_exit_timer="handle", session_exit_armed_at=FIXED_NOW - 100
        )
        app.states[(FP300, None)] = "off"
        app.states[(LEFT, None)] = "off"
        app.states[(RIGHT, None)] = "off"
        app.states[(TV_POWER, None)] = "unavailable"
        app._session_exit_fire({})
        self.assertFalse(app._session)

    def test_arm_session_exit_records_armed_at(self):
        app = make_session_app(session=True)
        app._arm_session_exit()
        self.assertEqual(app._session_exit_armed_at, FIXED_NOW)

    def test_cancel_session_exit_clears_armed_at(self):
        app = make_session_app(session=True, session_exit_timer="handle", session_exit_armed_at=FIXED_NOW - 5)
        app._cancel_session_exit()
        self.assertIsNone(app._session_exit_armed_at)

    def test_rearm_on_hold_does_not_reset_armed_at(self):
        # The cap is measured from the FIRST arm, not extended on every hold.
        armed_at = FIXED_NOW - 100
        app = make_session_app(session=True, session_exit_timer="handle", session_exit_armed_at=armed_at)
        app.states[(FP300, None)] = "off"
        app.states[(LEFT, None)] = "on"
        app._session_exit_fire({})
        self.assertEqual(app._session_exit_armed_at, armed_at)


class SessionEntryArmsExitIfPresenceAlreadyClear(unittest.TestCase):
    """Latent-bug fix: a session started while FP300 is already not "on" (sleep-mode-on,
    or a reconciled session at restart) must arm its own exit timer immediately - otherwise
    no future off-edge will ever arrive to arm one, and the session could never end."""

    def setUp(self):
        self._real_time = bedroom_lights.time
        bedroom_lights.time = types.SimpleNamespace(time=lambda: FIXED_NOW)
        self.addCleanup(self._restore_time)

    def _restore_time(self):
        bedroom_lights.time = self._real_time

    def test_sleep_mode_on_with_fp300_already_off_arms_exit(self):
        app = make_session_app(session=False, dark=True)
        app.states[(FP300, None)] = "off"
        app._on_sleep_mode_on(SLEEP, "state", "off", "on", {})
        self.assertTrue(app._session)
        app.run_in.assert_called_once_with(app._session_exit_fire, app.session_exit_debounce_sec)
        self.assertIsNotNone(app._session_exit_timer)

    def test_sleep_mode_on_with_fp300_on_does_not_arm_exit(self):
        app = make_session_app(session=False, dark=True)
        app.states[(FP300, None)] = "on"
        app._on_sleep_mode_on(SLEEP, "state", "off", "on", {})
        self.assertTrue(app._session)
        app.run_in.assert_not_called()
        self.assertIsNone(app._session_exit_timer)

    def test_reconcile_with_withings_and_fp300_off_arms_exit(self):
        app = make_session_app(session=False)
        app.states[(SESSION, None)] = "off"
        app.states[(FP300, None)] = "off"
        app.states[(LEFT, None)] = "on"
        app._reconcile_session()
        self.assertTrue(app._session)
        app.run_in.assert_called_once_with(app._session_exit_fire, app.session_exit_debounce_sec)

    def test_reconcile_with_fp300_on_does_not_arm_exit(self):
        app = make_session_app(session=False)
        app.states[(SESSION, None)] = "off"
        app.states[(FP300, None)] = "on"
        app.states[(LEFT, None)] = "on"
        app._reconcile_session()
        self.assertTrue(app._session)
        app.run_in.assert_not_called()

    def test_reconcile_with_sleep_on_does_not_arm_exit(self):
        # sleep_on already blocks the exit path entirely (_is_sleep_mode_active), no timer needed.
        app = make_session_app(session=False)
        app.states[(SESSION, None)] = "off"
        app.states[(FP300, None)] = "off"
        app.states[(SLEEP, None)] = "on"
        app._reconcile_session()
        self.assertTrue(app._session)
        app.run_in.assert_not_called()


class LightDecision(unittest.TestCase):
    def test_session_active_turns_bed_on_ceiling_off(self):
        app = make_session_app(session=True, dark=True)
        app.states[(CEILING, None)] = "on"
        app._evaluate_lights("TEST")
        app.turn_off.assert_called_once_with(CEILING)
        app.turn_on.assert_called_once_with(BED)

    def test_session_ended_turns_ceiling_on_bed_off(self):
        app = make_session_app(session=False, dark=True)
        app.states[(FP300, None)] = "on"  # occupied without relying on the session itself
        app.states[(BED, None)] = "on"
        app._evaluate_lights("TEST")
        app.turn_off.assert_called_once_with(BED)
        app.turn_on.assert_called_once_with(CEILING)

    def test_blind_closed_at_99_blocks_auto_on(self):
        app = make_session_app(session=False, dark=True)
        app.states[(FP300, None)] = "on"
        app.states[(BLIND, "current_position")] = 99
        app._evaluate_lights("TEST")
        app.turn_on.assert_not_called()
        app.turn_off.assert_not_called()

    def test_blind_at_94_does_not_block_auto_on(self):
        app = make_session_app(session=False, dark=True)
        app.states[(FP300, None)] = "on"
        app.states[(BLIND, "current_position")] = 94
        app._evaluate_lights("TEST")
        app.turn_on.assert_called_once_with(CEILING)

    def test_ceiling_already_on_follows_to_bed_despite_sleep_mode(self):
        # 2026-08-20: sleep mode can auto-arm in the SAME instant a body sensor
        # starts the session (phone already charging at bedtime) - the ceiling was
        # already lit from getting ready, and the sleep-mode no-auto-on gate below
        # must not strand it there. Re-pointing existing light is not the same as
        # conjuring new light in a dark/private room.
        app = make_session_app(session=True, dark=True)
        app.states[(CEILING, None)] = "on"
        app.states[(SLEEP, None)] = "on"
        app._evaluate_lights("TEST")
        app.turn_off.assert_called_once_with(CEILING)
        app.turn_on.assert_called_once_with(BED)

    def test_ceiling_already_on_follows_to_bed_despite_blind_closed(self):
        app = make_session_app(session=True, dark=True)
        app.states[(CEILING, None)] = "on"
        app.states[(BLIND, "current_position")] = 100
        app._evaluate_lights("TEST")
        app.turn_off.assert_called_once_with(CEILING)
        app.turn_on.assert_called_once_with(BED)

    def test_no_swap_when_ceiling_already_off_and_sleep_mode_on(self):
        # The fix only re-points an ALREADY-LIT room. With the ceiling already off,
        # sleep mode's no-auto-on gate must still hold - no new light from darkness.
        app = make_session_app(session=True, dark=True)
        app.states[(SLEEP, None)] = "on"
        app._evaluate_lights("TEST")
        app.turn_on.assert_not_called()
        app.turn_off.assert_not_called()

    def test_no_swap_when_session_not_active_even_if_ceiling_on_and_sleep_mode(self):
        app = make_session_app(session=False, dark=True)
        app.states[(FP300, None)] = "on"
        app.states[(CEILING, None)] = "on"
        app.states[(SLEEP, None)] = "on"
        app._evaluate_lights("TEST")
        app.turn_on.assert_not_called()
        app.turn_off.assert_not_called()

    def test_ceiling_swap_does_not_redundantly_call_turn_on_if_bed_already_on(self):
        app = make_session_app(session=True, dark=True)
        app.states[(CEILING, None)] = "on"
        app.states[(BED, None)] = "on"
        app.states[(SLEEP, None)] = "on"
        app._evaluate_lights("TEST")
        app.turn_off.assert_called_once_with(CEILING)
        app.turn_on.assert_not_called()


class BathroomDoorGrace(unittest.TestCase):
    # 2026-08-20: closing the bathroom door dropped bathroom-PIR's contribution to
    # occupancy instantly; if FP300 hadn't yet confirmed bedroom presence, occupancy
    # fell to zero and both lights died in the same second - observed 4x on 08-19.

    def test_close_with_no_other_occupancy_arms_grace_not_immediate_off(self):
        app = make_session_app(session=False, dark=True)
        app.states[(CEILING, None)] = "on"  # would be killed immediately pre-fix
        app._on_bathroom_door_change(BATH_DOOR, "state", "on", "off", {})
        app.run_in.assert_called_once()
        app.turn_off.assert_not_called()

    def test_close_with_fp300_still_on_evaluates_normally_no_grace(self):
        app = make_session_app(session=False, dark=True)
        app.states[(FP300, None)] = "on"
        app._on_bathroom_door_change(BATH_DOOR, "state", "on", "off", {})
        app.run_in.assert_not_called()

    def test_grace_fire_confirms_vacancy_turns_off(self):
        app = make_session_app(session=False, dark=True)
        app.states[(CEILING, None)] = "on"
        app._arm_vacancy_grace()
        app._vacancy_grace_fire({})
        app.turn_off.assert_any_call(CEILING)

    def test_grace_fire_noop_if_fp300_recovered_before_it_fires(self):
        app = make_session_app(session=False, dark=True)
        app.states[(CEILING, None)] = "on"
        app._arm_vacancy_grace()
        app.states[(FP300, None)] = "on"  # confirmed before the timer fires
        app._vacancy_grace_fire({})
        app.turn_off.assert_not_called()

    def test_door_reopen_cancels_pending_grace(self):
        app = make_session_app(session=False, dark=True)
        app._vacancy_grace_timer = "pending-handle"
        app._on_bathroom_door_change(BATH_DOOR, "state", "off", "on", {})
        app.cancel_timer.assert_called_once_with("pending-handle")
        self.assertIsNone(app._vacancy_grace_timer)


class BlindClosedThreshold(unittest.TestCase):
    def test_pos_99_with_threshold_95_is_closed(self):
        app = make_session_app()
        app.states[(BLIND, "current_position")] = 99
        self.assertTrue(app._is_blind_closed())

    def test_pos_94_with_threshold_95_is_not_closed(self):
        app = make_session_app()
        app.states[(BLIND, "current_position")] = 94
        self.assertFalse(app._is_blind_closed())


class EffectiveOccupancy(unittest.TestCase):
    def test_true_via_fp300(self):
        app = make_session_app(session=False)
        app.states[(FP300, None)] = "on"
        self.assertTrue(app._get_effective_occupancy())

    def test_true_via_bathroom(self):
        app = make_session_app(session=False)
        app.states[(BATH_DOOR, None)] = "on"
        app.states[(BATH_PIR, None)] = "on"
        self.assertTrue(app._get_effective_occupancy())

    def test_true_via_session(self):
        app = make_session_app(session=True)
        self.assertTrue(app._get_effective_occupancy())

    def test_false_when_all_clear(self):
        app = make_session_app(session=False)
        self.assertFalse(app._get_effective_occupancy())


FIXED_NOW = 1_800_000_000.0


def _iso(seconds_ago: float) -> str:
    return datetime.datetime.fromtimestamp(
        FIXED_NOW - seconds_ago, tz=datetime.timezone.utc
    ).isoformat()


class ReconcileSession(unittest.TestCase):
    """Restart-safe rebuild of self._session. time.time() is pinned to FIXED_NOW (the
    bedroom_lights module's own `time` name, not the real global module) so the
    last_changed-vs-debounce math is deterministic regardless of wall clock/timezone."""

    def setUp(self):
        self._real_time = bedroom_lights.time
        bedroom_lights.time = types.SimpleNamespace(time=lambda: FIXED_NOW)
        self.addCleanup(self._restore_time)

    def _restore_time(self):
        bedroom_lights.time = self._real_time

    def _app(self, *, persisted, fp300_on, withings=False, sleep_on=False, last_changed=None):
        app = make_session_app(session=False)
        app.states[(SESSION, None)] = "on" if persisted else "off"
        app.states[(FP300, None)] = "on" if fp300_on else "off"
        app.states[(LEFT, None)] = "on" if withings else "off"
        app.states[(SLEEP, None)] = "on" if sleep_on else "off"
        if last_changed is not None:
            app.states[(FP300, "last_changed")] = last_changed
        return app

    def test_persisted_on_fp300_on_stays_on(self):
        app = self._app(persisted=True, fp300_on=True)
        app._reconcile_session()
        self.assertTrue(app._session)
        app.call_service.assert_not_called()

    def test_persisted_on_fp300_off_beyond_debounce_goes_off(self):
        app = self._app(persisted=True, fp300_on=False, last_changed=_iso(200))
        app._reconcile_session()
        self.assertFalse(app._session)
        app.call_service.assert_called_once_with("input_boolean/turn_off", entity_id=SESSION)

    def test_persisted_on_fp300_off_recent_stays_on(self):
        app = self._app(persisted=True, fp300_on=False, last_changed=_iso(10))
        app._reconcile_session()
        self.assertTrue(app._session)
        app.call_service.assert_not_called()

    def test_persisted_off_sleep_on_turns_on(self):
        app = self._app(persisted=False, fp300_on=False, sleep_on=True)
        app._reconcile_session()
        self.assertTrue(app._session)
        app.call_service.assert_called_once_with("input_boolean/turn_on", entity_id=SESSION)

    def test_persisted_off_withings_on_turns_on(self):
        app = self._app(persisted=False, fp300_on=False, withings=True)
        app._reconcile_session()
        self.assertTrue(app._session)
        app.call_service.assert_called_once_with("input_boolean/turn_on", entity_id=SESSION)

    def test_all_clear_stays_off(self):
        app = self._app(persisted=False, fp300_on=False)
        app._reconcile_session()
        self.assertFalse(app._session)
        app.call_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
