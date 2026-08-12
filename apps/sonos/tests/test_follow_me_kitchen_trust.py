"""SonosFollowMe kitchen presence + presence_trust (asymmetric rule).

Kitchen mmWave-only presence while the kitchen speaker plays is SUSPECT
(cone micro-motion ghost - measured 2026-08-05..08-11: 575 min of it, PIR
never firing). Where kitchen presence would trigger something ON (unmute),
_is_present must return None (indeterminate): every caller skips the mute
change, so a ghost can never unmute a muted speaker AND a possibly-real
still person is never muted. Hallway presence (a separate composite) stays
a real trigger, and true absence (composite off) still mutes.
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
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

KPIR = "binary_sensor.kitchen_pir_presence"
HPIR = "binary_sensor.hallway_pir_presence"
MMWAVE = "binary_sensor.kitchen_presence_presence"
PIR = "binary_sensor.kitchen_presence_pir_detection"
SPEAKER_MEDIA = "media_player.kitchen_2"
KITCHEN_SPEAKER = "media_player.kitchen"


def utc(hour, minute=0, second=0):
    return datetime(2026, 8, 9, hour, minute, second, tzinfo=timezone.utc).isoformat()


def ghost_states(
    pir_last_changed=utc(15, 35),
    hallway="off",
    kitchen_composite="on",
    interferer="playing",
):
    return {
        KPIR: {
            "state": kitchen_composite,
            "attributes": {"entity_id": [MMWAVE, PIR]},
            "last_changed": utc(16, 1),
        },
        MMWAVE: {"state": "on", "last_changed": utc(16, 1)},
        PIR: {"state": "off", "last_changed": pir_last_changed},
        SPEAKER_MEDIA: {"state": interferer},
        HPIR: {"state": hallway},
        KITCHEN_SPEAKER: {
            "state": "playing",
            "attributes": {
                "group_members": [KITCHEN_SPEAKER, "media_player.living_room"],
                "is_volume_muted": False,
            },
        },
    }


def make_app(states):
    app = fm.SonosFollowMe.__new__(fm.SonosFollowMe)
    app.args = {"all_speakers": []}
    app.room_map = {"kitchen": KPIR, "hallway": HPIR}
    app.speaker_map = {KITCHEN_SPEAKER: "kitchen"}
    app.special = {"kitchen_or_hallway": True}
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
        if attribute == "last_changed":
            return ent.get("last_changed")
        if attribute == "all":
            return {
                "attributes": ent.get("attributes", {}),
                "last_changed": ent.get("last_changed"),
            }
        return ent.get("attributes", {}).get(attribute)

    app.get_state = get_state
    app.call_service = MagicMock()
    return app


class KitchenPresenceTrust(unittest.TestCase):
    def test_ghost_kitchen_is_indeterminate(self):
        """Suspect-only kitchen -> None: every caller skips the mute change."""
        app = make_app(ghost_states())
        self.assertIsNone(app._is_present("kitchen", KITCHEN_SPEAKER))
        self.assertTrue(any("SUSPECT" in msg for _lvl, msg in app.log_calls))

    def test_hallway_presence_still_wins_as_real(self):
        app = make_app(ghost_states(hallway="on"))
        self.assertIs(app._is_present("kitchen", KITCHEN_SPEAKER), True)

    def test_real_kitchen_presence_pir_fired_in_span(self):
        app = make_app(ghost_states(pir_last_changed=utc(16, 30)))
        self.assertIs(app._is_present("kitchen", KITCHEN_SPEAKER), True)

    def test_interferer_not_playing_presence_trusted(self):
        app = make_app(ghost_states(interferer="idle"))
        self.assertIs(app._is_present("kitchen", KITCHEN_SPEAKER), True)

    def test_true_absence_is_still_false(self):
        app = make_app(ghost_states(kitchen_composite="off"))
        self.assertIs(app._is_present("kitchen", KITCHEN_SPEAKER), False)


class MuteActionsUnderSuspicion(unittest.TestCase):
    def _wire(self, app):
        app._should_follow_me_be_active = lambda: True
        app._is_room_excluded_from_follow_me = lambda room: False
        return app

    def test_ghost_presence_does_not_remute_or_unmute(self):
        """_preserve_mute_state_if_needed sees indeterminate presence and must
        leave the mute state alone (no mute on a possibly-real still person)."""
        app = self._wire(make_app(ghost_states()))
        app._preserve_mute_state_if_needed(KITCHEN_SPEAKER)
        app.call_service.assert_not_called()

    def test_true_absence_still_mutes_playing_grouped_speaker(self):
        """When the composite really drops, the normal follow-me mute applies -
        the ghost fix must not weaken real vacancy muting."""
        app = self._wire(make_app(ghost_states(kitchen_composite="off")))
        app._preserve_mute_state_if_needed(KITCHEN_SPEAKER)
        app.call_service.assert_called_once_with(
            "media_player/volume_mute",
            entity_id=KITCHEN_SPEAKER,
            is_volume_muted=True,
        )


if __name__ == "__main__":
    unittest.main()
