# tests/test_bed_occupied_witness.py - smart_cooling's quiet gate (_bed_occupied:
# skip stall-burps, use the quiet fan) with the ESPHome strip's either-side sensor
# OR-ed in alongside the Withings mats (2026-08-12, bed_occupancy_sensors in
# smart_cooling.yaml). For a SUPPRESSION gate the unproven witness may only ever
# err quieter: any exact "on" suppresses, unavailable/unknown is inert.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import asyncio
import sys
import types
import unittest
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

import smart_cooling as sc  # noqa: E402

LEFT_MAT = "binary_sensor.left_bedside"
RIGHT_MAT = "binary_sensor.right_bedside"
STRIP_EITHER = "binary_sensor.bed_presence_6b9c94_bed_occupied_either"


def make_app(states):
    app = sc.SmartCooling.__new__(sc.SmartCooling)
    app.bed_sensors = [LEFT_MAT, RIGHT_MAT, STRIP_EITHER]

    async def _state(entity, *a, **kw):
        return states.get(entity)

    app._state = _state
    return app


def occupied(app):
    return asyncio.run(app._bed_occupied())


class BedOccupiedWithTheStrip(unittest.TestCase):
    def test_strip_on_alone_suppresses(self):
        # The safe failure direction: a stuck-on strip only ever means quieter.
        app = make_app({LEFT_MAT: "off", RIGHT_MAT: "off", STRIP_EITHER: "on"})
        self.assertTrue(occupied(app))

    def test_mat_on_still_suppresses_with_strip_unavailable(self):
        app = make_app({LEFT_MAT: "on", RIGHT_MAT: "off",
                        STRIP_EITHER: "unavailable"})
        self.assertTrue(occupied(app))

    def test_all_off_or_unreadable_is_unoccupied(self):
        app = make_app({LEFT_MAT: "off", RIGHT_MAT: "unknown",
                        STRIP_EITHER: "unavailable"})
        self.assertFalse(occupied(app))

    def test_quiet_fan_follows_the_same_signal(self):
        self.assertEqual(sc.SmartCooling._cooling_fan("medium", "silent", True),
                         "silent")
        self.assertEqual(sc.SmartCooling._cooling_fan("medium", "silent", False),
                         "medium")


if __name__ == "__main__":
    unittest.main()
