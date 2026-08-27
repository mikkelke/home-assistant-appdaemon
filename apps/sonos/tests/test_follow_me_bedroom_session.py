"""SonosFollowMe bedroom presence + bed-session latch (additive OR).

The raw bedroom group (binary_sensor.bedroom_pir_presence) is a zero-debounce
OR that flickers off for 30-90s stretches while someone lies still (measured
2026-08-27 morning: 07:21-07:36 gaps while input_boolean.bedroom_bed_session
held ON rock-solid), muting the bedroom speaker mid-use. With
special_conditions.bedroom_with_bed_session, _is_present ORs in the session
latch (bedroom_lights' 90s-debounced multi-witness signal). Additive only:
the session can make the bedroom read MORE present, never less - group ON
still wins on its own, and both-off still mutes.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# follow_me imports presence_trust from apps/lights (AppDaemon puts every app dir on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lights"))

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

import follow_me as fm  # noqa: E402

BPIR = "binary_sensor.bedroom_pir_presence"
SESSION = "input_boolean.bedroom_bed_session"
BEDROOM_SPEAKER = "media_player.bedroom"


def bedroom_states(group="off", session="on"):
    return {
        BPIR: {"state": group},
        SESSION: {"state": session},
        BEDROOM_SPEAKER: {
            "state": "playing",
            "attributes": {
                "group_members": [BEDROOM_SPEAKER, "media_player.living_room"],
                "is_volume_muted": False,
            },
        },
    }


def make_app(states, special=None):
    app = fm.SonosFollowMe.__new__(fm.SonosFollowMe)
    app.args = {"all_speakers": []}
    app.room_map = {"bedroom": BPIR}
    app.speaker_map = {BEDROOM_SPEAKER: "bedroom"}
    app.special = {"bedroom_with_bed_session": True} if special is None else special
    app.bed_session_entity = SESSION
    app._reset_in_progress = False

    app.log_calls = []
    app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))

    app.states = dict(states)

    def get_state(entity, attribute=None, **kw):
        ent = app.states.get(entity)
        if ent is None:
            return None
        if attribute is None:
            return ent.get("state")
        return ent.get("attributes", {}).get(attribute)

    app.get_state = get_state
    app.call_service = MagicMock()
    return app


class BedroomSessionPresence(unittest.TestCase):
    def test_session_holds_presence_through_group_flicker(self):
        """The bug: group flickers off while lying still - session keeps it present."""
        app = make_app(bedroom_states(group="off", session="on"))
        self.assertIs(app._is_present("bedroom", BEDROOM_SPEAKER), True)

    def test_both_on_is_present(self):
        app = make_app(bedroom_states(group="on", session="on"))
        self.assertIs(app._is_present("bedroom", BEDROOM_SPEAKER), True)

    def test_group_alone_still_wins(self):
        """Additive only: session off must not weaken a real group ON."""
        app = make_app(bedroom_states(group="on", session="off"))
        self.assertIs(app._is_present("bedroom", BEDROOM_SPEAKER), True)

    def test_both_off_is_absent(self):
        app = make_app(bedroom_states(group="off", session="off"))
        self.assertIs(app._is_present("bedroom", BEDROOM_SPEAKER), False)

    def test_both_unknown_is_indeterminate(self):
        """Both unknown -> None: every caller skips the mute change (kitchen convention)."""
        app = make_app(bedroom_states(group="unknown", session="unavailable"))
        self.assertIsNone(app._is_present("bedroom", BEDROOM_SPEAKER))
        self.assertTrue(any("both_unknown" in msg for _lvl, msg in app.log_calls))

    def test_session_rescues_unavailable_group(self):
        app = make_app(bedroom_states(group="unavailable", session="on"))
        self.assertIs(app._is_present("bedroom", BEDROOM_SPEAKER), True)

    def test_flag_disabled_falls_back_to_group_only(self):
        """Without the special condition the session is ignored entirely."""
        app = make_app(bedroom_states(group="off", session="on"), special={})
        self.assertIs(app._is_present("bedroom", BEDROOM_SPEAKER), False)


class MuteActionsWithSession(unittest.TestCase):
    def _wire(self, app):
        app._should_follow_me_be_active = lambda: True
        app._is_room_excluded_from_follow_me = lambda room: False
        return app

    def test_session_on_blocks_mute_during_group_flicker(self):
        """_preserve_mute_state_if_needed sees presence (via the session) and must
        not mute the playing grouped bedroom speaker - the exact reported bug."""
        app = self._wire(make_app(bedroom_states(group="off", session="on")))
        app._preserve_mute_state_if_needed(BEDROOM_SPEAKER)
        app.call_service.assert_not_called()

    def test_true_absence_still_mutes_playing_grouped_speaker(self):
        """When group AND session are both off, real vacancy muting still applies."""
        app = self._wire(make_app(bedroom_states(group="off", session="off")))
        app._preserve_mute_state_if_needed(BEDROOM_SPEAKER)
        app.call_service.assert_called_once_with(
            "media_player/volume_mute",
            entity_id=BEDROOM_SPEAKER,
            is_volume_muted=True,
        )


if __name__ == "__main__":
    unittest.main()
