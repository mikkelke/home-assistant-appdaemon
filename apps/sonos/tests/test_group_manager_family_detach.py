from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_group_manager_wedge import gm, make_app  # noqa: E402

LIVING, KITCHEN, DINING, ROOFTOP = (
    "media_player.living_room", "media_player.kitchen", "media_player.dining_room", "media_player.rooftop",
)
CLAUDIA_ROOM, BEDROOM = "media_player.claudias_room", "media_player.bedroom"
CLAUDIA_UID = "c93da325aaaa4bbbbccccddddeeeeffff"
HOUSE_UID = "f4fda494358943beaf8ad2c70db099f8"


class FakeAttribution:
    house_user_ids = {HOUSE_UID}


def make_detach_app():
    app = make_app()
    app.speakers = [LIVING, KITCHEN, DINING, ROOFTOP, CLAUDIA_ROOM, BEDROOM]
    app.family_zone_speakers = [LIVING, KITCHEN, DINING]
    app.rooftop_entity = ROOFTOP
    app.rooftop_charging_sensor = "binary_sensor.rooftop_charging"
    app.family_zone_auto_group_entity = "input_boolean.auto"
    app._family_zone_sync_timeout_s = 15
    app._family_detached = {}
    app._family_detach_handle = None
    app._prev_group_members = {}
    app._pending_unjoin_checks = set()
    app.lock = gm.threading.Lock()
    app.now = datetime(2026, 11, 18, 15, 38, 48)
    app.datetime = lambda: app.now
    app.states = {
        "input_boolean.auto": {"state": "on", "attributes": {}},
        "binary_sensor.rooftop_charging": {"state": "on", "attributes": {}},
    }
    for sp in app.speakers:
        app.states[sp] = {"state": "idle", "attributes": {"group_members": []}}

    def get_state(entity, attribute=None):
        s = app.states.get(entity)
        if s is None:
            return None
        if attribute == "all":
            return s
        if attribute:
            return s["attributes"].get(attribute)
        return s["state"]

    app.get_state = get_state
    app.get_app = lambda name: FakeAttribution()
    app.queued = []

    def queue(op, master, members, priority="normal"):
        app.queued.append((op, master, sorted(members) if isinstance(members, list) else members))

    app._queue_group_operation = queue
    app._last_group = {}
    return app


def group(app, members, state="playing"):
    for sp in members:
        app.states[sp] = {"state": state, "attributes": {"group_members": list(members)}}
        app._last_group[sp] = list(members)


def call(service, user_id, **service_data):
    return {
        "domain": "media_player",
        "service": service,
        "service_data": service_data,
        "metadata": {"context": {"user_id": user_id, "parent_id": None}},
    }


def last_round(app):
    for cb, delay, kw, handle in reversed(app.scheduled):
        if cb == app._family_detach_round:
            return cb
    return None


class FamilyDetachTest(unittest.TestCase):
    def test_person_removing_a_family_speaker_from_a_mixed_group_marks_the_others(self):
        app = make_detach_app()
        group(app, [KITCHEN, CLAUDIA_ROOM, LIVING, DINING, ROOFTOP])
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id=[KITCHEN]))
        self.assertEqual(app._family_detached_now(), {CLAUDIA_ROOM})
        self.assertIsNotNone(last_round(app))

    def test_round_unjoins_followers_first_and_the_family_coordinator_last(self):
        app = make_detach_app()
        group(app, [KITCHEN, CLAUDIA_ROOM, LIVING, DINING, ROOFTOP])
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id=[KITCHEN]))
        group(app, [DINING, CLAUDIA_ROOM, LIVING, ROOFTOP])  # MA re-formed around dining
        app._family_detach_round()
        self.assertEqual(app.queued, [("unjoin", None, [LIVING, ROOFTOP])])
        group(app, [DINING, CLAUDIA_ROOM])
        app._family_detach_round()
        self.assertEqual(app.queued[-1], ("unjoin", None, [DINING]))

    def test_non_family_coordinator_keeps_playing_while_all_family_leave(self):
        app = make_detach_app()
        group(app, [BEDROOM, LIVING, KITCHEN, DINING])
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id=[LIVING]))
        app._family_detach_round()
        self.assertEqual(app.queued, [("unjoin", None, [DINING, KITCHEN, LIVING])])

    def test_family_only_group_is_left_to_the_normal_rejoin(self):
        app = make_detach_app()
        group(app, [LIVING, KITCHEN, DINING, ROOFTOP])
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id=[KITCHEN]))
        self.assertEqual(app._family_detached, {})
        app.states[KITCHEN] = {"state": "idle", "attributes": {"group_members": []}}
        group(app, [LIVING, DINING, ROOFTOP])
        app._check_family_zone_synchronization()
        self.assertEqual(app.queued, [("join", LIVING, [KITCHEN])])

    def test_house_and_userless_unjoins_change_nothing(self):
        for uid in (HOUSE_UID, None):
            app = make_detach_app()
            group(app, [KITCHEN, CLAUDIA_ROOM, LIVING, DINING])
            app._on_family_unjoin_call(call("unjoin", uid, entity_id=[KITCHEN]))
            self.assertEqual(app._family_detached, {})

    def test_unjoin_of_an_already_solo_speaker_is_ignored(self):
        app = make_detach_app()
        group(app, [CLAUDIA_ROOM, LIVING])
        app._last_group[KITCHEN] = []
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id="media_player.kitchen"))
        self.assertEqual(app._family_detached, {})

    def test_auto_group_skips_the_detached_group_until_the_mark_expires(self):
        app = make_detach_app()
        group(app, [CLAUDIA_ROOM, LIVING, DINING, KITCHEN])
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id=[ROOFTOP]))
        self.assertEqual(app._family_detached, {})  # rooftop was not in that group
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id=[KITCHEN]))
        app.states[KITCHEN] = {"state": "idle", "attributes": {"group_members": []}}
        group(app, [CLAUDIA_ROOM, LIVING, DINING])
        app._family_zone_sync_in_progress = False
        app._check_family_zone_synchronization()
        self.assertNotIn("join", [q[0] for q in app.queued])
        app.now += timedelta(seconds=gm.FAMILY_DETACH_S + 1)
        app._family_zone_sync_in_progress = False
        app._check_family_zone_synchronization()
        self.assertEqual(app.queued[-1][0], "join")

    def test_person_adding_a_family_speaker_lets_the_rest_be_pulled_in(self):
        app = make_detach_app()
        group(app, [KITCHEN, CLAUDIA_ROOM, LIVING])
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id=[KITCHEN]))
        app._on_family_join_call(call("join", HOUSE_UID, entity_id=[CLAUDIA_ROOM], group_members=[KITCHEN]))
        self.assertEqual(app._family_detached_now(), {CLAUDIA_ROOM})
        app._on_family_join_call(call("join", CLAUDIA_UID, entity_id=[CLAUDIA_ROOM], group_members=[KITCHEN]))
        self.assertEqual(app._family_detached, {})
        app.states[LIVING] = {"state": "idle", "attributes": {"group_members": []}}
        group(app, [CLAUDIA_ROOM, KITCHEN])
        app._check_family_zone_synchronization()
        self.assertEqual(app.queued[-1], ("join", CLAUDIA_ROOM, [DINING, LIVING, ROOFTOP]))

    def test_queued_auto_group_joins_are_dropped_at_the_removal(self):
        app = make_detach_app()
        group(app, [KITCHEN, CLAUDIA_ROOM, LIVING, DINING])
        app._group_operation_queue = [
            {"type": "join", "target": KITCHEN, "members": [ROOFTOP], "priority": "normal"},
            {"type": "join", "target": KITCHEN, "members": [BEDROOM], "priority": "high"},
        ]
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id=[KITCHEN]))
        self.assertEqual([op["members"] for op in app._group_operation_queue], [[BEDROOM]])

    def test_group_change_on_a_detached_speaker_with_family_schedules_a_round(self):
        app = make_detach_app()
        group(app, [KITCHEN, CLAUDIA_ROOM, LIVING])
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id=[KITCHEN]))
        app.scheduled.clear()
        app._should_follow_me_be_active = lambda: False
        app._maybe_trigger = lambda *a, **k: None
        app._on_group_change(CLAUDIA_ROOM, "group_members", [], [LIVING, CLAUDIA_ROOM], {})
        self.assertIsNotNone(last_round(app))
        self.assertEqual(app._last_group[CLAUDIA_ROOM], [LIVING, CLAUDIA_ROOM])

    def test_reset_clears_the_mark(self):
        app = make_detach_app()
        group(app, [KITCHEN, CLAUDIA_ROOM, LIVING])
        app._on_family_unjoin_call(call("unjoin", CLAUDIA_UID, entity_id=[KITCHEN]))
        app.fire_event = lambda *a, **k: None
        app._on_reset_requested("sonos_reset_requested", {}, {})
        self.assertEqual(app._family_detached, {})


if __name__ == "__main__":
    unittest.main()
