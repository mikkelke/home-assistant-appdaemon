"""wake_window_open(): the one signal other apps read to know the wake-up is playing out.

Open from the alarm firing for wake_window_min minutes, cut short when the routine is stopped or
its speakers are no longer one group, and kept across an AppDaemon restart for as long as it has
left to run. BedroomBathroomVolumeSync asks it and copies no volume while it is open; the timing
lives here and nowhere else.
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
ALARM_TIME = "input_datetime.wakeup_bedroom"
ALARM_ENABLED = "input_boolean.wakeup_bedroom"
FIRED = datetime(2026, 10, 21, 7, 40, 0)
GROUPED = [BEDROOM, BATHROOM]


def window_app(now=FIRED, *, join=(BATHROOM,), window_min=30, state=None):
    """A WakeupRoutine with just what the wake window reads and writes."""
    app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
    app.logged = []
    app.log = lambda msg, level="INFO", **kw: app.logged.append((level, msg))
    app.user_log = "test_log"
    app.master_player = BEDROOM
    app.join_players = list(join)
    app.volume_entities = [BEDROOM, BATHROOM]
    app.volume_start = 0.06
    app.volume_target = 0.18
    app.standards = {"speaker_volumes": {BEDROOM: 0.14, BATHROOM: 0.18}, "default_volume": 0.12}
    app.get_app = lambda name: SimpleNamespace(args=app.standards) if name == wb.STANDARD_VOLUME_APP else None
    app.wake_window_min = window_min
    app._wake_window_until = None
    app._wake_group_seen = False
    app._alarm_fired_at = FIRED
    app._state = dict(state or {})
    app._save_state = MagicMock()
    app.now = now
    app.datetime = lambda: app.now
    app.group_members = GROUPED
    app.get_state = lambda entity, **kw: (
        app.group_members if (entity, kw.get("attribute")) == (BEDROOM, "group_members") else None
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
    """wake_volume_cap(): the most an automation may raise a wake speaker to while the window is
    open - its standard volume - and None the rest of the time."""

    def test_no_cap_without_a_window(self):
        app = window_app()
        self.assertIsNone(app.wake_volume_cap(BATHROOM))

    def test_each_wake_speaker_is_capped_at_its_own_standard_while_the_window_is_open(self):
        app = opened()
        self.assertEqual(app.wake_volume_cap(BATHROOM), 0.18)
        self.assertEqual(app.wake_volume_cap(BEDROOM), 0.14)

    def test_the_cap_is_the_standard_not_the_ramp_target(self):
        app = opened()
        app.volume_target = 0.10
        self.assertEqual(app.wake_volume_cap(BATHROOM), 0.18)

    def test_a_speaker_that_is_not_a_wake_speaker_has_no_cap(self):
        self.assertIsNone(opened().wake_volume_cap("media_player.kitchen"))

    def test_the_cap_ends_with_the_window(self):
        app = opened()
        app.now = FIRED + timedelta(minutes=30)
        self.assertIsNone(app.wake_volume_cap(BATHROOM))
        app = opened()
        for name in ("_stop_media", "_resume_adaptive_brightness", "_stop_light_ramp", "_cleanup_listeners"):
            setattr(app, name, MagicMock())
        app._stop_all("Alarm toggle turned off")
        self.assertIsNone(app.wake_volume_cap(BATHROOM))

    def test_unreadable_standards_cap_at_the_most_conservative_level_with_a_warning(self):
        app = opened()
        app.get_app = lambda name: None
        self.assertEqual(app.wake_volume_cap(BATHROOM), 0.06)
        self.assertTrue(any(level == "WARNING" and BATHROOM in msg for level, msg in app.logged))


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

    def initialized(self, now, state=None, **arg_overrides):
        with open(APPS / "rutines/wakeup_bedroom.yaml") as f:
            args = next(iter(yaml.safe_load(f).values()))
        args.update(arg_overrides)
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

    def test_the_wake_group_is_ramped_even_if_volume_entities_forgets_it(self):
        self.assertEqual(self.initialized(FIRED).volume_entities, [BEDROOM, BATHROOM])
        app = self.initialized(FIRED, volume_entities=[BATHROOM])
        self.assertEqual(app.volume_entities, [BEDROOM, BATHROOM])
        app = self.initialized(FIRED, volume_entities=[BATHROOM, "media_player.kitchen"])
        self.assertEqual(app.volume_entities, [BEDROOM, BATHROOM, "media_player.kitchen"])


if __name__ == "__main__":
    unittest.main()
