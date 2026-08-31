"""The ESPHome pressure strip (binary_sensor.bed_presence_6b9c94_bed_occupied_left,
added 2026-08-12) as a third entry in withings_in_bed_entities: it may START a
bed-light session exactly like a mat (ON edge, room-active-gated) and must degrade
safe - only an exact "on" counts anywhere the list is read, so an unavailable/unknown
strip is inert. Same __new__ + monkeypatched harness as test_bedroom_lights_session."""

from __future__ import annotations

import datetime
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_LIGHTS_DIR = Path(__file__).resolve().parents[1]
if str(_LIGHTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTS_DIR))

# bedroom_lights imports cover_util, which lives in apps/blinds
_BLINDS_DIR = Path(__file__).resolve().parents[2] / "blinds"
if str(_BLINDS_DIR) not in sys.path:
    sys.path.insert(0, str(_BLINDS_DIR))

# bedroom_lights imports room_active_read, which lives in apps/presence
_PRESENCE_DIR = Path(__file__).resolve().parents[2] / "presence"
if str(_PRESENCE_DIR) not in sys.path:
    sys.path.insert(0, str(_PRESENCE_DIR))

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

import bedroom_lights  # noqa: E402

LEFT = "binary_sensor.left_bedside"
RIGHT = "binary_sensor.right_bedside"
STRIP = "binary_sensor.bed_presence_6b9c94_bed_occupied_left"
ROOM_ACTIVE = "binary_sensor.bedroom_active"
SLEEP = "input_boolean.mikkel_sleep_mode"
SESSION = "input_boolean.bedroom_bed_session"


def make_app(states=None):
    """BedroomLights with just what the in-bed witness list machinery touches."""
    app = bedroom_lights.BedroomLights.__new__(bedroom_lights.BedroomLights)
    app.withings_in_bed_entities = [LEFT, RIGHT, STRIP]
    app.bedroom_active_entity = ROOM_ACTIVE
    app.bed_session_entity = SESSION
    app.mikkel_sleep_entity = SLEEP
    app.session_exit_debounce_sec = 90
    app._session = False
    app._session_exit_timer = None

    app.states = {
        (ROOM_ACTIVE, None): "off",
        (ROOM_ACTIVE, "computed_at"): datetime.datetime.now(datetime.timezone.utc).isoformat(),
        (LEFT, None): "off",
        (RIGHT, None): "off",
        (STRIP, None): "off",
        (SLEEP, None): "off",
        (SESSION, None): "off",
    }
    app.states.update(states or {})

    app.get_state = lambda entity, **kw: app.states.get((entity, kw.get("attribute")))
    app.log = lambda *a, **kw: None
    app.call_service = MagicMock()
    app.cancel_timer = MagicMock()
    app._evaluate_lights = MagicMock()
    return app


class WithingsInBedListWithTheStrip(unittest.TestCase):
    """_withings_in_bed is the restart-reconcile read of the witness list."""

    def test_strip_on_counts_as_in_bed(self):
        app = make_app({(STRIP, None): "on"})
        self.assertTrue(app._withings_in_bed())

    def test_strip_unavailable_is_inert(self):
        app = make_app({(STRIP, None): "unavailable"})
        self.assertFalse(app._withings_in_bed())

    def test_strip_unknown_is_inert(self):
        app = make_app({(STRIP, None): "unknown"})
        self.assertFalse(app._withings_in_bed())

    def test_mat_still_counts_when_strip_is_unavailable(self):
        app = make_app({(LEFT, None): "on", (STRIP, None): "unavailable"})
        self.assertTrue(app._withings_in_bed())


class StripOnEdgeStartsASession(unittest.TestCase):
    """The strip's ON edge goes through the same room-active-gated session start as the
    mats - a stale/ghost 'on' with nobody in the room must not relight the bed."""

    def test_strip_on_edge_with_room_presence_starts_session(self):
        app = make_app({(ROOM_ACTIVE, None): "on", (STRIP, None): "on"})
        app._on_withings_in_bed_on(STRIP, "state", "unknown", "on", {})
        self.assertTrue(app._session)

    def test_strip_on_edge_without_room_presence_does_not_start_session(self):
        app = make_app({(ROOM_ACTIVE, None): "off", (STRIP, None): "on"})
        app._on_withings_in_bed_on(STRIP, "state", "off", "on", {})
        self.assertFalse(app._session)


if __name__ == "__main__":
    unittest.main()
