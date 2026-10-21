"""The wake-up volume ramp never plays louder than the speaker's standard volume.

The standard lives in one place, the SonosStateReset config (speaker_volumes,
default_volume), and the wake routine reads it when the alarm fires:
ceiling = min(volume_target, the speaker's standard). When that config cannot be
read the ramp is capped at the most conservative level instead of falling back to
volume_target - an alarm that is too quiet is recoverable, one that is louder than
the house is not what the household asked for.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lights"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds"))

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

import wakeup_bedroom as wb  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

APPS = Path(__file__).resolve().parents[2]
BEDROOM = "media_player.bedroom"
BATHROOM = "media_player.bathroom"
KITCHEN = "media_player.kitchen"
STANDARDS = {BEDROOM: 0.14, BATHROOM: 0.18}


def reset_app(args):
    """get_app() for a house where SonosStateReset is loaded with these args."""
    return lambda name: SimpleNamespace(args=args) if name == wb.STANDARD_VOLUME_APP else None


def _raises(name):
    raise RuntimeError("app registry unavailable")


class Rig:
    """A wake routine wired to one fake speaker (the master) and a fake get_app()."""

    def __init__(self, get_app, *, master=BEDROOM, target=0.18, start=0.06, step=0.03):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        self.app = app
        self.logged = []
        self.sets = []
        self.volume = {master: 0.14}

        app.user_log = "test_log"
        app.log = lambda msg, level="INFO", **kw: self.logged.append((level, msg))
        app.get_app = get_app
        app.master_player = master
        app.join_players = []
        app.volume_start = start
        app.volume_target = target
        app.volume_step = step
        app.volume_interval_sec = 60
        app.volume_active = False
        app.volume_timer = None
        app.volume_ceiling = None
        app.stream_id = "stream"
        app.media_verify_delay_sec = 12
        app.media_still_playing_check_sec = 45
        app.bedroom_presence_entity = None
        app._play_media = MagicMock()
        app.run_in = MagicMock(return_value="timer")
        app.call_service = self._call_service
        app.get_state = self._get_state

    def _call_service(self, service, **kw):
        if service == "media_player/volume_set":
            self.volume[kw["entity_id"]] = kw["volume_level"]
            self.sets.append(kw["volume_level"])

    def _get_state(self, entity, **kw):
        if kw.get("attribute") == "all":
            return {"attributes": {"volume_level": self.volume.get(entity)}}
        return None

    def run_ramp(self, max_ticks=50):
        """Fire the alarm's ramp and run every tick; returns each level set, in order."""
        self.app._start_media_and_volume_ramp()
        for _ in range(max_ticks):
            if not self.app.volume_active:
                return list(self.sets)
            self.app._volume_tick(None)
        raise AssertionError("volume ramp never completed")

    def messages(self):
        return [msg for _, msg in self.logged]

    def warnings(self):
        return [msg for level, msg in self.logged if level == "WARNING"]


class RampStopsAtTheStandard(unittest.TestCase):
    def test_bedroom_ramp_stops_at_its_standard_below_the_target(self):
        rig = Rig(reset_app({"speaker_volumes": STANDARDS, "default_volume": 0.12}))
        self.assertEqual(rig.run_ramp(), [0.06, 0.09, 0.12, 0.14])
        self.assertFalse(rig.app.volume_active)
        self.assertIn("[wake] Volume ramp on media_player.bedroom: 0.06 -> 0.14 (target 0.18)", rig.messages())
        self.assertIn("[wake] Volume ramp complete at 0.14", rig.messages())
        self.assertEqual(rig.warnings(), [])

    def test_every_speaker_gets_its_own_ceiling(self):
        rig = Rig(reset_app({"speaker_volumes": STANDARDS, "default_volume": 0.12}))
        self.assertEqual(rig.app._volume_ceiling_for(BEDROOM), 0.14)
        self.assertEqual(rig.app._volume_ceiling_for(BATHROOM), 0.18)

    def test_a_standard_above_the_target_leaves_the_target_as_the_ceiling(self):
        rig = Rig(reset_app({"speaker_volumes": {BEDROOM: 0.30}, "default_volume": 0.12}))
        self.assertEqual(rig.run_ramp(), [0.06, 0.09, 0.12, 0.15, 0.18])

    def test_a_target_below_the_standard_still_wins(self):
        rig = Rig(reset_app({"speaker_volumes": STANDARDS, "default_volume": 0.12}), target=0.10)
        self.assertEqual(rig.run_ramp(), [0.06, 0.09, 0.10])

    def test_a_speaker_the_reset_app_does_not_list_gets_its_default_volume(self):
        rig = Rig(reset_app({"speaker_volumes": {BATHROOM: 0.18}, "default_volume": 0.12}))
        self.assertEqual(rig.app._volume_ceiling_for(BEDROOM), 0.12)
        self.assertEqual(rig.warnings(), [])

    def test_a_standard_below_volume_start_caps_the_first_step_too(self):
        rig = Rig(reset_app({"speaker_volumes": {BEDROOM: 0.05}, "default_volume": 0.12}))
        self.assertEqual(max(rig.run_ramp()), 0.05)

    def test_a_volume_raised_past_the_ceiling_mid_ramp_is_pulled_back(self):
        rig = Rig(reset_app({"speaker_volumes": STANDARDS, "default_volume": 0.12}))
        rig.app._start_media_and_volume_ramp()
        rig.volume[BEDROOM] = 0.24
        rig.app._volume_tick(None)
        self.assertEqual(rig.volume[BEDROOM], 0.14)
        self.assertFalse(rig.app.volume_active)


class MissingStandardCapsAtTheMostConservativeLevel(unittest.TestCase):
    def assert_flat_at_start_with_one_warning(self, rig):
        sets = rig.run_ramp()
        self.assertEqual(max(sets), rig.app.volume_start)
        self.assertFalse(rig.app.volume_active)
        self.assertEqual(len(rig.warnings()), 1)
        self.assertIn(BEDROOM, rig.warnings()[0])
        self.assertIn(wb.STANDARD_VOLUME_APP, rig.warnings()[0])

    def test_reset_app_not_loaded(self):
        self.assert_flat_at_start_with_one_warning(Rig(lambda name: None))

    def test_app_registry_raises(self):
        self.assert_flat_at_start_with_one_warning(Rig(_raises))

    def test_reset_app_without_speaker_volumes(self):
        self.assert_flat_at_start_with_one_warning(Rig(reset_app({"default_volume": 0.12})))

    def test_unusable_values_count_as_missing(self):
        args = {"speaker_volumes": {BEDROOM: "loud", BATHROOM: 3.5, KITCHEN: None}, "default_volume": "x"}
        self.assert_flat_at_start_with_one_warning(Rig(reset_app(args)))

    def test_an_unlisted_speaker_without_a_default_uses_the_lowest_standard_known(self):
        rig = Rig(reset_app({"speaker_volumes": {BATHROOM: 0.18, KITCHEN: 0.13}}))
        self.assertEqual(rig.app._volume_ceiling_for(BEDROOM), 0.13)
        self.assertEqual(len(rig.warnings()), 1)

    def test_a_missing_standard_never_falls_back_to_the_target(self):
        rig = Rig(lambda name: None, target=0.5)
        self.assertEqual(max(rig.run_ramp()), 0.06)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class RealConfigs(unittest.TestCase):
    """The repo's own wake and state-reset configs: one source of truth, no silent fallback."""

    @staticmethod
    def load(path):
        with open(APPS / path) as f:
            return next(iter(yaml.safe_load(f).values()))

    def setUp(self):
        self.wake = self.load("rutines/wakeup_bedroom.yaml")
        self.reset = self.load("sonos/state_reset.yaml")

    def rig(self):
        return Rig(
            reset_app(self.reset),
            master=self.wake["media_master"],
            target=self.wake["volume_target"],
            start=self.wake["volume_start"],
            step=self.wake["volume_step"],
        )

    def test_every_wake_speaker_has_a_standard_of_its_own(self):
        speakers = [self.wake["media_master"], *self.wake["volume_entities"]]
        for speaker in speakers:
            self.assertIn(speaker, self.reset["speaker_volumes"], speaker)

    def test_no_wake_speaker_is_ever_driven_past_its_standard(self):
        rig = self.rig()
        standards = self.reset["speaker_volumes"]
        for speaker in self.wake["volume_entities"]:
            self.assertEqual(
                rig.app._volume_ceiling_for(speaker),
                min(self.wake["volume_target"], standards[speaker]),
                speaker,
            )
        self.assertLessEqual(max(rig.run_ramp()), standards[self.wake["media_master"]])
        self.assertEqual(rig.warnings(), [])


if __name__ == "__main__":
    unittest.main()
