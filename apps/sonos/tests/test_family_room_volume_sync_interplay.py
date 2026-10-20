"""FamilyRoomVolumeSync next to the other apps that write Sonos volumes.

Each test runs a real writer (SonosStateReset, SonosGroupManager, SonosFollowMe, SonosNotifier)
against the sync app's rig. The writer's service calls change the rig's Home Assistant state the
way Home Assistant, Music Assistant and the speakers do, and the sync app sees the resulting
state changes. None of them may make the sync write a volume.
"""

from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime
from pathlib import Path

TESTS = Path(__file__).resolve().parent
APPS = TESTS.parents[1]
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(APPS / "sonos"))
sys.path.insert(0, str(APPS / "lights"))
sys.path.insert(0, str(APPS / "notify"))

from test_family_room_volume_sync import (  # noqa: E402
    DINING,
    FAMILY,
    KITCHEN,
    LIVING,
    ROOFTOP,
    Rig,
    twin,
)

import follow_me as fm  # noqa: E402
import group_manager as gm  # noqa: E402
import sonos_notifier as sn  # noqa: E402
import state_reset as sr  # noqa: E402

CLIP_URL = "https://ha.example/local/chime_tts/clip.mp3"
MASTER_SELECT = "input_select.sonos_master_speaker"
OFF_DEFAULT = {ROOFTOP: 0.33, LIVING: 0.35, KITCHEN: 0.33, DINING: 0.30}


def setUpModule():
    global yaml
    try:
        import yaml
    except ImportError:
        raise unittest.SkipTest("PyYAML not installed")


def app_args(path):
    with open(APPS / path) as f:
        return next(iter(yaml.safe_load(f).values()))


class House:
    """Applies other apps' service calls to the rig's state, a moment later, the way Home
    Assistant and the speakers do: Music Assistant truncates a volume to whole percent, the
    native twin rounds it."""

    LATENCY = 0.05

    def __init__(self, rig):
        self.rig = rig
        self.calls = []
        self.events = []
        self.ignored = set()
        self.volume_listeners = []
        rig_change = rig.change

        def change(entity, state=None, user_id=None, fire=True, **attrs):
            before = self.volume(entity)
            rig_change(entity, state=state, user_id=user_id, fire=fire, **attrs)
            after = self.volume(entity)
            if fire and before != after:
                for listener in self.volume_listeners:
                    listener(entity, "volume_level", before, after, {})

        rig.change = change

    def volume(self, entity):
        st = self.rig.states.get(entity)
        return None if st is None else st["attributes"].get("volume_level")

    def get_state(self, entity_id=None, attribute=None, **kw):
        return self.rig._get_state(entity_id, attribute=attribute, **kw)

    def run_in(self, callback, delay, **kwargs):
        self.rig._schedule(delay, lambda: callback(dict(kwargs)))

    def fire_event(self, event, **kwargs):
        self.events.append((event, kwargs))

    def call_service(self, service, **data):
        self.calls.append((service, copy.deepcopy(data)))
        if service == "chime_tts/say_url":
            return {
                "success": True,
                "result": {"context": {}, "response": {"url": CLIP_URL, "success": True, "duration": 3.1}},
            }
        if service == "media_player/play_media" and data.get("callback"):
            data["callback"]({"success": True})
        elif service not in self.ignored:
            entities = data.get("entity_id")
            for entity in [entities] if isinstance(entities, str) else list(entities or []):
                self.rig._schedule(self.LATENCY, lambda e=entity: self.apply(service, e, data))
        return {"success": True}

    def apply(self, service, entity, data):
        native = entity.endswith("_2")
        speaker = entity[: -len("_2")] if native else entity
        if speaker not in self.rig.states:
            return
        if service == "media_player/volume_set":
            level = data["volume_level"]
            percent = round(level * 100) if native else int(level * 100)
            self.rig.states[twin(speaker)]["attributes"]["volume_level"] = percent / 100
            self.rig.move(speaker, percent / 100)
        elif service == "media_player/volume_mute":
            self.rig.change(speaker, is_volume_muted=data["is_volume_muted"])
        elif service == "media_player/media_pause":
            self.rig.change(speaker, state="paused")
        elif service == "media_player/media_play":
            self.rig.change(speaker, state="playing")
        elif service == "media_player/unjoin":
            rest = [m for m in self.rig.states[speaker]["attributes"]["group_members"] if m != speaker]
            self.rig.change(speaker, group_members=[])
            for m in rest:
                if m in self.rig.states:
                    self.rig.change(m, group_members=rest if len(rest) > 1 else [])

    def volume_sets(self, entity=None):
        return [d["volume_level"] for s, d in self.calls
                if s == "media_player/volume_set" and (entity is None or d["entity_id"] in (entity, [entity]))]

    def volumes(self):
        return {m: self.rig.vol(m) for m in FAMILY}


def quiet_sync(test, rig):
    test.assertEqual(rig.write_log, [])
    test.assertEqual(rig.logs(contains="family_volume_sync_apply"), [])


class StateResetInterplay(unittest.TestCase):
    """The 20-minute idle reset ungroups, then sets every speaker to its own default volume."""

    def setUp(self):
        self.rig = Rig(OFF_DEFAULT)
        self.house = House(self.rig)
        args = app_args("sonos/state_reset.yaml")
        app = sr.SonosStateReset.__new__(sr.SonosStateReset)
        app.speakers = list(args["all_speakers"])
        app.speaker_volumes = dict(args["speaker_volumes"])
        app.default_volume = float(args["default_volume"])
        app.ungroup_settle_sec = int(args["ungroup_settle_seconds"])
        app.unjoin_strategy = str(args["unjoin_strategy"])
        app.reset_solo_poll_sec = float(args["reset_solo_poll_seconds"])
        app.reset_solo_max_polls = int(args["reset_solo_max_poll_attempts"])
        app.inactivity_sec = int(args["inactivity_seconds"])
        app._reset_in_progress = False
        app._reset_wedge_timer = None
        app._active_reset_ctx = None
        app.log = lambda *a, **k: None
        app.get_state = self.house.get_state
        app.call_service = self.house.call_service
        app.fire_event = self.house.fire_event
        app.run_in = self.house.run_in
        app.timer_running = lambda handle: False
        app.cancel_timer = lambda handle: None
        self.app = app
        self.defaults = {m: float(args["speaker_volumes"][m]) for m in FAMILY}

    def reset(self, targets, trigger, source):
        self.app._do_reset(targets, trigger=trigger, source=source)
        requested = [kw for event, kw in self.house.events if event == "sonos_reset_requested"]
        self.assertEqual(len(requested), 1)
        self.app._on_reset_ready("sonos_reset_ready", requested[0], {})
        self.rig.advance(60)

    def test_idle_reset_of_the_family_group_writes_nothing(self):
        for m in FAMILY:
            self.rig.change(m, state="paused")
        self.reset(list(FAMILY), "auto_trigger", LIVING)
        self.assertEqual(sorted(self.house.volume_sets()), sorted(self.defaults.values()))
        self.assertEqual(self.house.volumes(), self.defaults)
        self.assertEqual(self.rig.arm_delays, [])
        quiet_sync(self, self.rig)

    def test_reset_that_finalizes_while_the_group_never_split_writes_nothing(self):
        self.house.ignored.add("media_player/unjoin")
        for m in FAMILY:
            self.rig.change(m, state="idle")
        self.reset(list(FAMILY), "auto_trigger", LIVING)
        self.assertEqual(self.house.volumes(), self.defaults)
        self.assertTrue(all(len(self.rig.states[m]["attributes"]["group_members"]) == 4 for m in FAMILY))
        self.assertEqual(self.rig.arm_delays, [])
        quiet_sync(self, self.rig)

    def test_tv_reset_of_the_playing_living_room_ungroups_first_and_writes_nothing(self):
        self.reset([LIVING], "manual_trigger", "Living room TV turned on")
        self.assertEqual(self.house.volume_sets(), [self.defaults[LIVING]])
        self.assertEqual(self.rig.vol(LIVING), self.defaults[LIVING])
        self.assertTrue(all(self.rig.states[m]["attributes"]["group_members"] == [] for m in FAMILY))
        self.assertEqual(self.rig.arm_delays, [])
        quiet_sync(self, self.rig)


class GroupManagerInterplay(unittest.TestCase):
    """GroupManager puts the master's last volume back when AirPlay drops it to 0."""

    def test_airplay_restore_on_the_master_writes_nothing(self):
        rig = Rig({KITCHEN: 0.30})
        house = House(rig)
        rig.states[MASTER_SELECT] = {"state": "Kitchen", "attributes": {}}
        rig.quietly(KITCHEN, source="AirPlay")
        app = gm.SonosGroupManager.__new__(gm.SonosGroupManager)
        app.master_select = MASTER_SELECT
        app._friendly_to_entity_map = {"Kitchen": KITCHEN}
        app._last_nonzero_volume = {}
        app._should_follow_me_be_active = lambda: True
        app.log = lambda *a, **k: None
        app.get_state = house.get_state
        app.call_service = house.call_service
        house.volume_listeners.append(app._on_volume_level_change)
        app._on_volume_level_change(KITCHEN, "volume_level", 0.25, 0.30, {})

        rig.move(KITCHEN, 0.0)
        rig.advance(0.5)
        app._restore_volume_if_airplay(KITCHEN)
        rig.advance(10)

        self.assertEqual(house.volume_sets(KITCHEN), [0.30])
        self.assertEqual(rig.vol(KITCHEN), 0.30)
        self.assertEqual(rig.arm_delays, [])
        quiet_sync(self, rig)


class FollowMeInterplay(unittest.TestCase):
    """follow_me mutes and unmutes grouped speakers, and puts back a volume AirPlay set to 0."""

    def setUp(self):
        self.rig = Rig({KITCHEN: 0.30})
        self.house = House(self.rig)
        reset_volumes = app_args("sonos/state_reset.yaml")
        reset_app = type("StateReset", (), {
            "speaker_volumes": dict(reset_volumes["speaker_volumes"]),
            "default_volume": float(reset_volumes["default_volume"]),
        })()
        app = fm.SonosFollowMe.__new__(fm.SonosFollowMe)
        app.args = {"all_speakers": list(FAMILY)}
        app._reset_in_progress = False
        app._last_nonzero_volume = {}
        app._should_follow_me_be_active = lambda: True
        app._get_current_coordinator = lambda use_cache=True: KITCHEN
        app.get_app = lambda name: reset_app
        app.log = lambda *a, **k: None
        app.get_state = self.house.get_state
        app.call_service = self.house.call_service
        app.run_in = self.house.run_in
        self.house.volume_listeners.append(app._on_volume_level_change)
        self.app = app

    def test_muting_and_unmuting_grouped_speakers_writes_nothing(self):
        for m in (DINING, ROOFTOP):
            self.app._apply_follow_me_mute(m, True)
        self.rig.advance(5)
        self.assertTrue(self.rig.states[DINING]["attributes"]["is_volume_muted"])
        for m in (DINING, ROOFTOP):
            self.app._apply_follow_me_mute(m, False, master=KITCHEN)
        self.rig.advance(5)
        self.assertFalse(self.rig.states[DINING]["attributes"]["is_volume_muted"])
        self.assertEqual(self.house.volume_sets(), [])
        self.assertEqual(self.rig.arm_delays, [])
        quiet_sync(self, self.rig)

    def test_airplay_dropping_the_master_to_zero_is_restored_without_a_sync(self):
        self.app._on_volume_level_change(KITCHEN, "volume_level", 0.25, 0.30, {})
        self.rig.move(KITCHEN, 0.0)
        self.rig.advance(10)
        self.assertEqual(self.house.volume_sets(KITCHEN), [0.30])
        self.assertEqual(self.rig.vol(KITCHEN), 0.30)
        self.assertEqual(self.rig.arm_delays, [])
        quiet_sync(self, self.rig)

    def test_unmuting_the_master_at_zero_restores_once_without_a_sync(self):
        self.rig.quietly(KITCHEN, volume_level=0.0, is_volume_muted=True)
        self.app._apply_follow_me_mute(KITCHEN, False, master=KITCHEN)
        self.rig.advance(10)
        default = float(app_args("sonos/state_reset.yaml")["speaker_volumes"][KITCHEN])
        self.assertEqual(self.house.volume_sets(KITCHEN), [default])
        self.assertEqual(self.rig.vol(KITCHEN), default)
        self.assertFalse(self.rig.states[KITCHEN]["attributes"]["is_volume_muted"])
        self.assertEqual(self.rig.arm_delays, [])
        quiet_sync(self, self.rig)


class SonosNotifierInterplay(unittest.TestCase):
    """An announcement is a chime_tts render played as a native Sonos audio clip: Sonos
    plays the clip over the music itself, so no player volume is written."""

    def setUp(self):
        self.rig = Rig()
        self.house = House(self.rig)
        app = sn.SonosNotifier.__new__(sn.SonosNotifier)
        app.args = app_args("notify/sonos_notifier.yaml")
        app.log = lambda *a, **k: None
        app.get_state = self.house.get_state
        app.call_service = self.house.call_service
        app.datetime = lambda: datetime(2026, 10, 13, 15, 0)
        app.initialize()
        self.app = app
        self.before = self.house.volumes()

    def announce(self, **kwargs):
        self.app.notify("Dryer is ready to be emptied", **kwargs)
        self.rig.advance(30)
        renders = [d for s, d in self.house.calls if s == "chime_tts/say_url"]
        self.assertEqual(len(renders), 1)
        plays = [d for s, d in self.house.calls if s == "media_player/play_media"]
        for play in plays:
            self.assertTrue(play["announce"])
            self.assertEqual(play["media_content_id"], "http://10.21.0.5:8123/local/chime_tts/clip.mp3")
        for play in plays:
            self.assertTrue(all(twin.endswith("_2") for twin in play["entity_id"]))
        return {twin[: -len("_2")]: play["extra"].get("volume") for play in plays for twin in play["entity_id"]}

    def test_an_announcement_while_the_family_group_plays_writes_nothing(self):
        announced = self.announce()
        self.assertEqual(sorted(announced), sorted(self.app.tts_group_all))
        for room in FAMILY:
            self.assertIsNone(announced[room])
        self.assertEqual(self.house.volume_sets(), [])
        self.assertEqual(self.house.volumes(), self.before)
        self.assertEqual(self.rig.arm_delays, [])
        quiet_sync(self, self.rig)

    def test_the_house_wide_alarm_level_writes_nothing(self):
        for name, volumes in (("offsets", None), ("one level", {m: 0.20 for m in FAMILY})):
            with self.subTest(name):
                self.setUp()
                for m, v in (volumes or {}).items():
                    self.rig.quietly(m, volume_level=v)
                before = self.house.volumes()
                announced = self.announce(
                    target_speakers=list(self.app.tts_group_all), override_quiet_hours=True, volume_level=0.7
                )
                self.assertEqual(set(announced.values()), {70})
                self.assertEqual(self.house.volume_sets(), [])
                self.assertEqual(self.house.volumes(), before)
                quiet_sync(self, self.rig)

    def test_the_kitchen_chime_over_playing_music_writes_nothing(self):
        self.rig.quietly(KITCHEN, media_duration=183, media_position=52)
        announced = self.announce(target_speakers=[KITCHEN], volume_level=0.15)
        self.assertEqual(announced, {KITCHEN: 15})
        self.assertEqual(self.house.volume_sets(), [])
        self.assertEqual(self.house.volumes(), self.before)
        quiet_sync(self, self.rig)


if __name__ == "__main__":
    unittest.main()
