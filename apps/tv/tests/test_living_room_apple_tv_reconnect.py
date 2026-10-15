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
RECONNECT = ("remote/turn_on", {"entity_id": REMOTE})


def states(**over):
    base = {TV: "on", HISENSE: "on", APPLE: "off", REMOTE: "off"}
    base.update(over)
    return base


def make_app(state_map, cooldown=600.0):
    """LivingRoomTvControl with fake get_state/call_service/run_in, without running
    AppDaemon's initialize()."""
    app = lrc.LivingRoomTvControl.__new__(lrc.LivingRoomTvControl)
    app.tv_entity = TV
    app.hisense_tv_entity = HISENSE
    app.apple_tv_entity = APPLE
    app.apple_tv_remote_entity = REMOTE
    app.apple_tv_reconnect_cooldown_seconds = cooldown
    app._apple_reconnect_at = None
    app.mono = 1000.0
    app._monotonic = lambda: app.mono
    app.get_state = lambda entity, **kw: state_map.get(entity)
    app.log = lambda *a, **kw: None
    app.service_calls = []
    app.call_service = lambda service, **kw: app.service_calls.append((service, kw))
    return app


def run_initialize(extra_args=None):
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
    app.log = lambda *a, **kw: None
    app.error = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None
    app.listen_state = lambda *a, **kw: None
    app.scheduled = []
    app.run_in = lambda cb, delay, **kw: app.scheduled.append((cb, delay, kw)) or object()
    app.initialize()
    return app


class AppleTvReconnectOnStartup(unittest.TestCase):
    """With start_off every HA restart leaves the Apple TV integration disconnected: the
    remote and the player both read off with no attributes while the box is in use, and
    media_player.turn_on does nothing in that state. remote.turn_on reconnects without
    waking the box. An HA restart re-runs initialize(), so the startup check is what
    heals it; it must send the reconnect only when the TV is on and the integration
    holds no connection."""

    def test_initialize_schedules_the_startup_check(self):
        app = run_initialize()
        self.assertIn((app._reconnect_apple_tv_on_init, 5), [(cb, d) for cb, d, _kw in app.scheduled])

    def test_tv_on_and_disconnected_reconnects_through_the_remote_only(self):
        app = make_app(states())
        app._reconnect_apple_tv_on_init({})
        self.assertEqual(app.service_calls, [RECONNECT])
        self.assertEqual([c for c in app.service_calls if c[0].startswith("media_player/")], [])

    def test_either_tv_signal_counts_as_on(self):
        app = make_app(states(**{TV: "off", HISENSE: "on"}))
        app._reconnect_apple_tv_on_init({})
        self.assertEqual(app.service_calls, [RECONNECT])
        app = make_app(states(**{TV: "on", HISENSE: "unavailable"}))
        app._reconnect_apple_tv_on_init({})
        self.assertEqual(app.service_calls, [RECONNECT])

    def test_tv_off_does_nothing(self):
        for tv, hisense in (("off", "off"), ("unavailable", "unavailable"), ("off", "standby"), (None, None)):
            app = make_app(states(**{TV: tv, HISENSE: hisense}))
            app._reconnect_apple_tv_on_init({})
            self.assertEqual(app.service_calls, [], (tv, hisense))

    def test_connected_apple_tv_does_nothing(self):
        for player in ("playing", "paused", "idle"):
            app = make_app(states(**{APPLE: player, REMOTE: "on"}))
            app._reconnect_apple_tv_on_init({})
            self.assertEqual(app.service_calls, [], player)

    def test_connected_but_sleeping_box_does_nothing(self):
        app = make_app(states(**{REMOTE: "on"}))
        app._reconnect_apple_tv_on_init({})
        self.assertEqual(app.service_calls, [])

    def test_unknown_player_state_does_nothing(self):
        app = make_app(states(**{APPLE: "unknown"}))
        app._reconnect_apple_tv_on_init({})
        self.assertEqual(app.service_calls, [])

    def test_missing_remote_does_nothing(self):
        state_map = states()
        del state_map[REMOTE]
        app = make_app(state_map)
        app._reconnect_apple_tv_on_init({})
        self.assertEqual(app.service_calls, [])

    def test_repeat_inside_the_window_is_skipped_and_allowed_after_it(self):
        app = make_app(states())
        app._reconnect_apple_tv_if_needed("startup")
        app.mono += 599
        app._reconnect_apple_tv_if_needed("startup")
        self.assertEqual(app.service_calls, [RECONNECT])
        app.mono += 2
        app._reconnect_apple_tv_if_needed("startup")
        self.assertEqual(app.service_calls, [RECONNECT, RECONNECT])

    def test_checks_that_send_nothing_do_not_use_up_the_window(self):
        state_map = states(**{REMOTE: "on"})
        app = make_app(state_map)
        app._reconnect_apple_tv_if_needed("startup")
        state_map[REMOTE] = "off"
        app._reconnect_apple_tv_if_needed("startup")
        self.assertEqual(app.service_calls, [RECONNECT])

    def test_window_comes_from_the_yaml_args(self):
        app = run_initialize({"apple_tv_reconnect_cooldown_seconds": 30})
        self.assertEqual(app.apple_tv_reconnect_cooldown_seconds, 30.0)
        self.assertEqual(run_initialize().apple_tv_reconnect_cooldown_seconds, 600.0)

    def test_remote_entity_follows_the_apple_tv_player(self):
        app = run_initialize({"apple_tv_entity": "media_player.kitchen_apple_tv"})
        self.assertEqual(app.apple_tv_remote_entity, "remote.kitchen_apple_tv")
        self.assertEqual(run_initialize().apple_tv_remote_entity, REMOTE)

    def test_a_failed_call_is_swallowed_and_does_not_start_the_window(self):
        app = make_app(states())

        def boom(service, **kw):
            raise RuntimeError("ha unreachable")

        app.call_service = boom
        app._reconnect_apple_tv_if_needed("startup")
        self.assertIsNone(app._apple_reconnect_at)


if __name__ == "__main__":
    unittest.main()
