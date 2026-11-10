from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_group_manager_wedge import gm, make_app  # noqa: E402

LIVING, KITCHEN, DINING = "media_player.living_room", "media_player.kitchen", "media_player.dining_room"


def make_zone_app(living_attrs):
    app = make_app()
    app.family_zone_speakers = [LIVING, KITCHEN, DINING]
    app.rooftop_entity = None
    app.family_zone_auto_group_entity = "input_boolean.auto"
    app._family_zone_sync_timeout_s = 15
    states = {
        LIVING: {"state": "playing", "attributes": {"group_members": [LIVING], **living_attrs}},
        KITCHEN: {"state": "idle", "attributes": {"group_members": []}},
        DINING: {"state": "idle", "attributes": {"group_members": []}},
        "input_boolean.auto": {"state": "on", "attributes": {}},
        "media_player.living_room_tv": {"state": "unavailable", "attributes": {}},
    }

    def get_state(entity, attribute=None):
        s = states.get(entity)
        if s is None:
            return None
        if attribute == "all":
            return s
        if attribute:
            return s["attributes"].get(attribute)
        return s["state"]

    app.get_state = get_state
    app.queued = []
    app._queue_group_operation = lambda op, master, members: app.queued.append((op, master, members))
    return app


class FamilyZoneTvAudioTest(unittest.TestCase):
    def test_tv_audio_on_living_room_does_not_pull_the_zone_in(self):
        app = make_zone_app({"app_id": "tv", "source": "TV", "media_content_id": "homeTheater-input"})
        app._check_family_zone_synchronization()
        self.assertEqual(app.queued, [])

    def test_native_sonos_tv_stream_is_tv_audio(self):
        app = make_zone_app({"media_content_id": "x-sonos-htastream:RINCON_X:spdif"})
        self.assertTrue(app._plays_tv_audio(LIVING))

    def test_solo_radio_still_pulls_the_zone_in(self):
        app = make_zone_app({"app_id": "music_assistant", "media_content_id": "library://radio/13"})
        app._check_family_zone_synchronization()
        self.assertEqual(app.queued, [("join", LIVING, [KITCHEN, DINING])])


if __name__ == "__main__":
    unittest.main()
