import unittest

from test_living_room_apple_tv_disconnect import make_app, states


SPEAKER = "media_player.living_room"
RAW = "media_player.living_room_2"
STOP = ("media_player/media_stop", {"entity_id": SPEAKER})


def app_with(raw_attrs, speaker_state="playing"):
    app = make_app(states(**{SPEAKER: speaker_state}))
    plain = app.get_state
    app.get_state = lambda entity, attribute=None, **kw: (
        raw_attrs.get(attribute) if entity == RAW and attribute else plain(entity)
    )
    app.events = []
    app.fire_event = lambda name, **kw: app.events.append((name, kw))
    return app


class TvOffMusicGroupTest(unittest.TestCase):
    def test_group_playing_spotify_is_left_alone(self):
        app = app_with({"group_members": [
            "media_player.kitchen_2", RAW, "media_player.dining_room_2",
        ], "source": "Spotify Connect"})
        app._stop_living_room_media_and_reset(reason="tv_off_confirmed")
        self.assertEqual(app.service_calls, [])
        self.assertEqual(app.events, [])

    def test_group_on_tv_audio_is_stopped_and_reset(self):
        app = app_with({"group_members": ["media_player.kitchen_2", RAW], "source": "TV"})
        app._stop_living_room_media_and_reset(reason="tv_off_confirmed")
        self.assertIn(STOP, app.service_calls)
        self.assertEqual([e[0] for e in app.events], ["sonos_reset_speakers"])

    def test_solo_speaker_is_stopped_and_reset(self):
        app = app_with({"group_members": [RAW], "source": "Spotify Connect"})
        app._stop_living_room_media_and_reset(reason="tv_off_confirmed")
        self.assertIn(STOP, app.service_calls)
        self.assertEqual([e[0] for e in app.events], ["sonos_reset_speakers"])


if __name__ == "__main__":
    unittest.main()
