# tests/test_presence_stuck_watch.py - PresenceStuckWatch: stuck-presence alerting (three
# rules: nobody_home / elsewhere / long_hold) plus the per-room presence/light on-time metric
# and its 23:55 anomaly alert. Same __new__ + monkeypatched-callables harness as the other
# tests in this directory (see test_house_night_mode.py / test_room_active.py).
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

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

import presence_stuck_watch as psw  # noqa: E402

BATHROOM = "bathroom"
PRESENCE = "binary_sensor.bathroom_presence_presence"
MOTION = "sensor.bathroom_presence_motion_state"
LIGHT = "light.bathroom_lights"
MIKKEL = "person.mikkel"
KRISTINE = "person.kristine"
CLAUDIA = "person.claudia"
PERSONS = [MIKKEL, KRISTINE, CLAUDIA]

ROOMS_RAW = {
    BATHROOM: {
        "presence": PRESENCE,
        "motion": MOTION,
        "light": LIGHT,
        "zone": "bathroom",
        "ignore_zones": ["guest_bathroom"],
        "nobody_home_min": 5,
        "elsewhere_min": 10,
        "long_hold_min": 60,
        "long_hold_no_motion_min": 30,
    }
}


def make_app(states=None, now=None, room_zones=None):
    """PresenceStuckWatch with one configured room (bathroom, matching the real yaml) and
    fake AppDaemon callables, without running initialize(). _save_metrics is stubbed to a
    spy list (lock_health.py test convention) so ordinary tests never touch a real
    filesystem - see PersistenceReload below for the dedicated real-file round-trip tests."""
    app = psw.PresenceStuckWatch.__new__(psw.PresenceStuckWatch)
    app.persons = list(PERSONS)
    app.rooms = psw.PresenceStuckWatch._parse_rooms(ROOMS_RAW)
    app._room_state = {r: psw.PresenceStuckWatch._fresh_room_state() for r in app.rooms}
    app._metrics = {r: {} for r in app.rooms}
    app._today = {}
    app._nobody_home_since = None
    app._state_file = Path("/nonexistent/presence_stuck_watch_state.json")

    app.states = dict(states or {})

    def get_state(entity, attribute=None, **kw):
        return app.states.get((entity, attribute))

    app.get_state = get_state

    clock = {"now": now or datetime(2026, 9, 23, 12, 0)}
    app._clock = clock
    app._now_local = lambda: clock["now"]

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.set_state = MagicMock()
    app.create_task = MagicMock()
    app._notifier = MagicMock()
    app._room_active_app = SimpleNamespace(
        zones=room_zones
        if room_zones is not None
        else {"bathroom": [], "guest_bathroom": [], "kitchen": [], "bedroom": []}
    )

    app.saves = []
    app._save_metrics = lambda: app.saves.append(True)

    for room in app.rooms:
        app._roll_to(room, clock["now"])
    return app


def _persist_app(state_file, rooms_raw=None):
    """Minimal PresenceStuckWatch with the REAL _save_metrics/_load_metrics bound (unlike
    make_app above) - for the persistence round-trip tests only."""
    app = psw.PresenceStuckWatch.__new__(psw.PresenceStuckWatch)
    app.rooms = psw.PresenceStuckWatch._parse_rooms(rooms_raw if rooms_raw is not None else ROOMS_RAW)
    app._room_state = {r: psw.PresenceStuckWatch._fresh_room_state() for r in app.rooms}
    app._metrics = {r: {} for r in app.rooms}
    app._today = {}
    app._state_file = state_file
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    return app


class ParseRooms(unittest.TestCase):
    def test_required_keys_pass_through(self):
        rooms = psw.PresenceStuckWatch._parse_rooms(ROOMS_RAW)
        self.assertEqual(rooms[BATHROOM]["presence"], PRESENCE)
        self.assertEqual(rooms[BATHROOM]["motion"], MOTION)
        self.assertEqual(rooms[BATHROOM]["light"], LIGHT)
        self.assertEqual(rooms[BATHROOM]["zone"], "bathroom")
        self.assertEqual(rooms[BATHROOM]["ignore_zones"], ["guest_bathroom"])
        self.assertEqual(rooms[BATHROOM]["nobody_home_min"], 5.0)
        self.assertEqual(rooms[BATHROOM]["elsewhere_min"], 10.0)
        self.assertEqual(rooms[BATHROOM]["long_hold_min"], 60.0)
        self.assertEqual(rooms[BATHROOM]["long_hold_no_motion_min"], 30.0)

    def test_defaults_when_optional_keys_omitted(self):
        raw = {"office": {"presence": "binary_sensor.x", "motion": "sensor.y", "light": "light.z"}}
        rooms = psw.PresenceStuckWatch._parse_rooms(raw)
        self.assertEqual(rooms["office"]["zone"], "office")
        self.assertEqual(rooms["office"]["ignore_zones"], [])
        self.assertEqual(rooms["office"]["nobody_home_min"], 5.0)
        self.assertEqual(rooms["office"]["long_hold_min"], 60.0)

    def test_missing_required_key_raises(self):
        with self.assertRaises(KeyError):
            psw.PresenceStuckWatch._parse_rooms({"office": {"presence": "binary_sensor.x"}})


class PrettyName(unittest.TestCase):
    def test_simple_room(self):
        self.assertEqual(psw.PresenceStuckWatch._pretty("bathroom"), "Bathroom")

    def test_underscored_room(self):
        self.assertEqual(psw.PresenceStuckWatch._pretty("guest_bathroom"), "Guest bathroom")


class OtherZonesHelper(unittest.TestCase):
    def test_excludes_own_zone_and_ignore_zones(self):
        app = make_app(room_zones={"bathroom": [], "guest_bathroom": [], "kitchen": [], "bedroom": []})
        self.assertEqual(app._other_zones(app.rooms[BATHROOM]), ["bedroom", "kitchen"])

    def test_empty_when_room_active_missing(self):
        app = make_app()
        app._room_active_app = None
        self.assertEqual(app._other_zones(app.rooms[BATHROOM]), [])


class NobodyHomeTracking(unittest.TestCase):
    def test_latches_when_nobody_home(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(
            states={(MIKKEL, None): "not_home", (KRISTINE, None): "not_home", (CLAUDIA, None): "not_home"},
            now=now0,
        )
        app._recompute_nobody_home(now0)
        self.assertEqual(app._nobody_home_since, now0)

    def test_does_not_move_once_latched(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(states={(MIKKEL, None): "not_home"}, now=now0)
        app._nobody_home_since = now0 - timedelta(minutes=10)
        app._recompute_nobody_home(now0)
        self.assertEqual(app._nobody_home_since, now0 - timedelta(minutes=10))

    def test_clears_when_someone_comes_home(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(states={(MIKKEL, None): "home"}, now=now0)
        app._nobody_home_since = now0 - timedelta(minutes=10)
        app._recompute_nobody_home(now0)
        self.assertIsNone(app._nobody_home_since)

    def test_a_named_zone_that_is_not_home_still_counts_as_away(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(states={(MIKKEL, None): "work"}, now=now0)
        app._recompute_nobody_home(now0)
        self.assertEqual(app._nobody_home_since, now0)

    def test_persons_home_count(self):
        app = make_app(states={(MIKKEL, None): "home", (KRISTINE, None): "home", (CLAUDIA, None): "not_home"})
        self.assertEqual(app._persons_home_count(), 2)


class SeedRoomAtStartup(unittest.TestCase):
    def test_motion_seeds_from_last_changed(self):
        now0 = datetime(2026, 9, 23, 12, 0, 0)
        last_changed_iso = (now0 - timedelta(minutes=15)).isoformat()
        app = make_app(
            states={(MOTION, None): "small", (MOTION, "last_changed"): last_changed_iso}, now=now0
        )
        app._seed_room(BATHROOM, app.rooms[BATHROOM], now0)
        seeded = app._room_state[BATHROOM]["last_motion_at"]
        self.assertAlmostEqual((now0 - seeded).total_seconds(), 900, delta=2)

    def test_motion_none_is_not_seeded(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(states={(MOTION, None): "none"}, now=now0)
        app._seed_room(BATHROOM, app.rooms[BATHROOM], now0)
        self.assertIsNone(app._room_state[BATHROOM]["last_motion_at"])

    def test_motion_small_without_last_changed_falls_back_to_now(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(states={(MOTION, None): "small"}, now=now0)
        app._seed_room(BATHROOM, app.rooms[BATHROOM], now0)
        self.assertEqual(app._room_state[BATHROOM]["last_motion_at"], now0)

    def test_presence_on_at_startup_seeds_conservatively_from_now(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(states={(PRESENCE, None): "on"}, now=now0)
        app._seed_room(BATHROOM, app.rooms[BATHROOM], now0)
        self.assertEqual(app._room_state[BATHROOM]["presence_on_since"], now0)

    def test_presence_off_at_startup_is_not_seeded(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(states={(PRESENCE, None): "off"}, now=now0)
        app._seed_room(BATHROOM, app.rooms[BATHROOM], now0)
        self.assertIsNone(app._room_state[BATHROOM]["presence_on_since"])

    def test_light_on_at_startup_seeds_conservatively_from_now(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(states={(LIGHT, None): "on"}, now=now0)
        app._seed_room(BATHROOM, app.rooms[BATHROOM], now0)
        self.assertEqual(app._room_state[BATHROOM]["light_on_since"], now0)


class NobodyHomeRule(unittest.TestCase):
    def test_fires_after_threshold(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(
            states={(PRESENCE, None): "on", (MIKKEL, None): "not_home", (KRISTINE, None): "not_home",
                    (CLAUDIA, None): "not_home"},
            now=now0,
        )
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._nobody_home_since = now0
        app._clock["now"] = now0 + timedelta(minutes=5)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_called_once()
        self.assertTrue(app._room_state[BATHROOM]["stuck"])

    def test_no_fire_before_threshold(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(states={(PRESENCE, None): "on"}, now=now0)
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._nobody_home_since = now0
        app._clock["now"] = now0 + timedelta(minutes=4)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_not_called()

    def test_no_fire_while_presence_off(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(states={(PRESENCE, None): "off"}, now=now0)
        app._nobody_home_since = now0 - timedelta(minutes=30)
        app._evaluate_room(BATHROOM, now0)
        app.create_task.assert_not_called()


class ElsewhereRule(unittest.TestCase):
    def _base_app(self, now0):
        app = make_app(
            states={(PRESENCE, None): "on", (MIKKEL, None): "home", (KRISTINE, None): "not_home",
                    (CLAUDIA, None): "not_home"},
            now=now0,
        )
        app._nobody_home_since = None  # Mikkel is home
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._room_state[BATHROOM]["last_motion_at"] = now0 - timedelta(minutes=20)
        return app

    def test_fires_after_elsewhere_min_since_other_zone_on(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = self._base_app(now0)
        zone_on_at = now0 - timedelta(minutes=5)
        app._room_state[BATHROOM]["elsewhere_since"] = zone_on_at
        app._clock["now"] = zone_on_at + timedelta(minutes=10)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_called_once()

    def test_no_fire_before_elsewhere_min(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = self._base_app(now0)
        zone_on_at = now0 - timedelta(minutes=5)
        app._room_state[BATHROOM]["elsewhere_since"] = zone_on_at
        app._clock["now"] = zone_on_at + timedelta(minutes=9)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_not_called()

    def test_does_not_fire_with_two_people_home(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = self._base_app(now0)
        app.states[(KRISTINE, None)] = "home"
        zone_on_at = now0 - timedelta(minutes=5)
        app._room_state[BATHROOM]["elsewhere_since"] = zone_on_at
        app._clock["now"] = zone_on_at + timedelta(minutes=15)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_not_called()

    def test_evidence_predating_last_motion_is_ignored(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = self._base_app(now0)
        app._room_state[BATHROOM]["elsewhere_since"] = now0 - timedelta(minutes=25)
        app._room_state[BATHROOM]["last_motion_at"] = now0 - timedelta(minutes=20)
        app._clock["now"] = now0
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_not_called()

    def test_fresh_motion_clears_elsewhere_evidence(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = self._base_app(now0)
        app._room_state[BATHROOM]["elsewhere_since"] = now0 - timedelta(minutes=5)
        app._on_motion_change(MOTION, "state", "none", "large", {"room": BATHROOM})
        self.assertIsNone(app._room_state[BATHROOM]["elsewhere_since"])


class LongHoldRule(unittest.TestCase):
    def _base_app(self, now0):
        app = make_app(states={(PRESENCE, None): "on", (MIKKEL, None): "home"}, now=now0)
        app._nobody_home_since = None
        app._room_state[BATHROOM]["presence_on_since"] = now0
        return app

    def test_fires_after_hold_and_no_motion_thresholds(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = self._base_app(now0)
        app._room_state[BATHROOM]["last_motion_at"] = now0 + timedelta(minutes=25)
        app._clock["now"] = now0 + timedelta(minutes=60)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_called_once()

    def test_no_fire_if_motion_recent_even_past_hold_threshold(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = self._base_app(now0)
        app._room_state[BATHROOM]["last_motion_at"] = now0 + timedelta(minutes=59)
        app._clock["now"] = now0 + timedelta(minutes=60)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_not_called()

    def test_no_fire_before_hold_threshold(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = self._base_app(now0)
        app._room_state[BATHROOM]["last_motion_at"] = None
        app._clock["now"] = now0 + timedelta(minutes=59)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_not_called()

    def test_no_motion_ever_falls_back_to_session_start(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = self._base_app(now0)
        app._room_state[BATHROOM]["last_motion_at"] = None
        app._clock["now"] = now0 + timedelta(minutes=60)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_called_once()


class OneAlertPerEpisode(unittest.TestCase):
    def test_repeated_evaluation_only_alerts_once(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(states={(PRESENCE, None): "on", (MIKKEL, None): "home"}, now=now0)
        app._nobody_home_since = None
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._room_state[BATHROOM]["last_motion_at"] = None
        for minutes in (60, 61, 65, 90):
            app._clock["now"] = now0 + timedelta(minutes=minutes)
            app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_called_once()

    def test_new_episode_after_presence_off_on_rearms(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(states={(PRESENCE, None): "on", (MIKKEL, None): "home"}, now=now0)
        app._nobody_home_since = None
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._room_state[BATHROOM]["last_motion_at"] = None
        app._clock["now"] = now0 + timedelta(minutes=60)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_called_once()

        app._close_presence_session(BATHROOM, app._clock["now"])
        self.assertFalse(app._room_state[BATHROOM]["stuck"])

        now1 = app._clock["now"] + timedelta(minutes=1)
        app._room_state[BATHROOM]["presence_on_since"] = now1
        app._clock["now"] = now1 + timedelta(minutes=60)
        app._evaluate_room(BATHROOM, app._clock["now"])
        self.assertEqual(app.create_task.call_count, 2)


class NoAlertOnNormalClear(unittest.TestCase):
    def test_no_alert_when_presence_goes_off_before_any_threshold(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(states={(PRESENCE, None): "on", (MIKKEL, None): "home"}, now=now0)
        app._nobody_home_since = None
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._clock["now"] = now0 + timedelta(minutes=3)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app._close_presence_session(BATHROOM, app._clock["now"])
        app.create_task.assert_not_called()
        self.assertFalse(app._room_state[BATHROOM]["stuck"])


class EpisodeClearLogging(unittest.TestCase):
    def test_logs_cleared_with_duration(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(states={(PRESENCE, None): "on", (MIKKEL, None): "home"}, now=now0)
        app._nobody_home_since = None
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._clock["now"] = now0 + timedelta(minutes=60)
        app._evaluate_room(BATHROOM, app._clock["now"])
        app.create_task.assert_called_once()

        app._clock["now"] += timedelta(minutes=5)
        app._close_presence_session(BATHROOM, app._clock["now"])
        cleared_logs = [c for c in app.log_calls if "cleared after" in c[0][0]]
        self.assertEqual(len(cleared_logs), 1)
        self.assertIn("5.0 min", cleared_logs[0][0][0])

    def test_no_cleared_log_when_never_stuck(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(states={(PRESENCE, None): "on"}, now=now0)
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._close_presence_session(BATHROOM, now0 + timedelta(minutes=2))
        cleared_logs = [c for c in app.log_calls if "cleared after" in c[0][0]]
        self.assertEqual(cleared_logs, [])


class OnPresenceChangeDispatch(unittest.TestCase):
    def test_on_edge_seeds_session(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(states={(PRESENCE, None): "on"}, now=now0)
        app._on_presence_change(PRESENCE, "state", "off", "on", {"room": BATHROOM})
        self.assertEqual(app._room_state[BATHROOM]["presence_on_since"], now0)

    def test_off_edge_closes_session_and_commits_metrics(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(states={(PRESENCE, None): "off"}, now=now0)
        app._room_state[BATHROOM]["presence_on_since"] = now0 - timedelta(minutes=10)
        app._on_presence_change(PRESENCE, "state", "on", "off", {"room": BATHROOM})
        self.assertIsNone(app._room_state[BATHROOM]["presence_on_since"])
        day = app._metrics[BATHROOM][app._today[BATHROOM]]
        self.assertEqual(day["sessions"], 1)
        self.assertAlmostEqual(day["presence_on_min"], 10.0, places=3)

    def test_ignores_missing_room_kwarg(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(now=now0)
        app._on_presence_change(PRESENCE, "state", "off", "on", {})
        app._on_presence_change(PRESENCE, "state", "off", "on", None)

    def test_unavailable_transition_is_inert(self):
        now0 = datetime(2026, 9, 23, 10, 0)
        app = make_app(states={(PRESENCE, None): "unavailable"}, now=now0)
        app._room_state[BATHROOM]["presence_on_since"] = now0 - timedelta(minutes=10)
        app._on_presence_change(PRESENCE, "state", "on", "unavailable", {"room": BATHROOM})
        self.assertIsNotNone(app._room_state[BATHROOM]["presence_on_since"])


class MetricsAccumulation(unittest.TestCase):
    def test_multiple_sessions_accumulate_and_track_max(self):
        now0 = datetime(2026, 9, 23, 8, 0)
        app = make_app(now=now0)
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._close_presence_session(BATHROOM, now0 + timedelta(minutes=4))
        app._room_state[BATHROOM]["presence_on_since"] = now0 + timedelta(minutes=10)
        app._close_presence_session(BATHROOM, now0 + timedelta(minutes=20))
        day = app._metrics[BATHROOM][app._today[BATHROOM]]
        self.assertEqual(day["sessions"], 2)
        self.assertAlmostEqual(day["presence_on_min"], 14.0, places=3)
        self.assertAlmostEqual(day["max_session_min"], 10.0, places=3)

    def test_light_on_off_accumulates(self):
        now0 = datetime(2026, 9, 23, 8, 0)
        app = make_app(now=now0)
        app._on_light_change(LIGHT, "state", "off", "on", {"room": BATHROOM})
        app._clock["now"] = now0 + timedelta(minutes=7)
        app._on_light_change(LIGHT, "state", "on", "off", {"room": BATHROOM})
        day = app._metrics[BATHROOM][app._today[BATHROOM]]
        self.assertAlmostEqual(day["light_on_min"], 7.0, places=3)
        self.assertIsNone(app._room_state[BATHROOM]["light_on_since"])


class TodayTotalsLive(unittest.TestCase):
    def test_live_session_counts_toward_today_without_a_completed_session(self):
        now0 = datetime(2026, 9, 23, 8, 0)
        app = make_app(now=now0)
        app._room_state[BATHROOM]["presence_on_since"] = now0
        totals = app._today_totals(BATHROOM, now0 + timedelta(minutes=10))
        self.assertEqual(totals["sessions"], 0)
        self.assertAlmostEqual(totals["presence_on_min"], 10.0, places=3)
        self.assertAlmostEqual(totals["avg_session_min"], 10.0, places=3)

    def test_avg_blends_completed_and_live_session(self):
        now0 = datetime(2026, 9, 23, 8, 0)
        app = make_app(now=now0)
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._close_presence_session(BATHROOM, now0 + timedelta(minutes=20))
        app._room_state[BATHROOM]["presence_on_since"] = now0 + timedelta(minutes=30)
        totals = app._today_totals(BATHROOM, now0 + timedelta(minutes=40))
        self.assertEqual(totals["sessions"], 1)
        self.assertAlmostEqual(totals["presence_on_min"], 30.0, places=3)
        self.assertAlmostEqual(totals["avg_session_min"], 15.0, places=3)

    def test_no_activity_avg_is_zero(self):
        now0 = datetime(2026, 9, 23, 8, 0)
        app = make_app(now=now0)
        totals = app._today_totals(BATHROOM, now0)
        self.assertEqual(totals["avg_session_min"], 0.0)


class MidnightRollover(unittest.TestCase):
    def test_new_day_creates_fresh_bucket_and_keeps_yesterday(self):
        day0 = datetime(2026, 9, 22, 23, 0)
        app = make_app(now=day0)
        app._room_state[BATHROOM]["presence_on_since"] = day0
        app._close_presence_session(BATHROOM, day0 + timedelta(minutes=10))
        self.assertEqual(app._today[BATHROOM], "2026-09-22")

        day1 = datetime(2026, 9, 23, 0, 30)
        app._roll_to(BATHROOM, day1)
        self.assertEqual(app._today[BATHROOM], "2026-09-23")
        self.assertEqual(app._metrics[BATHROOM]["2026-09-22"]["sessions"], 1)
        self.assertEqual(app._metrics[BATHROOM]["2026-09-23"]["sessions"], 0)

    def test_spanning_session_is_split_at_midnight(self):
        day0 = datetime(2026, 9, 22, 23, 40)
        app = make_app(now=day0)
        app._room_state[BATHROOM]["presence_on_since"] = day0

        day1 = datetime(2026, 9, 23, 0, 20)
        app._roll_to(BATHROOM, day1)

        yesterday = app._metrics[BATHROOM]["2026-09-22"]
        self.assertAlmostEqual(yesterday["presence_on_min"], 20.0, places=3)
        self.assertAlmostEqual(yesterday["max_session_min"], 20.0, places=3)
        self.assertEqual(app._room_state[BATHROOM]["presence_on_since"], datetime(2026, 9, 23, 0, 0))

        app._close_presence_session(BATHROOM, day1)
        today = app._metrics[BATHROOM]["2026-09-23"]
        self.assertAlmostEqual(today["presence_on_min"], 20.0, places=3)
        self.assertEqual(today["sessions"], 1)

    def test_spanning_light_session_is_split_at_midnight(self):
        day0 = datetime(2026, 9, 22, 23, 50)
        app = make_app(now=day0)
        app._room_state[BATHROOM]["light_on_since"] = day0

        day1 = datetime(2026, 9, 23, 0, 5)
        app._roll_to(BATHROOM, day1)

        self.assertAlmostEqual(app._metrics[BATHROOM]["2026-09-22"]["light_on_min"], 10.0, places=3)
        self.assertEqual(app._room_state[BATHROOM]["light_on_since"], datetime(2026, 9, 23, 0, 0))

    def test_prune_keeps_only_14_most_recent_days(self):
        app = make_app(now=datetime(2026, 1, 1, 0, 0))
        app._metrics[BATHROOM] = {f"2026-01-{i + 1:02d}": psw._fresh_day() for i in range(20)}
        app._today[BATHROOM] = "2026-01-20"
        app._prune_old_days(BATHROOM)
        self.assertEqual(len(app._metrics[BATHROOM]), 14)
        self.assertEqual(min(app._metrics[BATHROOM]), "2026-01-07")


class PriorDaysAndAverages(unittest.TestCase):
    def test_avg_session_min_7d_averages_daily_averages(self):
        app = make_app(now=datetime(2026, 9, 23, 12, 0))
        app._today[BATHROOM] = "2026-09-23"
        app._metrics[BATHROOM] = {
            "2026-09-23": psw._fresh_day(),
            "2026-09-22": {**psw._fresh_day(), "sessions": 2, "presence_on_min": 20.0},
            "2026-09-21": {**psw._fresh_day(), "sessions": 4, "presence_on_min": 60.0},
        }
        self.assertAlmostEqual(app._avg_session_min_7d(BATHROOM), 12.5, places=3)

    def test_days_with_zero_sessions_are_excluded_from_session_average(self):
        app = make_app(now=datetime(2026, 9, 23, 12, 0))
        app._today[BATHROOM] = "2026-09-23"
        app._metrics[BATHROOM] = {
            "2026-09-23": psw._fresh_day(),
            "2026-09-22": psw._fresh_day(),
            "2026-09-21": {**psw._fresh_day(), "sessions": 2, "presence_on_min": 20.0},
        }
        self.assertAlmostEqual(app._avg_session_min_7d(BATHROOM), 10.0, places=3)

    def test_only_last_7_prior_days_considered(self):
        app = make_app(now=datetime(2026, 9, 23, 12, 0))
        app._today[BATHROOM] = "2026-09-23"
        days = {"2026-09-23": psw._fresh_day()}
        for i in range(1, 10):
            days[f"2026-09-{23 - i:02d}"] = {**psw._fresh_day(), "sessions": 1, "presence_on_min": float(i)}
        app._metrics[BATHROOM] = days
        self.assertEqual(len(app._prior_days(BATHROOM)), 7)

    def test_light_on_min_7d_avg(self):
        app = make_app(now=datetime(2026, 9, 23, 12, 0))
        app._today[BATHROOM] = "2026-09-23"
        app._metrics[BATHROOM] = {
            "2026-09-23": psw._fresh_day(),
            "2026-09-22": {**psw._fresh_day(), "light_on_min": 10.0},
            "2026-09-21": {**psw._fresh_day(), "light_on_min": 20.0},
        }
        self.assertAlmostEqual(app._light_on_min_7d_avg(BATHROOM), 15.0, places=3)

    def test_no_prior_days_returns_zero(self):
        app = make_app(now=datetime(2026, 9, 23, 12, 0))
        app._today[BATHROOM] = "2026-09-23"
        app._metrics[BATHROOM] = {"2026-09-23": psw._fresh_day()}
        self.assertEqual(app._avg_session_min_7d(BATHROOM), 0.0)
        self.assertEqual(app._light_on_min_7d_avg(BATHROOM), 0.0)


class AnomalyAlert(unittest.TestCase):
    def _app_with_prior_days(self, now0, prior_on_minutes):
        app = make_app(now=now0)
        app._today[BATHROOM] = now0.strftime("%Y-%m-%d")
        days = {app._today[BATHROOM]: psw._fresh_day()}
        for i, minutes in enumerate(prior_on_minutes, start=1):
            date_str = (now0 - timedelta(days=i)).strftime("%Y-%m-%d")
            days[date_str] = {**psw._fresh_day(), "sessions": 1, "presence_on_min": minutes}
        app._metrics[BATHROOM] = days
        return app

    def test_no_alert_with_fewer_than_3_prior_days(self):
        now0 = datetime(2026, 9, 23, 23, 55)
        app = self._app_with_prior_days(now0, [20.0, 25.0])
        app._room_state[BATHROOM]["presence_on_since"] = now0 - timedelta(minutes=200)
        app._check_anomaly(BATHROOM, now0)
        app.create_task.assert_not_called()

    def test_alert_when_over_floor_threshold(self):
        # avg prior = 5 min/day -> 2.5x = 12.5, so the 60-min floor is what applies.
        now0 = datetime(2026, 9, 23, 23, 55)
        app = self._app_with_prior_days(now0, [5.0, 5.0, 5.0])
        app._room_state[BATHROOM]["presence_on_since"] = now0 - timedelta(minutes=65)
        app._check_anomaly(BATHROOM, now0)
        app.create_task.assert_called_once()

    def test_no_alert_under_floor_threshold(self):
        now0 = datetime(2026, 9, 23, 23, 55)
        app = self._app_with_prior_days(now0, [5.0, 5.0, 5.0])
        app._room_state[BATHROOM]["presence_on_since"] = now0 - timedelta(minutes=30)
        app._check_anomaly(BATHROOM, now0)
        app.create_task.assert_not_called()

    def test_alert_when_over_multiplier_threshold(self):
        # avg prior = 100 min/day -> 2.5x = 250, above the 60-min floor.
        now0 = datetime(2026, 9, 23, 23, 55)
        app = self._app_with_prior_days(now0, [100.0, 100.0, 100.0])
        app._room_state[BATHROOM]["presence_on_since"] = now0 - timedelta(minutes=260)
        app._check_anomaly(BATHROOM, now0)
        app.create_task.assert_called_once()

    def test_no_alert_when_under_multiplier_threshold(self):
        now0 = datetime(2026, 9, 23, 23, 55)
        app = self._app_with_prior_days(now0, [100.0, 100.0, 100.0])
        app._room_state[BATHROOM]["presence_on_since"] = now0 - timedelta(minutes=240)
        app._check_anomaly(BATHROOM, now0)
        app.create_task.assert_not_called()


class PublishAttributesNeverFalsy(unittest.TestCase):
    """Regression guard for the AppDaemon 4.5.13 set_state falsy-attribute-drop bug - every
    published attribute must be a non-empty string (room_active.py convention)."""

    def test_fresh_room_publishes_all_truthy_string_attributes(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(now=now0)
        app._publish(BATHROOM, now0)
        kwargs = app.set_state.call_args.kwargs
        self.assertEqual(kwargs["state"], "0.0")
        attrs = kwargs["attributes"]
        expected_keys = {
            "sessions_today", "presence_on_min_today", "max_session_min_today",
            "light_on_min_today", "avg_session_min_7d", "light_on_min_7d_avg",
            "stuck_episodes_today", "last_stuck_at", "unit_of_measurement",
        }
        self.assertEqual(set(attrs.keys()), expected_keys)
        for key, value in attrs.items():
            self.assertIsInstance(value, str)
            self.assertTrue(value, f"attribute {key!r} is falsy: {value!r}")

    def test_entity_id_and_replace_true(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(now=now0)
        app._publish(BATHROOM, now0)
        args, kwargs = app.set_state.call_args
        self.assertEqual(args[0], f"sensor.{BATHROOM}_presence_stats")
        self.assertTrue(kwargs["replace"])

    def test_last_stuck_at_defaults_to_never(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(now=now0)
        app._publish(BATHROOM, now0)
        self.assertEqual(app.set_state.call_args.kwargs["attributes"]["last_stuck_at"], "never")

    def test_stuck_episode_updates_published_attributes(self):
        now0 = datetime(2026, 9, 23, 12, 0)
        app = make_app(states={(PRESENCE, None): "on"}, now=now0)
        app._nobody_home_since = now0
        app._room_state[BATHROOM]["presence_on_since"] = now0
        app._evaluate_room(BATHROOM, now0 + timedelta(minutes=5))
        app._publish(BATHROOM, now0 + timedelta(minutes=5))
        attrs = app.set_state.call_args.kwargs["attributes"]
        self.assertEqual(attrs["stuck_episodes_today"], "1")
        self.assertNotEqual(attrs["last_stuck_at"], "never")


class PersistenceReload(unittest.TestCase):
    def test_round_trip_preserves_days_and_last_stuck_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "presence_stuck_watch_state.json"
            now0 = datetime(2026, 9, 23, 12, 0)

            app_a = _persist_app(state_file)
            app_a._metrics[BATHROOM]["2026-09-22"] = {
                "sessions": 3, "presence_on_min": 45.5, "max_session_min": 20.0,
                "light_on_min": 40.0, "stuck_episodes": 1,
            }
            app_a._today[BATHROOM] = "2026-09-23"
            app_a._room_state[BATHROOM]["last_stuck_at"] = now0 - timedelta(hours=2)
            app_a._save_metrics()

            app_b = _persist_app(state_file)
            app_b._load_metrics()

            self.assertEqual(app_b._metrics[BATHROOM]["2026-09-22"]["presence_on_min"], 45.5)
            self.assertEqual(app_b._metrics[BATHROOM]["2026-09-22"]["sessions"], 3)
            self.assertEqual(app_b._room_state[BATHROOM]["last_stuck_at"], now0 - timedelta(hours=2))

    def test_room_dropped_from_config_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "presence_stuck_watch_state.json"
            state_file.write_text(json.dumps({"rooms": {"kitchen": {"days": {}, "last_stuck_at": None}}}))
            app = _persist_app(state_file)
            app._load_metrics()
            self.assertNotIn("kitchen", app._metrics)

    def test_missing_file_is_not_an_error(self):
        state_file = Path(tempfile.gettempdir()) / "presence-stuck-watch-does-not-exist" / "x.json"
        app = _persist_app(state_file)
        app._load_metrics()
        self.assertEqual(app._metrics[BATHROOM], {})

    def test_corrupt_json_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "presence_stuck_watch_state.json"
            state_file.write_text("{not valid json")
            app = _persist_app(state_file)
            app._load_metrics()
            self.assertEqual(app._metrics[BATHROOM], {})


BASE_ARGS = {"persons": list(PERSONS), "rooms": dict(ROOMS_RAW)}


def make_full_app():
    """PresenceStuckWatch with initialize() actually run (AppDaemon primitives stubbed only,
    persistence stubbed out) - used to verify listener registration and the startup publish,
    same shape as room_active.py's own InitializeRegistration harness."""
    app = psw.PresenceStuckWatch.__new__(psw.PresenceStuckWatch)
    app.args = dict(BASE_ARGS)
    app.states = {}
    app.get_state = lambda entity, attribute=None, **kw: app.states.get((entity, attribute))
    app.log = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock(return_value="tick-handle")
    app.run_daily = MagicMock(return_value="anomaly-handle")
    app.set_state = MagicMock()
    app.create_task = MagicMock()
    room_active_stub = SimpleNamespace(zones={"bathroom": [], "guest_bathroom": [], "kitchen": []})
    notifier_stub = MagicMock()
    app.get_app = MagicMock(
        side_effect=lambda name: {"MobileNotifier": notifier_stub, "RoomActive": room_active_stub}.get(name)
    )
    app._save_metrics = MagicMock()
    app._load_metrics = MagicMock()
    app.initialize()
    return app


class InitializeRegistration(unittest.TestCase):
    def setUp(self):
        self.app = make_full_app()

    def _listened_entities(self):
        return {c.args[1] for c in self.app.listen_state.call_args_list if len(c.args) > 1}

    def test_listens_to_presence_motion_light(self):
        entities = self._listened_entities()
        self.assertIn(PRESENCE, entities)
        self.assertIn(MOTION, entities)
        self.assertIn(LIGHT, entities)

    def test_listens_to_other_zones_only(self):
        entities = self._listened_entities()
        self.assertIn("binary_sensor.kitchen_active", entities)
        self.assertNotIn("binary_sensor.bathroom_active", entities)
        self.assertNotIn("binary_sensor.guest_bathroom_active", entities)

    def test_listens_to_every_person(self):
        entities = self._listened_entities()
        for p in PERSONS:
            self.assertIn(p, entities)

    def test_listens_for_plugin_started(self):
        calls = [c for c in self.app.listen_event.call_args_list if c.args and c.args[1] == "plugin_started"]
        self.assertEqual(len(calls), 1)

    def test_registers_60s_tick_and_daily_anomaly_check(self):
        self.app.run_every.assert_called_once()
        self.assertEqual(self.app.run_every.call_args.args[2], 60)
        self.app.run_daily.assert_called_once()

    def test_initial_publish_happens(self):
        self.app.set_state.assert_called_once()
        self.assertEqual(self.app.set_state.call_args.args[0], f"sensor.{BATHROOM}_presence_stats")


if __name__ == "__main__":
    unittest.main()
