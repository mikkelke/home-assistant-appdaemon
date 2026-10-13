"""FamilyRoomLights counts every household member's sleep-mode boolean as a sleeper."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

_LIGHTS_DIR = Path(__file__).resolve().parents[1]
if str(_LIGHTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTS_DIR))

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

import family_room_lights as fr  # noqa: E402


def make_app(persons, sleeping):
    states = {f"input_boolean.{p.split('.')[1]}_sleep_mode": ("on" if p in sleeping else "off") for p in persons}
    states["zone.home"] = str(len(persons))
    app = object.__new__(fr.FamilyRoomLights)
    app._safe_get_state = lambda entity, default=None, **_: states.get(entity, default)
    app.get_state = lambda entity, attribute=None, **_: list(persons) if attribute == "persons" else states.get(entity)
    app.log = lambda *a, **k: None
    return app


class SleepStatusTest(unittest.TestCase):
    def test_claudia_asleep_alone_counts_as_someone_sleeping(self):
        status = make_app(["person.mikkel", "person.claudia"], {"person.claudia"})._get_sleep_mode_status()
        self.assertTrue(status["anyone_sleeping"])
        self.assertEqual(status["people_sleeping"], ["person.claudia"])
        self.assertFalse(status["everyone_sleeping"])

    def test_all_three_asleep_is_everyone_sleeping(self):
        persons = ["person.mikkel", "person.kristine", "person.claudia"]
        status = make_app(persons, set(persons))._get_sleep_mode_status()
        self.assertTrue(status["everyone_sleeping"])

    def test_claudia_awake_keeps_everyone_sleeping_false(self):
        persons = ["person.mikkel", "person.kristine", "person.claudia"]
        status = make_app(persons, {"person.mikkel", "person.kristine"})._get_sleep_mode_status()
        self.assertTrue(status["anyone_sleeping"])
        self.assertFalse(status["everyone_sleeping"])


if __name__ == "__main__":
    unittest.main()
