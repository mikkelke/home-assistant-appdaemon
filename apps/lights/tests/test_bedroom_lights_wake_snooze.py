import datetime
import unittest
from unittest.mock import MagicMock

from test_bedroom_lights_session import (
    BATH_PIR, BED, BLIND, CEILING, ROOM_ACTIVE, SESSION, SLEEP,
    make_session_app,
)

WAKE_SESSION = "sensor.wake_alarm_session"


def wake_state(status="snoozing", curtain="closed", overdue=False):
    delta = datetime.timedelta(minutes=-1 if overdue else 10)
    return {
        "state": status,
        "attributes": {
            "session_id": "morning",
            "snooze_curtain": curtain,
            "snooze_until": (datetime.datetime.now(datetime.timezone.utc) + delta).isoformat(),
        },
    }


def make_app(session=True, dark=True, wake=None):
    app = make_session_app(session=session, dark=dark)
    app.states[(ROOM_ACTIVE, None)] = "on"
    app.states[(BED, None)] = "off"
    app.states[(CEILING, None)] = "off"
    app.states[(WAKE_SESSION, "all")] = wake if wake is not None else wake_state()
    return app


class CloseAgainSnooze(unittest.TestCase):
    def test_bathroom_pir_cannot_relight_bed_while_blind_is_moving_or_held_open(self):
        for position in (0, 38, 94):
            with self.subTest(position=position):
                app = make_app()
                app.states[(BLIND, "current_position")] = position
                app._on_raw_bathroom_pir_change(BATH_PIR, "state", "off", "on", {})
                app.turn_on.assert_not_called()
                app.turn_off.assert_not_called()

    def test_snooze_blocks_ceiling_to_bed_transfer_as_well_as_new_light(self):
        app = make_app()
        app.states[(CEILING, None)] = "on"
        app._evaluate_lights("BATHROOM_PIR_CHANGE")
        app.turn_on.assert_not_called()
        app.turn_off.assert_not_called()

    def test_keep_room_snooze_retains_normal_bed_lighting(self):
        app = make_app(wake=wake_state(curtain="open"))
        app._on_raw_bathroom_pir_change(BATH_PIR, "state", "off", "on", {})
        app.turn_on.assert_called_once_with(BED)

    def test_cancel_dismiss_return_and_wake_now_release_on_next_presence_event(self):
        for status in ("dismissed", "ringing", "stopped", "idle"):
            with self.subTest(status=status):
                app = make_app()
                app._evaluate_lights("BEFORE")
                app.turn_on.assert_not_called()
                app.states[(WAKE_SESSION, "all")] = wake_state(status=status)
                app._on_raw_bathroom_pir_change(BATH_PIR, "state", "off", "on", {})
                app.turn_on.assert_called_once_with(BED)

    def test_release_keeps_existing_closed_blind_guard(self):
        app = make_app(wake=wake_state(status="dismissed"))
        app.states[(BLIND, "current_position")] = 100
        app._on_raw_bathroom_pir_change(BATH_PIR, "state", "off", "on", {})
        app.turn_on.assert_not_called()
        app.turn_off.assert_not_called()

    def test_quiet_does_not_latch_sleep_mode_or_stop_bed_session_exit(self):
        app = make_app()
        self.assertFalse(app._is_sleep_mode_active())
        app.states[(ROOM_ACTIVE, None)] = "off"
        app._session_exit_fire({})
        self.assertFalse(app._session)
        app.call_service.assert_called_once_with("input_boolean/turn_off", entity_id=SESSION)
        app.states[(WAKE_SESSION, "all")] = wake_state(status="dismissed")
        app.states[(ROOM_ACTIVE, None)] = "on"
        app._on_presence_change_session(ROOM_ACTIVE, "state", "off", "on", {})
        app.turn_on.assert_called_once_with(CEILING)
        self.assertEqual(app.states[(SLEEP, None)], "off")

    def test_restart_rebuilds_bed_session_and_preserves_quiet_from_published_state(self):
        app = make_app()
        app._session = False
        app._initial_check()
        self.assertTrue(app._session)
        app.turn_on.assert_not_called()
        app.states[(WAKE_SESSION, "all")] = wake_state(status="ringing")
        app._initial_check()
        app.turn_on.assert_called_once_with(BED)

    def test_quiet_still_allows_bright_and_vacant_off(self):
        for occupied, dark in ((False, True), (True, False)):
            with self.subTest(occupied=occupied, dark=dark):
                app = make_app(session=False, dark=dark)
                app.states[(ROOM_ACTIVE, None)] = "on" if occupied else "off"
                app.states[(BED, None)] = "on"
                app._evaluate_lights("CHECK")
                app.turn_off.assert_any_call(BED)
                app.turn_off.assert_any_call(CEILING)
                app.turn_on.assert_not_called()

    def test_expired_or_invalid_quiet_state_cannot_strand_occupied_room_without_light(self):
        invalid = [None, "unavailable", {}, {"state": "snoozing", "attributes": []},
                   wake_state(overdue=True)]
        for deadline in (None, "invalid", "2026-11-01T05:40:00"):
            state = wake_state()
            state["attributes"]["snooze_until"] = deadline
            invalid.append(state)
        for state in invalid:
            with self.subTest(state=state):
                app = make_app()
                app.states[(WAKE_SESSION, "all")] = state
                app._evaluate_lights("PRESENCE")
                app.turn_on.assert_called_once_with(BED)

    def test_quiet_uses_one_atomic_sensor_read(self):
        app = make_app()
        app.get_state = MagicMock(wraps=app.get_state)
        app._evaluate_lights("PRESENCE")
        app.get_state.assert_any_call(WAKE_SESSION, attribute="all")
        calls = [call for call in app.get_state.call_args_list if call.args[0] == WAKE_SESSION]
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
