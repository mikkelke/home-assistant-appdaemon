"""BedroomBathroomVolumeSync: while the bathroom door has been continuously open for
door_open_debounce_sec, the bedroom speaker's volume tracks the bathroom speaker's volume -
one-directional (bathroom leads, bedroom follows, never the reverse), only while both speakers
are actively playing. Door closing deactivates tracking immediately, no forced restore.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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

import bedroom_bathroom_volume_sync as bbvs  # noqa: E402

DOOR = "binary_sensor.bathroom_door_contact"
BATHROOM = "media_player.bathroom"
BEDROOM = "media_player.bedroom"
DEBOUNCE_SEC = 120


def make_app(
    door="off",
    bathroom_state="playing",
    bedroom_state="playing",
    bathroom_volume=0.5,
    bedroom_volume=0.3,
    sync_active=False,
    sync_arm_timer=None,
):
    """BedroomBathroomVolumeSync with fake AppDaemon callables, without running initialize()."""
    app = bbvs.BedroomBathroomVolumeSync.__new__(bbvs.BedroomBathroomVolumeSync)
    app.bathroom_door_sensor = DOOR
    app.bathroom_speaker = BATHROOM
    app.bedroom_speaker = BEDROOM
    app.door_open_debounce_sec = DEBOUNCE_SEC
    app._sync_active = sync_active
    app._sync_arm_timer = sync_arm_timer

    app.states = {
        (DOOR, None): door,
        (BATHROOM, None): bathroom_state,
        (BATHROOM, "volume_level"): bathroom_volume,
        (BEDROOM, None): bedroom_state,
        (BEDROOM, "volume_level"): bedroom_volume,
    }

    def get_state(entity, attribute=None, **kw):
        return app.states.get((entity, attribute))

    app.get_state = get_state
    app.log = lambda *a, **kw: None
    app.run_in = MagicMock(return_value="arm-timer-handle")
    app.cancel_timer = MagicMock()
    app.call_service = MagicMock()
    return app


BASE_ARGS = {
    "bathroom_door_sensor": DOOR,
    "bathroom_speaker": BATHROOM,
    "bedroom_speaker": BEDROOM,
    "door_open_debounce_sec": DEBOUNCE_SEC,
}


def make_full_app(door_state="off", overrides=None):
    """BedroomBathroomVolumeSync with initialize() actually run (AppDaemon primitives stubbed
    only) - used to verify listener *registration* (directional-only: no listener on the
    bedroom's volume_level) and initialize()-time restart safety (door already open at boot)."""
    app = bbvs.BedroomBathroomVolumeSync.__new__(bbvs.BedroomBathroomVolumeSync)
    app.args = dict(BASE_ARGS)
    if overrides:
        app.args.update(overrides)
    app.log = MagicMock()
    app.listen_state = MagicMock()
    app.run_in = MagicMock(return_value="arm-timer-handle")
    app.get_state = lambda entity, **kw: door_state if entity == app.args["bathroom_door_sensor"] else None
    app.initialize()
    return app


class DoorOpenArmsTimer(unittest.TestCase):
    def test_door_open_arms_timer_once_not_yet_active(self):
        app = make_app(door="off")
        app._on_door_change(DOOR, "state", "off", "on", {})
        app.run_in.assert_called_once_with(app._sync_timer_fire, DEBOUNCE_SEC)
        self.assertFalse(app._sync_active)

    def test_reentrant_arm_while_already_armed_does_not_call_run_in_again(self):
        app = make_app(door="off")
        app._on_door_change(DOOR, "state", "off", "on", {})
        app._on_door_change(DOOR, "state", "on", "on", {})  # re-entrant edge, already armed
        app.run_in.assert_called_once()


class DoorClosesBeforeTimerFires(unittest.TestCase):
    def test_close_cancels_armed_timer_and_clears_handle(self):
        app = make_app(sync_arm_timer="arm-timer-handle")
        app._on_door_change(DOOR, "state", "on", "off", {})
        app.cancel_timer.assert_called_once_with("arm-timer-handle")
        self.assertIsNone(app._sync_arm_timer)
        self.assertFalse(app._sync_active)


class TimerFire(unittest.TestCase):
    def test_fire_while_door_still_open_activates_and_syncs(self):
        app = make_app(door="on", bathroom_volume=0.5, bedroom_volume=0.3)
        app._sync_timer_fire({})
        self.assertTrue(app._sync_active)
        app.call_service.assert_called_once_with(
            "media_player/volume_set", entity_id=BEDROOM, volume_level=0.5
        )

    def test_fire_while_door_closed_stays_inactive_no_sync(self):
        app = make_app(door="off")
        app._sync_timer_fire({})
        self.assertFalse(app._sync_active)
        app.call_service.assert_not_called()


class BathroomVolumeChangeTracksWhileActive(unittest.TestCase):
    def test_bathroom_volume_change_applies_to_bedroom(self):
        app = make_app(sync_active=True, bathroom_volume=0.7, bedroom_volume=0.3)
        app._on_bathroom_volume_change(BATHROOM, "volume_level", 0.5, 0.7, {})
        app.call_service.assert_called_once_with(
            "media_player/volume_set", entity_id=BEDROOM, volume_level=0.7
        )

    def test_bathroom_not_playing_skips_sync(self):
        app = make_app(sync_active=True, bathroom_state="paused")
        app._on_bathroom_volume_change(BATHROOM, "volume_level", 0.5, 0.7, {})
        app.call_service.assert_not_called()

    def test_bedroom_not_playing_skips_sync(self):
        app = make_app(sync_active=True, bedroom_state="paused")
        app._on_bathroom_volume_change(BATHROOM, "volume_level", 0.5, 0.7, {})
        app.call_service.assert_not_called()

    def test_epsilon_skip_when_already_matched(self):
        app = make_app(sync_active=True, bathroom_volume=0.500, bedroom_volume=0.497)
        app._maybe_sync("test")
        app.call_service.assert_not_called()


class BedroomVolumeIsNeverAListenTarget(unittest.TestCase):
    """Directional-only: nothing listens for the bedroom speaker's volume_level, so a bedroom
    volume change can never itself trigger a call_service (there is no handler to reach)."""

    def setUp(self):
        self.app = make_full_app(door_state="off")

    def test_no_listen_state_registered_for_bedroom_volume_level(self):
        calls = [
            c
            for c in self.app.listen_state.call_args_list
            if c.args and c.args[1] == BEDROOM and c.kwargs.get("attribute") == "volume_level"
        ]
        self.assertEqual(calls, [])

    def test_bathroom_volume_level_is_registered(self):
        calls = [
            c
            for c in self.app.listen_state.call_args_list
            if c.args and c.args[1] == BATHROOM and c.kwargs.get("attribute") == "volume_level"
        ]
        self.assertEqual(len(calls), 1)


class DoorCloseDeactivatesTracking(unittest.TestCase):
    def test_close_while_active_deactivates_and_stops_future_sync(self):
        app = make_app(sync_active=True)
        app._on_door_change(DOOR, "state", "on", "off", {})
        self.assertFalse(app._sync_active)

        app.call_service.reset_mock()
        app._on_bathroom_volume_change(BATHROOM, "volume_level", 0.5, 0.7, {})
        app.call_service.assert_not_called()


class RestartSafety(unittest.TestCase):
    """initialize()-level restart safety: an already-open door must arm the sync timer
    immediately instead of waiting for a future door-open edge."""

    def test_door_already_open_at_init_arms_timer_immediately(self):
        app = make_full_app(door_state="on")
        app.run_in.assert_called_once_with(app._sync_timer_fire, DEBOUNCE_SEC)
        self.assertEqual(app._sync_arm_timer, "arm-timer-handle")

    def test_door_closed_at_init_does_not_arm_timer(self):
        app = make_full_app(door_state="off")
        app.run_in.assert_not_called()
        self.assertIsNone(app._sync_arm_timer)


if __name__ == "__main__":
    unittest.main()
