"""AssistRooms: group, move and ungroup speakers by voice through GroupManager events."""

from __future__ import annotations

import copy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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

import assist_rooms as ar  # noqa: E402

AREAS = ("bathroom", "bedroom", "kitchen", "living_room", "dining_room", "guest_bathroom",
         "rooftop")
SPEAKERS = {area: f"media_player.{area}" for area in AREAS}
BATH, BED, KIT, LIV, DIN, GUEST, ROOF = (SPEAKERS[a] for a in AREAS)


def snap(value="playing", group=()):
    return {"state": value, "attributes": {"group_members": list(group)}}


def join(master, speaker):
    return {"event": "request_sonos_group_join",
            "data": {"master_entity_id": master, "slave_entity_id": speaker}}


def unjoin(speaker):
    return {"event": "request_sonos_group_unjoin", "data": {"entity_id": speaker}}


class JoinDecisions(unittest.TestCase):
    def test_solo_source_gets_every_target(self):
        states = {BED: snap(), KIT: snap("idle"), LIV: snap("idle")}
        self.assertEqual(ar.group_actions("join", [BED], [KIT, LIV], states),
                         [join(BED, KIT), join(BED, LIV)])

    def test_grouped_source_joins_through_its_leader(self):
        states = {BED: snap(group=[KIT, BED]), LIV: snap("idle")}
        self.assertEqual(ar.group_actions("join", [BED], [LIV], states), [join(KIT, LIV)])

    def test_source_itself_and_targets_already_in_its_group_are_skipped(self):
        states = {BED: snap(group=[BED, KIT]), KIT: snap(group=[BED, KIT]), LIV: snap("idle")}
        self.assertEqual(ar.group_actions("join", [BED], [BED, KIT, LIV], states),
                         [join(BED, LIV)])

    def test_source_never_joins_itself_even_if_its_group_list_omits_it(self):
        states = {BED: snap(group=[KIT, LIV]), DIN: snap("idle")}
        self.assertEqual(ar.group_actions("join", [BED], [BED, DIN], states), [join(KIT, DIN)])

    def test_everyone_already_together_does_nothing(self):
        group = [BED, KIT, LIV]
        states = {room: snap(group=group) for room in group}
        self.assertEqual(ar.group_actions("join", [BED], [KIT, LIV], states), [])

    def test_named_source_wins_when_both_play(self):
        states = {BED: snap(), KIT: snap()}
        self.assertEqual(ar.group_actions("join", [BED], [KIT], states), [join(BED, KIT)])

    def test_playing_target_is_the_source_when_the_named_source_is_silent(self):
        states = {BED: snap("idle"), LIV: snap(group=[LIV, KIT]), KIT: snap(group=[LIV, KIT])}
        self.assertEqual(ar.group_actions("join", [BED], [LIV], states), [join(LIV, BED)])

    def test_multi_room_source_uses_first_playing_room_and_pulls_the_rest(self):
        states = {LIV: snap("idle"), KIT: snap(), DIN: snap("idle"), BED: snap("idle")}
        self.assertEqual(ar.group_actions("join", [LIV, KIT, DIN], [BED], states),
                         [join(KIT, LIV), join(KIT, DIN), join(KIT, BED)])

    def test_nothing_playing_does_nothing(self):
        for value in ("idle", "paused", "off", "unavailable", "unknown", None):
            with self.subTest(value=value):
                states = {BED: snap(value), KIT: snap(value)}
                self.assertEqual(ar.group_actions("join", [BED], [KIT], states), [])
        self.assertEqual(ar.group_actions("join", [BED], [KIT], {}), [])

    def test_rooms_named_twice_join_once(self):
        states = {BED: snap(), KIT: snap("idle")}
        self.assertEqual(ar.group_actions("join", [BED, BED], [KIT, KIT, BED], states),
                         [join(BED, KIT)])

    def test_empty_or_malformed_group_members_count_as_solo(self):
        for members in (None, [], [BED], "x", [None, 3]):
            with self.subTest(members=members):
                states = {BED: {"state": "playing", "attributes": {"group_members": members}},
                          KIT: snap("idle")}
                self.assertEqual(ar.group_actions("join", [BED], [KIT], states),
                                 [join(BED, KIT)])
        states = {BED: {"state": "playing"}, KIT: snap("idle")}
        self.assertEqual(ar.group_actions("join", [BED], [KIT], states), [join(BED, KIT)])

    def test_unknown_mode_does_nothing(self):
        for mode in ("leave", "", None):
            self.assertEqual(ar.group_actions(mode, [BED], [KIT], {BED: snap(), KIT: snap()}), [])

    def test_decisions_do_not_mutate_inputs(self):
        states = {BED: snap(group=[BED, KIT]), KIT: snap(group=[BED, KIT]), LIV: snap("idle")}
        before, sources, targets = copy.deepcopy(states), [BED], [LIV, KIT]
        ar.group_actions("join", sources, targets, states)
        ar.group_actions("move", sources, targets, states)
        ar.group_actions("ungroup", sources, targets, states)
        ar.move_leave_actions(sources, targets, states)
        self.assertEqual((states, sources, targets), (before, [BED], [LIV, KIT]))


class MoveDecisions(unittest.TestCase):
    def test_targets_join_without_the_source_rooms(self):
        states = {BED: snap(), KIT: snap("idle")}
        self.assertEqual(ar.group_actions("move", [BED], [BED, KIT], states), [join(BED, KIT)])

    def test_move_never_swaps_in_a_playing_target(self):
        states = {BED: snap("idle"), LIV: snap(), KIT: snap("idle")}
        self.assertEqual(ar.group_actions("move", [BED], [LIV, KIT], states), [])

    def test_move_skips_targets_already_in_the_group(self):
        group = [BED, KIT]
        states = {BED: snap(group=group), KIT: snap(group=group), LIV: snap("idle")}
        self.assertEqual(ar.group_actions("move", [BED], [KIT, LIV], states), [join(BED, LIV)])

    def test_move_joins_through_the_leader_when_the_source_follows(self):
        states = {BED: snap(group=[LIV, BED]), KIT: snap("idle")}
        self.assertEqual(ar.group_actions("move", [BED], [KIT], states), [join(LIV, KIT)])

    def test_leave_waits_until_every_target_shows_in_the_group(self):
        pending = {BED: snap(group=[BED, KIT])}
        self.assertIsNone(ar.move_leave_actions([BED], [KIT, LIV], pending))
        self.assertIsNone(ar.move_leave_actions([BED], [KIT], {BED: snap()}))

    def test_leader_leaves_last(self):
        group = [KIT, LIV, DIN, BED]
        states = {room: snap(group=group) for room in (LIV, KIT, DIN)}
        self.assertEqual(ar.move_leave_actions([LIV, KIT, DIN], [BED], states),
                         [unjoin(LIV), unjoin(DIN), unjoin(KIT)])

    def test_only_source_rooms_inside_the_group_leave(self):
        states = {BED: snap(group=[BED, KIT, LIV]), DIN: snap("idle")}
        self.assertEqual(ar.move_leave_actions([BED, DIN], [KIT], states), [unjoin(BED)])

    def test_targets_that_were_already_there_let_the_source_leave_at_once(self):
        group = [BED, KIT]
        states = {BED: snap(group=group)}
        self.assertEqual(ar.move_leave_actions([BED], [KIT], states), [unjoin(BED)])

    def test_no_target_or_silent_source_means_nobody_leaves(self):
        group = [BED, KIT]
        playing = {BED: snap(group=group)}
        self.assertEqual(ar.move_leave_actions([BED], [], playing), [])
        self.assertEqual(ar.move_leave_actions([BED], [BED], playing), [])
        self.assertEqual(ar.move_leave_actions([BED], [KIT], {BED: snap("paused", group)}), [])
        self.assertEqual(ar.move_leave_actions([BED], [KIT], {}), [])


class UngroupDecisions(unittest.TestCase):
    def test_followers_leave_and_leaders_keep_playing(self):
        group = [BED, KIT, LIV]
        states = {BATH: snap("idle"), BED: snap(group=group), KIT: snap(group=group),
                  LIV: snap(group=group), DIN: snap(group=[DIN])}
        self.assertEqual(ar.group_actions("ungroup", [], [], states), [unjoin(KIT), unjoin(LIV)])

    def test_separate_groups_all_split(self):
        states = {BED: snap(group=[BED, KIT]), KIT: snap(group=[BED, KIT]),
                  DIN: snap(group=[DIN, ROOF]), ROOF: snap(group=[DIN, ROOF])}
        self.assertEqual(ar.group_actions("ungroup", [], [], states), [unjoin(KIT), unjoin(ROOF)])

    def test_nothing_grouped_does_nothing(self):
        states = {room: snap(group=[]) for room in (BED, KIT)}
        self.assertEqual(ar.group_actions("ungroup", [], [], states), [])
        self.assertEqual(ar.group_actions("ungroup", [], [], {}), [])


def make_app(states=None, speakers=SPEAKERS):
    app = ar.AssistRooms.__new__(ar.AssistRooms)
    app.args = {"satellites": {}, "speakers": dict(speakers), "answer_sensors": []}
    app.states = states if states is not None else {}

    def get_state(entity, attribute=None):
        full = app.states.get(entity, {})
        if attribute == "all":
            return full
        if attribute:
            return full.get("attributes", {}).get(attribute)
        return full.get("state")

    app.get_state = get_state
    app.log = MagicMock()
    app.call_service = MagicMock()
    app.fire_event = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_in = MagicMock()
    app.initialize()
    return app


def request(app, mode, source=None, targets=None):
    app._on_group_intent("assist_music_group",
                         {"mode": mode, "source_area": source, "target_areas": targets}, {})


def fired(app):
    return [(call.args[0], call.kwargs) for call in app.fire_event.call_args_list]


def joined(master, speaker):
    return ("request_sonos_group_join", {"master_entity_id": master, "slave_entity_id": speaker})


def left(speaker):
    return ("request_sonos_group_unjoin", {"entity_id": speaker})


class GroupGlue(unittest.TestCase):
    def test_listens_for_the_group_event(self):
        app = make_app()
        self.assertIn("assist_music_group", {call.args[1] for call in app.listen_event.call_args_list})

    def test_join_sends_one_request_per_target_with_the_leader_as_master(self):
        app = make_app({BED: snap(), KIT: snap("idle"), LIV: snap("idle")})
        request(app, "join", ["bedroom"], ["kitchen", "living_room"])
        self.assertEqual(fired(app), [joined(BED, KIT), joined(BED, LIV)])
        app.call_service.assert_not_called()
        app.run_in.assert_not_called()

    def test_areas_may_arrive_as_a_comma_separated_string(self):
        app = make_app({KIT: snap(), LIV: snap("idle"), DIN: snap("idle")})
        request(app, "join", "kitchen", "living_room, dining_room")
        self.assertEqual(fired(app), [joined(KIT, LIV), joined(KIT, DIN)])

    def test_family_room_as_source_pulls_every_family_speaker(self):
        app = make_app({LIV: snap("idle"), KIT: snap("idle"), DIN: snap(), BED: snap("idle")})
        request(app, "join", ["living_room", "kitchen", "dining_room"], ["bedroom"])
        self.assertEqual(fired(app), [joined(DIN, LIV), joined(DIN, KIT), joined(DIN, BED)])

    def test_rooms_without_a_speaker_are_dropped_with_a_warning(self):
        app = make_app({BED: snap(), KIT: snap("idle")})
        request(app, "join", ["bedroom"], ["hallway", "spare_room", "kitchen", ""])
        self.assertEqual(fired(app), [joined(BED, KIT)])
        warned = [call.args[0] for call in app.log.call_args_list
                  if call.kwargs.get("level") == "WARNING"]
        self.assertEqual(len(warned), 2)
        self.assertTrue(any("hallway" in text for text in warned))

    def test_only_unknown_targets_do_nothing(self):
        app = make_app({BED: snap()})
        request(app, "join", ["bedroom"], ["hallway"])
        app.fire_event.assert_not_called()

    def test_nothing_playing_sends_nothing(self):
        app = make_app({BED: snap("idle"), KIT: snap("idle")})
        request(app, "join", ["bedroom"], ["kitchen"])
        app.fire_event.assert_not_called()
        app.run_in.assert_not_called()

    def test_missing_lists_do_nothing(self):
        app = make_app({BED: snap()})
        for source, targets in ((None, None), ([], []), ("", ""), (7, {"a": 1})):
            request(app, "join", source, targets)
            request(app, "move", source, targets)
        app.fire_event.assert_not_called()

    def test_unknown_mode_warns_and_does_nothing(self):
        for mode in ("leave", None, ""):
            app = make_app({BED: snap(), KIT: snap("idle")})
            request(app, mode, ["bedroom"], ["kitchen"])
            app.fire_event.assert_not_called()
            self.assertTrue(any(call.kwargs.get("level") == "WARNING"
                                for call in app.log.call_args_list))

    def test_ungroup_splits_every_speaker_that_follows_a_leader(self):
        group = [BED, KIT, LIV, BATH]
        app = make_app({BED: snap(group=group), KIT: snap(group=group), LIV: snap(group=group),
                        BATH: snap(group=group), DIN: snap("idle"), GUEST: snap("idle")})
        request(app, "ungroup")
        self.assertEqual(fired(app), [left(BATH), left(KIT), left(LIV)])

    def test_ungroup_leaves_unlisted_speakers_alone(self):
        outsider = "media_player.spare_room"
        group = [BED, KIT, outsider]
        app = make_app({BED: snap(group=group), KIT: snap(group=group),
                        outsider: snap(group=group)})
        request(app, "ungroup")
        self.assertEqual(fired(app), [left(KIT)])

    def test_ungroup_with_nothing_grouped_sends_nothing(self):
        app = make_app({BED: snap(), KIT: snap("idle")})
        request(app, "ungroup")
        app.fire_event.assert_not_called()


class MoveGlue(unittest.TestCase):
    def test_source_leaves_only_after_the_targets_show_in_the_group(self):
        app = make_app({BED: snap(), KIT: snap("idle")})
        request(app, "move", ["bedroom"], ["kitchen"])
        self.assertEqual(fired(app), [joined(BED, KIT)])
        timer = app.run_in.call_args
        self.assertEqual(timer.args[1], ar.MOVE_POLL_S)
        app.fire_event.reset_mock()
        timer.args[0](timer.kwargs)
        app.fire_event.assert_not_called()
        timer = app.run_in.call_args
        app.states[BED] = snap(group=[BED, KIT])
        timer.args[0](timer.kwargs)
        self.assertEqual(fired(app), [left(BED)])
        self.assertEqual(app.run_in.call_count, 2)

    def test_targets_already_in_the_group_let_the_source_leave_at_once(self):
        group = [BED, KIT]
        app = make_app({BED: snap(group=group), KIT: snap(group=group)})
        request(app, "move", ["bedroom"], ["kitchen"])
        self.assertEqual(fired(app), [left(BED)])
        app.run_in.assert_not_called()

    def test_family_room_leaves_leader_last_once_the_target_joined(self):
        group = [KIT, LIV, DIN, BED]
        app = make_app({KIT: snap(group=group), LIV: snap(group=group), DIN: snap(group=group),
                        BED: snap(group=group)})
        request(app, "move", ["living_room", "kitchen", "dining_room"], ["bedroom"])
        self.assertEqual(fired(app), [left(LIV), left(DIN), left(KIT)])

    def test_gives_up_with_a_warning_and_keeps_the_source_in_place(self):
        app = make_app({BED: snap(), KIT: snap("idle")})
        request(app, "move", ["bedroom"], ["kitchen"])
        app.fire_event.reset_mock()
        app.log.reset_mock()
        for _ in range(ar.MOVE_GIVE_UP_S // ar.MOVE_POLL_S + 1):
            timer = app.run_in.call_args
            if timer.kwargs["waited"] >= ar.MOVE_GIVE_UP_S:
                break
            timer.args[0](timer.kwargs)
        waits = [call.kwargs["waited"] for call in app.run_in.call_args_list]
        self.assertEqual(waits, list(range(ar.MOVE_POLL_S, ar.MOVE_GIVE_UP_S + 1, ar.MOVE_POLL_S)))
        timer = app.run_in.call_args
        timer.args[0](timer.kwargs)
        self.assertEqual(app.run_in.call_count, len(waits))
        app.fire_event.assert_not_called()
        self.assertTrue(any(call.kwargs.get("level") == "WARNING"
                            for call in app.log.call_args_list))

    def test_silent_source_moves_nothing_and_does_not_poll(self):
        app = make_app({BED: snap("idle"), KIT: snap()})
        request(app, "move", ["bedroom"], ["kitchen"])
        app.fire_event.assert_not_called()
        app.run_in.assert_not_called()

    def test_moving_a_room_onto_itself_does_not_make_it_leave(self):
        app = make_app({BED: snap(group=[BED, KIT]), KIT: snap(group=[BED, KIT])})
        request(app, "move", ["bedroom"], ["bedroom"])
        app.fire_event.assert_not_called()
        app.run_in.assert_not_called()


if __name__ == "__main__":
    unittest.main()
