"""SonosFollowMe publishes what it did to each speaker and why (sensor.follow_me).

Every mute or unmute goes through _set_mute, which makes the one media_player/volume_mute call and
then records {action, at, why} for that speaker and publishes the entity: state = how many
speakers FollowMe last left muted, attribute `speakers` = {speaker: {action, at, why}}, strings
only. FollowMe keeps music playing in every room, so what is pinned first is that the mute
decisions did not move: the same calls in the same cases, and a failing publish never costs a call.
"""

from __future__ import annotations

import itertools
import re
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
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

STATUS = "sensor.follow_me"
TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 10, 14, 35, 12, tzinfo=TZ)
STAMP = "2026-11-10T14:35:12+01:00"

ROOMS = ("bathroom", "bedroom", "kitchen", "living_room", "guest_bathroom")
SPEAKER = {room: f"media_player.{room}" for room in ROOMS}
ROOFTOP = "media_player.rooftop"
ALL_SPEAKERS = [*SPEAKER.values(), ROOFTOP]

ARGS = {
    "room_presence_map": {
        **{room: f"binary_sensor.{room}_active" for room in ROOMS},
        "hallway": "binary_sensor.hallway_active",
        "rooftop_charging": "binary_sensor.rooftop_charging",
    },
    "all_speakers": ALL_SPEAKERS,
    "speaker_room_map": {entity: room for room, entity in SPEAKER.items()},
    "special_conditions": {"kitchen_or_hallway": True, "rooftop_with_living_room": True},
    "status_entity": STATUS,
}


def make_app(args=None, published=None):
    """SonosFollowMe with the real initialize() against a faked AppDaemon surface. `published` is
    the speakers attribute the status entity already carries, as after an AppDaemon restart."""
    app = fm.SonosFollowMe.__new__(fm.SonosFollowMe)
    app.args = {**ARGS, **(args or {})}
    app.states = {}   # entity -> {"state": str, "attributes": dict}
    app.logged = []
    app.published = []
    app.scheduled = []
    app.now = NOW
    ticks = itertools.count()
    # Each datetime() call is a minute later than the last, so the 1 s coordinator cache never answers.
    app.datetime = lambda: datetime(2026, 11, 10, 14, 0, 0) + timedelta(minutes=next(ticks))
    app.get_now = lambda: app.now
    app.log = lambda msg, level="INFO": app.logged.append((level, msg))
    app.call_service = MagicMock()
    app.set_state = lambda entity, **kw: app.published.append((entity, kw))
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_in = lambda cb, delay, **kw: app.scheduled.append((cb, delay, kw)) or object()
    app.timer_running = lambda handle: False
    app.cancel_timer = MagicMock()
    app.get_app = lambda name: None

    def get_state(entity, attribute=None, **kw):
        if entity == STATUS and attribute == "speakers":
            return published
        ent = app.states.get(entity)
        if ent is None:
            return None
        return ent["state"] if attribute is None else ent.get("attributes", {}).get(attribute)

    app.get_state = get_state
    app.initialize()
    return app


def put(app, entity, state, **attributes):
    app.states[entity] = {"state": state, "attributes": attributes}


def world(app, playing=("kitchen", "bathroom"), muted=(), grouped=True, present=()):
    """The `playing` speakers play (as one group when `grouped`), `muted` ones are muted, every
    other room's presence sensor reads off, and the ones in `present` read on."""
    members = [SPEAKER[room] for room in playing]
    for room in ROOMS:
        put(app, SPEAKER[room], "playing" if room in playing else "idle",
            group_members=members if grouped and room in playing else [],
            is_volume_muted=room in muted, volume_level=0.3)
        put(app, f"binary_sensor.{room}_active", "on" if room in present else "off")
    put(app, "binary_sensor.hallway_active", "on" if "hallway" in present else "off")
    put(app, ROOFTOP, "idle", group_members=[], is_volume_muted=False, volume_level=0.3)
    put(app, "binary_sensor.rooftop_charging", "off")
    app._invalidate_coordinator_cache()


def last_published(app):
    entity, kw = app.published[-1]
    assert entity == STATUS
    return kw


def speakers(app):
    return last_published(app)["attributes"]["speakers"]


def mute_calls(app):
    return [c for c in app.call_service.call_args_list if c.args[:1] == ("media_player/volume_mute",)]


def muted_call(entity, muted):
    return (("media_player/volume_mute",), {"entity_id": entity, "is_volume_muted": muted})


class TheDecisionsDidNotMove(unittest.TestCase):
    """_apply_follow_me_mute: the same call in the same cases, with or without a status entity."""

    def apply(self, app, entity, desired, room):
        app._apply_follow_me_mute(entity, desired, room=room, master=SPEAKER["kitchen"])
        return [(c.args, c.kwargs) for c in mute_calls(app)]

    def test_a_playing_speaker_in_an_empty_room_is_muted(self):
        for status in (STATUS, None):
            with self.subTest(status=status):
                app = make_app(args={"status_entity": status})
                world(app)
                self.assertEqual(self.apply(app, SPEAKER["bathroom"], True, "bathroom"),
                                 [muted_call(SPEAKER["bathroom"], True)])

    def test_a_muted_speaker_in_a_room_with_someone_is_unmuted(self):
        for status in (STATUS, None):
            with self.subTest(status=status):
                app = make_app(args={"status_entity": status})
                world(app, muted=("bathroom",), present=("bathroom",))
                self.assertEqual(self.apply(app, SPEAKER["bathroom"], False, "bathroom"),
                                 [muted_call(SPEAKER["bathroom"], False)])

    def test_nothing_is_called_when_the_speaker_is_already_as_wanted(self):
        app = make_app()
        world(app, muted=("bathroom",))
        self.assertEqual(self.apply(app, SPEAKER["bathroom"], True, "bathroom"), [])
        world(app)
        self.assertEqual(self.apply(app, SPEAKER["bathroom"], False, "bathroom"), [])

    def test_nothing_is_called_for_a_speaker_that_is_not_playing(self):
        app = make_app()
        world(app, playing=("kitchen",), grouped=False)
        self.assertEqual(self.apply(app, SPEAKER["bedroom"], True, "bedroom"), [])

    def test_nothing_is_called_during_a_reset(self):
        app = make_app()
        world(app)
        app._reset_in_progress = True
        self.assertEqual(self.apply(app, SPEAKER["bathroom"], True, "bathroom"), [])

    def test_nothing_is_called_for_an_excluded_room(self):
        app = make_app()
        world(app, playing=("living_room",), grouped=False)
        self.assertEqual(self.apply(app, SPEAKER["living_room"], True, "living_room"), [])

    def test_a_skipped_decision_leaves_the_status_alone(self):
        app = make_app()
        world(app)
        published = len(app.published)
        app._reset_in_progress = True
        self.apply(app, SPEAKER["bathroom"], True, "bathroom")
        self.assertEqual(len(app.published), published)


class SetMuteHelper(unittest.TestCase):
    def test_it_makes_the_one_service_call_and_records_it(self):
        app = make_app()
        app._set_mute(SPEAKER["bathroom"], True, "because")
        self.assertEqual([(c.args, c.kwargs) for c in app.call_service.call_args_list],
                         [muted_call(SPEAKER["bathroom"], True)])
        self.assertEqual(speakers(app), {SPEAKER["bathroom"]: {"action": "muted", "at": STAMP, "why": "because"}})

    def test_the_state_counts_the_speakers_last_left_muted(self):
        app = make_app()
        self.assertEqual(last_published(app)["state"], "0")
        app._set_mute(SPEAKER["bathroom"], True, "a")
        app._set_mute(SPEAKER["kitchen"], True, "b")
        self.assertEqual(last_published(app)["state"], "2")
        app._set_mute(SPEAKER["bathroom"], False, "c")
        self.assertEqual(last_published(app)["state"], "1")
        self.assertEqual({k: v["action"] for k, v in speakers(app).items()},
                         {SPEAKER["bathroom"]: "unmuted", SPEAKER["kitchen"]: "muted"})

    def test_each_speaker_keeps_only_its_last_action(self):
        app = make_app()
        app.now = NOW
        app._set_mute(SPEAKER["bathroom"], True, "first")
        app.now = NOW + timedelta(minutes=5)
        app._set_mute(SPEAKER["bathroom"], False, "second")
        self.assertEqual(speakers(app), {SPEAKER["bathroom"]: {
            "action": "unmuted", "at": "2026-11-10T14:40:12+01:00", "why": "second"}})

    def test_everything_published_is_a_string_and_replaces_the_old_attributes(self):
        app = make_app()
        app._set_mute(SPEAKER["bathroom"], True, "because")
        kw = last_published(app)
        self.assertTrue(kw["replace"])
        self.assertIsInstance(kw["state"], str)
        for key in ("friendly_name", "icon"):
            self.assertIsInstance(kw["attributes"][key], str)
        for record in kw["attributes"]["speakers"].values():
            for value in record.values():
                self.assertIsInstance(value, str)
                self.assertTrue(value)

    def test_a_failing_publish_never_costs_the_call(self):
        app = make_app()

        def boom(entity, **kw):
            raise RuntimeError("HA is away")

        app.set_state = boom
        app._set_mute(SPEAKER["bathroom"], True, "because")
        self.assertEqual(len(mute_calls(app)), 1)
        self.assertTrue(any(lvl == "WARNING" and "status_publish_error" in msg for lvl, msg in app.logged))

    def test_a_failing_record_never_costs_the_call(self):
        app = make_app()

        def boom():
            raise RuntimeError("no clock")

        app.get_now = boom
        app._set_mute(SPEAKER["bathroom"], False, "because")
        self.assertEqual(len(mute_calls(app)), 1)
        self.assertTrue(any(lvl == "WARNING" and "status_record_error" in msg for lvl, msg in app.logged))

    def test_a_failing_service_call_is_not_recorded_as_done(self):
        app = make_app()
        app.call_service = MagicMock(side_effect=RuntimeError("HA is away"))
        with self.assertRaises(RuntimeError):
            app._set_mute(SPEAKER["bathroom"], True, "because")
        self.assertEqual(speakers(app), {})

    def test_without_a_status_entity_it_only_makes_the_call(self):
        app = make_app(args={"status_entity": None})
        self.assertEqual(app.published, [])
        app._set_mute(SPEAKER["bathroom"], True, "because")
        self.assertEqual(len(mute_calls(app)), 1)
        self.assertEqual(app.published, [])

    def test_an_app_that_was_never_initialized_still_makes_the_call(self):
        app = fm.SonosFollowMe.__new__(fm.SonosFollowMe)
        app.call_service = MagicMock()
        app._set_mute(SPEAKER["bathroom"], True, "because")
        self.assertEqual([(c.args, c.kwargs) for c in app.call_service.call_args_list],
                         [muted_call(SPEAKER["bathroom"], True)])


class WhyTexts(unittest.TestCase):
    def test_presence_decides_a_mute_in_an_empty_room(self):
        app = make_app()
        world(app)
        app._apply_follow_me_mute(SPEAKER["bathroom"], True, room="bathroom")
        self.assertEqual(speakers(app)[SPEAKER["bathroom"]]["why"], "nobody seen in the bathroom")
        self.assertEqual(speakers(app)[SPEAKER["bathroom"]]["action"], "muted")

    def test_presence_decides_an_unmute_when_someone_comes_in(self):
        app = make_app()
        world(app, muted=("bathroom",), present=("bathroom",))
        app._apply_follow_me_mute(SPEAKER["bathroom"], False, room="bathroom")
        self.assertEqual(speakers(app)[SPEAKER["bathroom"]]["why"], "someone in the bathroom")
        self.assertEqual(speakers(app)[SPEAKER["bathroom"]]["action"], "unmuted")

    def test_room_names_read_as_words(self):
        app = make_app()
        self.assertEqual(app._presence_why("guest_bathroom", True), "nobody seen in the guest bathroom")
        self.assertEqual(app._presence_why("living_room", False), "someone in the living room")

    def test_the_kitchen_is_the_kitchen_or_the_hallway(self):
        app = make_app()
        self.assertEqual(app._presence_why("kitchen", True), "nobody seen in the kitchen or hallway")
        self.assertEqual(app._presence_why("kitchen", False), "someone in the kitchen or hallway")

    def test_the_kitchen_alone_when_the_hallway_does_not_count(self):
        app = make_app(args={"special_conditions": {}})
        self.assertEqual(app._presence_why("kitchen", True), "nobody seen in the kitchen")

    def test_a_docked_rooftop_speaker_is_muted_for_the_living_room(self):
        app = make_app()
        world(app)
        self.assertEqual(app._presence_why("rooftop", True), "docked and nobody in the living room")

    def test_a_rooftop_unmute_says_which_rule_unmuted_it(self):
        app = make_app()
        world(app)
        self.assertEqual(app._presence_why("rooftop", False), "off its dock")
        put(app, "binary_sensor.rooftop_charging", "on")
        self.assertEqual(app._presence_why("rooftop", False), "someone in the living room")
        put(app, "input_boolean.rooftop_keep_speaker_on", "on")
        self.assertEqual(app._presence_why("rooftop", False), "keep speaker on is set")

    def test_the_rooftop_mute_goes_through_the_canonical_path_with_its_text(self):
        app = make_app()
        world(app, playing=("kitchen",), grouped=False)
        put(app, ROOFTOP, "playing", group_members=[SPEAKER["kitchen"], ROOFTOP], is_volume_muted=False)
        put(app, SPEAKER["kitchen"], "playing", group_members=[SPEAKER["kitchen"], ROOFTOP], is_volume_muted=False)
        put(app, "binary_sensor.rooftop_charging", "on")
        app._invalidate_coordinator_cache()
        app._apply_follow_me_mute(ROOFTOP, True, room="rooftop")
        self.assertEqual(speakers(app)[ROOFTOP]["why"], "docked and nobody in the living room")

    def test_a_room_that_cannot_be_read_still_gets_a_phrase(self):
        app = make_app()
        app.special = None
        self.assertEqual(app._presence_why("kitchen", True), "presence changed")

    def test_the_phrase_can_never_cost_the_mute(self):
        app = make_app()
        world(app)
        app.speaker_map = None
        app.room_map = None
        app._apply_follow_me_mute(SPEAKER["bathroom"], True)
        self.assertEqual([(c.args, c.kwargs) for c in mute_calls(app)], [muted_call(SPEAKER["bathroom"], True)])
        self.assertEqual(speakers(app)[SPEAKER["bathroom"]]["why"], "presence changed")
        del app.speaker_map
        world(app, muted=("bedroom",), playing=("kitchen", "bedroom"), present=("bedroom",))
        app.call_service.reset_mock()
        app._apply_follow_me_mute(SPEAKER["bedroom"], False)
        self.assertEqual([(c.args, c.kwargs) for c in mute_calls(app)], [muted_call(SPEAKER["bedroom"], False)])

    def test_a_call_without_a_room_takes_it_from_the_speaker_map_and_then_the_entity_name(self):
        app = make_app()
        world(app, playing=("kitchen", "bathroom", "bedroom"))
        app._apply_follow_me_mute(SPEAKER["bathroom"], True)
        self.assertEqual(speakers(app)[SPEAKER["bathroom"]]["why"], "nobody seen in the bathroom")
        app.speaker_map.pop(SPEAKER["bedroom"])
        app._apply_follow_me_mute(SPEAKER["bedroom"], True)
        self.assertEqual(speakers(app)[SPEAKER["bedroom"]]["why"], "nobody seen in the bedroom")

    def test_an_external_unmute_that_is_put_back_is_recorded_as_a_mute_for_the_room(self):
        app = make_app()
        world(app)
        app._preserve_mute_state_if_needed(SPEAKER["bathroom"])
        self.assertEqual([(c.args, c.kwargs) for c in mute_calls(app)], [muted_call(SPEAKER["bathroom"], True)])
        self.assertEqual(speakers(app)[SPEAKER["bathroom"]],
                         {"action": "muted", "at": STAMP, "why": "nobody seen in the bathroom"})

    def test_a_speaker_that_stops_playing_is_unmuted_for_that(self):
        app = make_app()
        world(app, muted=("bathroom",))
        put(app, SPEAKER["bathroom"], "idle", group_members=[], is_volume_muted=True)
        app._on_speaker_state_change(SPEAKER["bathroom"], "state", "playing", "idle", {})
        self.assertEqual([(c.args, c.kwargs) for c in mute_calls(app)], [muted_call(SPEAKER["bathroom"], False)])
        self.assertEqual(speakers(app)[SPEAKER["bathroom"]]["why"], "stopped playing")

    def test_a_speaker_that_leaves_the_group_idle_is_unmuted_for_that(self):
        app = make_app()
        world(app, playing=("kitchen",), muted=("bathroom",), grouped=False)
        app._on_speaker_group_change(SPEAKER["bathroom"], "group_members",
                                     [SPEAKER["kitchen"], SPEAKER["bathroom"]], [SPEAKER["bathroom"]], {})
        self.assertEqual([(c.args, c.kwargs) for c in mute_calls(app)], [muted_call(SPEAKER["bathroom"], False)])
        self.assertEqual(speakers(app)[SPEAKER["bathroom"]]["why"], "left the group")

    def test_the_startup_cleanup_says_so(self):
        app = make_app()
        world(app, playing=("kitchen",), muted=("bathroom", "bedroom", "kitchen"), grouped=False)
        app._check_and_unmute_stale_speakers({})
        self.assertEqual({c.kwargs["entity_id"] for c in mute_calls(app)}, {SPEAKER["bathroom"], SPEAKER["bedroom"]})
        for room in ("bathroom", "bedroom"):
            self.assertEqual(speakers(app)[SPEAKER[room]],
                             {"action": "unmuted", "at": STAMP, "why": "cleanup after an AppDaemon restart"})
        self.assertNotIn(SPEAKER["kitchen"], speakers(app))

    def test_an_airplay_volume_of_zero_is_unmuted_for_that(self):
        app = make_app()
        world(app)
        put(app, SPEAKER["kitchen"], "playing", group_members=[SPEAKER["kitchen"], SPEAKER["bathroom"]],
            is_volume_muted=True, volume_level=0.0)
        app._restore_volume_if_airplay(SPEAKER["kitchen"])
        self.assertEqual([(c.args, c.kwargs) for c in mute_calls(app)], [muted_call(SPEAKER["kitchen"], False)])
        self.assertEqual(speakers(app)[SPEAKER["kitchen"]]["why"], "AirPlay set the volume to 0")
        self.assertTrue(any(c.args[:1] == ("media_player/volume_set",) for c in app.call_service.call_args_list))


class Republishing(unittest.TestCase):
    def test_it_publishes_at_start_so_the_entity_exists(self):
        app = make_app()
        self.assertEqual(len(app.published), 1)
        self.assertEqual((last_published(app)["state"], speakers(app)), ("0", {}))

    def test_it_publishes_again_from_memory_when_home_assistant_comes_back(self):
        app = make_app()
        app._set_mute(SPEAKER["bathroom"], True, "because")
        count = len(app.published)
        self.assertIn(("plugin_started", app._on_plugin_started),
                      [(c.args[1], c.args[0]) for c in app.listen_event.call_args_list])
        app._on_plugin_started("plugin_started", {}, {})
        self.assertEqual(len(app.published), count + 1)
        self.assertEqual(last_published(app)["state"], "1")
        self.assertEqual(speakers(app)[SPEAKER["bathroom"]]["why"], "because")

    def test_the_records_come_back_from_the_entity_after_an_appdaemon_restart(self):
        before = {SPEAKER["bathroom"]: {"action": "muted", "at": STAMP, "why": "nobody seen in the bathroom"},
                  SPEAKER["kitchen"]: {"action": "unmuted", "at": STAMP, "why": "someone in the kitchen or hallway"}}
        app = make_app(published=before)
        self.assertEqual(speakers(app), before)
        self.assertEqual(last_published(app)["state"], "1")
        app._set_mute(SPEAKER["bedroom"], True, "new")
        self.assertEqual(set(speakers(app)), {SPEAKER["bathroom"], SPEAKER["kitchen"], SPEAKER["bedroom"]})
        self.assertEqual(last_published(app)["state"], "2")

    def test_records_that_are_not_records_are_left_behind(self):
        junk = {SPEAKER["bathroom"]: {"action": "muted", "at": STAMP, "why": "ok"},
                SPEAKER["kitchen"]: {"action": "paused", "at": STAMP, "why": "no"},
                SPEAKER["bedroom"]: {"action": "muted", "at": 5, "why": "no"},
                SPEAKER["living_room"]: {"action": "muted", "at": STAMP},
                SPEAKER["guest_bathroom"]: "muted",
                7: {"action": "muted", "at": STAMP, "why": "no"}}
        self.assertEqual(set(speakers(make_app(published=junk))), {SPEAKER["bathroom"]})
        for not_a_dict in ("muted", ["x"], None, 5):
            with self.subTest(published=not_a_dict):
                self.assertEqual(speakers(make_app(published=not_a_dict)), {})


class OneCallSite(unittest.TestCase):
    def test_volume_mute_is_called_in_exactly_one_place(self):
        source = (Path(__file__).resolve().parents[1] / "follow_me.py").read_text()
        self.assertEqual(len(re.findall(r'call_service\(\s*"media_player/volume_mute"', source)), 1)
        self.assertEqual(len(re.findall(r"self\._set_mute\(", source)), 6)

    def test_the_status_entity_is_in_the_yaml(self):
        text = (Path(__file__).resolve().parents[1] / "follow_me.yaml").read_text()
        self.assertRegex(text, r"(?m)^  status_entity: sensor\.follow_me\s*$")


if __name__ == "__main__":
    unittest.main()
