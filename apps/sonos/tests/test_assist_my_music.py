"""AssistMyMusic: who is speaking, "play my music" plans and "what is playing" answers."""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import assist_my_music as mm  # noqa: E402

NOW = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)
BATH = "media_player.bathroom"
KITCHEN = "media_player.kitchen"
ACCOUNTS = {"Mikkel": "media_player.spotify_mikkel", "Kristine": "media_player.spotify_kristine",
            "Claudia": "media_player.spotify_claudia"}
SONOS_KEYS = {"bathroom": BATH, "kitchen": KITCHEN}
NAMES = {BATH: "Bathroom", KITCHEN: "Kitchen"}


def account(state="playing", source="OPPO Find X9 Pro", title="Op med ho'det", artist="Natasja",
            uri="spotify:track:2hj4INbCeEhiYsuBgAoNc8"):
    if state == "idle":
        return {"state": "idle", "attributes": {}}
    return {"state": state, "attributes": {"media_content_id": uri, "media_title": title,
                                           "media_artist": artist, "source": source}}


def speaker(state="idle", members=None, title=None, artist=None):
    attrs = {"group_members": members or []}
    if title:
        attrs.update(media_title=title, media_artist=artist)
    return {"state": state, "attributes": attrs}


class ResolveSpeaker(unittest.TestCase):

    def test_identity_wins(self):
        self.assertEqual(mm.resolve_speaker("bathroom", "Mikkel", {"Kristine": "Bathroom"}, None), "Mikkel")

    def test_only_resident_bluetooth_places_there(self):
        ble = {"Mikkel": "Kitchen", "Kristine": "Guest bathroom", "Claudia": "unknown"}
        self.assertEqual(mm.resolve_speaker("guest_bathroom", None, ble, None), "Kristine")

    def test_two_there_falls_back_to_owner(self):
        ble = {"Mikkel": "Bathroom", "Kristine": "Bathroom"}
        self.assertEqual(mm.resolve_speaker("bathroom", None, ble, "Mikkel"), "Mikkel")

    def test_nobody_known(self):
        self.assertIsNone(mm.resolve_speaker("kitchen", None, {"Mikkel": "Bedroom"}, None))


class MyMusicPlan(unittest.TestCase):

    def plan(self, acc, last=None, spk=None):
        return mm.my_music_plan("Kristine", BATH, spk or speaker(), ACCOUNTS["Kristine"], acc, last,
                                SONOS_KEYS, NOW, 7)

    def test_phone_playing_moves_here_and_pauses_phone(self):
        speech, actions = self.plan(account())
        self.assertEqual(speech, "Kristine, your Spotify moves here: Op med ho'det by Natasja.")
        self.assertEqual(actions, [
            {"service": "music_assistant/play_media",
             "data": {"entity_id": BATH, "media_id": "spotify:track:2hj4INbCeEhiYsuBgAoNc8", "radio_mode": True}},
            {"service": "media_player/media_pause", "data": {"entity_id": ACCOUNTS["Kristine"]}}])

    def test_paused_phone_plays_without_pause_call(self):
        speech, actions = self.plan(account(state="paused"))
        self.assertEqual(speech, "Kristine, your last song, Op med ho'det by Natasja, then more like it.")
        self.assertEqual([a["service"] for a in actions], ["music_assistant/play_media"])

    def test_spotify_on_another_sonos_joins_it(self):
        speech, actions = self.plan(account(source="Kitchen + 2"))
        self.assertEqual(speech, "Kristine, your Spotify joins from the kitchen.")
        self.assertEqual(actions, [{"event": "request_sonos_group_join",
                                    "data": {"master_entity_id": KITCHEN, "slave_entity_id": BATH}}])

    def test_already_here(self):
        speech, actions = self.plan(account(source="Bathroom"))
        self.assertEqual((speech, actions), ("Kristine, your Spotify is already playing here.", []))

    def test_idle_uses_remembered_song(self):
        last = {"uri": "spotify:track:x", "title": "Fugle", "artist": "Djämes Braun",
                "at": (NOW - timedelta(days=2)).isoformat()}
        speech, actions = self.plan(account(state="idle"), last)
        self.assertEqual(speech, "Kristine, your last song, Fugle by Djämes Braun, then more like it.")
        self.assertEqual(actions[0]["data"]["media_id"], "spotify:track:x")

    def test_old_song_is_not_lately(self):
        last = {"uri": "spotify:track:x", "title": "Fugle", "artist": "", "at": (NOW - timedelta(days=9)).isoformat()}
        speech, actions = self.plan(account(state="idle"), last)
        self.assertEqual((speech, actions), ("Kristine, you haven't played anything on Spotify lately.", []))

    def test_grouped_speaker_leaves_first(self):
        _, actions = self.plan(account(state="paused"), spk=speaker("playing", [KITCHEN, BATH]))
        self.assertEqual(actions[0], {"event": "request_sonos_group_unjoin", "data": {"entity_id": BATH}})
        self.assertEqual(actions[1]["delay"], 1)

    def test_music_assistant_stream_is_not_own_listening(self):
        last = {"uri": "spotify:track:x", "title": "Fugle", "artist": "Djämes Braun", "at": NOW.isoformat()}
        speech, actions = self.plan(account(source="Music Assistant Playback"), last)
        self.assertEqual(speech, "Kristine, your last song, Fugle by Djämes Braun, then more like it.")
        self.assertEqual([a["service"] for a in actions], ["music_assistant/play_media"])

    def test_unknown_person(self):
        self.assertEqual(mm.my_music_plan(None, BATH, {}, None, {}, None, SONOS_KEYS, NOW, 7),
                         ("I can't tell whose music to play.", []))


class WhatIsPlaying(unittest.TestCase):

    def answer(self, states, accounts=None, sessions=None, here=True, spk=BATH):
        acc = {ACCOUNTS[p]: a for p, a in (accounts or {}).items()}
        return mm.what_playing_text(here, spk, states, NAMES, ACCOUNTS, acc, sessions or {})

    def test_jam_names_host_first(self):
        states = {BATH: speaker("playing", title="Sim Sima", artist="Troo.L.S")}
        accounts = {"Mikkel": account(source="Bathroom"), "Kristine": account(source="Bathroom")}
        sessions = {BATH: {"started_at": "2026-11-21T15:00:00+01:00", "started_by": "Kristine and Mikkel"}}
        self.assertEqual(self.answer(states, accounts, sessions), "Kristine's Jam with Mikkel: Sim Sima by Troo.L.S.")

    def test_one_account(self):
        states = {BATH: speaker("playing", members=[BATH, KITCHEN], title="Fugle", artist="Djämes Braun")}
        self.assertEqual(self.answer(states, {"Claudia": account(source="Kitchen")}),
                         "Claudia's Spotify: Fugle by Djämes Braun.")

    def test_started_by_person(self):
        states = {BATH: speaker("playing", title="DR P3")}
        sessions = {BATH: {"started_at": "2026-11-21T15:00:00+01:00", "started_by": "Mikkel",
                           "stopped_at": "2026-11-21T09:00:00+01:00", "stopped_by": "Mikkel"}}
        self.assertEqual(self.answer(states, sessions=sessions), "DR P3, put on by Mikkel.")

    def test_ended_session_and_someone_name_nobody(self):
        states = {BATH: speaker("playing", title="DR P3")}
        ended = {BATH: {"started_at": "2026-11-21T09:00:00+01:00", "started_by": "Mikkel",
                        "stopped_at": "2026-11-21T10:00:00+01:00"}}
        self.assertEqual(self.answer(states, sessions=ended), "DR P3.")
        someone = {BATH: {"started_at": "2026-11-21T09:00:00+01:00", "started_by": "Someone"}}
        self.assertEqual(self.answer(states, sessions=someone), "DR P3.")

    def test_nothing_here_names_the_playing_leader(self):
        states = {BATH: speaker(), KITCHEN: speaker("playing", title="Whine Dat", artist="Kongsted")}
        self.assertEqual(self.answer(states), "Nothing here. The kitchen plays Whine Dat by Kongsted.")

    def test_named_room(self):
        states = {BATH: speaker(), KITCHEN: speaker("playing", title="Whine Dat", artist="Kongsted")}
        self.assertEqual(self.answer(states, here=False, spk=KITCHEN), "In the kitchen: Whine Dat by Kongsted.")

    def test_music_this_app_started(self):
        states = {BATH: speaker("playing", title="Sim Sima", artist="Troo.L.S")}
        mine = {BATH: {"person": "Kristine", "at": "2026-11-21T15:00:00+01:00"}}
        before = {BATH: {"started_at": "2026-11-21T09:00:00+01:00", "started_by": "Mikkel",
                         "stopped_at": "2026-11-21T10:00:00+01:00"}}
        answer = mm.what_playing_text(True, BATH, states, NAMES, ACCOUNTS, {}, before, mine=mine)
        self.assertEqual(answer, "Kristine's music: Sim Sima by Troo.L.S.")
        stopped = {BATH: {"stopped_at": "2026-11-21T15:10:00+01:00"}}
        self.assertEqual(mm.what_playing_text(True, BATH, states, NAMES, ACCOUNTS, {}, stopped, mine=mine),
                         "Sim Sima by Troo.L.S.")
        newer = {BATH: {"started_at": "2026-11-21T15:20:00+01:00", "started_by": "Mikkel"}}
        self.assertEqual(mm.what_playing_text(True, BATH, states, NAMES, ACCOUNTS, {}, newer, mine=mine),
                         "Sim Sima by Troo.L.S, put on by Mikkel.")

    def test_room_name_given(self):
        states = {BATH: speaker(), KITCHEN: speaker()}
        self.assertEqual(mm.what_playing_text(False, KITCHEN, states, NAMES, ACCOUNTS, {}, {}, "family room"),
                         "Nothing is playing in the family room.")

    def test_title_from_account_when_speaker_has_none(self):
        states = {BATH: speaker("playing")}
        self.assertEqual(self.answer(states, {"Mikkel": account(source="Bathroom")}),
                         "Mikkel's Spotify: Op med ho'det by Natasja.")

    def test_silent_house(self):
        self.assertEqual(self.answer({BATH: speaker(), KITCHEN: speaker()}), "Nothing is playing here.")


if __name__ == "__main__":
    unittest.main()
