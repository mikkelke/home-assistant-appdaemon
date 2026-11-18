"""AssistRooms: the Sonos duck while a satellite is busy, and room music intents."""

from __future__ import annotations

import copy
import json
import ssl
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
HALLWAY = "assist_satellite.hallway"
ANSWER = "binary_sensor.bathroom_bathroom_display_answer_playing"
MASTER = "input_select.sonos_master_speaker"
RADIO = "dr_podcasts://radio/p3"
HOST = "192.0.2.10"
BEDROOM_HOST = "10.21.0.160"


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

    def test_decisions_do_not_mutate_state(self):
        full = state(grouped=True)
        before = copy.deepcopy(full)
        ar.intent_actions("assist_radio", BATHROOM, full, BEDROOM, state(), RADIO)
        self.assertEqual(full, before)

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


def make_app(duck_hosts=None, states=None):
    app = ar.AssistRooms.__new__(ar.AssistRooms)
    app.args = {
        "satellites": {SATELLITE: "bathroom", HALLWAY: "hallway"},
        "speakers": {"bathroom": BATHROOM, "bedroom": BEDROOM},
        "master_select": MASTER, "radio": RADIO,
        "answer_sensors": [ANSWER],
        "duck_hosts": {"bathroom": HOST} if duck_hosts is None else duck_hosts,
    }
    app.states = {BATHROOM: state(), BEDROOM: state(),
                  SATELLITE: {"state": "idle"}, ANSWER: {"state": "off"},
                  MASTER: {"state": "Bedroom", "attributes": {"master_entity_id": BEDROOM}}}
    app.states.update(states or {})

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


def feed(app, entity, new):
    old = app.states.get(entity, {}).get("state")
    app.states[entity] = {"state": new}
    app._on_state(entity, "state", old, new, {})


def satellite(app, *states):
    for new in states:
        feed(app, SATELLITE, new)


def answer(app, new):
    feed(app, ANSWER, new)


def fire(timer):
    timer.args[0](timer.kwargs)


def logged(app, prefix, level="INFO"):
    return [call.args[0] for call in app.log.call_args_list
            if call.kwargs.get("level") == level and call.args[0].startswith(prefix)]


class AppTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(ar, "sonos_duck")
        self.duck = patcher.start()
        self.addCleanup(patcher.stop)

    def ducks(self):
        return [call.args for call in self.duck.call_args_list]


class AssistRoomsGlue(AppTest):
    def test_registers_all_satellites_answer_sensors_and_intents(self):
        app = make_app()
        for entity in [*app.args["satellites"], *app.args["answer_sensors"]]:
            self.assertTrue(any(call.args[0] == app._on_state and call.args[1] == entity
                                for call in app.listen_state.call_args_list))
        self.assertEqual(len(app.listen_state.call_args_list), 3)
        self.assertEqual({call.args[1] for call in app.listen_event.call_args_list},
                         {"assist_music_here", "assist_music_leave", "assist_radio",
                          "assist_music_group"})

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


class MultiRoomRadio(AppTest):
    def test_two_rooms_play_as_one_group(self):
        for area_id in ("bathroom,bedroom", ["bathroom", "bedroom"]):
            with self.subTest(area_id=area_id):
                app = make_app()
                app._on_intent("assist_radio", {"area_id": area_id}, {})
                app.call_service.assert_called_once_with(
                    "media_player/play_media", entity_id=BATHROOM,
                    media_content_id=RADIO, media_content_type="music")
                app.fire_event.assert_not_called()
                timer = app.run_in.call_args
                self.assertEqual(timer.args[1], ar.RADIO_JOIN_S)
                fire(timer)
                app.fire_event.assert_called_once_with(
                    "request_sonos_group_join", master_entity_id=BATHROOM, slave_entity_id=BEDROOM)

    def test_grouped_leader_leaves_first_then_the_others_join(self):
        actions = ar.radio_actions([BATHROOM, BEDROOM], {BATHROOM: state(grouped=True)}, RADIO)
        self.assertEqual([a.get("delay", 0) for a in actions], [0, 1, 1 + ar.RADIO_JOIN_S])
        self.assertEqual(actions[0]["event"], "request_sonos_group_unjoin")
        self.assertEqual(actions[2]["data"], {"master_entity_id": BATHROOM, "slave_entity_id": BEDROOM})

    def test_one_room_in_a_list_is_the_single_room_path(self):
        app = make_app()
        app._on_intent("assist_radio", {"area_id": ["bedroom"]}, {})
        app.call_service.assert_called_once_with(
            "media_player/play_media", entity_id=BEDROOM,
            media_content_id=RADIO, media_content_type="music")
        app.run_in.assert_not_called()


class DuckGlue(AppTest):
    def test_wake_while_playing_ducks_once_and_never_touches_volume(self):
        app = make_app()
        satellite(app, "listening")
        self.duck.assert_called_once_with(HOST, True)
        app.call_service.assert_not_called()
        self.assertEqual(logged(app, "Ducked"), [f"Ducked {BATHROOM} for assist (bathroom)"])
        self.assertEqual(app._ducked, {"bathroom"})

    def test_replayed_conversation_ducks_each_busy_edge_and_lifts_once_after_the_last(self):
        app = make_app()
        steps = [(SATELLITE, "listening", 1), (SATELLITE, "processing", 2),
                 (SATELLITE, "responding", 2), (ANSWER, "on", 3), (ANSWER, "off", 3),
                 (SATELLITE, "idle", 3), (SATELLITE, "listening", 4),
                 (SATELLITE, "processing", 5), (SATELLITE, "idle", 5)]
        for entity, new, ducks in steps:
            feed(app, entity, new)
            self.assertEqual(self.duck.call_count, ducks, (entity, new))
        self.assertEqual(set(self.ducks()), {(HOST, True)})
        self.assertEqual(logged(app, "Ducked"), [f"Ducked {BATHROOM} for assist (bathroom)"])
        timers = app.run_in.call_args_list
        self.assertEqual([timer.args[1] for timer in timers], [ar.QUIET_S] * 4)

        for timer in timers[:3]:
            fire(timer)
        self.assertEqual(self.duck.call_count, 5)
        self.assertEqual(app._ducked, {"bathroom"})
        self.assertEqual(logged(app, "Unducked"), [])

        fire(timers[3])
        self.assertEqual(self.ducks()[5:], [(HOST, False)])
        self.assertFalse(app._ducked)
        for timer in timers:
            fire(timer)
        self.assertEqual(self.ducks()[5:], [(HOST, False)])
        self.assertEqual(logged(app, "Unducked"), [f"Unducked {BATHROOM} (bathroom)"])
        self.assertEqual(logged(app, "Ducked"), [f"Ducked {BATHROOM} for assist (bathroom)"])

    def test_processing_to_idle_ends_the_conversation(self):
        app = make_app()
        satellite(app, "listening", "processing", "idle")
        self.assertEqual(self.duck.call_count, 2)
        fire(app.run_in.call_args)
        self.assertEqual(self.ducks(), [(HOST, True), (HOST, True), (HOST, False)])
        self.assertFalse(app._ducked)

    def test_responding_without_an_answer_ends_the_conversation(self):
        app = make_app()
        satellite(app, "listening", "processing", "responding")
        self.assertEqual(self.duck.call_count, 2)
        fire(app.run_in.call_args)
        self.assertEqual(self.ducks(), [(HOST, True), (HOST, True), (HOST, False)])
        self.assertFalse(app._ducked)

    def test_satellite_going_away_ends_the_conversation(self):
        for new in ("unavailable", "unknown", None):
            with self.subTest(new=new):
                self.duck.reset_mock()
                app = make_app()
                satellite(app, "listening", new)
                fire(app.run_in.call_args)
                self.assertEqual(self.ducks(), [(HOST, True), (HOST, False)])
                self.assertFalse(app._ducked)

    def test_follow_up_inside_the_quiet_time_keeps_the_duck(self):
        app = make_app()
        satellite(app, "listening", "responding", "idle", "listening")
        for timer in app.run_in.call_args_list:
            fire(timer)
        self.assertEqual(self.ducks(), [(HOST, True), (HOST, True)])
        self.assertEqual(app._ducked, {"bathroom"})

    def test_room_that_is_not_playing_gets_no_duck_timer_or_unduck(self):
        for value in ("paused", "idle", "off", "unavailable", "unknown", None):
            with self.subTest(value=value):
                self.duck.reset_mock()
                app = make_app()
                app.states[BATHROOM] = state(value)
                satellite(app, "listening", "processing", "responding", "idle")
                answer(app, "on")
                answer(app, "off")
                self.duck.assert_not_called()
                app.run_in.assert_not_called()
                app.call_service.assert_not_called()
                app.log.assert_not_called()
                self.assertFalse(app._ducked)

    def test_music_started_mid_conversation_is_ducked_at_the_next_busy_edge(self):
        app = make_app()
        app.states[BATHROOM] = state("idle")
        satellite(app, "listening")
        self.duck.assert_not_called()
        app.states[BATHROOM] = state("playing")
        satellite(app, "processing")
        self.assertEqual(self.ducks(), [(HOST, True)])
        self.assertEqual(logged(app, "Ducked"), [f"Ducked {BATHROOM} for assist (bathroom)"])
        satellite(app, "idle")
        fire(app.run_in.call_args)
        self.assertEqual(self.ducks(), [(HOST, True), (HOST, False)])

    def test_failed_duck_warns_once_and_owns_nothing(self):
        app = make_app()
        self.duck.side_effect = OSError("unreachable")
        satellite(app, "listening")
        self.assertEqual(app.log.call_count, 1)
        warning = logged(app, "Sonos duck", level="WARNING")
        self.assertEqual(len(warning), 1)
        self.assertIn("OSError: unreachable", warning[0])
        self.assertFalse(app._ducked)
        satellite(app, "idle")
        app.run_in.assert_not_called()
        self.duck.assert_called_once_with(HOST, True)

    def test_failed_duck_is_tried_again_at_the_next_busy_edge(self):
        app = make_app()
        self.duck.side_effect = [OSError("unreachable"), None]
        satellite(app, "listening", "processing")
        self.assertEqual(self.ducks(), [(HOST, True), (HOST, True)])
        self.assertEqual(app._ducked, {"bathroom"})
        self.assertEqual(logged(app, "Ducked"), [f"Ducked {BATHROOM} for assist (bathroom)"])

    def test_failed_unduck_warns_and_forgets_the_duck(self):
        app = make_app()
        satellite(app, "listening", "idle")
        timer = app.run_in.call_args
        self.duck.side_effect = OSError("unreachable")
        fire(timer)
        self.assertEqual(len(logged(app, "Sonos unduck", level="WARNING")), 1)
        self.assertEqual(logged(app, "Unducked"), [])
        self.assertFalse(app._ducked)
        fire(timer)
        self.assertEqual(self.ducks(), [(HOST, True), (HOST, False)])

    def test_room_without_a_host_does_nothing(self):
        app = make_app(duck_hosts={})
        satellite(app, "listening", "processing", "idle")
        answer(app, "on")
        answer(app, "off")
        self.duck.assert_not_called()
        app.run_in.assert_not_called()
        app.call_service.assert_not_called()
        app.log.assert_not_called()
        self.assertFalse(app._ducked)

    def test_satellite_without_a_speaker_does_nothing(self):
        app = make_app()
        feed(app, HALLWAY, "listening")
        feed(app, HALLWAY, "idle")
        self.duck.assert_not_called()
        app.run_in.assert_not_called()
        app.call_service.assert_not_called()

    def test_answer_sensor_alone_renews_the_duck(self):
        app = make_app()
        satellite(app, "listening", "responding")
        stale = app.run_in.call_args
        answer(app, "on")
        self.assertEqual(self.ducks(), [(HOST, True), (HOST, True)])
        self.assertEqual(logged(app, "Ducked"), [f"Ducked {BATHROOM} for assist (bathroom)"])
        answer(app, "off")
        fire(stale)
        self.assertEqual(self.duck.call_count, 2)
        fire(app.run_in.call_args)
        self.assertEqual(self.ducks()[-1], (HOST, False))
        self.assertFalse(app._ducked)

    def test_answer_sensor_alone_ducks_a_playing_room(self):
        app = make_app()
        answer(app, "on")
        self.duck.assert_called_once_with(HOST, True)
        answer(app, "off")
        fire(app.run_in.call_args)
        self.assertEqual(self.ducks(), [(HOST, True), (HOST, False)])

    def test_satellite_ducks_only_its_own_room(self):
        app = make_app(duck_hosts={"bathroom": HOST, "bedroom": BEDROOM_HOST})
        satellite(app, "listening")
        self.duck.assert_called_once_with(HOST, True)
        self.assertEqual(app._ducked, {"bathroom"})
        answer(app, "on")
        self.assertEqual(self.ducks()[1:], [(HOST, True), (BEDROOM_HOST, True)])
        satellite(app, "processing")
        self.assertEqual(self.ducks()[3:], [(HOST, True)])

    def test_answer_sensor_names_no_room_so_it_holds_every_duck_room(self):
        app = make_app(duck_hosts={"bathroom": HOST, "bedroom": BEDROOM_HOST})
        answer(app, "on")
        self.assertEqual(self.ducks(), [(HOST, True), (BEDROOM_HOST, True)])
        self.assertEqual(app._ducked, {"bathroom", "bedroom"})
        answer(app, "off")
        for timer in app.run_in.call_args_list:
            fire(timer)
        self.assertEqual(self.ducks()[2:], [(HOST, False), (BEDROOM_HOST, False)])
        self.assertFalse(app._ducked)

    def test_unchanged_state_is_ignored(self):
        app = make_app()
        app._on_state(SATELLITE, "state", "listening", "listening", {})
        self.duck.assert_not_called()
        self.assertEqual(app._seen[SATELLITE], "idle")

    def test_callback_values_win_over_a_stale_state_store(self):
        app = make_app()
        app._on_state(ANSWER, "state", "off", "on", {})
        self.duck.assert_called_once_with(HOST, True)
        app.states[ANSWER] = {"state": "on"}
        app._on_state(ANSWER, "state", "on", "off", {})
        self.assertEqual(len(app.run_in.call_args_list), 1)

    def test_answer_playing_at_startup_counts(self):
        app = make_app(states={ANSWER: {"state": "on"}})
        satellite(app, "responding")
        self.duck.assert_called_once_with(HOST, True)


class SonosDuckRequest(unittest.TestCase):
    def send(self, duck):
        with patch.object(ar.urllib.request, "urlopen") as urlopen:
            ar.sonos_duck(HOST, duck)
        urlopen.assert_called_once()
        return urlopen

    def check(self, urlopen, action, body):
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url,
                         f"https://{HOST}:1443/api/v1/players/local/playerVolume/{action}")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), body)
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers["x-sonos-api-key"], "123e4567-e89b-12d3-a456-426655440000")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 2)
        context = urlopen.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)
        urlopen.return_value.__exit__.assert_called_once()

    def test_duck_asks_for_a_timed_duck(self):
        self.check(self.send(True), "duck", {"durationMillis": 60000})

    def test_unduck_sends_an_empty_body(self):
        self.check(self.send(False), "unduck", {})


if __name__ == "__main__":
    unittest.main()
