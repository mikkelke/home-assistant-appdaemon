"""ResidentRoomLightsOff: a resident's room lights go off once the room has stood empty for the
vacancy hold while the owner is away - never while the room reads active or unreadable, never
while the owner is home, and never straight from initialize."""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

_LIGHTS_DIR = Path(__file__).resolve().parents[1]
if str(_LIGHTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTS_DIR))

# resident_room_lights_off imports room_active_read, which lives in apps/presence
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

import resident_room_lights_off as rrlo  # noqa: E402

OWNER = "person.claudia"
ACTIVE = "binary_sensor.claudias_room_active"
CEILING = "light.claudias_room_ceiling_lights"
STRING = "light.claudias_room_string_lights"
HOLD_MIN = 60
HOLD = HOLD_MIN * 60
MIN = 60
LONG = 12 * 3600

ARGS = {
    "vacancy_hold_min": HOLD_MIN,
    "rooms": {
        "claudias_room": {
            "owner": OWNER,
            "name": "Claudia",
            "room_label": "her room",
            "lights": [CEILING, STRING],
        },
    },
}

# Ways the room-active entity can fail to say "empty" for sure.
UNREADABLE = (
    ("unavailable", {"state": "unavailable"}),
    ("unknown", {"state": "unknown"}),
    ("missing", {"state": None}),
    ("witness stale", {"state": "off", "stale": "true"}),
)


class House:
    """Fake HA state machine plus the AppDaemon primitives the app uses: state listeners that
    fire like AppDaemon's (plain = state changes only, attribute="all" = every update) and a
    run_in scheduler on a fake clock."""

    def __init__(self):
        self.now = 0.0
        self.store = {}
        self.listeners = []
        self.pending = []
        self.seq = 0
        self.turned_off = []
        self.events = []
        self.logs = []

    def full(self, entity):
        item = self.store.get(entity)
        if item is None:
            return None
        return {"entity_id": entity, "state": item["state"], "attributes": dict(item["attributes"])}

    def set(self, entity, state, notify=True, **attributes):
        old = self.full(entity)
        if state is None:
            self.store.pop(entity, None)
        else:
            self.store[entity] = {"state": state, "attributes": attributes}
        if notify:
            self.notify(entity, old, self.full(entity))

    def notify(self, entity, old, new):
        for callback, listened, attribute, kwargs in list(self.listeners):
            if listened != entity:
                continue
            if attribute == "all":
                callback(entity, attribute, old, new, kwargs)
                continue
            old_state = old["state"] if old else None
            new_state = new["state"] if new else None
            if old_state != new_state:
                callback(entity, "state", old_state, new_state, kwargs)

    def room(self, state, stale="false", notify=True):
        """Publish the room-active entity the way RoomActive does (fresh computed_at)."""
        if state is None:
            self.set(ACTIVE, None, notify=notify)
            return
        self.set(ACTIVE, state, notify=notify, stale=stale, computed_at=datetime.now(timezone.utc).isoformat())

    def room_unreadable(self, how):
        self.room(how["state"], stale=how.get("stale", "false"))

    def advance(self, seconds):
        end = self.now + seconds
        while True:
            due = [p for p in self.pending if p[0] <= end]
            if not due:
                break
            item = min(due, key=lambda p: (p[0], p[1]))
            self.pending.remove(item)
            self.now = item[0]
            item[2](item[3])
        self.now = end

    def heartbeat(self, minutes, every=2):
        """Advance with RoomActive's periodic republish of an unchanged payload."""
        for _ in range(int(minutes // every)):
            self.advance(every * MIN)
            current = self.full(ACTIVE)
            if current is not None:
                self.room(current["state"], stale=current["attributes"].get("stale", "false"))

    def off_times(self, entity=None):
        return [t for t, e in self.turned_off if entity is None or e == entity]


def make_app(house, args=ARGS):
    app = rrlo.ResidentRoomLightsOff.__new__(rrlo.ResidentRoomLightsOff)
    app.args = args

    def get_state(entity, attribute=None, **kwargs):
        full = house.full(entity)
        if full is None:
            return None
        if attribute == "all":
            return full
        if attribute is None:
            return full["state"]
        return full["attributes"].get(attribute)

    def listen_state(callback, entity, attribute=None, **kwargs):
        house.listeners.append((callback, entity, attribute, kwargs))

    def run_in(callback, delay, **kwargs):
        house.seq += 1
        house.pending.append((house.now + delay, house.seq, callback, kwargs))
        return house.seq

    def cancel_timer(handle):
        house.pending = [p for p in house.pending if p[1] != handle]

    def turn_off(entity, **kwargs):
        house.turned_off.append((house.now, entity))
        house.set(entity, "off")

    app.get_state = get_state
    app.get_app = lambda name: None
    app.listen_state = listen_state
    app.run_in = run_in
    app.cancel_timer = cancel_timer
    app.turn_off = turn_off
    app.fire_event = lambda event, **data: house.events.append((event, data))
    app.log = lambda msg, level="INFO", **kwargs: house.logs.append((level, msg))
    app.initialize()
    return app


def claudia_home_in_her_room(ceiling="on", string="off"):
    house = House()
    house.set(OWNER, "home")
    house.room("on")
    house.set(CEILING, ceiling)
    house.set(STRING, string)
    make_app(house)
    return house


class OwnerLeavesEmptyRoom(unittest.TestCase):
    def test_off_after_the_hold_from_the_departure(self):
        house = claudia_home_in_her_room()
        house.room("off")
        house.heartbeat(10)
        house.set(OWNER, "not_home")
        left = house.now
        house.heartbeat(HOLD_MIN - 2)
        house.advance(2 * MIN - 1)
        self.assertEqual(house.turned_off, [])
        house.advance(1)
        self.assertEqual(house.turned_off, [(left + HOLD, CEILING)])

    def test_away_zone_name_counts_as_away(self):
        house = claudia_home_in_her_room()
        house.room("off")
        house.set(OWNER, "Work")
        house.advance(HOLD)
        self.assertEqual(house.off_times(CEILING), [HOLD])

    def test_reports_to_the_feed_for_her_and_admins(self):
        house = claudia_home_in_her_room()
        house.room("off")
        house.set(OWNER, "not_home")
        house.advance(HOLD)
        self.assertEqual(
            house.events,
            [(
                "house_events_report",
                {
                    "cause": "Claudia left - her room light was on",
                    "effect": "Turned it off",
                    "icon": "mdi:lightbulb-off",
                    "audience_users": ["Claudia"],
                },
            )],
        )

    def test_two_lights_on_both_off_and_plural_wording(self):
        house = claudia_home_in_her_room(string="on")
        house.room("off")
        house.set(OWNER, "not_home")
        house.advance(HOLD)
        self.assertEqual(sorted(e for _, e in house.turned_off), [CEILING, STRING])
        data = house.events[0][1]
        self.assertEqual(data["cause"], "Claudia left - her room lights were on")
        self.assertEqual(data["effect"], "Turned them off")

    def test_nothing_on_means_no_action_and_no_report(self):
        house = claudia_home_in_her_room(ceiling="off")
        house.room("off")
        house.set(OWNER, "not_home")
        house.advance(LONG)
        self.assertEqual(house.turned_off, [])
        self.assertEqual(house.events, [])

    def test_one_hold_per_away_and_empty_stretch(self):
        house = claudia_home_in_her_room()
        house.room("off")
        house.set(OWNER, "not_home")
        house.heartbeat(HOLD_MIN + 2)
        self.assertEqual(len(house.turned_off), 1)
        house.set(CEILING, "on")
        house.heartbeat(LONG // MIN)
        self.assertEqual(len(house.turned_off), 1)
        self.assertEqual(len(house.events), 1)


class FriendStaysBehind(unittest.TestCase):
    def test_off_a_hold_after_the_friend_leaves(self):
        house = claudia_home_in_her_room()
        house.set(OWNER, "not_home")
        house.heartbeat(90)
        self.assertEqual(house.turned_off, [])
        house.room("off")
        friend_left = house.now
        house.advance(HOLD - 1)
        self.assertEqual(house.turned_off, [])
        house.advance(1)
        self.assertEqual(house.off_times(CEILING), [friend_left + HOLD])

    def test_friend_who_never_leaves_keeps_the_light(self):
        house = claudia_home_in_her_room()
        house.set(OWNER, "not_home")
        house.heartbeat(LONG // MIN)
        self.assertEqual(house.turned_off, [])


class FalseDrop(unittest.TestCase):
    def test_drop_shorter_than_the_hold_never_turns_off(self):
        house = claudia_home_in_her_room()
        house.set(OWNER, "not_home")
        house.heartbeat(20)
        house.room("off")
        house.heartbeat(HOLD_MIN - 2)
        house.room("on")
        house.heartbeat(LONG // MIN)
        self.assertEqual(house.turned_off, [])

    def test_repeated_drops_each_restart_the_hold(self):
        house = claudia_home_in_her_room()
        house.set(OWNER, "not_home")
        for _ in range(5):
            house.room("off")
            house.advance(HOLD - 5 * MIN)
            house.room("on")
            house.advance(MIN)
        self.assertEqual(house.turned_off, [])
        house.room("off")
        last_drop = house.now
        house.advance(HOLD)
        self.assertEqual(house.off_times(CEILING), [last_drop + HOLD])

    def test_callback_saying_active_wins_over_a_store_still_saying_off(self):
        house = claudia_home_in_her_room()
        house.room("off")
        house.set(OWNER, "not_home")
        house.advance(10 * MIN)
        old = house.full(ACTIVE)
        new = dict(old, state="on")
        house.notify(ACTIVE, old, new)
        house.advance(LONG)
        self.assertEqual(house.turned_off, [])


class OwnerComesHome(unittest.TestCase):
    def test_return_before_the_hold_cancels_it(self):
        house = claudia_home_in_her_room()
        house.room("off")
        house.set(OWNER, "not_home")
        house.advance(HOLD - MIN)
        house.set(OWNER, "home")
        self.assertEqual(house.pending, [])
        house.advance(LONG)
        self.assertEqual(house.turned_off, [])

    def test_tracker_blip_never_turns_off(self):
        house = claudia_home_in_her_room()
        house.room("off")
        house.set(OWNER, "not_home")
        house.advance(30)
        house.set(OWNER, "home")
        house.advance(LONG)
        self.assertEqual(house.turned_off, [])

    def test_unknown_owner_counts_as_not_away(self):
        house = claudia_home_in_her_room()
        house.room("off")
        house.set(OWNER, "not_home")
        house.advance(HOLD - MIN)
        house.set(OWNER, "unknown")
        house.advance(2 * MIN)
        self.assertEqual(house.turned_off, [])
        house.set(OWNER, "not_home")
        back_away = house.now
        house.advance(HOLD)
        self.assertEqual(house.off_times(CEILING), [back_away + HOLD])

    def test_second_departure_restarts_the_hold(self):
        house = claudia_home_in_her_room()
        house.room("off")
        house.set(OWNER, "not_home")
        house.advance(HOLD // 2)
        house.set(OWNER, "home")
        house.advance(5 * MIN)
        house.set(OWNER, "not_home")
        second = house.now
        house.advance(HOLD - 1)
        self.assertEqual(house.turned_off, [])
        house.advance(1)
        self.assertEqual(house.off_times(CEILING), [second + HOLD])


class Restart(unittest.TestCase):
    def away_house(self):
        house = House()
        house.set(OWNER, "not_home")
        house.room("off")
        house.set(CEILING, "on")
        house.set(STRING, "off")
        return house

    def test_init_while_away_schedules_instead_of_acting(self):
        house = self.away_house()
        make_app(house)
        self.assertEqual(house.turned_off, [])
        self.assertEqual([p[0] for p in house.pending], [HOLD])
        house.advance(HOLD - 1)
        self.assertEqual(house.turned_off, [])
        house.advance(1)
        self.assertEqual(house.off_times(CEILING), [HOLD])

    def test_init_while_home_schedules_nothing(self):
        house = self.away_house()
        house.set(OWNER, "home")
        make_app(house)
        self.assertEqual(house.pending, [])
        house.advance(LONG)
        self.assertEqual(house.turned_off, [])

    def test_init_while_the_room_is_active_schedules_nothing(self):
        house = self.away_house()
        house.room("on")
        make_app(house)
        self.assertEqual(house.pending, [])

    def test_hold_elapsing_on_a_disagreeing_store_holds_again(self):
        house = self.away_house()
        make_app(house)
        house.room("on", notify=False)
        house.advance(HOLD)
        self.assertEqual(house.turned_off, [])
        self.assertEqual([p[0] for p in house.pending], [2 * HOLD])
        house.room("off", notify=False)
        house.advance(HOLD)
        self.assertEqual(house.off_times(CEILING), [2 * HOLD])

    def test_hold_elapsing_while_the_store_says_home_does_not_act(self):
        house = self.away_house()
        make_app(house)
        house.set(OWNER, "home", notify=False)
        house.advance(HOLD)
        self.assertEqual(house.turned_off, [])

    def test_departure_reported_after_a_stale_away_seed_restarts_the_hold(self):
        house = self.away_house()
        make_app(house)
        house.advance(HOLD // 2)
        house.notify(OWNER, {"state": "home"}, {"state": "not_home"})
        left = house.now
        house.advance(HOLD // 2)
        self.assertEqual(house.turned_off, [])
        house.advance(HOLD // 2)
        self.assertEqual(house.off_times(CEILING), [left + HOLD])

    def test_room_emptying_reported_after_a_stale_empty_seed_restarts_the_hold(self):
        house = self.away_house()
        make_app(house)
        house.advance(HOLD // 2)
        empty = house.full(ACTIVE)
        house.notify(ACTIVE, dict(empty, state="on"), empty)
        emptied = house.now
        house.advance(HOLD // 2)
        self.assertEqual(house.turned_off, [])
        house.advance(HOLD // 2)
        self.assertEqual(house.off_times(CEILING), [emptied + HOLD])


class RoomUnreadable(unittest.TestCase):
    """Case 6: every scenario above with the room unreadable takes no action."""

    def test_owner_leaves_while_the_room_is_unreadable(self):
        for label, how in UNREADABLE:
            with self.subTest(label):
                house = claudia_home_in_her_room()
                house.room_unreadable(how)
                house.set(OWNER, "not_home")
                house.advance(LONG)
                self.assertEqual(house.turned_off, [])

    def test_friend_leaves_but_the_room_goes_unreadable_instead_of_off(self):
        for label, how in UNREADABLE:
            with self.subTest(label):
                house = claudia_home_in_her_room()
                house.set(OWNER, "not_home")
                house.advance(30 * MIN)
                house.room_unreadable(how)
                house.advance(LONG)
                self.assertEqual(house.turned_off, [])

    def test_unreadable_spell_shorter_than_the_hold(self):
        for label, how in UNREADABLE:
            with self.subTest(label):
                house = claudia_home_in_her_room()
                house.set(OWNER, "not_home")
                house.room_unreadable(how)
                house.advance(HOLD - MIN)
                house.room("on")
                house.advance(LONG)
                self.assertEqual(house.turned_off, [])

    def test_owner_returns_while_the_room_is_unreadable(self):
        for label, how in UNREADABLE:
            with self.subTest(label):
                house = claudia_home_in_her_room()
                house.room_unreadable(how)
                house.set(OWNER, "not_home")
                house.advance(HOLD - MIN)
                house.set(OWNER, "home")
                house.advance(LONG)
                self.assertEqual(house.turned_off, [])

    def test_restart_while_away_and_the_room_is_unreadable(self):
        for label, how in UNREADABLE:
            with self.subTest(label):
                house = House()
                house.set(OWNER, "not_home")
                house.room_unreadable(how)
                house.set(CEILING, "on")
                make_app(house)
                self.assertEqual(house.pending, [])
                house.advance(LONG)
                self.assertEqual(house.turned_off, [])

    def test_unreadable_spell_mid_hold_restarts_it(self):
        for label, how in UNREADABLE:
            with self.subTest(label):
                house = claudia_home_in_her_room()
                house.room("off")
                house.set(OWNER, "not_home")
                house.advance(HOLD // 2)
                house.room_unreadable(how)
                house.advance(10 * MIN)
                house.room("off")
                readable = house.now
                house.advance(HOLD - 1)
                self.assertEqual(house.turned_off, [])
                house.advance(1)
                self.assertEqual(house.off_times(CEILING), [readable + HOLD])

    def test_hold_elapsing_on_a_stale_witness_store_does_not_act(self):
        house = claudia_home_in_her_room()
        house.room("off")
        house.set(OWNER, "not_home")
        house.room("off", stale="true", notify=False)
        house.advance(HOLD)
        self.assertEqual(house.turned_off, [])


class TwoRooms(unittest.TestCase):
    def test_rooms_are_independent(self):
        k_owner, k_active, k_lamp = "person.kristine", "binary_sensor.kristines_room_active", "light.floor_lamp"
        args = {
            "vacancy_hold_min": HOLD_MIN,
            "rooms": dict(
                ARGS["rooms"],
                kristines_room={"owner": k_owner, "name": "Kristine", "room_label": "her room", "lights": [k_lamp]},
            ),
        }
        house = House()
        house.set(OWNER, "home")
        house.room("on")
        house.set(CEILING, "on")
        house.set(k_owner, "home")
        house.set(k_active, "off", stale="false", computed_at=datetime.now(timezone.utc).isoformat())
        house.set(k_lamp, "on")
        make_app(house, args)
        house.set(k_owner, "not_home")
        house.advance(HOLD)
        self.assertEqual(house.turned_off, [(HOLD, k_lamp)])
        self.assertEqual(house.events[0][1]["audience_users"], ["Kristine"])


class Helpers(unittest.TestCase):
    def test_is_away(self):
        for state in ("not_home", "Work", "away"):
            self.assertTrue(rrlo.is_away(state))
        for state in (None, "home", "unknown", "unavailable"):
            self.assertFalse(rrlo.is_away(state))

    def test_reads_empty_needs_off_and_every_witness_readable(self):
        self.assertTrue(rrlo.reads_empty({"state": "off", "attributes": {"stale": "false"}}))
        self.assertFalse(rrlo.reads_empty({"state": "off", "attributes": {"stale": "true"}}))
        self.assertFalse(rrlo.reads_empty({"state": "off", "attributes": {}}))
        self.assertFalse(rrlo.reads_empty({"state": "on", "attributes": {"stale": "false"}}))
        self.assertFalse(rrlo.reads_empty({"state": "unavailable", "attributes": {}}))
        self.assertFalse(rrlo.reads_empty(None))


if __name__ == "__main__":
    unittest.main()
