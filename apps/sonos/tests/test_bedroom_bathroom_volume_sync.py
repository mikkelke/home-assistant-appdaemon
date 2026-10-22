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
from types import SimpleNamespace
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


def wake_app(cap):
    """get_app() for a wake routine whose wake_volume_cap answers `cap` for the bedroom speaker
    (None: no wake-up, or the bed reads empty)."""
    asked = []

    def wake_volume_cap(entity):
        asked.append(entity)
        return cap

    def get_app(name):
        return SimpleNamespace(wake_volume_cap=wake_volume_cap) if name == "WakeupBedroom" else None

    get_app.asked = asked
    return get_app


def wake_make_app(get_app, *, bathroom=0.24, bedroom=0.12, **kw):
    """An active sync with the bathroom louder than the bedroom, asking the wake routine."""
    app = make_app(sync_active=True, bathroom_volume=bathroom, bedroom_volume=bedroom, **kw)
    app.wake_app = "WakeupBedroom"
    app.bedroom_speaker = BEDROOM
    app.get_app = get_app
    app.log = MagicMock()
    return app


def written(app):
    return [c.kwargs["volume_level"] for c in app.call_service.call_args_list]


class WakeCapLimitsTheCopy(unittest.TestCase):
    """While the wake-up plays out with someone still in the bed, the bedroom copies the bathroom
    only up to the cap the wake routine names - whatever triggers the copy."""

    def test_a_louder_bathroom_is_copied_only_up_to_the_cap(self):
        app = wake_make_app(wake_app(0.14))
        app._on_bathroom_volume_change(BATHROOM, "volume_level", 0.18, 0.24, {})
        self.assertEqual(written(app), [0.14])

    def test_every_trigger_is_capped(self):
        for trigger in (
            lambda app: app._on_speaker_state_change(BATHROOM, "state", "idle", "playing", {}),
            lambda app: app._maybe_sync("anything"),
        ):
            app = wake_make_app(wake_app(0.14), bathroom=0.43)
            trigger(app)
            self.assertEqual(written(app), [0.14])

    def test_the_door_arming_timer_copies_only_up_to_the_cap(self):
        app = wake_make_app(wake_app(0.14), bathroom=0.34)
        app._sync_active = False
        app.states[(DOOR, None)] = "on"
        app._sync_timer_fire({})
        self.assertTrue(app._sync_active)
        self.assertEqual(written(app), [0.14])

    def test_a_bathroom_below_the_cap_is_copied_as_always(self):
        app = wake_make_app(wake_app(0.14), bathroom=0.09, bedroom=0.14)
        app._maybe_sync("bathroom_volume_change")
        self.assertEqual(written(app), [0.09])

    def test_a_bedroom_already_at_the_cap_is_not_written_again(self):
        app = wake_make_app(wake_app(0.14), bathroom=0.34, bedroom=0.14)
        app._maybe_sync("bathroom_volume_change")
        app.call_service.assert_not_called()

    def test_a_bedroom_found_above_the_cap_is_brought_down_to_it(self):
        app = wake_make_app(wake_app(0.14), bathroom=0.34, bedroom=0.24)
        app._maybe_sync("bathroom_volume_change")
        self.assertEqual(written(app), [0.14])

    def test_the_wake_routine_is_asked_about_the_bedroom_speaker(self):
        get_app = wake_app(0.14)
        wake_make_app(get_app)._maybe_sync("x")
        self.assertEqual(get_app.asked, [BEDROOM])

    def test_the_log_says_the_copy_was_capped(self):
        app = wake_make_app(wake_app(0.14))
        app._maybe_sync("bathroom_volume_change")
        logged = [str(c) for c in app.log.call_args_list]
        self.assertTrue(any("volume_sync_apply" in line and "capped at the wake standard 0.14" in line for line in logged))


class SyncBehavesAsBeforeWithoutACap(unittest.TestCase):
    """No cap means no wake-up in progress, or the bed reads empty (the volume follows the person),
    or the wake routine cannot be asked."""

    def test_no_cap_copies_the_bathroom_in_full(self):
        app = wake_make_app(wake_app(None))
        app._on_bathroom_volume_change(BATHROOM, "volume_level", 0.18, 0.24, {})
        self.assertEqual(written(app), [0.24])
        logged = [str(c) for c in app.log.call_args_list]
        self.assertFalse(any("capped" in line for line in logged))

    def test_the_bed_going_empty_lets_the_next_copy_through(self):
        cap = {"v": 0.14}
        app = wake_make_app(lambda name: SimpleNamespace(wake_volume_cap=lambda entity: cap["v"]))
        app._maybe_sync("a")
        self.assertEqual(written(app), [0.14])
        cap["v"] = None
        app._maybe_sync("b")
        self.assertEqual(written(app), [0.14, 0.24])

    def test_a_wake_routine_that_cannot_be_asked_changes_nothing(self):
        def broken(name):
            raise RuntimeError("app registry unavailable")

        for get_app in (lambda name: None, broken, lambda name: SimpleNamespace()):
            app = wake_make_app(get_app)
            app._maybe_sync("bathroom_volume_change")
            self.assertEqual(written(app), [0.24])

    def test_a_wake_routine_whose_answer_raises_or_is_not_a_number_changes_nothing(self):
        def raising(entity):
            raise RuntimeError("mid reload")

        for answer in (raising, lambda entity: "loud"):
            app = wake_make_app(lambda name, answer=answer: SimpleNamespace(wake_volume_cap=answer))
            app._maybe_sync("bathroom_volume_change")
            self.assertEqual(written(app), [0.24])

    def test_the_wake_app_name_is_configurable_and_defaults_to_the_routine(self):
        self.assertEqual(make_full_app().wake_app, "WakeupBedroom")
        self.assertEqual(make_full_app(overrides={"wake_app": "OtherWake"}).wake_app, "OtherWake")


if __name__ == "__main__":
    unittest.main()
