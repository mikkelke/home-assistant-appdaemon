from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

import yaml

from test_living_room_apple_tv_disconnect import APPLE, HISENSE, TV, make_app, states


POWER = "sensor.living_room_tv_relay_power"
LIFT = "select.living_room_tv_lift_position"
WALL = ("select/select_option", {"entity_id": LIFT, "option": "Wall"})
NOW = datetime(2026, 10, 25, 18, tzinfo=timezone.utc)


def wall_app(tv_state="off", **args):
    app = make_app(states(**{TV: tv_state, LIFT: "Living room"}), {
        "power_aware_wall_move_enabled": True,
        "tv_power_entity": POWER,
        "session_end_grace_seconds": 0,
        "smart_wall_move_delay_seconds": 0,
        "min_wall_move_delay_seconds": 0,
        **args,
    })
    app.now = NOW
    app.datetime = lambda **kwargs: app.now
    set_power(app, 27)
    return app


def set_power(app, value, age=0, **fields):
    app.states[POWER] = {
        "state": str(value),
        "last_updated": (app.now - timedelta(seconds=age)).isoformat(),
        **fields,
    }


def advance(app, seconds):
    app.mono += seconds
    app.now += timedelta(seconds=seconds)


def take_timer(app, callback):
    for handle, (cb, delay, kwargs) in list(app.timers.items()):
        if cb == callback:
            del app.timers[handle]
            return cb, delay, kwargs
    raise AssertionError(f"Missing timer for {callback.__name__}")


def fire_timer(app, callback):
    cb, _, kwargs = take_timer(app, callback)
    cb(kwargs)


def wall_calls(app):
    return [call for call in app.service_calls if call == WALL]


class WallRequiresConfirmedStandby(unittest.TestCase):
    def test_first_low_read_waits_two_seconds_then_moves_exactly_once(self):
        app = wall_app()
        app._begin_power_verify_then_wall("off", "off")
        self.assertEqual(wall_calls(app), [])
        advance(app, 1.9)
        fire_timer(app, app._power_wall_verify_tick)
        self.assertEqual(wall_calls(app), [])
        advance(app, 0.1)
        fire_timer(app, app._power_wall_verify_tick)
        self.assertEqual(wall_calls(app), [WALL])
        # A duplicate already-dispatched move cannot send the service again.
        app._move_lift_to_wall({"wall_generation": app._wall_timer_generations["move"] - 1})
        self.assertEqual(wall_calls(app), [WALL])

    def test_77_watts_blocks_every_wall_entry_path(self):
        for path in ("session", "init_session", "power_session", "verify", "schedule", "move"):
            with self.subTest(path=path):
                app = wall_app(tv_state="on" if path == "power_session" else "off")
                set_power(app, 77)
                if path == "session":
                    app._fire_wall_after_session_over()
                elif path == "init_session":
                    app._fire_wall_after_session_over(from_init=True)
                elif path == "power_session":
                    app._fire_wall_after_session_over(from_power=True)
                elif path == "verify":
                    app._begin_power_verify_then_wall("off", "off")
                elif path == "schedule":
                    app._schedule_wall_move_after_tv_off("off", "off")
                else:
                    app._move_lift_to_wall(app._wall_timer_kwargs("move"))
                if app._power_wall_verify_handle:
                    advance(app, 10)
                    fire_timer(app, app._power_wall_verify_tick)
                self.assertEqual(wall_calls(app), [])

    def test_init_and_session_paths_also_wait_for_first_low_confirmation(self):
        for init in (False, True):
            with self.subTest(init=init):
                app = wall_app()
                app._fire_wall_after_session_over(from_init=init)
                self.assertEqual(wall_calls(app), [])
                advance(app, 2)
                fire_timer(app, app._power_wall_verify_tick)
                if init:
                    fire_timer(app, app._move_lift_to_wall)
                self.assertEqual(wall_calls(app), [WALL])

    def test_known_on_lag_can_move_only_in_power_standby_mode(self):
        app = wall_app(tv_state="on")
        app.states[HISENSE] = "on"
        app.states[APPLE] = "paused"
        app._evaluate_power_standby_wall()
        self.assertEqual(wall_calls(app), [])
        advance(app, 2)
        fire_timer(app, app._power_standby_wall_tick)
        self.assertEqual(wall_calls(app), [])
        advance(app, 2)
        fire_timer(app, app._power_wall_verify_tick)
        self.assertEqual(wall_calls(app), [WALL])

    def test_invalid_power_blocks_and_recovery_requires_new_full_confirmation(self):
        invalid = (
            ("unknown", 0, {}), ("unavailable", 0, {}), ("NaN", 0, {}),
            ("inf", 0, {}), ("-inf", 0, {}), (-1, 0, {}),
            (27, 121, {}), (27, -1, {}),
            (27, 0, {"last_reported": (NOW - timedelta(seconds=121)).isoformat()}),
            (27, 0, {"last_reported": "bad-date"}),
        )
        for value, age, fields in invalid:
            with self.subTest(value=value, age=age, fields=fields):
                app = wall_app()
                app._read_tv_power_w()
                advance(app, 2)
                set_power(app, value, age, **fields)
                app._on_relay_power_changed(POWER, None, "27", str(value), {})
                self.assertIsNone(app._wall_power_below_since)
                app._move_lift_to_wall(app._wall_timer_kwargs("move"))
                self.assertEqual(wall_calls(app), [])
                set_power(app, 27)
                self.assertFalse(app._wall_power_confirmed())
                advance(app, 1.99)
                self.assertFalse(app._wall_power_confirmed())
                advance(app, 0.01)
                self.assertTrue(app._wall_power_confirmed())

    def test_last_reported_takes_precedence_and_last_updated_is_valid_fallback(self):
        app = wall_app()
        set_power(app, 27, 121, last_reported=NOW.isoformat())
        self.assertEqual(app._read_tv_power_w(), 27)
        set_power(app, 0)
        self.assertEqual(app._read_tv_power_w(), 0)
        set_power(app, 27, 120)
        self.assertEqual(app._read_tv_power_w(), 27)

    def test_unknown_primary_state_blocks_both_normal_and_power_paths(self):
        for state in (None, "unknown", "unavailable"):
            for mode in ("debounced", "power_standby"):
                with self.subTest(state=state, mode=mode):
                    app = wall_app()
                    app._read_tv_power_w()
                    advance(app, 2)
                    app.states[TV] = state
                    app._move_lift_to_wall(app._wall_timer_kwargs("move", schedule_mode=mode))
                    self.assertEqual(wall_calls(app), [])
                    self.assertIsNone(app._wall_power_below_since)

    def test_unknown_hisense_blocks_even_when_aggregate_is_off(self):
        for state in (None, "unknown", "unavailable"):
            for mode in ("debounced", "power_standby"):
                with self.subTest(state=state, mode=mode):
                    app = wall_app()
                    app._read_tv_power_w()
                    advance(app, 2)
                    app.states[HISENSE] = state
                    app._schedule_wall_move_after_tv_off("off", state, mode)
                    app._move_lift_to_wall(app._wall_timer_kwargs("move", schedule_mode=mode))
                    self.assertEqual(wall_calls(app), [])
                    self.assertIsNone(app._wall_power_below_since)

    def test_pending_move_rechecks_power_state_and_operation(self):
        for change in ("power", "state", "operation", "stale"):
            with self.subTest(change=change):
                app = wall_app(smart_wall_move_delay_seconds=5)
                app._begin_power_verify_then_wall("off", "off")
                advance(app, 2)
                fire_timer(app, app._power_wall_verify_tick)
                if change == "power":
                    set_power(app, 77)
                    app._on_relay_power_changed(POWER, None, "27", "77", {})
                elif change == "state":
                    app.states[TV] = "on"
                elif change == "operation":
                    app.tv_operation_in_progress = True
                else:
                    set_power(app, 27, 121)
                fire_timer(app, app._move_lift_to_wall)
                self.assertEqual(wall_calls(app), [])

    def test_cancelled_callbacks_cannot_restart_a_wall_path(self):
        for timer in ("move", "verify", "session", "standby", "debounce"):
            with self.subTest(timer=timer):
                app = wall_app()
                kwargs = app._wall_timer_kwargs(timer)
                if timer == "move":
                    callback = app._move_lift_to_wall
                elif timer == "verify":
                    app._begin_power_verify_then_wall("off", "off")
                    _, _, kwargs = take_timer(app, app._power_wall_verify_tick)
                    callback = app._power_wall_verify_tick
                elif timer == "session":
                    callback = app._session_end_wall_fired
                elif timer == "standby":
                    callback = app._power_standby_wall_tick
                else:
                    callback = app._handle_tv_off_debounced
                app._cancel_delayed_wall_move("viewing resumed")
                app.timers.clear()
                app._read_tv_power_w()
                advance(app, 2)
                callback(kwargs)
                self.assertEqual(wall_calls(app), [])
                self.assertEqual(app.timers, {})

    def test_missing_sensor_fails_closed_with_power_guard_enabled(self):
        app = wall_app(tv_power_entity=None)
        app._begin_power_verify_then_wall("off", "off")
        app._move_lift_to_wall(app._wall_timer_kwargs("move"))
        self.assertEqual(wall_calls(app), [])


class ProductionPrimaryPowerSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config_path = Path(__file__).resolve().parents[1] / "living_room_tv_control.yaml"
        cls.config = yaml.safe_load(config_path.read_text())["living_room_tv_control"]

    def production_app(self, aggregate="off", primary="off"):
        app = wall_app(tv_state=aggregate, **self.config)
        app.states[app.hisense_tv_entity] = primary
        self.assertTrue(app.power_aware_wall_move_enabled)
        self.assertFalse(app.power_standby_wall_enabled)
        return app

    def test_either_on_report_blocks_wall_at_27_watts(self):
        for aggregate, primary in (("off", "on"), ("on", "on"), ("on", "off")):
            with self.subTest(aggregate=aggregate, primary=primary):
                app = self.production_app(aggregate, primary)
                app._read_tv_power_w()
                advance(app, 2)
                self.assertTrue(app._wall_power_confirmed())
                self.assertEqual(app._hisense_blocks_wall(), primary == "on")
                app._fire_wall_after_session_over(from_power=aggregate == "on")
                app._begin_power_verify_then_wall("off", primary)
                app._schedule_wall_move_after_tv_off("off", primary)
                app._move_lift_to_wall(app._wall_timer_kwargs("move"))
                self.assertEqual(wall_calls(app), [])
                self.assertIsNone(app._power_wall_verify_handle)

    def test_disabled_standby_path_rejects_queued_callbacks_even_when_both_off(self):
        app = self.production_app()
        app._read_tv_power_w()
        advance(app, 2)
        app._begin_power_verify_then_wall("off", "off", "power_standby")
        app._schedule_wall_move_after_tv_off("off", "off", "power_standby")
        app._move_lift_to_wall(app._wall_timer_kwargs("move", schedule_mode="power_standby"))
        self.assertEqual(wall_calls(app), [])
        self.assertIsNone(app._power_wall_verify_handle)

    def test_both_off_still_require_two_seconds_then_use_normal_wall_delay(self):
        app = self.production_app()
        app._fire_wall_after_session_over()
        self.assertEqual(wall_calls(app), [])
        self.assertIsNone(app._delayed_wall_handle)
        advance(app, 2)
        fire_timer(app, app._power_wall_verify_tick)
        self.assertEqual(wall_calls(app), [])
        callback, delay, kwargs = take_timer(app, app._move_lift_to_wall)
        advance(app, delay)
        callback(kwargs)
        self.assertEqual(wall_calls(app), [WALL])


if __name__ == "__main__":
    unittest.main()
