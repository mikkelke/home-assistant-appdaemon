from __future__ import annotations

import sys
import types
import unittest
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

import living_room_tv_control as lrc  # noqa: E402


TV = "media_player.living_room_tv"
HISENSE = "media_player.living_room_hisense_tv_television"
APPLE = "media_player.living_room_apple_tv"
REMOTE = "remote.living_room_apple_tv"
CONNECT = ("remote/turn_on", {"entity_id": REMOTE})
DISCONNECT = ("remote/turn_off", {"entity_id": REMOTE})


def states(**over):
    base = {TV: "off", HISENSE: "off", APPLE: "off", REMOTE: "off"}
    base.update(over)
    return base


def make_app(state_map, extra_args=None):
    """An initialized LivingRoomTvControl whose timers, listeners, states and service calls
    are driven by hand. The timers initialize() scheduled are kept in app.startup."""
    args = {
        "tv_entity": TV,
        "lift_select_entity": "select.living_room_tv_lift_position",
        "zwave_device_id": "device",
        "button_3_property_key": "003",
        "button_4_property_key": "004",
        "zwave_command_class": 91,
        "zwave_endpoint": 0,
        "zwave_event_value_held": "KeyHeldDown",
    }
    args.update(extra_args or {})
    app = lrc.LivingRoomTvControl.__new__(lrc.LivingRoomTvControl)
    app.args = args
    app.states = state_map
    app.mono = 1000.0
    app._monotonic = lambda: app.mono
    app.log = lambda *a, **kw: None
    app.error = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None
    app.fire_event = lambda *a, **kw: None
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.service_calls = []
    app.call_service = lambda service, **kw: app.service_calls.append((service, kw))
    app.listeners = []
    app.listen_state = lambda cb, entity, **kw: app.listeners.append((cb, entity, kw))
    app.timers = {}
    app.next_handle = 0

    def run_in(cb, delay, **kw):
        app.next_handle += 1
        app.timers[app.next_handle] = (cb, delay, kw)
        return app.next_handle

    app.run_in = run_in
    app.timer_running = lambda handle: handle in app.timers
    app.cancel_timer = lambda handle: app.timers.pop(handle, None)
    app.initialize()
    app.startup = list(app.timers.values())
    app.timers.clear()
    return app


def pending(app, cb):
    return [delay for c, delay, _kw in app.timers.values() if c == cb]


def fire(app, cb):
    for handle, (c, _delay, kw) in list(app.timers.items()):
        if c == cb:
            del app.timers[handle]
            c(kw)


def tv_changes(app, old, new):
    app.states[TV] = new
    app.handle_tv_state_change(TV, None, old, new, {})


def remote_connects(app):
    app.states[REMOTE] = "on"
    app._on_apple_tv_remote_connected(REMOTE, None, "off", "on", {})


class DisconnectAfterTheGrace(unittest.TestCase):
    """With start_off the integration keeps connecting to the sleeping box and losing it
    again, which flips remote.living_room_apple_tv about once a minute for as long as the
    TV is off. remote.turn_off closes the connection and stops that."""

    def test_tv_off_starts_the_grace_and_sends_nothing_yet(self):
        app = make_app(states(**{TV: "on", HISENSE: "on", REMOTE: "on"}))
        tv_changes(app, "on", "off")
        self.assertEqual(pending(app, app._apple_tv_disconnect_due), [120.0])
        self.assertEqual(app.service_calls, [])

    def test_tv_still_off_after_the_grace_disconnects_through_the_remote_only(self):
        app = make_app(states(**{REMOTE: "on"}))
        tv_changes(app, "on", "off")
        fire(app, app._apple_tv_disconnect_due)
        self.assertEqual(app.service_calls, [DISCONNECT])

    def test_tv_back_on_inside_the_grace_cancels_the_disconnect(self):
        app = make_app(states(**{REMOTE: "on"}))
        tv_changes(app, "on", "off")
        tv_changes(app, "off", "on")
        self.assertEqual(pending(app, app._apple_tv_disconnect_due), [])
        self.assertEqual(app.service_calls, [])

    def test_a_missed_cancel_still_cannot_disconnect_while_the_tv_is_on(self):
        for tv, hisense in (("on", "on"), ("playing", "off"), ("off", "on")):
            app = make_app(states(**{TV: tv, HISENSE: hisense, REMOTE: "on"}))
            app._apple_tv_disconnect_due({})
            self.assertEqual(app.service_calls, [], (tv, hisense))

    def test_already_disconnected_sends_nothing(self):
        app = make_app(states(**{REMOTE: "off"}))
        tv_changes(app, "on", "off")
        fire(app, app._apple_tv_disconnect_due)
        self.assertEqual(app.service_calls, [])

    def test_an_unknown_blip_restarts_the_grace(self):
        app = make_app(states(**{REMOTE: "on"}))
        tv_changes(app, "on", "off")
        tv_changes(app, "off", "unknown")
        self.assertEqual(pending(app, app._apple_tv_disconnect_due), [])
        tv_changes(app, "unknown", "off")
        self.assertEqual(pending(app, app._apple_tv_disconnect_due), [120.0])
        self.assertEqual(app.service_calls, [])

    def test_grace_comes_from_the_yaml_args(self):
        app = make_app(states(), {"apple_tv_disconnect_grace_seconds": 45})
        tv_changes(app, "on", "off")
        self.assertEqual(pending(app, app._apple_tv_disconnect_due), [45.0])

    def test_unreadable_states_do_nothing(self):
        app = make_app(states(**{REMOTE: "on"}))

        def boom(entity, **kw):
            raise RuntimeError("ha unreachable")

        app.get_state = boom
        app._apple_tv_disconnect_due({})
        self.assertEqual(app.service_calls, [])

    def test_a_failed_call_is_swallowed_and_keeps_the_reconnect_window(self):
        app = make_app(states(**{REMOTE: "on"}))
        app._apple_reconnect_at = 900.0

        def boom(service, **kw):
            raise RuntimeError("ha unreachable")

        app.call_service = boom
        app._apple_tv_disconnect_due({})
        self.assertEqual(app._apple_reconnect_at, 900.0)

    def test_a_scheduling_failure_is_swallowed(self):
        app = make_app(states())

        def boom(*a, **kw):
            raise RuntimeError("scheduler down")

        app.run_in = boom
        app._arm_apple_tv_disconnect("TV off")
        self.assertIsNone(app._apple_tv_disconnect_handle)


class ReconnectsAfterTheGraceAreCut(unittest.TestCase):
    """The integration can be in the off half of its connect/lost cycle when the grace
    ends, so the check there sees a disconnected remote and the next connect has to be
    cut instead."""

    def test_a_reconnect_after_the_grace_is_cut(self):
        app = make_app(states())
        tv_changes(app, "on", "off")
        fire(app, app._apple_tv_disconnect_due)
        self.assertEqual(app.service_calls, [])
        remote_connects(app)
        self.assertEqual(app.service_calls, [DISCONNECT])

    def test_the_connect_event_itself_is_trusted_over_a_stale_read(self):
        app = make_app(states())
        tv_changes(app, "on", "off")
        fire(app, app._apple_tv_disconnect_due)
        app._on_apple_tv_remote_connected(REMOTE, None, "off", "on", {})
        self.assertEqual(app.service_calls, [DISCONNECT])

    def test_a_reconnect_inside_the_grace_is_left_alone(self):
        app = make_app(states())
        tv_changes(app, "on", "off")
        remote_connects(app)
        self.assertEqual(app.service_calls, [])

    def test_a_reconnect_with_the_tv_on_is_left_alone(self):
        app = make_app(states())
        tv_changes(app, "on", "off")
        fire(app, app._apple_tv_disconnect_due)
        tv_changes(app, "off", "on")
        remote_connects(app)
        self.assertEqual(app.service_calls, [])

    def test_a_reconnect_racing_the_tv_state_update_is_left_alone(self):
        app = make_app(states())
        tv_changes(app, "on", "off")
        fire(app, app._apple_tv_disconnect_due)
        app.states[HISENSE] = "on"
        remote_connects(app)
        self.assertEqual(app.service_calls, [])

    def test_wiring_listens_for_the_remote_turning_on(self):
        app = make_app(states())
        self.assertIn(
            (app._on_apple_tv_remote_connected, REMOTE, {"new": "on"}),
            app.listeners,
        )

    def test_powering_the_tv_on_gives_the_new_connection_a_fresh_grace(self):
        app = make_app(states())
        tv_changes(app, "on", "off")
        fire(app, app._apple_tv_disconnect_due)
        app._power_on_tv_if_needed(reason="button")
        self.assertEqual(app.service_calls, [("media_player/turn_on", {"entity_id": TV})])
        self.assertEqual(pending(app, app._apple_tv_disconnect_due), [120.0])
        remote_connects(app)
        self.assertNotIn(DISCONNECT, app.service_calls)
        fire(app, app._apple_tv_disconnect_due)
        self.assertIn(DISCONNECT, app.service_calls)

    def test_powering_the_tv_on_when_it_is_already_on_starts_no_grace(self):
        app = make_app(states(**{TV: "on", HISENSE: "on"}))
        app._power_on_tv_if_needed(reason="button")
        self.assertEqual(pending(app, app._apple_tv_disconnect_due), [])


class NoFightWithTheReconnect(unittest.TestCase):
    """72e3c75 reconnects a disconnected Apple TV when the TV is on; the disconnect only
    acts when the TV is off, so the two never work against each other."""

    def test_startup_with_the_tv_on_reconnects_and_arms_no_disconnect(self):
        app = make_app(states(**{TV: "on", HISENSE: "on"}))
        for cb, delay, kw in app.startup:
            if cb in (app._reconnect_apple_tv_on_init, app._arm_apple_tv_disconnect_on_init):
                self.assertEqual(delay, 5)
                cb(kw)
        self.assertEqual(app.service_calls, [CONNECT])
        self.assertEqual(pending(app, app._apple_tv_disconnect_due), [])

    def test_startup_with_the_tv_off_arms_the_disconnect_and_reconnects_nothing(self):
        app = make_app(states(**{REMOTE: "on"}))
        callbacks = [cb for cb, _delay, _kw in app.startup]
        self.assertIn(app._arm_apple_tv_disconnect_on_init, callbacks)
        for cb, _delay, kw in app.startup:
            if cb in (app._reconnect_apple_tv_on_init, app._arm_apple_tv_disconnect_on_init):
                cb(kw)
        self.assertEqual(app.service_calls, [])
        self.assertEqual(pending(app, app._apple_tv_disconnect_due), [120.0])
        fire(app, app._apple_tv_disconnect_due)
        self.assertEqual(app.service_calls, [DISCONNECT])

    def test_for_every_tv_and_remote_state_at_most_one_of_the_two_acts(self):
        for tv in ("on", "off"):
            for remote in ("on", "off"):
                reconnect_app = make_app(states(**{TV: tv, HISENSE: tv, REMOTE: remote}))
                reconnect_app._reconnect_apple_tv_if_needed("startup")
                disconnect_app = make_app(states(**{TV: tv, HISENSE: tv, REMOTE: remote}))
                disconnect_app._apple_tv_disconnect_due({})
                self.assertEqual(
                    reconnect_app.service_calls,
                    [CONNECT] if (tv, remote) == ("on", "off") else [],
                    (tv, remote),
                )
                self.assertEqual(
                    disconnect_app.service_calls,
                    [DISCONNECT] if (tv, remote) == ("off", "on") else [],
                    (tv, remote),
                )

    def test_tv_turning_on_after_a_disconnect_reconnects_inside_the_cooldown_window(self):
        app = make_app(states(**{TV: "on", HISENSE: "on"}))
        app._reconnect_apple_tv_if_needed("startup")
        app.mono += 60
        app.states[REMOTE] = "on"
        tv_changes(app, "on", "off")
        app.states[HISENSE] = "off"
        fire(app, app._apple_tv_disconnect_due)
        self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])
        app.mono += 60
        app.states[REMOTE] = "off"
        app.states[HISENSE] = "on"
        tv_changes(app, "off", "on")
        app._execute_tv_power_on_actions()
        self.assertEqual(app.service_calls, [CONNECT, DISCONNECT, CONNECT])

    def test_tv_turning_on_reconnects_without_waiting_for_the_remote_reset(self):
        app = make_app(states(**{TV: "on", HISENSE: "on"}))
        app._execute_tv_power_on_actions()
        self.assertEqual(app.service_calls, [CONNECT])
        self.assertEqual(pending(app, app._apple_tv_remote_reset), [30])


if __name__ == "__main__":
    unittest.main()
