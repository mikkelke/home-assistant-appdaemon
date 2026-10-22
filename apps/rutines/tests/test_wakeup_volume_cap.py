"""The wake-up volume ramp never plays a speaker louder than its standard volume.

Every speaker in the wake group is set to volume_start when the alarm fires - whatever it
was left at - and ramps up on its own to its own ceiling; the ramp only ever raises a speaker,
so a bath boost or a hand on the volume is never undone. The standard lives in one place, the
SonosStateReset config (speaker_volumes, default_volume), and the wake routine reads it when
the alarm fires: ceiling = min(volume_target, the speaker's standard). When that config cannot
be read the ramp is capped at the most conservative level instead of falling back to
volume_target: an alarm that is too quiet is recoverable, one that is louder than the house is
not what the household asked for.
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
    """A wake routine wired to fake speakers and a fake get_app()."""

    def __init__(self, get_app, *, entities=(BEDROOM, BATHROOM), target=0.18, start=0.06,
                 step=0.03, leftover=0.43):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        self.app = app
        self.logged = []
        self.sets = []
        self.broken = set()
        self.unreadable = False
        self.volume = {e: leftover for e in entities}

        app.user_log = "test_log"
        app.log = lambda msg, level="INFO", **kw: self.logged.append((level, msg))
        app.get_app = get_app
        app.master_player = entities[0]
        app.join_players = list(entities[1:])
        app.volume_entities = list(entities)
        app.volume_start = start
        app.volume_target = target
        app.volume_step = step
        app.volume_interval_sec = 60
        app.volume_active = False
        app.volume_timer = None
        app.volume_ramp = {}
        app.volume_levels = {}
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
            if kw["entity_id"] in self.broken:
                raise RuntimeError("speaker offline")
            self.volume[kw["entity_id"]] = kw["volume_level"]
            self.sets.append((kw["entity_id"], kw["volume_level"]))

    def _get_state(self, entity, **kw):
        if kw.get("attribute") == "all":
            level = None if self.unreadable else self.volume.get(entity)
            return {"attributes": {"volume_level": level}}
        return None

    def run_ramp(self, max_ticks=50):
        """Fire the alarm's ramp and run every tick; returns the levels set, per speaker."""
        self.app._start_media_and_volume_ramp()
        for _ in range(max_ticks):
            if not self.app.volume_active:
                return self.levels()
            self.app._volume_tick(None)
        raise AssertionError("volume ramp never completed")

    def levels(self):
        out = {}
        for entity, level in self.sets:
            out.setdefault(entity, []).append(level)
        return out

    def messages(self):
        return [msg for _, msg in self.logged]

    def warnings(self):
        return [msg for level, msg in self.logged if level == "WARNING"]


def standards_rig(**kw):
    return Rig(reset_app({"speaker_volumes": STANDARDS, "default_volume": 0.12}), **kw)


class EverySpeakerRampsToItsOwnCeiling(unittest.TestCase):
    def test_bedroom_stops_at_its_standard_while_the_bathroom_goes_on_to_its_own(self):
        rig = standards_rig()
        levels = rig.run_ramp()
        self.assertEqual(levels[BEDROOM], [0.06, 0.09, 0.12, 0.14])
        self.assertEqual(levels[BATHROOM], [0.06, 0.09, 0.12, 0.15, 0.18])
        self.assertFalse(rig.app.volume_active)
        self.assertEqual(rig.warnings(), [])

    def test_the_log_says_what_each_speaker_will_do(self):
        rig = standards_rig()
        rig.run_ramp()
        self.assertIn("[wake] Volume ramp on media_player.bedroom: 0.06 -> 0.14 (target 0.18)", rig.messages())
        self.assertIn("[wake] Volume ramp on media_player.bathroom: 0.06 -> 0.18 (target 0.18)", rig.messages())
        self.assertIn("[wake] Volume ramp complete on media_player.bedroom at 0.14", rig.messages())
        self.assertIn("[wake] Volume ramp complete on media_player.bathroom at 0.18", rig.messages())

    def test_every_speaker_starts_at_volume_start_whatever_it_was_left_at(self):
        rig = standards_rig(leftover=0.43)
        rig.app._start_media_and_volume_ramp()
        self.assertEqual(rig.volume, {BEDROOM: 0.06, BATHROOM: 0.06})

    def test_a_speaker_at_its_ceiling_is_left_alone_while_the_others_ramp_on(self):
        rig = standards_rig()
        rig.run_ramp()
        self.assertEqual(rig.sets.count((BEDROOM, 0.14)), 1)

    def test_every_speaker_gets_its_own_ceiling(self):
        rig = standards_rig()
        self.assertEqual(rig.app._volume_ceiling_for(BEDROOM), 0.14)
        self.assertEqual(rig.app._volume_ceiling_for(BATHROOM), 0.18)

    def test_a_standard_above_the_target_leaves_the_target_as_the_ceiling(self):
        rig = Rig(reset_app({"speaker_volumes": {BEDROOM: 0.30, BATHROOM: 0.30}, "default_volume": 0.12}))
        levels = rig.run_ramp()
        self.assertEqual(levels[BEDROOM], [0.06, 0.09, 0.12, 0.15, 0.18])
        self.assertEqual(levels[BATHROOM], [0.06, 0.09, 0.12, 0.15, 0.18])

    def test_a_target_below_the_standard_still_wins(self):
        levels = standards_rig(target=0.10).run_ramp()
        self.assertEqual(levels[BEDROOM], [0.06, 0.09, 0.10])
        self.assertEqual(levels[BATHROOM], [0.06, 0.09, 0.10])

    def test_a_speaker_the_reset_app_does_not_list_gets_its_default_volume(self):
        rig = Rig(reset_app({"speaker_volumes": {BATHROOM: 0.18}, "default_volume": 0.12}))
        self.assertEqual(rig.app._volume_ceiling_for(BEDROOM), 0.12)
        self.assertEqual(rig.warnings(), [])

    def test_a_standard_below_volume_start_caps_the_first_step_too(self):
        rig = Rig(reset_app({"speaker_volumes": {BEDROOM: 0.05, BATHROOM: 0.18}, "default_volume": 0.12}))
        self.assertEqual(max(rig.run_ramp()[BEDROOM]), 0.05)


class TheRampOnlyEverRaises(unittest.TestCase):
    def test_a_speaker_taken_past_its_ceiling_mid_ramp_is_left_where_it_is(self):
        rig = standards_rig()
        rig.app._start_media_and_volume_ramp()
        rig.volume[BEDROOM] = 0.24
        rig.volume[BATHROOM] = 0.34
        rig.app._volume_tick(None)
        self.assertEqual(rig.volume, {BEDROOM: 0.24, BATHROOM: 0.34})
        self.assertEqual(rig.sets, [(BEDROOM, 0.06), (BATHROOM, 0.06)])
        self.assertFalse(rig.app.volume_active)

    def test_a_boost_in_the_middle_of_the_ramp_is_never_pulled_back_down(self):
        rig = standards_rig()
        rig.app._start_media_and_volume_ramp()
        rig.app._volume_tick(None)
        rig.volume[BATHROOM] = round(rig.volume[BATHROOM] + 0.10, 2)
        boosted = rig.volume[BATHROOM]
        for _ in range(6):
            rig.app._volume_tick(None)
            self.assertGreaterEqual(rig.volume[BATHROOM], boosted)
        self.assertFalse(rig.app.volume_active)

    def test_a_boost_below_the_ceiling_is_ramped_on_not_undone(self):
        rig = standards_rig()
        rig.app._start_media_and_volume_ramp()
        rig.volume[BATHROOM] = 0.16
        rig.app._volume_tick(None)
        self.assertEqual(rig.volume[BATHROOM], 0.18)

    def test_one_speaker_failing_does_not_stop_the_other_or_the_ramp(self):
        rig = standards_rig()
        rig.broken.add(BATHROOM)
        levels = rig.run_ramp()
        self.assertEqual(levels[BEDROOM], [0.06, 0.09, 0.12, 0.14])
        self.assertNotIn(BATHROOM, levels)
        self.assertFalse(rig.app.volume_active)
        self.assertTrue(any(BATHROOM in w and "failed" in w for w in rig.warnings()))

    def test_unreadable_volumes_do_not_keep_the_ramp_running_forever(self):
        rig = standards_rig()
        rig.unreadable = True
        levels = rig.run_ramp()
        self.assertEqual(levels[BEDROOM], [0.06, 0.09, 0.12, 0.14])
        self.assertEqual(levels[BATHROOM], [0.06, 0.09, 0.12, 0.15, 0.18])

    def test_stopping_the_routine_stops_the_ramp(self):
        rig = standards_rig()
        rig.app._start_media_and_volume_ramp()
        rig.app.volume_active = False
        before = list(rig.sets)
        rig.app._volume_tick(None)
        self.assertEqual(rig.sets, before)


class MissingStandardCapsAtTheMostConservativeLevel(unittest.TestCase):
    def assert_flat_at_start_with_a_warning_per_speaker(self, rig):
        levels = rig.run_ramp()
        for entity in (BEDROOM, BATHROOM):
            self.assertEqual(max(levels[entity]), rig.app.volume_start)
            self.assertTrue(any(entity in w and wb.STANDARD_VOLUME_APP in w for w in rig.warnings()), entity)
        self.assertEqual(len(rig.warnings()), 2)
        self.assertFalse(rig.app.volume_active)

    def test_reset_app_not_loaded(self):
        self.assert_flat_at_start_with_a_warning_per_speaker(Rig(lambda name: None))

    def test_app_registry_raises(self):
        self.assert_flat_at_start_with_a_warning_per_speaker(Rig(_raises))

    def test_reset_app_without_speaker_volumes(self):
        self.assert_flat_at_start_with_a_warning_per_speaker(Rig(reset_app({"default_volume": 0.12})))

    def test_unusable_values_count_as_missing(self):
        args = {"speaker_volumes": {BEDROOM: "loud", BATHROOM: 3.5, KITCHEN: None}, "default_volume": "x"}
        self.assert_flat_at_start_with_a_warning_per_speaker(Rig(reset_app(args)))

    def test_an_unlisted_speaker_without_a_default_uses_the_lowest_standard_known(self):
        rig = Rig(reset_app({"speaker_volumes": {BATHROOM: 0.18, KITCHEN: 0.13}}))
        self.assertEqual(rig.app._volume_ceiling_for(BEDROOM), 0.13)
        self.assertEqual(len(rig.warnings()), 1)

    def test_a_missing_standard_never_falls_back_to_the_target(self):
        rig = Rig(lambda name: None, target=0.5)
        for entity, levels in rig.run_ramp().items():
            self.assertEqual(max(levels), 0.06, entity)


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
            entities=tuple(self.wake["volume_entities"]),
            target=self.wake["volume_target"],
            start=self.wake["volume_start"],
            step=self.wake["volume_step"],
        )

    def test_the_wake_group_is_exactly_the_ramped_speakers(self):
        group = {self.wake["media_master"], *self.wake["media_join"]}
        self.assertEqual(group, set(self.wake["volume_entities"]))

    def test_the_bed_speaker_is_a_wake_speaker_and_the_bedroom_has_bed_witnesses(self):
        self.assertIn(self.wake["bed_speaker"], self.wake["volume_entities"])
        zones = self.load("presence/room_active.yaml")["zones"]
        self.assertTrue(any(w["tier"] == "bed" for w in zones["bedroom"]["witnesses"]))

    def test_every_ramped_speaker_has_a_standard_of_its_own(self):
        for speaker in self.wake["volume_entities"]:
            self.assertIn(speaker, self.reset["speaker_volumes"], speaker)

    def test_no_speaker_is_ever_driven_past_its_standard(self):
        rig = self.rig()
        standards = self.reset["speaker_volumes"]
        for speaker in self.wake["volume_entities"]:
            self.assertEqual(
                rig.app._volume_ceiling_for(speaker),
                min(self.wake["volume_target"], standards[speaker]),
                speaker,
            )
        for speaker, levels in rig.run_ramp().items():
            self.assertLessEqual(max(levels), standards[speaker], speaker)
        self.assertEqual(rig.warnings(), [])


if __name__ == "__main__":
    unittest.main()
