"""AssistRooms: room voice ducking and music intents without changing volume levels."""

from __future__ import annotations

import copy
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

import assist_rooms as ar  # noqa: E402

BATHROOM = "media_player.bathroom"
BEDROOM = "media_player.bedroom"
SATELLITE = "assist_satellite.bathroom_bathroom_display_assist_satellite"
ANSWER = "binary_sensor.bathroom_display_answer_playing"
MASTER = "input_select.sonos_master_speaker"
RADIO = "dr_podcasts://radio/p3"


def state(value="playing", muted=False, grouped=False):
    return {"state": value, "attributes": {
        "is_volume_muted": muted,
        "group_members": [BATHROOM, BEDROOM] if grouped else [BATHROOM],
    }}


def service(name, **data):
    return {"service": f"media_player/{name}", "data": {"entity_id": BATHROOM, **data}}


def radio_action():
    return service("play_media", media_content_id=RADIO, media_content_type="music")


class DuckDecisions(unittest.TestCase):
    def test_mute_only_playing_unmuted_speaker(self):
        self.assertEqual(ar.duck_actions(BATHROOM, state(), True, False),
                         [service("volume_mute", is_volume_muted=True)])
        for value in ("paused", "idle", "off", "unavailable", "unknown", None):
            with self.subTest(value=value):
                self.assertEqual(ar.duck_actions(BATHROOM, state(value), True, False), [])
        self.assertEqual(ar.duck_actions(BATHROOM, state(muted=True), True, False), [])
        self.assertEqual(ar.duck_actions(BATHROOM, state(), True, True), [])

    def test_release_only_owned_mute_regardless_playback(self):
        for value in ("playing", "paused", "idle"):
            with self.subTest(value=value):
                self.assertEqual(ar.duck_actions(BATHROOM, state(value, muted=True), False, True),
                                 [service("volume_mute", is_volume_muted=False)])
        self.assertEqual(ar.duck_actions(BATHROOM, state(muted=True), False, False), [])
        self.assertEqual(ar.duck_actions(BATHROOM, state(), False, True), [])

    def test_decisions_do_not_mutate_state(self):
        full = state(muted=True, grouped=True)
        before = copy.deepcopy(full)
        ar.duck_actions(BATHROOM, full, False, True)
        ar.intent_actions("assist_radio", BATHROOM, full, BEDROOM, state(), RADIO)
        self.assertEqual(full, before)

    def test_no_speaker_has_no_duck_actions(self):
        self.assertEqual(ar.duck_actions(None, state(), True, False), [])


class IntentDecisions(unittest.TestCase):
    def decide(self, event, full=None, master=BEDROOM, master_state=None):
        return ar.intent_actions(event, BATHROOM, full or state(), master,
                                 master_state or state("idle"), RADIO)

    def test_music_here_joins_playing_master_before_resuming_room(self):
        self.assertEqual(self.decide("assist_music_here", state("paused"), master_state=state()), [{
            "event": "request_sonos_group_join",
            "data": {"master_entity_id": BEDROOM, "slave_entity_id": BATHROOM},
        }])

    def test_music_here_resumes_paused_room(self):
        self.assertEqual(self.decide("assist_music_here", state("paused")), [service("media_play")])

    def test_music_here_starts_radio_otherwise(self):
        for value in ("playing", "idle", "unavailable"):
            with self.subTest(value=value):
                self.assertEqual(self.decide("assist_music_here", state(value)), [radio_action()])
        self.assertEqual(self.decide("assist_music_here", master=BATHROOM, master_state=state()),
                         [radio_action()])
        self.assertEqual(self.decide("assist_music_here", master=None), [radio_action()])

    def test_music_leave_unjoins_group_before_pausing(self):
        self.assertEqual(self.decide("assist_music_leave", state(grouped=True)), [{
            "event": "request_sonos_group_unjoin", "data": {"entity_id": BATHROOM},
        }])

    def test_music_leave_pauses_solo_playback(self):
        self.assertEqual(self.decide("assist_music_leave"), [service("media_pause")])

    def test_music_leave_silent_room_does_nothing(self):
        for value in ("paused", "idle", "off", "unavailable"):
            self.assertEqual(self.decide("assist_music_leave", state(value)), [])

    def test_radio_unjoins_then_plays_later(self):
        delayed = {**radio_action(), "delay": 1}
        self.assertEqual(self.decide("assist_radio", state(grouped=True)), [{
            "event": "request_sonos_group_unjoin", "data": {"entity_id": BATHROOM},
        }, delayed])

    def test_radio_alone_plays_immediately(self):
        self.assertEqual(self.decide("assist_radio"), [radio_action()])

    def test_no_speaker_has_no_intent_actions(self):
        for event in ("assist_music_here", "assist_music_leave", "assist_radio"):
            self.assertEqual(ar.intent_actions(event, None, state(), BEDROOM, state(), RADIO), [])


def make_app():
    app = ar.AssistRooms.__new__(ar.AssistRooms)
    app.args = {
        "satellites": {SATELLITE: "bathroom", "assist_satellite.hallway": "hallway"},
        "speakers": {"bathroom": BATHROOM, "bedroom": BEDROOM},
        "master_select": MASTER, "radio": RADIO, "duck_failsafe_s": 20,
        "answer_sensors": {SATELLITE: ANSWER},
    }
    app.states = {BATHROOM: state(), BEDROOM: state(), MASTER: {
        "state": "Bedroom", "attributes": {"master_entity_id": BEDROOM},
    }}

    def get_state(entity, attribute=None):
        full = app.states.get(entity, {})
        if attribute == "all":
            return full
        if attribute:
            return full.get("attributes", {}).get(attribute)
        return full.get("state")

    app.get_state = get_state
    app.log = MagicMock()
    app.call_service = MagicMock()
    app.fire_event = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_in = MagicMock()
    app.initialize()
    return app


def satellite(app, old, new):
    app._on_satellite_state(SATELLITE, "state", old, new, {})


class AssistRoomsGlue(unittest.TestCase):
    def test_registers_all_satellites_and_intents(self):
        app = make_app()
        for entity in app.args["satellites"]:
            self.assertTrue(any(call.args[1] == entity for call in app.listen_state.call_args_list))
        self.assertEqual({call.args[1] for call in app.listen_event.call_args_list},
                         {"assist_music_here", "assist_music_leave", "assist_radio"})

    def test_listening_exit_releases_owned_mute_even_after_pause(self):
        for new in ("responding", "idle", "unavailable", None):
            with self.subTest(new=new):
                app = make_app()
                satellite(app, "idle", "listening")
                app.call_service.assert_called_once_with(
                    "media_player/volume_mute", entity_id=BATHROOM, is_volume_muted=True)
                app.states[BATHROOM] = state("paused", muted=True)
                satellite(app, "listening", new)
                self.assertEqual(app.call_service.call_count, 2)
                app.call_service.assert_called_with(
                    "media_player/volume_mute", entity_id=BATHROOM, is_volume_muted=False)
                self.assertFalse(app._ducked)

    def test_already_muted_is_never_owned_or_released(self):
        app = make_app()
        app.states[BATHROOM] = state(muted=True)
        satellite(app, "idle", "listening")
        satellite(app, "listening", "responding")
        app.call_service.assert_not_called()
        app.run_in.assert_not_called()
        self.assertFalse(app._ducked)

    def test_failsafe_releases_once_and_clears_ownership(self):
        app = make_app()
        satellite(app, "idle", "listening")
        timer = app.run_in.call_args
        self.assertEqual(timer.args[1], 20)
        app.states[BATHROOM] = state(muted=True)
        timer.args[0](timer.kwargs)
        timer.args[0](timer.kwargs)
        satellite(app, "listening", "responding")
        self.assertEqual(app.call_service.call_count, 2)
        self.assertFalse(app._ducked)

    def test_external_unmute_prevents_release_and_clears_ownership(self):
        app = make_app()
        satellite(app, "idle", "listening")
        satellite(app, "listening", "responding")
        self.assertEqual(app.call_service.call_count, 1)
        self.assertFalse(app._ducked)

    def test_failsafe_after_external_unmute_clears_ownership(self):
        app = make_app()
        satellite(app, "idle", "listening")
        timer = app.run_in.call_args
        timer.args[0](timer.kwargs)
        self.assertEqual(app.call_service.call_count, 1)
        self.assertFalse(app._ducked)

    def test_responding_to_idle_has_no_duck_actions(self):
        app = make_app()
        satellite(app, "responding", "idle")
        app.call_service.assert_not_called()
        app.run_in.assert_not_called()

    def test_old_failsafe_cannot_release_new_listening_cycle(self):
        app = make_app()
        satellite(app, "idle", "listening")
        old_timer = app.run_in.call_args
        app.states[BATHROOM] = state(muted=True)
        satellite(app, "listening", "responding")
        app.states[BATHROOM] = state()
        satellite(app, "responding", "listening")
        app.states[BATHROOM] = state(muted=True)
        old_timer.args[0](old_timer.kwargs)
        self.assertEqual(app.call_service.call_count, 3)
        self.assertTrue(app._ducked)

    def test_wake_while_answer_plays_never_mutes(self):
        app = make_app()
        app.states[ANSWER] = {"state": "on"}
        satellite(app, "responding", "listening")
        app.call_service.assert_not_called()
        app.run_in.assert_not_called()
        self.assertFalse(app._ducked)

    def test_follow_up_after_answer_mutes(self):
        app = make_app()
        app.states[ANSWER] = {"state": "off"}
        satellite(app, "responding", "listening")
        app.call_service.assert_called_once_with(
            "media_player/volume_mute", entity_id=BATHROOM, is_volume_muted=True)

    def test_satellite_without_speaker_does_nothing(self):
        app = make_app()
        app._on_satellite_state("assist_satellite.hallway", "state", "idle", "listening", {})
        app.call_service.assert_not_called()
        app.run_in.assert_not_called()

    def test_unknown_or_missing_area_warns_without_actions(self):
        for data in ({}, {"area_id": "unknown"}, {"area_id": "hallway"}):
            for event in ("assist_music_here", "assist_music_leave", "assist_radio"):
                with self.subTest(data=data, event=event):
                    app = make_app()
                    app.log.reset_mock()
                    app._on_intent(event, data, {})
                    app.call_service.assert_not_called()
                    app.fire_event.assert_not_called()
                    app.run_in.assert_not_called()
                    self.assertTrue(any(call.kwargs.get("level") == "WARNING"
                                        for call in app.log.call_args_list))

    def test_master_attribute_drives_join_payload(self):
        app = make_app()
        app._on_intent("assist_music_here", {"area_id": "bathroom"}, {})
        app.fire_event.assert_called_once_with(
            "request_sonos_group_join", master_entity_id=BEDROOM, slave_entity_id=BATHROOM)
        app.call_service.assert_not_called()

    def test_grouped_radio_is_delayed_and_keeps_target(self):
        app = make_app()
        app.states[BATHROOM] = state(grouped=True)
        app._on_intent("assist_radio", {"area_id": "bathroom"}, {})
        app.fire_event.assert_called_once_with("request_sonos_group_unjoin", entity_id=BATHROOM)
        app.call_service.assert_not_called()
        timer = app.run_in.call_args
        self.assertEqual(timer.args[1], 1)
        timer.args[0](timer.kwargs)
        app.call_service.assert_called_once_with(
            "media_player/play_media", entity_id=BATHROOM,
            media_content_id=RADIO, media_content_type="music")


if __name__ == "__main__":
    unittest.main()
