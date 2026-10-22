"""The wake window and what hangs on it.

wake_window_open() is open from the alarm firing for wake_window_min minutes, cut short when the
routine is stopped or its speakers are no longer one group, and kept across an AppDaemon restart
for as long as it has left to run. The timing lives here and nowhere else.

wake_volume_cap() is what other apps (BedroomBathroomVolumeSync) ask: the speaker in the room with
the bed is held to its standard volume during the window for as long as the bed is not known to be
empty, because the cap is for whoever stays in bed. An empty bed - and the bathroom, always - is
not capped: the volume follows the person who got up. An unreadable bed counts as occupied.
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lights"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "presence"))

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

import room_active_read  # noqa: E402
import wakeup_bedroom as wb  # noqa: E402

# wakeup_bedroom imports its shared layers defensively, and another test file may have imported it
# first without apps/presence on the path; the cap needs the real reader.
wb.room_active_read = room_active_read

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

APPS = Path(__file__).resolve().parents[2]
BEDROOM = "media_player.bedroom"
BATHROOM = "media_player.bathroom"
ALARM_TIME = "input_datetime.wakeup_bedroom"
ALARM_ENABLED = "input_boolean.wakeup_bedroom"
FIRED = datetime(2026, 10, 21, 7, 40, 0)
GROUPED = [BEDROOM, BATHROOM]
LEFT, RIGHT, STRIP = "binary_sensor.left_bedside", "binary_sensor.right_bedside", "binary_sensor.strip_either"
OCCUPIED = {LEFT: "off", RIGHT: "off", STRIP: "on"}
EMPTY = {LEFT: "off", RIGHT: "off", STRIP: "off"}
UNREADABLE = {LEFT: "off", RIGHT: "unknown", STRIP: "off"}


def window_app(now=FIRED, *, join=(BATHROOM,), window_min=30, state=None):
    """A WakeupRoutine with just what the wake window reads and writes."""
    app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
    app.logged = []
    app.log = lambda msg, level="INFO", **kw: app.logged.append((level, msg))
    app.user_log = "test_log"
    app.master_player = BEDROOM
    app.join_players = list(join)
    app.volume_entities = [BEDROOM, BATHROOM]
    app.bed_speaker = BEDROOM
    app.volume_start = 0.06
    app.volume_target = 0.18
    app.standards = {"speaker_volumes": {BEDROOM: 0.14, BATHROOM: 0.18}, "default_volume": 0.12}
    app.bed = dict(OCCUPIED)
    app.volume = {BEDROOM: 0.14, BATHROOM: 0.18}
    app.zones = {"bedroom": [{"entity": e, "tier": "bed"} for e in (LEFT, RIGHT, STRIP)]}
    app.get_app = lambda name: (
        SimpleNamespace(args=app.standards) if name == wb.STANDARD_VOLUME_APP
        else SimpleNamespace(zones=app.zones) if name == "RoomActive" else None
    )
    app.wake_window_min = window_min
    app._wake_window_until = None
    app._wake_group_seen = False
    app._alarm_fired_at = FIRED
    app._state = dict(state or {})
    app._save_state = MagicMock()
    app.now = now
    app.datetime = lambda: app.now
    app.group_members = GROUPED

    def get_state(entity, **kw):
        if (entity, kw.get("attribute")) == (BEDROOM, "group_members"):
            return app.group_members
        if kw.get("attribute") == "all":
            return {"attributes": {"volume_level": app.volume[entity]}}
        return app.bed.get(entity)

    app.get_state = get_state
    app.sets = []
    app.call_service = lambda service, **kw: (
        app.sets.append((kw["entity_id"], kw["volume_level"])), app.volume.update({kw["entity_id"]: kw["volume_level"]})
    )
    return app


def opened(**kw):
    app = window_app(**kw)
    app._open_wake_window()
    return app


class TheWindowRunsForWakeWindowMin(unittest.TestCase):
    def test_closed_until_an_alarm_fires(self):
        self.assertFalse(window_app().wake_window_open())

    def test_open_from_the_alarm_to_the_last_second(self):
        app = opened()
        for offset in (0, 1, 60, 29 * 60 + 59):
            app.now = FIRED + timedelta(seconds=offset)
            self.assertTrue(app.wake_window_open(), offset)

    def test_closed_from_wake_window_min_after_the_alarm(self):
        app = opened()
        for offset in (30 * 60, 30 * 60 + 1, 3 * 3600):
            app.now = FIRED + timedelta(seconds=offset)
            self.assertFalse(app.wake_window_open(), offset)

    def test_the_length_is_the_configured_one(self):
        app = opened(window_min=10)
        app.now = FIRED + timedelta(minutes=9, seconds=59)
        self.assertTrue(app.wake_window_open())
        app.now = FIRED + timedelta(minutes=10)
        self.assertFalse(app.wake_window_open())

    def test_the_deadline_is_written_to_the_state_file(self):
        app = opened()
        self.assertEqual(app._state["wake_window_until"], (FIRED + timedelta(minutes=30)).isoformat())
        app._save_state.assert_called()


class TheWindowEndsEarly(unittest.TestCase):
    def test_stopping_the_routine_closes_it_and_the_state_file_forgets_it(self):
        app = opened()
        for name in ("_stop_media", "_resume_adaptive_brightness", "_stop_light_ramp", "_cleanup_listeners"):
            setattr(app, name, MagicMock())
        app._stop_all("Alarm toggle turned off")
        self.assertFalse(app.wake_window_open())
        self.assertIsNone(app._state["wake_window_until"])

    def test_a_dissolved_wake_group_closes_it(self):
        app = opened()
        self.assertTrue(app.wake_window_open())
        app.group_members = [BEDROOM]
        self.assertFalse(app.wake_window_open())

    def test_a_group_that_has_not_formed_yet_does_not_close_it(self):
        app = opened()
        app.group_members = [BEDROOM]
        self.assertTrue(app.wake_window_open())
        app.group_members = GROUPED
        self.assertTrue(app.wake_window_open())
        app.group_members = [BEDROOM]
        self.assertFalse(app.wake_window_open())

    def test_an_unreadable_group_does_not_close_it(self):
        app = opened()
        self.assertTrue(app.wake_window_open())
        app.group_members = None
        self.assertTrue(app.wake_window_open())

    def test_a_wake_without_joined_speakers_runs_the_full_time(self):
        app = opened(join=())
        app.group_members = [BEDROOM]
        self.assertTrue(app.wake_window_open())

    def test_a_second_fire_starts_the_window_over(self):
        app = opened()
        app.now = FIRED + timedelta(minutes=29)
        app._alarm_fired_at = app.now
        app._open_wake_window()
        app.now = FIRED + timedelta(minutes=45)
        self.assertTrue(app.wake_window_open())


class TheCapOtherAppsStayUnder(unittest.TestCase):
    """wake_volume_cap(entity): the most an automation may set the speaker to right now on account
    of the wake-up, or None."""

    def test_no_cap_without_a_window(self):
        self.assertIsNone(window_app().wake_volume_cap(BEDROOM))

    def test_the_bedroom_is_held_to_its_standard_while_the_bed_is_occupied(self):
        app = opened()
        self.assertEqual(app.wake_volume_cap(BEDROOM), 0.14)

    def test_the_bathroom_is_never_capped(self):
        app = opened()
        self.assertIsNone(app.wake_volume_cap(BATHROOM))
        app.bed = dict(EMPTY)
        self.assertIsNone(app.wake_volume_cap(BATHROOM))

    def test_a_bed_that_reads_empty_lifts_the_bedroom_cap_and_an_occupied_one_brings_it_back(self):
        app = opened()
        app.bed = dict(EMPTY)
        self.assertIsNone(app.wake_volume_cap(BEDROOM))
        app.bed = dict(OCCUPIED)
        self.assertEqual(app.wake_volume_cap(BEDROOM), 0.14)

    def test_an_unreadable_bed_counts_as_occupied(self):
        app = opened()
        app.bed = dict(UNREADABLE)
        self.assertEqual(app.wake_volume_cap(BEDROOM), 0.14)

    def test_any_bed_witness_on_beats_the_rest(self):
        app = opened()
        app.bed = {LEFT: "on", RIGHT: "off", STRIP: "off"}
        self.assertEqual(app.wake_volume_cap(BEDROOM), 0.14)

    def test_a_bed_that_cannot_be_judged_counts_as_occupied(self):
        app = opened()
        app.bed = dict(EMPTY)
        app.zones = {}
        self.assertEqual(app.wake_volume_cap(BEDROOM), 0.14)

        app = opened()
        app.bed = dict(EMPTY)
        standards = app.get_app
        app.get_app = lambda name: (_ for _ in ()).throw(RuntimeError("registry down")) if name == "RoomActive" else standards(name)
        self.assertEqual(app.wake_volume_cap(BEDROOM), 0.14)

    def test_a_missing_or_failing_bed_reader_counts_as_occupied(self):
        class Raising:
            @staticmethod
            def bed_occupied(hass):
                raise RuntimeError("boom")

        for reader in (None, Raising):
            app = opened()
            app.bed = dict(EMPTY)
            original = wb.room_active_read
            wb.room_active_read = reader
            self.addCleanup(setattr, wb, "room_active_read", original)
            self.assertEqual(app.wake_volume_cap(BEDROOM), 0.14)
            wb.room_active_read = original

    def test_the_cap_is_the_standard_not_the_ramp_target(self):
        app = opened()
        app.volume_target = 0.10
        self.assertEqual(app.wake_volume_cap(BEDROOM), 0.14)

    def test_the_cap_ends_with_the_window(self):
        app = opened()
        app.now = FIRED + timedelta(minutes=30)
        self.assertIsNone(app.wake_volume_cap(BEDROOM))
        app = opened()
        for name in ("_stop_media", "_resume_adaptive_brightness", "_stop_light_ramp", "_cleanup_listeners"):
            setattr(app, name, MagicMock())
        app._stop_all("Alarm toggle turned off")
        self.assertIsNone(app.wake_volume_cap(BEDROOM))

    def test_a_speaker_that_is_not_the_bed_speaker_has_no_cap(self):
        self.assertIsNone(opened().wake_volume_cap("media_player.kitchen"))

    def test_unreadable_standards_cap_at_the_most_conservative_level_with_a_warning(self):
        app = opened()
        standards_down = app.get_app

        def get_app(name):
            return None if name == wb.STANDARD_VOLUME_APP else standards_down(name)

        app.get_app = get_app
        self.assertEqual(app.wake_volume_cap(BEDROOM), 0.06)
        self.assertTrue(any(level == "WARNING" and BEDROOM in msg for level, msg in app.logged))


class TheBedGuardBringsTheBedSpeakerBack(unittest.TestCase):
    """The volume follows a person once the bed reads empty, so the bed speaker can end up past
    its standard. When the bed reads occupied again inside the window the guard (once a minute)
    puts it back - and only then: it never touches the speaker otherwise."""

    def guard(self, app, bed, volume=None):
        app.bed = dict(bed)
        if volume is not None:
            app.volume[BEDROOM] = volume
        app.now += timedelta(seconds=60)
        app._bed_guard(None)

    def setUp(self):
        self.app = opened()
        self.app._bed_was_empty = False

    def test_an_empty_bed_that_fills_again_brings_a_followed_up_speaker_back_to_the_standard(self):
        self.guard(self.app, EMPTY, volume=0.24)
        self.assertEqual(self.app.sets, [])
        self.guard(self.app, OCCUPIED)
        self.assertEqual(self.app.sets, [(BEDROOM, 0.14)])
        self.assertTrue(any("Bed occupied again" in msg for _, msg in self.app.logged))

    def test_an_unreadable_bed_after_an_empty_one_counts_as_occupied_again(self):
        self.guard(self.app, EMPTY, volume=0.24)
        self.guard(self.app, UNREADABLE)
        self.assertEqual(self.app.sets, [(BEDROOM, 0.14)])

    def test_a_bed_that_stays_occupied_is_left_alone_whatever_the_volume(self):
        for _ in range(3):
            self.guard(self.app, OCCUPIED, volume=0.24)
        self.assertEqual(self.app.sets, [])

    def test_a_bed_that_stays_empty_is_left_alone(self):
        for _ in range(3):
            self.guard(self.app, EMPTY, volume=0.24)
        self.assertEqual(self.app.sets, [])

    def test_a_speaker_already_at_its_standard_is_not_touched(self):
        self.guard(self.app, EMPTY, volume=0.14)
        self.guard(self.app, OCCUPIED)
        self.assertEqual(self.app.sets, [])

    def test_only_the_edge_acts_so_a_hand_on_the_volume_afterwards_stands(self):
        self.guard(self.app, EMPTY, volume=0.24)
        self.guard(self.app, OCCUPIED)
        self.guard(self.app, OCCUPIED, volume=0.20)
        self.assertEqual(self.app.sets, [(BEDROOM, 0.14)])

    def test_every_new_occupied_edge_acts(self):
        self.guard(self.app, EMPTY, volume=0.24)
        self.guard(self.app, OCCUPIED)
        self.guard(self.app, EMPTY, volume=0.24)
        self.guard(self.app, OCCUPIED)
        self.assertEqual(self.app.sets, [(BEDROOM, 0.14), (BEDROOM, 0.14)])

    def test_nothing_happens_outside_the_window(self):
        self.guard(self.app, EMPTY, volume=0.24)
        self.app.now = FIRED + timedelta(minutes=31)
        self.guard(self.app, OCCUPIED)
        self.assertEqual(self.app.sets, [])
        self.assertFalse(self.app._bed_was_empty)

    def test_a_failure_never_escapes(self):
        self.app.wake_volume_cap = lambda entity: (_ for _ in ()).throw(RuntimeError("boom"))
        self.app._bed_guard(None)
        self.assertTrue(any(level == "WARNING" and "Bed guard" in msg for level, msg in self.app.logged))


def fire_app(now=FIRED):
    """Enough of WakeupRoutine for the REAL _alarm_fire (manual fire, so the trigger-window and
    toggle guards stand aside); everything the fire calls past the window is stubbed."""
    app = window_app(now)
    app.alarm_time_entity = ALARM_TIME
    app.alarm_enabled_entity = ALARM_ENABLED
    app.get_state = lambda entity, **kw: {ALARM_TIME: "07:40:00"}.get(entity)
    app._manual_fire_in_progress = True
    app._late_catchup_in_progress = False
    app._fired_via_heartbeat = False
    app.last_fire_date = None
    app._both_away = lambda: False
    app.fire_event = MagicMock()
    app._attach_cancel_listeners = MagicMock()
    app._group_speakers = MagicMock()
    app._start_media_and_volume_ramp = MagicMock()
    app.bedroom_cover = "cover.bedroom_blind"
    app.bathroom_cover = "cover.bathroom_blind"
    app.bedroom_cover_target = 38
    app.bathroom_cover_target = 40
    app._decide_bedroom_wake_target = lambda: (38, "cool / no sun")
    app._nudge_cover_if_closed = lambda entity, target: None
    app._cover_position = lambda entity: 38
    app._maybe_start_light_ramp = MagicMock()
    return app


class TheAlarmOpensIt(unittest.TestCase):
    def test_the_alarm_firing_opens_the_window_before_it_starts_anything(self):
        app = fire_app()
        seen = []
        app._group_speakers = lambda: seen.append(("group", app.wake_window_open()))
        app._start_media_and_volume_ramp = lambda: seen.append(("ramp", app.wake_window_open()))
        app._alarm_fire({})
        self.assertEqual(seen, [("group", True), ("ramp", True)])
        self.assertEqual(app._state["wake_window_until"], (FIRED + timedelta(minutes=30)).isoformat())

    def test_a_window_that_cannot_be_opened_never_stops_the_wake_up(self):
        app = fire_app()
        app._open_wake_window = MagicMock(side_effect=RuntimeError("disk full"))
        app._alarm_fire({})
        app._start_media_and_volume_ramp.assert_called_once()
        self.assertTrue(any(level == "WARNING" and "wake window" in msg for level, msg in app.logged))

    def test_a_skipped_alarm_opens_nothing(self):
        app = fire_app()
        app._manual_fire_in_progress = False
        app._is_within_trigger_window = lambda seconds: False
        app._alarm_fire({})
        self.assertFalse(app.wake_window_open())


@unittest.skipIf(yaml is None, "PyYAML not installed")
class InitializeKeepsAndExtendsIt(unittest.TestCase):
    """Runs the real initialize() against the repo's own wakeup_bedroom.yaml."""

    def initialized(self, now, state=None, drop=(), **arg_overrides):
        with open(APPS / "rutines/wakeup_bedroom.yaml") as f:
            args = next(iter(yaml.safe_load(f).values()))
        args.update(arg_overrides)
        for key in drop:
            del args[key]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args["state_file"] = str(Path(tmp.name) / "wakeup_bedroom_state.json")
        if state is not None:
            Path(args["state_file"]).write_text(json.dumps(state))
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        app.args = args
        app.log = MagicMock()
        app.set_log_level = MagicMock()
        app.get_state = lambda *a, **kw: None
        app.datetime = lambda: now
        app.timer_running = lambda handle: False
        for name in ("listen_state", "run_daily", "run_at", "run_in", "run_every", "cancel_timer", "create_task"):
            setattr(app, name, MagicMock())
        app.initialize()
        return app

    def test_the_shipped_yaml_sets_the_window_length(self):
        app = self.initialized(FIRED, wake_window_min=17)
        self.assertEqual(app.wake_window_min, 17)
        self.assertEqual(self.initialized(FIRED).wake_window_min, 30)

    def test_a_restart_inside_the_window_keeps_it_for_the_time_it_has_left(self):
        until = FIRED + timedelta(minutes=30)
        state = {"wake_window_until": until.isoformat()}
        app = self.initialized(FIRED + timedelta(minutes=12), state)
        self.assertTrue(app.wake_window_open())
        app = self.initialized(until, state)
        self.assertFalse(app.wake_window_open())

    def test_nothing_unreadable_in_the_state_file_opens_it(self):
        for state in (None, {}, {"wake_window_until": None}, {"wake_window_until": "soon"}):
            self.assertFalse(self.initialized(FIRED, state).wake_window_open(), state)

    def test_the_bed_speaker_comes_from_the_yaml_and_defaults_to_the_master(self):
        self.assertEqual(self.initialized(FIRED).bed_speaker, BEDROOM)
        self.assertEqual(self.initialized(FIRED, drop=("bed_speaker",)).bed_speaker, BEDROOM)
        self.assertEqual(self.initialized(FIRED, bed_speaker="media_player.other").bed_speaker, "media_player.other")

    def test_a_missing_bed_reader_is_said_out_loud_at_start(self):
        original = wb.room_active_read
        self.addCleanup(setattr, wb, "room_active_read", original)
        wb.room_active_read = None
        app = self.initialized(FIRED)
        warned = [c for c in app.log.call_args_list if "room_active_read" in str(c) and "WARNING" in str(c)]
        self.assertEqual(len(warned), 1)
        wb.room_active_read = original
        app = self.initialized(FIRED)
        self.assertFalse([c for c in app.log.call_args_list if "room_active_read" in str(c)])

    def test_the_bed_guard_is_scheduled_once_a_minute(self):
        app = self.initialized(FIRED)
        guards = [c for c in app.run_every.call_args_list if c.args[0] == app._bed_guard]
        self.assertEqual(len(guards), 1)
        self.assertEqual(guards[0].args[2], 60)

    def test_the_wake_group_is_ramped_even_if_volume_entities_forgets_it(self):
        self.assertEqual(self.initialized(FIRED).volume_entities, [BEDROOM, BATHROOM])
        app = self.initialized(FIRED, volume_entities=[BATHROOM])
        self.assertEqual(app.volume_entities, [BEDROOM, BATHROOM])
        app = self.initialized(FIRED, volume_entities=[BATHROOM, "media_player.kitchen"])
        self.assertEqual(app.volume_entities, [BEDROOM, BATHROOM, "media_player.kitchen"])


if __name__ == "__main__":
    unittest.main()
