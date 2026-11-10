"""AssistRooms: room music intents."""

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
MASTER = "input_select.sonos_master_speaker"
RADIO = "dr_podcasts://radio/p3"


def state(value="playing", grouped=False):
    return {"state": value, "attributes": {
        "group_members": [BATHROOM, BEDROOM] if grouped else [BATHROOM],
    }}


def service(name, **data):
    return {"service": f"media_player/{name}", "data": {"entity_id": BATHROOM, **data}}


def radio_action():
    return service("play_media", media_content_id=RADIO, media_content_type="music")


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
        "speakers": {"bathroom": BATHROOM, "bedroom": BEDROOM},
        "master_select": MASTER, "radio": RADIO,
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


class AssistRoomsGlue(unittest.TestCase):
    def test_registers_intents(self):
        app = make_app()
        app.listen_state.assert_not_called()
        self.assertEqual({call.args[1] for call in app.listen_event.call_args_list},
                         {"assist_music_here", "assist_music_leave", "assist_radio"})

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
