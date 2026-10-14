"""FamilyRoomVolumeSync: relative volume sync across the family-room Sonos group.

When speakers of a Sonos group holding 2+ family speakers move by the same step, every other
active member moves by that step. Followers are written through their native <id>_2 twin, and the
next change a written speaker reports answers that write and never leads; a party-mode helper
switches the whole thing off. The rig below drives an initialize()d app with a fake clock,
scheduler and Home Assistant state, gives every speaker one live event after startup, and echoes
every successful write back on the Music Assistant entity about 0.1 s later, the way the native
twin's write is reflected there.
"""

from __future__ import annotations

import copy
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import family_room_volume_sync as frvs  # noqa: E402

ROOFTOP = "media_player.rooftop"
LIVING = "media_player.living_room"
KITCHEN = "media_player.kitchen"
DINING = "media_player.dining_room"
BATHROOM = "media_player.bathroom"
BEDROOM = "media_player.bedroom"
FAMILY = [ROOFTOP, LIVING, KITCHEN, DINING]
PARTY = "input_boolean.family_volume_sync_party"

MIKKEL_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
KRISTINE_ID = "0f9e8d7c6b5a49382716f5e4d3c2b1a0"

BASE_ARGS = {
    "speakers": FAMILY,
    "tv_speaker": LIVING,
    "party_mode_entity": PARTY,
    "settle_seconds": 0.8,
    "max_window_seconds": 5,
    "party_mode_max_hours": 12,
}
START_VOLUMES = {ROOFTOP: 0.18, LIVING: 0.20, KITCHEN: 0.18, DINING: 0.18}
T0 = 1000.0
WALL_START = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
HOUR = 3600.0
PARTY_CHECK_S = 900.0


def twin(entity):
    return entity + "_2"


def speaker_state(volume, state="playing", group=None, source="Spotify", muted=False):
    return {
        "state": state,
        "attributes": {
            "volume_level": volume,
            "is_volume_muted": muted,
            "group_members": list(FAMILY if group is None else group),
            "source": source,
        },
        "context": {"id": "ctx", "parent_id": None, "user_id": None},
    }


def twin_state(volume, state="playing"):
    return {"state": state, "attributes": {"volume_level": volume}}


def party_state(on, since):
    return {"state": "on" if on else "off", "attributes": {}, "last_changed": since.isoformat()}


def raising_for(entity):
    def hook(service, data):
        if data.get("entity_id") == entity:
            raise RuntimeError("boom")
        return {"success": True}

    return hook


def rejecting_for(entity):
    def hook(service, data):
        if data.get("entity_id") == entity:
            return {"success": False, "error": {"code": "service_validation_error", "message": "nope"}}
        return {"success": True}

    return hook


def rejecting_service(name):
    def hook(service, data):
        if service == name:
            return {"success": False, "error": {"code": "unknown", "message": "no"}}
        return {"success": True}

    return hook


def no_result(service, data):
    return None


def turn_offs(rig):
    return [c for c in rig.service_calls if c[0] == "input_boolean/turn_off"]


class Forgetful(dict):
    """Stands in for the app's record of writes waiting for an answer and remembers none."""

    def __setitem__(self, key, value):
        pass


class Rig:
    """FamilyRoomVolumeSync with initialize() actually run and AppDaemon stubbed out."""

    def __init__(self, volumes=None, auto_echo=True, echo_delay=0.1, party=(False, WALL_START), live=True):
        self.t = T0
        self.timers = {}
        self.periodic = []
        self._timer_seq = 0
        self.arm_delays = []
        self.run_every_calls = []
        self.service_calls = []
        self.write_log = []
        self.service_hook = None
        self.auto_echo = auto_echo
        self.echo_delay = echo_delay
        vols = dict(START_VOLUMES)
        vols.update(volumes or {})
        self.states = {m: speaker_state(vols[m]) for m in FAMILY}
        self.states.update({twin(m): twin_state(vols[m]) for m in FAMILY})
        if isinstance(party, str):
            self.states[PARTY] = {"state": party, "attributes": {}, "last_changed": WALL_START.isoformat()}
        elif party is not None:
            self.states[PARTY] = party_state(*party)
        self.persons = {
            "person.mikkel": {"state": "home", "attributes": {"user_id": MIKKEL_ID, "friendly_name": "Mikkel"}},
            "person.kristine": {"state": "away", "attributes": {"user_id": KRISTINE_ID, "friendly_name": "Kristine"}},
            "person.guest": {"state": "away", "attributes": {"friendly_name": "Guest"}},
        }

        app = frvs.FamilyRoomVolumeSync.__new__(frvs.FamilyRoomVolumeSync)
        app.args = copy.deepcopy(BASE_ARGS)
        app.get_state = self._get_state
        app.listen_state = MagicMock()
        app.run_in = self._run_in
        app.run_every = self._run_every
        app.call_service = self._call_service
        app.log = MagicMock()
        app._now = lambda: self.t
        app._wall_now = lambda: self.wall
        self.app = app
        app.initialize()
        if live:
            for m in FAMILY:
                self.change(m)

    @property
    def wall(self):
        return WALL_START + timedelta(seconds=self.t - T0)

    def _get_state(self, entity_id=None, attribute=None, **kw):
        if entity_id == "person":
            return copy.deepcopy(self.persons)
        st = self.states.get(entity_id)
        if st is None:
            return None
        if attribute == "all":
            return copy.deepcopy(st)
        if attribute is None:
            return st["state"]
        return st["attributes"].get(attribute)

    def _run_in(self, callback, delay, **kw):
        self._timer_seq += 1
        handle = f"timer-{self._timer_seq}"
        self.timers[handle] = (self.t + delay, callback)
        self.arm_delays.append(delay)
        return handle

    def _run_every(self, callback, start, interval, **kw):
        self.run_every_calls.append((start, interval))
        first = float(start.split("+")[1]) if "+" in start else interval
        self.periodic.append([self.t + first, interval, callback])

    def _schedule(self, delay, action):
        self._timer_seq += 1
        self.timers[f"rig-{self._timer_seq}"] = (self.t + delay, lambda kw: action())

    def _call_service(self, service, **data):
        self.service_calls.append((service, data))
        if service == "media_player/volume_set":
            self.write_log.append((self.t, data["entity_id"], data["volume_level"]))
        result = {"success": True, "result": {"context": {"id": "x"}}}
        if self.service_hook is not None:
            result = self.service_hook(service, data)
        accepted = not (isinstance(result, dict) and result.get("success") is False)
        if accepted and service == "input_boolean/turn_off":
            self._schedule(0.05, lambda: self.set_party(False))
        if accepted and service == "media_player/volume_set" and self.auto_echo:
            entity, level = data["entity_id"], data["volume_level"]
            self._schedule(self.echo_delay, lambda: self.move(entity[: -len("_2")], level))
        return result

    def vol(self, entity):
        return self.states[entity]["attributes"]["volume_level"]

    def change(self, entity, state=None, user_id=None, fire=True, **attrs):
        old = copy.deepcopy(self.states[entity])
        new = copy.deepcopy(old)
        if state is not None:
            new["state"] = state
        new["attributes"].update(attrs)
        new["context"] = {"id": "ctx", "parent_id": None, "user_id": user_id}
        self.states[entity] = new
        if fire:
            self.app._on_change(entity, "all", old, new, {})

    def move(self, entity, volume, user_id=None):
        self.change(entity, user_id=user_id, volume_level=volume)

    def quietly(self, entity, state=None, **attrs):
        """Change that was already true when the app started: it is in the snapshot, no event."""
        self.change(entity, state=state, fire=False, **attrs)
        self.app._snap[entity] = copy.deepcopy(self.states[entity])

    def regroup(self, *groups):
        for group in groups:
            for m in group:
                if m in self.states:
                    self.change(m, group_members=list(group))

    def advance(self, seconds):
        end = self.t + seconds
        while True:
            due = [(when, 0, handle) for handle, (when, _) in self.timers.items() if when <= end + 1e-9]
            due += [(p[0], 1, i) for i, p in enumerate(self.periodic) if p[0] <= end + 1e-9]
            if not due:
                break
            when, kind, ref = min(due)
            self.t = max(self.t, when)
            if kind == 0:
                _, callback = self.timers.pop(ref)
                callback({})
            else:
                p = self.periodic[ref]
                p[0] += p[1]
                p[2]({})
        self.t = end

    def written(self):
        return {entity: level for _, entity, level in self.write_log}

    def set_party(self, on, since=None, store=True):
        """The helper changes in Home Assistant. store=False models AppDaemon not knowing it."""
        old = copy.deepcopy(self.states.get(PARTY))
        new = party_state(on, since or self.wall)
        if store:
            self.states[PARTY] = new
        else:
            self.states.pop(PARTY, None)
        self.app._on_party(PARTY, "all", old, copy.deepcopy(new), {})

    def logs(self, level=None, contains=None):
        out = []
        for c in self.app.log.call_args_list:
            msg = c.args[0]
            lvl = c.kwargs.get("level", "INFO")
            if (level is None or lvl == level) and (contains is None or contains in msg):
                out.append(msg)
        return out


class CoreSync(unittest.TestCase):
    def test_leader_shifts_every_follower_by_the_same_delta_and_offsets_are_kept(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(0.79)
        self.assertEqual(r.write_log, [])
        r.advance(0.02)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(DINING): 0.23, twin(ROOFTOP): 0.23})
        self.assertEqual(len(r.write_log), 3)

    def test_every_speaker_can_lead(self):
        for leader in FAMILY:
            with self.subTest(leader=leader):
                r = Rig()
                r.move(leader, round(r.vol(leader) + 0.05, 2))
                expected = {twin(m): round(r.vol(m) + 0.05, 2) for m in FAMILY if m != leader}
                r.advance(1)
                self.assertEqual(r.written(), expected)

    def test_writes_are_volume_set_on_native_twins_and_never_on_the_leader(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual({service for service, _ in r.service_calls}, {"media_player/volume_set"})
        for _, entity, _ in r.write_log:
            self.assertTrue(entity.endswith("_2"))
            self.assertIn(entity[: -len("_2")], FAMILY)
        self.assertNotIn(twin(KITCHEN), r.written())

    def test_negative_delta_moves_followers_down(self):
        r = Rig({KITCHEN: 0.30})
        r.move(KITCHEN, 0.25)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.15, twin(DINING): 0.13, twin(ROOFTOP): 0.13})

    def test_member_order_in_group_members_does_not_matter(self):
        r = Rig()
        r.change(LIVING, group_members=[LIVING, KITCHEN, DINING, ROOFTOP])
        r.change(KITCHEN, group_members=[KITCHEN, LIVING, ROOFTOP, DINING])
        r.change(DINING, group_members=[DINING, ROOFTOP, KITCHEN, LIVING])
        r.change(ROOFTOP, group_members=[ROOFTOP, DINING, LIVING, KITCHEN])
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(set(r.written()), {twin(LIVING), twin(DINING), twin(ROOFTOP)})

    def test_drag_inside_the_window_propagates_once_with_the_net_amount(self):
        r = Rig()
        for volume in (0.19, 0.20, 0.21, 0.22, 0.23):
            r.move(KITCHEN, volume)
            r.advance(0.2)
        r.advance(3)
        self.assertEqual(len(r.write_log), 3)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(DINING): 0.23, twin(ROOFTOP): 0.23})

    def test_window_rearms_until_the_leader_has_been_quiet_for_the_settle_time(self):
        r = Rig()
        r.move(KITCHEN, 0.19)
        r.advance(0.5)
        r.move(KITCHEN, 0.20)
        r.advance(0.7)
        self.assertEqual(r.write_log, [])
        r.advance(0.2)
        self.assertEqual(r.written(), {twin(LIVING): 0.22, twin(DINING): 0.20, twin(ROOFTOP): 0.20})
        self.assertEqual(len(r.write_log), 3)

    def test_window_closes_at_the_cap_while_the_leader_keeps_moving(self):
        r = Rig()
        volume = 0.18
        for i in range(10):
            volume = round(volume + 0.01, 2)
            r.move(KITCHEN, volume)
            if i < 9:
                r.advance(0.5)
        r.advance(0.49)
        self.assertEqual(r.write_log, [])
        r.advance(0.02)
        self.assertEqual(r.written(), {twin(LIVING): 0.30, twin(DINING): 0.28, twin(ROOFTOP): 0.28})
        self.assertAlmostEqual(r.write_log[0][0] - T0, 5.0, places=6)

    def test_movement_after_the_cap_opens_a_new_window(self):
        r = Rig()
        volume = 0.18
        for _ in range(12):
            volume = round(volume + 0.01, 2)
            r.move(KITCHEN, volume)
            r.advance(0.5)
        r.advance(5)
        self.assertEqual(r.vol(KITCHEN), 0.30)
        self.assertEqual(r.written(), {twin(LIVING): 0.32, twin(DINING): 0.30, twin(ROOFTOP): 0.30})
        self.assertEqual(len(r.write_log), 6)

    def test_events_without_a_volume_change_open_no_window(self):
        r = Rig()
        r.change(KITCHEN, media_position=12)
        r.change(KITCHEN, group_members=list(FAMILY))
        r.advance(5)
        self.assertEqual(r.arm_delays, [])
        self.assertEqual(r.write_log, [])

    def test_sub_percent_volume_noise_is_not_a_move(self):
        r = Rig()
        r.move(KITCHEN, 0.1830)
        r.advance(5)
        self.assertEqual(r.arm_delays, [])
        self.assertEqual(r.write_log, [])

    def test_single_step_is_synced_despite_float_noise(self):
        self.assertLess(0.47 - 0.46, 0.01)
        r = Rig({KITCHEN: 0.46, LIVING: 0.40, DINING: 0.40, ROOFTOP: 0.40})
        r.move(KITCHEN, 0.47)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.41, twin(DINING): 0.41, twin(ROOFTOP): 0.41})


class LoopPrevention(unittest.TestCase):
    def test_own_echo_is_ignored(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(1)
        windows_before = len(r.arm_delays)
        r.advance(5)
        self.assertEqual(len(r.write_log), 3)
        self.assertEqual(len(r.arm_delays), windows_before)
        self.assertEqual(len(r.logs(level="DEBUG", contains="answers our write")), 3)
        self.assertEqual((r.vol(LIVING), r.vol(DINING), r.vol(ROOFTOP)), (0.25, 0.23, 0.23))

    def test_an_echo_however_late_never_leads(self):
        for delay in (2.1, 11.0, 60.0, 600.0):
            with self.subTest(echo_delay=delay):
                r = Rig(echo_delay=delay)
                r.move(KITCHEN, 0.23)
                r.advance(delay + 60)
                self.assertEqual(len(r.write_log), 3)
                self.assertEqual(r.arm_delays, [0.8])
                self.assertEqual(
                    (r.vol(KITCHEN), r.vol(LIVING), r.vol(DINING), r.vol(ROOFTOP)), (0.23, 0.25, 0.23, 0.23)
                )

    def test_the_first_change_after_our_write_never_leads_whatever_its_value(self):
        r = Rig(auto_echo=False)
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.written()[twin(LIVING)], 0.25)
        windows = len(r.arm_delays)
        r.move(LIVING, 0.28)
        r.advance(5)
        self.assertEqual(len(r.arm_delays), windows)
        self.assertEqual(len(r.write_log), 3)
        r.move(LIVING, 0.33)
        r.advance(1)
        self.assertEqual(
            {e: v for _, e, v in r.write_log[3:]}, {twin(KITCHEN): 0.28, twin(DINING): 0.23, twin(ROOFTOP): 0.23}
        )

    def test_only_the_latest_write_waits_for_an_answer(self):
        r = Rig(auto_echo=False)
        for volume in (0.23, 0.28):
            r.move(KITCHEN, volume)
            r.advance(1)
        self.assertEqual([v for _, e, v in r.write_log if e == twin(LIVING)], [0.25, 0.25])
        r.move(LIVING, 0.25)
        r.advance(5)
        r.move(LIVING, 0.30)
        r.advance(1)
        r.move(LIVING, 0.25)
        r.advance(1)
        self.assertEqual(len(r.logs(contains="family_volume_sync_apply")), 4)

    def test_a_feedback_loop_the_echo_check_misses_stops_after_four_syncs(self):
        r = Rig(echo_delay=11.0)
        r.app._outstanding = Forgetful()
        r.move(KITCHEN, 0.23)
        r.advance(120)
        self.assertEqual(len(r.logs(contains="family_volume_sync_apply")), 4)
        self.assertEqual(len(r.write_log), 8)
        warnings = r.logs(level="WARNING")
        self.assertEqual(len(warnings), 1)
        self.assertIn("family_volume_sync_stopped", warnings[0])
        frozen = len(r.write_log)
        r.move(DINING, round(r.vol(DINING) + 0.05, 2))
        r.advance(60)
        self.assertEqual(len(r.write_log), frozen)
        r.advance(600)
        r.move(DINING, round(r.vol(DINING) + 0.05, 2))
        r.advance(1)
        self.assertGreater(len(r.write_log), frozen)

    def test_the_lead_is_measured_from_the_last_state_the_app_saw(self):
        r = Rig()
        stale_old = copy.deepcopy(r.states[KITCHEN])
        stale_old["attributes"]["volume_level"] = 0.10
        new = copy.deepcopy(r.states[KITCHEN])
        new["attributes"]["volume_level"] = 0.23
        r.states[KITCHEN] = copy.deepcopy(new)
        r.app._on_change(KITCHEN, "all", stale_old, new, {})
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(DINING): 0.23, twin(ROOFTOP): 0.23})

    def test_repeated_user_steps_with_late_echoes_never_run_away(self):
        r = Rig(echo_delay=2.1)
        for step in range(3):
            r.move(KITCHEN, round(r.vol(KITCHEN) + 0.05, 2))
            r.advance(10)
        r.advance(60)
        self.assertEqual(len(r.write_log), 9)
        self.assertEqual(
            (r.vol(KITCHEN), r.vol(LIVING), r.vol(DINING), r.vol(ROOFTOP)), (0.33, 0.35, 0.33, 0.33)
        )

    def test_a_write_that_never_echoes_does_not_inflate_the_next_round(self):
        r = Rig(auto_echo=False)
        r.move(KITCHEN, 0.23)
        r.advance(1.0)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(DINING): 0.23, twin(ROOFTOP): 0.23})
        r.move(KITCHEN, 0.28)
        r.advance(1.0)
        second = {e: v for _, e, v in r.write_log[3:]}
        self.assertEqual(second, {twin(LIVING): 0.25, twin(DINING): 0.23, twin(ROOFTOP): 0.23})

    def test_an_unanswered_write_keeps_its_speaker_from_leading_however_long_it_takes(self):
        for wait in (3.0, 11.0, 3600.0):
            with self.subTest(wait=wait):
                r = Rig(auto_echo=False)
                r.move(KITCHEN, 0.23)
                r.advance(wait)
                windows = len(r.arm_delays)
                r.move(LIVING, 0.25)
                r.advance(2)
                self.assertEqual(len(r.arm_delays), windows)
                self.assertNotIn(twin(KITCHEN), r.written())

    def test_change_on_a_follower_inside_the_old_echo_window_is_a_lead(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(1.5)
        writes_before = len(r.write_log)
        r.move(LIVING, 0.28)
        r.advance(2)
        new_writes = {e: v for _, e, v in r.write_log[writes_before:]}
        self.assertEqual(new_writes, {twin(KITCHEN): 0.26, twin(DINING): 0.26, twin(ROOFTOP): 0.26})

    def test_user_change_on_a_follower_right_after_our_write_is_not_overwritten_by_the_next_sync(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(1.2)
        self.assertEqual(r.vol(LIVING), 0.25)
        r.move(LIVING, 0.40)
        r.advance(0.3)
        r.move(KITCHEN, 0.28)
        r.advance(5)
        self.assertEqual([v for _, e, v in r.write_log if e == twin(LIVING)], [0.25])
        self.assertEqual(r.vol(LIVING), 0.40)
        self.assertEqual(len(r.write_log), 3)
        self.assertEqual(len(r.logs(contains="family_volume_sync_skip")), 1)

    def test_follower_tapped_inside_the_old_echo_window_is_a_touch_and_is_not_written(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(1.2)
        r.move(LIVING, 0.26)
        r.advance(0.2)
        r.move(LIVING, 0.25)
        r.advance(0.1)
        r.move(KITCHEN, 0.28)
        r.advance(5)
        second = {e: v for _, e, v in r.write_log[3:]}
        self.assertEqual(second, {twin(DINING): 0.28, twin(ROOFTOP): 0.28})
        self.assertIn("left alone: living_room (touched)", r.logs(contains="family_volume_sync_apply")[-1])

    def test_our_own_echo_arriving_inside_the_next_window_is_not_a_touch(self):
        r = Rig(echo_delay=0.5)
        r.move(KITCHEN, 0.23)
        r.advance(0.8)
        r.advance(0.2)
        r.move(KITCHEN, 0.28)
        r.advance(5)
        second = {e: v for _, e, v in r.write_log[3:]}
        self.assertEqual(second, {twin(LIVING): 0.30, twin(DINING): 0.28, twin(ROOFTOP): 0.28})
        self.assertNotIn("touched", r.logs(contains="family_volume_sync_apply")[1])

    def test_second_round_right_after_the_first_builds_on_the_echoed_volumes(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(1.1)
        r.move(KITCHEN, 0.24)
        r.advance(1.0)
        second = {e: v for _, e, v in r.write_log[3:]}
        self.assertEqual(second, {twin(LIVING): 0.26, twin(DINING): 0.24, twin(ROOFTOP): 0.24})


class ProductionRegressions(unittest.TestCase):
    def test_single_move_then_a_dashboard_group_step_is_together_and_writes_nothing_more(self):
        r = Rig({ROOFTOP: 0.22, LIVING: 0.23, KITCHEN: 0.22, DINING: 0.18})
        r.move(DINING, 0.24, user_id=MIKKEL_ID)
        r.advance(0.8)
        self.assertEqual(r.written(), {twin(ROOFTOP): 0.28, twin(LIVING): 0.29, twin(KITCHEN): 0.28})
        r.advance(0.5)
        for m in (LIVING, DINING, ROOFTOP, KITCHEN):
            r.move(m, round(r.vol(m) - 0.03, 2), user_id=MIKKEL_ID)
            r.advance(0.01)
        r.advance(10)
        self.assertEqual(len(r.write_log), 3)
        self.assertEqual(len(r.logs(contains="family_volume_sync_apply")), 1)
        self.assertEqual(len(r.logs(contains="family_volume_sync_together")), 1)
        self.assertEqual(r.logs(contains="family_volume_sync_skip"), [])
        self.assertEqual(
            (r.vol(ROOFTOP), r.vol(LIVING), r.vol(KITCHEN), r.vol(DINING)), (0.25, 0.26, 0.25, 0.21)
        )

    def test_group_step_half_a_second_after_a_sync_is_not_undone_when_the_dashboard_steps_from_stale_levels(self):
        r = Rig({ROOFTOP: 0.22, LIVING: 0.23, KITCHEN: 0.22, DINING: 0.18})
        r.move(DINING, 0.24, user_id=MIKKEL_ID)
        r.advance(0.8)
        self.assertEqual(r.written(), {twin(ROOFTOP): 0.28, twin(LIVING): 0.29, twin(KITCHEN): 0.28})
        r.advance(0.5)
        for m, volume in ((LIVING, 0.18), (DINING, 0.19), (KITCHEN, 0.17), (ROOFTOP, 0.17)):
            r.move(m, volume, user_id=MIKKEL_ID)
            r.advance(0.01)
        r.advance(10)
        self.assertEqual(len(r.write_log), 3)
        self.assertEqual(len(r.logs(contains="family_volume_sync_apply")), 1)
        self.assertEqual(len(r.logs(contains="family_volume_sync_skip")), 1)
        self.assertEqual(r.logs(level="DEBUG", contains="answers our write")[3:], [])
        self.assertEqual(
            (r.vol(ROOFTOP), r.vol(LIVING), r.vol(KITCHEN), r.vol(DINING)), (0.17, 0.18, 0.17, 0.19)
        )


class StepperBursts(unittest.TestCase):
    def test_burst_with_equal_deltas_writes_nothing(self):
        r = Rig()
        for m in FAMILY:
            r.move(m, round(r.vol(m) + 0.03, 2))
            r.advance(0.1)
        r.advance(3)
        self.assertEqual(r.write_log, [])
        lines = r.logs(contains="family_volume_sync_together")
        self.assertEqual(len(lines), 1)
        self.assertIn("+0.03", lines[0])
        self.assertIn("nothing to follow", lines[0])

    def test_burst_with_mixed_two_and_three_percent_steps_writes_nothing(self):
        r = Rig()
        steps = {ROOFTOP: 0.02, LIVING: 0.03, KITCHEN: 0.03, DINING: 0.02}
        for m in FAMILY:
            r.move(m, round(r.vol(m) + steps[m], 2))
            r.advance(0.1)
        r.advance(3)
        self.assertEqual(r.write_log, [])

    def test_straddled_burst_writes_the_late_speaker_exactly_once(self):
        r = Rig()
        for m in (KITCHEN, DINING, ROOFTOP):
            r.move(m, round(r.vol(m) + 0.03, 2))
            r.advance(0.1)
        r.advance(0.75)
        self.assertEqual(r.written(), {twin(LIVING): 0.23})
        r.move(LIVING, 0.23)
        r.advance(5)
        self.assertEqual(len(r.write_log), 1)

    def test_fast_double_tap_is_merged_into_one_window(self):
        r = Rig()
        r.move(KITCHEN, 0.19)
        r.advance(0.3)
        r.move(KITCHEN, 0.20)
        r.advance(3)
        self.assertEqual(r.written(), {twin(LIVING): 0.22, twin(DINING): 0.20, twin(ROOFTOP): 0.20})
        self.assertEqual(len(r.write_log), 3)

    def test_opposite_taps_cancel_out(self):
        r = Rig()
        r.move(KITCHEN, 0.19)
        r.advance(0.2)
        r.move(KITCHEN, 0.18)
        r.advance(3)
        self.assertEqual(r.write_log, [])


class SpeakerLiftedFromZero(unittest.TestCase):
    """A group step lifts a speaker at 0 to the first step while the others step as well. That
    speaker is active by the time the window closes, but it already took part in the move."""

    STEPS = {ROOFTOP: 0.21, LIVING: 0.23, DINING: 0.21}

    def group_step(self, kitchen_position):
        r = Rig({KITCHEN: 0.0})
        steps = list(self.STEPS.items())
        steps.insert(kitchen_position, (KITCHEN, 0.03))
        for m, volume in steps:
            r.move(m, volume)
            r.advance(0.1)
        r.advance(3)
        return r

    def test_speaker_lifted_from_zero_by_a_group_step_is_not_stepped_again(self):
        r = self.group_step(kitchen_position=3)
        self.assertEqual(r.write_log, [])
        lines = r.logs(contains="family_volume_sync_together")
        self.assertEqual(len(lines), 1)
        self.assertIn("nothing to follow | left alone: kitchen (touched)", lines[0])

    def test_same_when_the_lifted_speakers_change_arrives_first(self):
        r = self.group_step(kitchen_position=0)
        self.assertEqual(r.write_log, [])

    def test_same_wherever_the_lifted_speakers_change_arrives(self):
        for position in (1, 2):
            with self.subTest(position=position):
                self.assertEqual(self.group_step(position).write_log, [])

    def test_lone_lift_from_zero_does_not_keep_the_speaker_out_of_a_later_move(self):
        r = Rig({KITCHEN: 0.0})
        r.move(KITCHEN, 0.05)
        r.advance(10)
        self.assertEqual(r.write_log, [])
        r.move(LIVING, 0.25)
        r.advance(1)
        self.assertEqual(r.written(), {twin(KITCHEN): 0.10, twin(DINING): 0.23, twin(ROOFTOP): 0.23})

    def test_a_touch_only_counts_for_max_window_seconds(self):
        for wait, kitchen_written in ((3.9, False), (4.3, True)):
            with self.subTest(wait=wait):
                r = Rig({KITCHEN: 0.0})
                r.move(KITCHEN, 0.05)
                r.advance(wait)
                r.move(LIVING, 0.25)
                r.advance(1)
                self.assertIn(twin(DINING), r.written())
                self.assertEqual(twin(KITCHEN) in r.written(), kitchen_written)

    def test_speaker_touched_inside_the_window_is_left_alone_even_when_it_nets_to_zero(self):
        r = Rig()
        r.move(KITCHEN, 0.19)
        r.advance(0.2)
        r.move(KITCHEN, 0.18)
        r.advance(0.2)
        r.move(LIVING, 0.25)
        r.advance(3)
        self.assertEqual(r.written(), {twin(DINING): 0.23, twin(ROOFTOP): 0.23})
        line = r.logs(contains="family_volume_sync_apply")[0]
        self.assertIn("dining_room 0.18->0.23", line)
        self.assertIn("left alone: kitchen (touched)", line)

    def test_touches_are_cleared_when_a_window_flushes(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(0.8)
        r.advance(2.1)
        r.move(LIVING, 0.28)
        r.advance(1)
        self.assertEqual({e: v for _, e, v in r.write_log[3:]},
                         {twin(KITCHEN): 0.26, twin(DINING): 0.26, twin(ROOFTOP): 0.26})


class RampsThroughInactive(unittest.TestCase):
    """Fades and restores step a speaker through 0 or a pause; none of those steps lead."""

    def fade(self, r, volumes, gap=0.125):
        for volume in volumes:
            r.move(KITCHEN, volume)
            r.advance(gap)

    def test_a_fade_in_from_zero_never_leads(self):
        r = Rig({KITCHEN: 0.0})
        self.fade(r, (0.05, 0.10, 0.15, 0.20))
        r.advance(10)
        self.assertEqual(r.arm_delays, [])
        self.assertEqual(r.write_log, [])

    def test_a_volume_set_while_paused_does_not_lead_once_it_plays_again(self):
        r = Rig()
        r.change(KITCHEN, state="paused")
        self.fade(r, (0.15, 0.0))
        r.change(KITCHEN, state="playing")
        self.fade(r, (0.05, 0.10, 0.15, 0.18))
        r.advance(10)
        self.assertEqual(r.write_log, [])

    def test_a_speaker_that_dips_to_zero_inside_the_window_gives_up_its_lead(self):
        r = Rig()
        self.fade(r, (0.14, 0.09, 0.0, 0.09, 0.14))
        r.advance(10)
        self.assertEqual(r.write_log, [])

    def test_a_speaker_leads_again_max_window_seconds_after_its_ramp(self):
        for wait, leads in ((4.9, False), (5.1, True)):
            with self.subTest(wait=wait):
                r = Rig({KITCHEN: 0.0})
                r.move(KITCHEN, 0.20)
                r.advance(wait)
                r.move(KITCHEN, 0.25)
                r.advance(1)
                expected = {twin(LIVING): 0.25, twin(DINING): 0.23, twin(ROOFTOP): 0.23} if leads else {}
                self.assertEqual(r.written(), expected)

    def test_the_other_speakers_still_lead_while_one_ramps(self):
        r = Rig({KITCHEN: 0.0})
        r.move(KITCHEN, 0.10)
        r.advance(0.1)
        r.move(LIVING, 0.25)
        r.advance(0.1)
        r.move(KITCHEN, 0.20)
        r.advance(2)
        self.assertEqual(r.written(), {twin(DINING): 0.23, twin(ROOFTOP): 0.23})


class GroupAndSystemChanges(unittest.TestCase):
    def test_sonos_group_volume_with_different_deltas_is_skipped(self):
        r = Rig({ROOFTOP: 0.10, LIVING: 0.20, KITCHEN: 0.30, DINING: 0.40})
        for m, volume in ((ROOFTOP, 0.15), (LIVING, 0.30), (KITCHEN, 0.45), (DINING, 0.60)):
            r.move(m, volume)
        r.advance(3)
        self.assertEqual(r.write_log, [])
        lines = r.logs(contains="family_volume_sync_skip")
        self.assertEqual(len(lines), 1)
        self.assertIn("rooftop +0.05, living_room +0.10, kitchen +0.15, dining_room +0.20", lines[0])
        self.assertIn("(group change)", lines[0])

    def test_group_volume_move_on_only_some_speakers_with_different_deltas_is_skipped(self):
        r = Rig({ROOFTOP: 0.10, LIVING: 0.20, KITCHEN: 0.30, DINING: 0.40})
        r.move(ROOFTOP, 0.15)
        r.move(LIVING, 0.30)
        r.move(KITCHEN, 0.45)
        r.advance(3)
        self.assertEqual(r.write_log, [])

    def test_announcement_setting_every_speaker_to_one_volume_writes_nothing(self):
        for name, volumes in (
            ("different start volumes", None),
            ("same start volume", {LIVING: 0.18}),
        ):
            with self.subTest(name):
                r = Rig(volumes)
                for m in FAMILY:
                    r.move(m, 0.35)
                r.advance(3)
                self.assertEqual(r.write_log, [])

    def test_changes_to_zero_never_lead(self):
        r = Rig()
        r.move(KITCHEN, 0.0)
        r.advance(3)
        self.assertEqual(r.arm_delays, [])
        self.assertEqual(r.write_log, [])

    def test_changes_from_zero_never_lead(self):
        r = Rig({KITCHEN: 0.0})
        r.move(KITCHEN, 0.18)
        r.advance(3)
        self.assertEqual(r.arm_delays, [])
        self.assertEqual(r.write_log, [])

    def test_speaker_at_zero_volume_is_never_written(self):
        r = Rig({LIVING: 0.0})
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.written(), {twin(DINING): 0.23, twin(ROOFTOP): 0.23})

    def test_followers_never_go_below_the_floor(self):
        r = Rig({KITCHEN: 0.30})
        r.move(KITCHEN, 0.05)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.01, twin(DINING): 0.01, twin(ROOFTOP): 0.01})
        self.assertTrue(all(level >= 0.01 for _, _, level in r.write_log))

    def test_speakers_that_are_not_playing_neither_lead_nor_follow(self):
        for state in ("paused", "idle", "standby", "off", "unavailable", "unknown"):
            with self.subTest(state=state):
                r = Rig()
                r.change(LIVING, state=state)
                r.move(KITCHEN, 0.23)
                r.advance(1)
                self.assertEqual(r.written(), {twin(DINING): 0.23, twin(ROOFTOP): 0.23})

                r.move(LIVING, 0.40)
                windows = len(r.arm_delays)
                r.advance(3)
                self.assertEqual(len(r.arm_delays), windows)
                self.assertEqual(len(r.write_log), 2)

    def test_unavailable_speaker_without_a_volume_is_ignored(self):
        r = Rig()
        r.change(ROOFTOP, state="unavailable", volume_level=None)
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(DINING): 0.23})
        r.change(ROOFTOP, state="playing", volume_level=0.18)
        r.advance(3)
        self.assertEqual(len(r.write_log), 2)

    def test_solo_speaker_neither_leads_nor_follows(self):
        r = Rig()
        r.regroup([LIVING, KITCHEN, ROOFTOP])
        r.change(DINING, group_members=[])
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(ROOFTOP): 0.23})

        r.move(DINING, 0.40)
        windows = len(r.arm_delays)
        r.advance(3)
        self.assertEqual(len(r.arm_delays), windows)
        self.assertEqual(len(r.write_log), 2)

    def test_leader_with_no_active_group_mates_writes_nothing(self):
        r = Rig()
        for m in (ROOFTOP, LIVING, DINING):
            r.change(m, state="paused")
        r.move(KITCHEN, 0.23)
        r.advance(3)
        self.assertEqual(r.write_log, [])

    def test_leader_that_pauses_before_the_window_closes_writes_nothing(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(0.3)
        r.change(KITCHEN, state="paused")
        r.advance(3)
        self.assertEqual(r.write_log, [])


class UnavailableTwin(unittest.TestCase):
    def test_follower_whose_native_twin_is_unavailable_is_skipped_and_named(self):
        for name, state in (("missing", None), ("unavailable", "unavailable"), ("unknown", "unknown")):
            with self.subTest(name):
                r = Rig()
                if state is None:
                    del r.states[twin(DINING)]
                else:
                    r.states[twin(DINING)]["state"] = state
                r.move(KITCHEN, 0.23)
                r.advance(1)
                self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(ROOFTOP): 0.23})
                lines = r.logs(contains="family_volume_sync_twin_unavailable")
                self.assertEqual(len(lines), 1)
                self.assertIn("media_player.dining_room_2", lines[0])
                self.assertEqual(r.logs(level="WARNING"), [])
                self.assertNotIn("dining_room", r.logs(contains="family_volume_sync_apply")[0].split(" | ")[1])

    def test_a_skipped_twin_is_not_expected_to_echo(self):
        r = Rig()
        r.states[twin(DINING)]["state"] = "unavailable"
        r.move(KITCHEN, 0.23)
        r.advance(1)
        windows = len(r.arm_delays)
        r.move(DINING, 0.23)
        r.advance(2)
        self.assertEqual(len(r.arm_delays), windows + 1)

    def test_nothing_is_written_when_every_twin_is_unavailable(self):
        r = Rig()
        for m in (ROOFTOP, LIVING, DINING):
            r.states[twin(m)]["state"] = "unavailable"
        r.move(KITCHEN, 0.23)
        r.advance(3)
        self.assertEqual(r.write_log, [])
        self.assertEqual(len(r.logs(contains="family_volume_sync_twin_unavailable")), 3)
        self.assertEqual(r.logs(contains="family_volume_sync_apply"), [])

    def test_an_idle_twin_is_written(self):
        r = Rig()
        r.states[twin(DINING)]["state"] = "idle"
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertIn(twin(DINING), r.written())


class Topology(unittest.TestCase):
    def test_a_speaker_that_leaves_and_rejoins_mid_window_receives_nothing(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(0.2)
        r.regroup([ROOFTOP, LIVING, KITCHEN])
        r.change(DINING, group_members=[])
        r.move(KITCHEN, 0.33)
        r.advance(0.2)
        r.regroup(FAMILY)
        r.advance(5)
        self.assertEqual(r.write_log, [])

    def test_a_coordinator_change_drops_the_window(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(0.3)
        r.regroup([LIVING, ROOFTOP, KITCHEN, DINING])
        r.advance(5)
        self.assertEqual(r.write_log, [])

    def test_a_regroup_during_the_window_drops_it(self):
        for name in ("joined", "left", "swapped"):
            with self.subTest(name):
                r = Rig()
                r.regroup([KITCHEN, LIVING])
                r.change(DINING, group_members=[])
                r.change(ROOFTOP, group_members=[])
                r.move(KITCHEN, 0.23)
                r.advance(0.3)
                if name == "joined":
                    r.regroup([KITCHEN, LIVING, DINING])
                elif name == "left":
                    r.change(LIVING, group_members=[])
                    r.change(KITCHEN, group_members=[])
                else:
                    r.change(LIVING, group_members=[])
                    r.regroup([KITCHEN, DINING])
                r.advance(5)
                self.assertEqual(r.write_log, [])
                self.assertEqual(r.logs(contains="family_volume_sync_apply"), [])

    def test_speaker_that_joined_before_the_first_change_is_written(self):
        r = Rig()
        r.regroup([KITCHEN, LIVING])
        r.regroup([KITCHEN, LIVING, DINING])
        r.move(KITCHEN, 0.23)
        r.advance(5)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(DINING): 0.23})

    def test_moves_made_after_a_join_sync_the_new_group(self):
        r = Rig()
        r.regroup([KITCHEN, LIVING])
        r.change(DINING, group_members=[])
        r.change(ROOFTOP, group_members=[])
        r.move(KITCHEN, 0.19)
        r.advance(0.2)
        r.regroup([KITCHEN, LIVING, DINING])
        r.move(KITCHEN, 0.23)
        r.advance(5)
        self.assertEqual(r.written(), {twin(LIVING): 0.24, twin(DINING): 0.22})


class SpeakersAndGroups(unittest.TestCase):
    def test_living_room_on_the_tv_source_does_not_follow(self):
        r = Rig({LIVING: 0.35})
        r.change(LIVING, source="TV")
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.written(), {twin(DINING): 0.23, twin(ROOFTOP): 0.23})

    def test_living_room_on_the_tv_source_does_not_lead(self):
        r = Rig({LIVING: 0.35})
        r.change(LIVING, source="TV")
        r.move(LIVING, 0.40)
        r.advance(3)
        self.assertEqual(r.arm_delays, [])
        self.assertEqual(r.write_log, [])

    def test_tv_source_only_excludes_the_configured_tv_speaker(self):
        r = Rig()
        r.change(KITCHEN, source="TV")
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(DINING): 0.23, twin(ROOFTOP): 0.23})

    def test_living_room_rejoining_after_the_tv_is_not_rebalanced(self):
        r = Rig({LIVING: 0.35})
        r.change(LIVING, source="TV", group_members=[])
        r.regroup([ROOFTOP, KITCHEN, DINING])
        r.move(LIVING, 0.40)
        r.advance(3)
        self.assertEqual(r.write_log, [])

        r.change(LIVING, source="Spotify")
        r.regroup(FAMILY)
        r.advance(10)
        self.assertEqual(r.write_log, [])
        self.assertEqual(r.arm_delays, [])

        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.45, twin(DINING): 0.23, twin(ROOFTOP): 0.23})

    def test_a_muted_follower_is_left_alone_and_keeps_its_level(self):
        r = Rig()
        r.quietly(DINING, is_volume_muted=True)
        for volume in (0.23, 0.28):
            r.move(KITCHEN, volume)
            r.advance(2)
        self.assertEqual(r.written(), {twin(LIVING): 0.30, twin(ROOFTOP): 0.28})
        self.assertEqual(r.vol(DINING), 0.18)
        self.assertEqual({service for service, _ in r.service_calls}, {"media_player/volume_set"})
        self.assertIn("left alone: dining_room (muted)", r.logs(contains="family_volume_sync_apply")[0])

    def test_followers_are_clamped_at_the_ceiling(self):
        r = Rig({LIVING: 0.98, DINING: 1.0, ROOFTOP: 0.95})
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 1.0, twin(ROOFTOP): 1.0})
        line = r.logs(contains="family_volume_sync_apply")[0]
        self.assertIn("living_room 0.98->1.00 clamped", line)
        self.assertIn("rooftop 0.95->1.00,", line)
        self.assertNotIn("dining_room", line.split(" | ")[1])

    def test_everyone_at_the_ceiling_writes_nothing(self):
        r = Rig({LIVING: 1.0, DINING: 1.0, ROOFTOP: 1.0})
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.write_log, [])
        self.assertEqual(r.logs(contains="family_volume_sync_apply"), [])

    def test_joining_speaker_is_not_rebalanced_and_keeps_its_offset_afterwards(self):
        r = Rig({DINING: 0.50})
        r.regroup([LIVING, KITCHEN, ROOFTOP])
        r.change(DINING, group_members=[])
        r.move(DINING, 0.60)
        r.advance(3)
        self.assertEqual(r.write_log, [])

        r.regroup(FAMILY)
        r.advance(10)
        self.assertEqual(r.write_log, [])
        self.assertEqual(r.arm_delays, [])

        r.move(KITCHEN, 0.21)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.23, twin(DINING): 0.63, twin(ROOFTOP): 0.21})

    def test_two_separate_family_groups_sync_independently(self):
        r = Rig()
        r.regroup([LIVING, KITCHEN], [DINING, ROOFTOP])
        r.move(KITCHEN, 0.23)
        r.advance(0.2)
        r.move(DINING, 0.21)
        r.advance(2)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(ROOFTOP): 0.21})
        self.assertEqual(len(r.write_log), 2)

    def test_non_family_group_members_are_never_written(self):
        r = Rig()
        everyone = FAMILY + [BATHROOM, BEDROOM]
        r.regroup(everyone)
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(set(r.written()), {twin(LIVING), twin(DINING), twin(ROOFTOP)})

    def test_group_with_a_single_family_speaker_does_nothing(self):
        r = Rig()
        r.change(KITCHEN, group_members=[KITCHEN, BATHROOM, BEDROOM])
        r.move(KITCHEN, 0.23)
        r.advance(3)
        self.assertEqual(r.arm_delays, [])
        self.assertEqual(r.write_log, [])


class Initialization(unittest.TestCase):
    def test_listens_on_the_music_assistant_ids_and_the_party_helper_and_writes_nothing(self):
        r = Rig()
        calls = r.app.listen_state.call_args_list
        self.assertEqual([c.args[1] for c in calls], FAMILY + [PARTY])
        self.assertTrue(all(c.kwargs == {"attribute": "all"} for c in calls))
        self.assertFalse(any(c.args[1].endswith("_2") for c in calls))
        self.assertEqual(r.service_calls, [])
        self.assertEqual(r.arm_delays, [])

    def test_speakers_read_at_startup_neither_lead_nor_follow_until_they_report_live(self):
        r = Rig(live=False)
        r.move(KITCHEN, 0.23)
        r.advance(3)
        self.assertEqual(r.arm_delays, [])
        r.move(KITCHEN, 0.28)
        r.advance(3)
        self.assertEqual(r.write_log, [])
        self.assertIn("living_room (not seen live yet)", r.logs(contains="family_volume_sync_together")[0])
        r.change(LIVING)
        r.change(ROOFTOP)
        r.change(DINING, state="idle", volume_level=0.0)
        r.move(KITCHEN, 0.33)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(ROOFTOP): 0.23})

    def test_an_echo_that_arrives_after_a_reload_does_not_lead(self):
        r = Rig(echo_delay=2.0)
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(len(r.write_log), 3)
        r.app.initialize()
        r.advance(10)
        self.assertEqual(len(r.write_log), 3)
        self.assertEqual(r.vol(KITCHEN), 0.23)

    def test_party_expiry_is_checked_periodically(self):
        r = Rig()
        self.assertEqual(r.run_every_calls, [("now+60", 900)])

    def test_config_is_read_from_args(self):
        r = Rig()
        app = r.app
        self.assertEqual(app.speakers, FAMILY)
        self.assertEqual(app.tv_speaker, LIVING)
        self.assertEqual(app.party_entity, PARTY)
        self.assertEqual((app.settle, app.max_window, app.party_max_hours), (0.8, 5.0, 12.0))


class ApplyLogLine(unittest.TestCase):
    def test_apply_line_names_leader_amount_who_and_every_follower(self):
        r = Rig({ROOFTOP: 0.98})
        r.quietly(DINING, is_volume_muted=True)
        r.move(KITCHEN, 0.23, user_id=MIKKEL_ID)
        r.advance(1)
        lines = r.logs(level="INFO", contains="family_volume_sync_apply")
        self.assertEqual(len(lines), 1)
        line = lines[0]
        for part in (
            "+0.05",
            "led by kitchen (Mikkel)",
            "living_room 0.20->0.25",
            "left alone: dining_room (muted)",
            "rooftop 0.98->1.00 clamped",
            "group dining_room,kitchen,living_room,rooftop",
        ):
            self.assertIn(part, line)

    def test_who_is_a_person_name_a_short_user_id_or_device(self):
        for user_id, label in (
            (MIKKEL_ID, "Mikkel"),
            (KRISTINE_ID, "Kristine"),
            ("deadbeef0123456789abcdef01234567", "user deadbeef"),
            (None, "device"),
        ):
            with self.subTest(user_id=user_id):
                r = Rig()
                r.move(KITCHEN, 0.23, user_id=user_id)
                r.advance(1)
                self.assertIn(f"led by kitchen ({label})", r.logs(contains="family_volume_sync_apply")[0])

    def test_several_leaders_are_all_named(self):
        r = Rig()
        r.move(KITCHEN, 0.23, user_id=MIKKEL_ID)
        r.move(DINING, 0.23)
        r.advance(1)
        line = r.logs(contains="family_volume_sync_apply")[0]
        self.assertIn("led by kitchen (Mikkel), dining_room (device)", line)
        self.assertIn("+0.05", line)

    def test_every_log_line_is_ascii(self):
        r = Rig({ROOFTOP: 0.98})
        r.move(KITCHEN, 0.23, user_id=MIKKEL_ID)
        r.advance(1)
        for m in FAMILY:
            r.move(m, 0.40)
        r.advance(5)
        for m in FAMILY:
            r.move(m, round(r.vol(m) + 0.05, 2))
        r.advance(5)
        r.states[twin(DINING)]["state"] = "unavailable"
        r.move(KITCHEN, 0.55)
        r.advance(1)
        r.regroup([KITCHEN, LIVING])
        r.move(KITCHEN, 0.60)
        r.regroup(FAMILY)
        r.advance(5)
        r.set_party(True)
        r.advance(13 * HOUR)
        messages = [c.args[0] for c in r.app.log.call_args_list]
        self.assertGreater(len(messages), 8)
        for message in messages:
            self.assertTrue(message.isascii(), message)


class FailedWrites(unittest.TestCase):
    def test_failed_write_logs_a_warning_and_is_not_expected_back(self):
        for name, hook, text in (
            ("exception", raising_for(twin(DINING)), "boom"),
            ("rejected result", rejecting_for(twin(DINING)), "service_validation_error nope"),
        ):
            with self.subTest(name):
                r = Rig()
                r.service_hook = hook
                r.move(KITCHEN, 0.23)
                r.advance(1)
                self.assertEqual({e for _, e, _ in r.write_log}, {twin(LIVING), twin(DINING), twin(ROOFTOP)})
                warnings = r.logs(level="WARNING")
                self.assertEqual(len(warnings), 1)
                self.assertIn(twin(DINING), warnings[0])
                self.assertIn(text, warnings[0])
                apply_line = r.logs(contains="family_volume_sync_apply")
                self.assertEqual(len(apply_line), 1)
                self.assertNotIn("dining_room", apply_line[0].split(" | ")[1])

                r.service_hook = None
                windows = len(r.arm_delays)
                r.move(DINING, 0.23)
                self.assertEqual(len(r.arm_delays), windows + 1)
                r.advance(1)
                latest = {e: v for _, e, v in r.write_log[3:]}
                self.assertEqual(latest, {twin(KITCHEN): 0.28, twin(LIVING): 0.30, twin(ROOFTOP): 0.28})

    def test_a_failed_write_does_not_stop_the_other_followers(self):
        r = Rig()
        r.service_hook = raising_for(twin(LIVING))
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual({e for _, e, _ in r.write_log}, {twin(LIVING), twin(DINING), twin(ROOFTOP)})
        self.assertEqual(len(r.logs(level="WARNING")), 1)

    def test_a_missing_websocket_result_still_counts_as_a_write(self):
        r = Rig()
        r.service_hook = no_result
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.logs(level="WARNING"), [])
        self.assertEqual(len(r.logs(contains="family_volume_sync_apply")), 1)


class PartyMode(unittest.TestCase):
    def test_an_unreadable_helper_pauses_the_sync_until_it_reports(self):
        for name, party in (("missing", None), ("unknown", "unknown"), ("unavailable", "unavailable")):
            with self.subTest(name):
                r = Rig(party=party)
                r.move(KITCHEN, 0.23)
                r.advance(3)
                self.assertEqual(r.arm_delays, [])
                self.assertEqual(r.write_log, [])
                r.set_party(False)
                r.move(KITCHEN, 0.28)
                r.advance(1)
                self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(DINING): 0.23, twin(ROOFTOP): 0.23})

    def test_switching_party_on_drops_the_moves_already_collected(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(0.2)
        r.set_party(True)
        r.advance(0.2)
        r.set_party(False)
        r.advance(5)
        self.assertEqual(r.write_log, [])

    def test_party_on_means_no_window_and_no_writes(self):
        r = Rig()
        r.set_party(True)
        r.move(KITCHEN, 0.23)
        r.advance(10)
        self.assertEqual(r.arm_delays, [])
        self.assertEqual(r.write_log, [])
        r.move(KITCHEN, 0.28)
        r.advance(10)
        self.assertEqual(r.write_log, [])
        self.assertEqual(len(r.logs(level="INFO", contains="party -> on")), 1)

    def test_party_switched_on_mid_window_writes_nothing_at_the_timer(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(0.3)
        r.set_party(True)
        r.advance(3)
        self.assertEqual(r.write_log, [])

    def test_sync_resumes_when_party_is_switched_off_and_the_party_moves_are_not_replayed(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(0.3)
        r.set_party(True)
        r.advance(3)
        r.set_party(False)
        r.move(KITCHEN, 0.28)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(DINING): 0.23, twin(ROOFTOP): 0.23})
        self.assertEqual(len(r.write_log), 3)
        self.assertEqual(len(r.logs(level="INFO", contains="party -> off")), 1)

    def test_party_start_and_end_are_each_logged_once(self):
        r = Rig()
        r.set_party(True)
        for volume in (0.23, 0.28, 0.33):
            r.move(KITCHEN, volume)
        r.set_party(False)
        for volume in (0.40, 0.45):
            r.move(KITCHEN, volume)
            r.advance(3.5)
        self.assertEqual(len(r.logs(level="INFO", contains="party -> on")), 1)
        self.assertEqual(len(r.logs(level="INFO", contains="party -> off")), 1)

    def test_party_is_switched_off_after_the_max_hours_without_any_volume_activity(self):
        r = Rig()
        r.set_party(True)
        r.advance(12 * HOUR)
        self.assertEqual(turn_offs(r), [])
        r.advance(PARTY_CHECK_S)
        self.assertEqual(turn_offs(r), [("input_boolean/turn_off", {"entity_id": PARTY})])
        self.assertEqual(len(r.logs(level="INFO", contains="party_expired")), 1)
        self.assertEqual(len(r.logs(level="INFO", contains="party -> off")), 1)
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.written(), {twin(LIVING): 0.25, twin(DINING): 0.23, twin(ROOFTOP): 0.23})

    def test_party_is_not_expired_at_exactly_the_max_hours(self):
        first_check = 60.0
        r = Rig(party=(True, WALL_START + timedelta(seconds=first_check) - timedelta(hours=12)))
        r.advance(first_check)
        self.assertEqual(turn_offs(r), [])
        r.advance(PARTY_CHECK_S)
        self.assertEqual(len(turn_offs(r)), 1)

    def test_party_inside_the_max_hours_stays_on(self):
        r = Rig()
        r.set_party(True)
        r.advance(11.9 * HOUR)
        self.assertEqual(turn_offs(r), [])
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.write_log, [])

    def test_party_that_was_already_on_when_the_app_started_still_expires(self):
        r = Rig(party=(True, WALL_START - timedelta(hours=11)))
        self.assertEqual(len(r.logs(level="INFO", contains="party -> on")), 1)
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.write_log, [])
        r.advance(1.1 * HOUR)
        self.assertEqual(len(turn_offs(r)), 1)

    def test_failing_to_switch_the_party_off_is_warned_and_retried_at_the_next_check(self):
        r = Rig()
        r.service_hook = rejecting_service("input_boolean/turn_off")
        r.set_party(True)
        r.advance(12 * HOUR + 2 * PARTY_CHECK_S)
        self.assertEqual(len(turn_offs(r)), 2)
        self.assertEqual(len(r.logs(level="WARNING", contains="input_boolean/turn_off")), 2)
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.write_log, [])

    def test_helper_created_while_appdaemon_runs_is_seen_from_its_first_event(self):
        r = Rig()
        r.set_party(True, store=False)
        self.assertIsNone(r._get_state(PARTY, attribute="all"))
        r.move(KITCHEN, 0.23)
        r.advance(5)
        self.assertEqual(r.write_log, [])
        self.assertEqual(len(r.logs(level="INFO", contains="party -> on")), 1)

    def test_a_helper_that_stops_reporting_pauses_the_sync(self):
        for name, new in (("deleted", None), ("unavailable", {"state": "unavailable", "attributes": {}})):
            with self.subTest(name):
                r = Rig()
                r.app._on_party(PARTY, "all", copy.deepcopy(r.states[PARTY]), new, {})
                r.move(KITCHEN, 0.23)
                r.advance(3)
                self.assertEqual(r.write_log, [])
                self.assertEqual(turn_offs(r), [])

    def test_an_off_helper_lets_the_sync_run(self):
        r = Rig()
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(len(r.write_log), 3)
        self.assertEqual(r.logs(contains="family_volume_sync_party"), [])

    def test_unreadable_last_changed_never_expires_the_party(self):
        r = Rig()
        r.app._on_party(PARTY, "all", None, {"state": "on", "attributes": {}, "last_changed": "never"}, {})
        r.advance(13 * HOUR)
        self.assertEqual(turn_offs(r), [])
        r.move(KITCHEN, 0.23)
        r.advance(1)
        self.assertEqual(r.write_log, [])


class AppYaml(unittest.TestCase):
    def setUp(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        path = Path(__file__).resolve().parents[1] / "family_room_volume_sync.yaml"
        with open(path) as f:
            self.apps = yaml.safe_load(f)

    def test_yaml_carries_only_scalar_and_list_keys_and_matches_the_test_config(self):
        self.assertEqual(list(self.apps), ["FamilyRoomVolumeSync"])
        cfg = self.apps["FamilyRoomVolumeSync"]
        self.assertEqual(cfg["module"], "family_room_volume_sync")
        self.assertEqual(cfg["class"], "FamilyRoomVolumeSync")
        self.assertEqual(cfg["log"], "sonos_log")
        self.assertFalse(any(isinstance(v, dict) for v in cfg.values()))
        for key, value in BASE_ARGS.items():
            self.assertEqual(cfg[key], value, key)
        self.assertEqual(set(cfg) - {"module", "class", "log"}, set(BASE_ARGS))


if __name__ == "__main__":
    unittest.main()
