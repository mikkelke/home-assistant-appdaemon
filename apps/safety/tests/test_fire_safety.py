# tests/test_fire_safety.py - unit tests for the Bosch Twinguard phase machine.
# Same __new__ + monkeypatched-callables harness as apps/climate/tests/test_climate_alarm.py.
# Run from repo root: python3 -m unittest discover -s apps/safety/tests -q

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

import fire_safety as fs  # noqa: E402

FIXED_NOW = datetime(2026, 9, 11, 18, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """datetime subclass whose now() is frozen at FIXED_NOW - lets the FSM's timer math be
    tested deterministically by pre-seeding since/off_since/hushed_until as offsets from
    FIXED_NOW instead of advancing a clock. fromisoformat stays the real inherited impl."""

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


class FakeMobileNotifier:
    def __init__(self):
        self.calls = []

    async def notify(self, **kwargs):
        self.calls.append(kwargs)

    async def clear_notification(self, **kwargs):
        pass


class FakeSonosNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, **kwargs):
        self.calls.append(kwargs)


def _make_app(**overrides):
    """A fully-wired FireSafety instance with every yaml-configurable attribute defaulted
    (mirrors initialize()'s defaults) and a fresh-install state (mirrors _load_state() with
    no state file). AppDaemon APIs are mocked; get_state reads from `app.states` (a plain
    dict the test populates); submit_to_executor runs its target inline; create_task queues
    onto app._test_tasks for tests that exercise the sync-listener wiring to `await
    asyncio.gather(*app._test_tasks)`."""
    app = fs.FireSafety.__new__(fs.FireSafety)

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.call_service = AsyncMock()
    app.set_state = AsyncMock()
    app.fire_event = AsyncMock()
    app.run_in = AsyncMock(return_value="handle")
    app.run_every = MagicMock()
    app.run_daily = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.get_app = MagicMock(return_value=None)
    app.submit_to_executor = MagicMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))

    app._test_tasks = []

    def create_task(coro):
        t = asyncio.ensure_future(coro)
        app._test_tasks.append(t)
        return t

    app.create_task = create_task

    app.states = {}

    async def get_state(entity_id, attribute=None):
        return app.states.get(entity_id)

    app.get_state = get_state

    app.mobile_notifier = FakeMobileNotifier()
    app.sonos_notifier = FakeSonosNotifier()

    # --- config defaults (mirrors initialize()'s yaml defaults) ---
    app.smoke_entity = "binary_sensor.kitchen_smoke_alarm_smoke"
    app.siren_state_entity = "sensor.kitchen_smoke_alarm_siren_state"
    app.battery_entity = "sensor.kitchen_smoke_alarm_battery"
    app.eco2_entity = "sensor.kitchen_smoke_alarm_eco2"
    app.aqi_entity = "sensor.kitchen_smoke_alarm_aqi"
    app.alarm_select_entity = "select.kitchen_smoke_alarm_alarm"
    app.self_test_switch_entity = "switch.kitchen_smoke_alarm_self_test"
    app.siren_alarm_values = ["fire"]
    app.siren_pre_alarm_values = ["pre_alarm"]
    app.siren_silenced_values = ["silenced"]
    app.siren_self_test_values = ["self_test"]
    app.hush_button_entity = "input_button.fire_safety_hush"
    app.clear_button_entity = "input_button.fire_safety_clear"
    app.test_button_entity = "input_button.fire_safety_test"
    app.cooking_mode_entity = "input_boolean.kitchen_cooking_mode"
    app.user_name_fallback = {}
    app._person_by_user_id = {}
    app.publish_entity = "sensor.fire_safety"
    app.dry_run = False
    app.test_audience = ["mikkel"]
    app.hush_minutes = 10
    app.max_hushes_per_episode = 2
    app.pre_alarm_timeout_min = 10
    app.cooldown_confirm_s = 60
    app.cooldown_clear_min = 15
    app.offline_after_min = 30
    app.cooking_mode_minutes = 45
    app.self_test_window_min = 6
    app.reannounce_interval_s = 45
    app.repush_interval_s = 120
    app.repush_interval_acked_s = 180
    app.relights_interval_s = 60
    app.tick_interval_s = 5
    app.battery_low_pct = 20
    app.test_overdue_days = 35
    app.fault_push_throttle_hours = 48
    app.push_tag = "fire_alarm"
    app.push_category = "fire_alarm"
    app.health_category = "fire_health"
    app.health_notify_target = "home"
    app.alarm_always_notify = ["mikkel"]
    app.alarm_notify_if_home = {"kristine": "person.kristine", "claudia": "person.claudia"}
    app.alarm_nobody_home = "always_only"
    app.sonos_kitchen_entity = "media_player.kitchen"
    app.sonos_all_entity = "media_player.sonos_tts_all"
    app.sonos_kitchen_volume = 0.15
    app.sonos_alarm_volume = 0.7
    app.alarm_lights = ["light.hallway_lights"]
    app.alarm_light_manual_booleans = ["input_boolean.hallway_lights_manual"]
    app.media_pause_players = ["media_player.sonos_tts_all"]
    app._source_entities = [app.smoke_entity, app.siren_state_entity]
    app._tz = None

    # --- state defaults (mirrors a fresh-install _load_state()) ---
    app.phase = "clear"
    app.since = FIXED_NOW
    app.episode_id = None
    app.episode_started_at = None
    app.hushed_until = None
    app.hushed_by = None
    app.hush_count = 0
    app.ack_by = None
    app.last_push_at = None
    app.last_announce_at = None
    app.last_lights_assert_at = None
    app.last_self_test_at = None
    app.last_smoke = None
    app.last_siren = None
    app.self_test_until = None
    app.cooking_until = None
    app.unavailable_since = None
    app.off_since = None
    app.last_fault_push_at = {}
    app.light_snapshot = {}
    app.light_snapshot_episode = None
    app.episode_notified = None

    for key, value in overrides.items():
        setattr(app, key, value)
    return app


class _FrozenTimeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_datetime = fs.datetime
        fs.datetime = _FrozenDatetime

    def tearDown(self):
        fs.datetime = self._orig_datetime


class PureHelpers(unittest.TestCase):
    def test_siren_matches_case_insensitive_exact_trimmed(self):
        self.assertTrue(fs._siren_matches("Fire", ["fire"]))
        self.assertTrue(fs._siren_matches("  pre_alarm  ", ["pre_alarm"]))
        self.assertFalse(fs._siren_matches("fire_alarm", ["fire"]))
        self.assertFalse(fs._siren_matches("clear", ["fire", "pre_alarm"]))
        self.assertFalse(fs._siren_matches(None, ["fire"]))
        self.assertFalse(fs._siren_matches("fire", None))
        self.assertFalse(fs._siren_matches("fire", []))

    def test_band_iaq_breakpoints(self):
        self.assertEqual(fs._band(None, fs.IAQ_BREAKPOINTS), "unknown")
        self.assertEqual(fs._band(10, fs.IAQ_BREAKPOINTS), "fresh")
        self.assertEqual(fs._band(50, fs.IAQ_BREAKPOINTS), "fresh")
        self.assertEqual(fs._band(75, fs.IAQ_BREAKPOINTS), "good")
        self.assertEqual(fs._band(150, fs.IAQ_BREAKPOINTS), "stuffy")
        self.assertEqual(fs._band(250, fs.IAQ_BREAKPOINTS), "poor")

    def test_band_eco2_breakpoints(self):
        self.assertEqual(fs._band(None, fs.ECO2_BREAKPOINTS), "unknown")
        self.assertEqual(fs._band(700, fs.ECO2_BREAKPOINTS), "fresh")
        self.assertEqual(fs._band(1000, fs.ECO2_BREAKPOINTS), "good")
        self.assertEqual(fs._band(1500, fs.ECO2_BREAKPOINTS), "stuffy")
        self.assertEqual(fs._band(2500, fs.ECO2_BREAKPOINTS), "poor")


class TransitionTable(_FrozenTimeTestCase):
    """One test per edge in the phase table (module docstring / assignment spec)."""

    async def test_clear_to_alarm_on_smoke(self):
        app = _make_app()
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")
        self.assertIsNotNone(app.episode_id)

    async def test_clear_to_alarm_on_siren_alarm_value(self):
        app = _make_app()
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "Fire"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")

    async def test_clear_to_pre_alarm_on_siren_pre_alarm_value(self):
        app = _make_app()
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        await app._evaluate()
        self.assertEqual(app.phase, "pre_alarm")

    async def test_clear_stays_clear_when_nothing_matches(self):
        app = _make_app()
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "clear")

    async def test_pre_alarm_stays_pre_alarm_when_smoke_on_and_siren_still_pre_alarm(self):
        # Twinguard's own smoke bit is SET during a real pre_alarm (verified against
        # bosch.js), so smoke=="on" must NOT alone escalate - only the siren value can.
        app = _make_app(phase="pre_alarm", since=FIXED_NOW)
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "pre_alarm"
        await app._evaluate()
        self.assertEqual(app.phase, "pre_alarm")
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_pre_alarm_to_alarm_on_siren_fire(self):
        app = _make_app(phase="pre_alarm", since=FIXED_NOW, last_siren="pre_alarm", last_smoke="on")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")

    async def test_clear_to_pre_alarm_when_smoke_already_on(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "pre_alarm"
        await app._evaluate()
        self.assertEqual(app.phase, "pre_alarm")
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_smoke_on_with_siren_none_is_alarm_fallback(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "on"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")

    async def test_burglar_siren_ignored_for_phases(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "burglar"
        await app._evaluate()
        self.assertEqual(app.phase, "clear")

    async def test_burglar_edge_logs_once_at_info(self):
        app = _make_app(phase="clear", last_siren="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "burglar"
        await app._evaluate()
        await app._evaluate()

        def is_burglar_info(call):
            args, kw = call
            return kw.get("level") == "INFO" and "burglar" in args[0].lower()

        self.assertEqual(len(list(filter(is_burglar_info, app.log_calls))), 1)

    async def test_pre_alarm_to_clear_on_siren_clear(self):
        app = _make_app(phase="pre_alarm", since=FIXED_NOW)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "clear")

    async def test_pre_alarm_to_clear_on_10min_timeout(self):
        app = _make_app(phase="pre_alarm", since=FIXED_NOW - timedelta(minutes=11))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        await app._evaluate()
        self.assertEqual(app.phase, "clear")

    async def test_pre_alarm_stays_before_timeout(self):
        app = _make_app(phase="pre_alarm", since=FIXED_NOW - timedelta(minutes=5))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        await app._evaluate()
        self.assertEqual(app.phase, "pre_alarm")

    async def test_alarm_to_cooldown_after_60s_smoke_off(self):
        app = _make_app(
            phase="alarm", episode_id="20260911180000",
            off_since=FIXED_NOW - timedelta(seconds=61),
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "cooldown")

    async def test_alarm_stays_before_60s_smoke_off(self):
        app = _make_app(
            phase="alarm", episode_id="20260911180000",
            off_since=FIXED_NOW - timedelta(seconds=30),
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")

    async def test_hushed_to_alarm_on_expiry_while_smoke_still_on(self):
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1, last_smoke="on",
            hushed_until=FIXED_NOW - timedelta(seconds=1),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")

    async def test_hushed_to_alarm_on_new_smoke_edge_before_expiry(self):
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1, last_smoke="off",
            hushed_until=FIXED_NOW + timedelta(minutes=9),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")

    async def test_hushed_stays_hushed_while_smoke_on_and_not_expired(self):
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1, last_smoke="on",
            hushed_until=FIXED_NOW + timedelta(minutes=9),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "hushed")

    async def test_hushed_to_cooldown_after_60s_smoke_off(self):
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1, last_smoke="off",
            hushed_until=FIXED_NOW + timedelta(minutes=9),
            off_since=FIXED_NOW - timedelta(seconds=61),
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "cooldown")

    async def test_cooldown_to_alarm_immediately_on_smoke_same_episode(self):
        app = _make_app(phase="cooldown", episode_id="X", hush_count=1)
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.episode_id, "X")
        self.assertEqual(app.hush_count, 1)

    async def test_cooldown_to_clear_after_15min(self):
        app = _make_app(
            phase="cooldown", since=FIXED_NOW - timedelta(minutes=16),
            episode_id="X", episode_started_at=FIXED_NOW - timedelta(minutes=20),
            hush_count=1,
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "clear")
        self.assertIsNone(app.episode_id)
        self.assertEqual(app.hush_count, 0)

    async def test_cooldown_stays_before_15min(self):
        app = _make_app(phase="cooldown", since=FIXED_NOW - timedelta(minutes=5), episode_id="X")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "cooldown")


class OfflineHandling(_FrozenTimeTestCase):
    async def test_first_unavailable_tick_records_timestamp_without_offline(self):
        app = _make_app(phase="clear", unavailable_since=None)
        app.states[app.smoke_entity] = None
        await app._evaluate()
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.unavailable_since, FIXED_NOW)

    async def test_not_offline_before_30_minutes(self):
        app = _make_app(phase="clear", unavailable_since=FIXED_NOW - timedelta(minutes=29))
        app.states[app.smoke_entity] = None
        await app._evaluate()
        self.assertEqual(app.phase, "clear")

    async def test_offline_after_30_minutes(self):
        app = _make_app(phase="clear", unavailable_since=FIXED_NOW - timedelta(minutes=31))
        app.states[app.smoke_entity] = None
        await app._evaluate()
        self.assertEqual(app.phase, "offline")

    async def test_offline_recovers_to_alarm_when_smoke_on(self):
        app = _make_app(phase="offline")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")

    async def test_offline_recovers_to_clear_when_smoke_off(self):
        app = _make_app(phase="offline")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "clear")


class HushBehavior(_FrozenTimeTestCase):
    async def test_hush_from_alarm_transitions_and_writes_stop_once(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0)
        await app._hush(FIXED_NOW, "the dashboard")
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hush_count, 1)
        self.assertEqual(app.hushed_by, "the dashboard")
        stop_calls = [c for c in app.call_service.call_args_list if c.args[0] == "select/select_option"]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(stop_calls[0].kwargs.get("option"), "stop")

    async def test_hush_ignored_outside_alarm_phase(self):
        app = _make_app(phase="clear")
        await app._hush(FIXED_NOW, "the dashboard")
        self.assertEqual(app.phase, "clear")
        app.call_service.assert_not_called()

    async def test_hush_bounded_rejects_at_limit(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=2, max_hushes_per_episode=2)
        await app._hush(FIXED_NOW, "the dashboard")
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.hush_count, 2)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertIn("Hush limit reached", app.mobile_notifier.calls[0]["message"])

    async def test_second_hush_within_limit_succeeds(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=1, max_hushes_per_episode=2)
        await app._hush(FIXED_NOW, "the dashboard")
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hush_count, 2)

    async def test_re_alarm_during_hush_escalates(self):
        app = _make_app(
            phase="hushed", episode_id="E1", hush_count=1, last_smoke="on",
            hushed_until=FIXED_NOW - timedelta(seconds=1),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.episode_id, "E1")
        self.assertEqual(app.hush_count, 1)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertEqual(len(app.sonos_notifier.calls), 1)
        light_calls = [c for c in app.call_service.call_args_list if c.args[0] == "light/turn_on"]
        self.assertEqual(len(light_calls), 1)

    async def test_physical_button_inferred_hush(self):
        # Edge into the verified "silenced" siren value - see _physical_hush_signal.
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0, last_siren="fire")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "silenced"
        await app._evaluate()
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hushed_by, "the button on the alarm")

    async def test_siren_clear_while_alarming_is_not_inferred_hush(self):
        # "clear" is not "silenced" - must not misfire on any non-silenced reading.
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0, last_siren="fire")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")

    def _button_press_data(self, entity, user_id=None):
        return {
            "entity_id": entity,
            "old_state": {"state": "2026-09-11T17:00:00+00:00"},
            "new_state": {
                "state": "2026-09-11T18:00:00+00:00",
                "context": {"user_id": user_id} if user_id else {},
            },
        }

    async def test_hush_button_press_wiring(self):
        app = _make_app(phase="alarm", episode_id="E1")
        app._on_button_state_changed(
            "state_changed", self._button_press_data(app.hush_button_entity), {}
        )
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hushed_by, "the dashboard")

    async def test_hush_button_resolves_actor_from_person_entity(self):
        app = _make_app(phase="alarm", episode_id="E1")
        app.states["person"] = ["person.kristine"]
        app.states["person.kristine"] = {"attributes": {"user_id": "uid-123", "friendly_name": "Kristine"}}
        app._on_button_state_changed(
            "state_changed", self._button_press_data(app.hush_button_entity, "uid-123"), {}
        )
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hushed_by, "Kristine")

    async def test_hush_button_resolves_actor_from_fallback_map(self):
        app = _make_app(phase="alarm", episode_id="E1", user_name_fallback={"uid456": "Claudia"})
        app._on_button_state_changed(
            "state_changed", self._button_press_data(app.hush_button_entity, "uid-456"), {}
        )
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.hushed_by, "Claudia")

    async def test_clear_button_resolves_actor_and_reports_it(self):
        app = _make_app(phase="alarm", episode_id="E1")
        app.states[app.smoke_entity] = "off"
        app.states["person"] = ["person.mikkel"]
        app.states["person.mikkel"] = {"attributes": {"user_id": "uid-999", "friendly_name": "Mikkel"}}
        app._on_button_state_changed(
            "state_changed", self._button_press_data(app.clear_button_entity, "uid-999"), {}
        )
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.phase, "cooldown")
        self.assertEqual(app.fire_event.call_args.kwargs.get("by"), "Mikkel")

    async def test_notification_hush_action_for_current_episode(self):
        app = _make_app(phase="alarm", episode_id="20260911180000")
        app._on_notification_action(
            "mobile_app_notification_action",
            {"action": "FIRE_HUSH_20260911180000_mikkel"},
            {},
        )
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hushed_by, "Mikkel")

    async def test_notification_hush_action_stale_episode_ignored(self):
        app = _make_app(phase="alarm", episode_id="20260911180000")
        app._on_notification_action(
            "mobile_app_notification_action",
            {"action": "FIRE_HUSH_OLDEPISODE_mikkel"},
            {},
        )
        self.assertEqual(app._test_tasks, [])
        self.assertEqual(app.phase, "alarm")

    async def test_notification_ack_action_sets_ack_by(self):
        app = _make_app(phase="alarm", episode_id="E1")
        app._on_notification_action("mobile_app_notification_action", {"action": "FIRE_ACK_E1_kristine"}, {})
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.ack_by, "Kristine")


class AlarmLightSnapshot(_FrozenTimeTestCase):
    async def test_snapshot_taken_once_per_episode_not_on_repeat_assert(self):
        app = _make_app(phase="alarm", episode_id="E1")
        app.states["light.hallway_lights"] = {"state": "off", "attributes": {}}
        reads = []
        underlying = app.get_state

        async def counting_get_state(entity_id, attribute=None):
            if attribute == "all":
                reads.append(entity_id)
            return await underlying(entity_id, attribute=attribute)

        app.get_state = counting_get_state
        await app._assert_lights(FIXED_NOW)
        await app._assert_lights(FIXED_NOW)
        self.assertEqual(reads.count("light.hallway_lights"), 1)
        self.assertEqual(app.light_snapshot_episode, "E1")

    async def test_restore_turns_off_what_was_off_and_reapplies_brightness(self):
        app = _make_app(
            phase="cooldown", episode_id="E1",
            light_snapshot={
                "light.hallway_lights": {"state": "off", "brightness": None},
                "light.kitchen_lights": {"state": "on", "brightness": 128},
            },
            light_snapshot_episode="E1",
        )
        await app._clear_lights()
        off_calls = [c for c in app.call_service.call_args_list if c.args[0] == "light/turn_off"]
        on_calls = [c for c in app.call_service.call_args_list if c.args[0] == "light/turn_on"]
        self.assertEqual(len(off_calls), 1)
        self.assertEqual(off_calls[0].kwargs.get("entity_id"), ["light.hallway_lights"])
        self.assertEqual(len(on_calls), 1)
        self.assertEqual(on_calls[0].kwargs.get("entity_id"), ["light.kitchen_lights"])
        self.assertEqual(on_calls[0].kwargs.get("brightness"), 128)

    async def test_booleans_cleared_after_restore(self):
        app = _make_app(
            phase="cooldown", episode_id="E1",
            light_snapshot={"light.hallway_lights": {"state": "on", "brightness": None}},
            light_snapshot_episode="E1",
        )
        await app._clear_lights()
        boolean_calls = [c for c in app.call_service.call_args_list if c.args[0] == "input_boolean/turn_off"]
        self.assertEqual(len(boolean_calls), 1)
        self.assertEqual(boolean_calls[0].kwargs.get("entity_id"), app.alarm_light_manual_booleans[0])
        turn_on_index = next(i for i, c in enumerate(app.call_service.call_args_list) if c.args[0] == "light/turn_on")
        boolean_index = next(i for i, c in enumerate(app.call_service.call_args_list) if c.args[0] == "input_boolean/turn_off")
        self.assertLess(turn_on_index, boolean_index)
        self.assertEqual(app.light_snapshot, {})
        self.assertIsNone(app.light_snapshot_episode)

    async def test_no_snapshot_falls_back_to_booleans_off_only(self):
        app = _make_app(phase="cooldown", episode_id="E1", light_snapshot={}, light_snapshot_episode=None)
        await app._clear_lights()
        light_calls = [c for c in app.call_service.call_args_list if c.args[0].startswith("light/")]
        self.assertEqual(light_calls, [])
        boolean_calls = [c for c in app.call_service.call_args_list if c.args[0] == "input_boolean/turn_off"]
        self.assertEqual(len(boolean_calls), 1)
        warnings = [a for a, kw in app.log_calls if kw.get("level") == "WARNING"]
        self.assertTrue(any("snapshot" in str(a[0]).lower() for a in warnings))

    async def test_dry_run_assert_lights_takes_no_snapshot_and_zero_calls(self):
        app = _make_app(dry_run=True, phase="alarm", episode_id="E1")
        await app._assert_lights(FIXED_NOW)
        app.call_service.assert_not_called()
        self.assertEqual(app.light_snapshot, {})
        self.assertIsNone(app.light_snapshot_episode)

    async def test_dry_run_clear_lights_makes_zero_calls(self):
        app = _make_app(
            dry_run=True, phase="cooldown", episode_id="E1",
            light_snapshot={"light.hallway_lights": {"state": "on", "brightness": 200}},
            light_snapshot_episode="E1",
        )
        await app._clear_lights()
        app.call_service.assert_not_called()
        self.assertEqual(app.light_snapshot, {"light.hallway_lights": {"state": "on", "brightness": 200}})

    async def test_snapshot_survives_save_load_round_trip(self):
        app = _make_app(
            phase="alarm", episode_id="E1",
            light_snapshot={"light.hallway_lights": {"state": "on", "brightness": 77}},
            light_snapshot_episode="E1",
        )
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app.state_file = path
        app.since = FIXED_NOW
        app._save_state()

        reloaded = fs.FireSafety.__new__(fs.FireSafety)
        reloaded.state_file = path
        reloaded.log = lambda *a, **kw: None
        reloaded._load_state()
        self.assertEqual(reloaded.light_snapshot, {"light.hallway_lights": {"state": "on", "brightness": 77}})
        self.assertEqual(reloaded.light_snapshot_episode, "E1")


class RepeatCadence(_FrozenTimeTestCase):
    async def test_repush_fires_once_interval_elapsed(self):
        app = _make_app(
            phase="alarm", episode_id="E1",
            last_push_at=FIXED_NOW - timedelta(seconds=121),
            last_announce_at=FIXED_NOW, last_lights_assert_at=FIXED_NOW,
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(len(app.mobile_notifier.calls), 1)

    async def test_no_repush_before_interval(self):
        app = _make_app(
            phase="alarm", episode_id="E1",
            last_push_at=FIXED_NOW - timedelta(seconds=30),
            last_announce_at=FIXED_NOW, last_lights_assert_at=FIXED_NOW,
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(len(app.mobile_notifier.calls), 0)

    async def test_repush_interval_extends_once_acked(self):
        app = _make_app(
            phase="alarm", episode_id="E1", ack_by="Mikkel",
            last_push_at=FIXED_NOW - timedelta(seconds=130),
            last_announce_at=FIXED_NOW, last_lights_assert_at=FIXED_NOW,
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(len(app.mobile_notifier.calls), 0)  # 130s < 180s acked interval


class DryRunGating(_FrozenTimeTestCase):
    async def test_dry_run_alarm_makes_zero_call_service_and_zero_notify(self):
        app = _make_app(dry_run=True)
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")
        app.call_service.assert_not_called()
        self.assertEqual(app.mobile_notifier.calls, [])
        self.assertEqual(app.sonos_notifier.calls, [])

    async def test_dry_run_hush_makes_zero_call_service(self):
        app = _make_app(phase="alarm", episode_id="E1", dry_run=True)
        await app._hush(FIXED_NOW, "the dashboard")
        app.call_service.assert_not_called()
        self.assertEqual(app.phase, "hushed")

    async def test_dry_run_self_test_makes_zero_call_service_and_zero_notify(self):
        app = _make_app(dry_run=True)
        await app._run_self_test(FIXED_NOW)
        app.call_service.assert_not_called()
        self.assertEqual(app.mobile_notifier.calls, [])


class TestAudiencePassthrough(_FrozenTimeTestCase):
    async def test_alarm_push_carries_test_audience(self):
        app = _make_app(test_audience=["mikkel"])
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.mobile_notifier.calls[0]["test_audience"], ["mikkel"])

    async def test_health_push_carries_test_audience(self):
        app = _make_app(test_audience=["mikkel"])
        await app._run_self_test(FIXED_NOW)
        self.assertEqual(app.mobile_notifier.calls[0]["test_audience"], ["mikkel"])

    async def test_non_null_test_audience_overrides_computed_audience(self):
        # Kristine is home (would join the computed audience) but the override still wins.
        app = _make_app(test_audience=["mikkel"])
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        app.states["person.kristine"] = "home"
        await app._evaluate()
        self.assertEqual(app.mobile_notifier.calls[0]["test_audience"], ["mikkel"])


class AlarmAudience(_FrozenTimeTestCase):
    async def test_mikkel_always_included(self):
        app = _make_app()
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        audience = await app._episode_audience(FIXED_NOW)
        self.assertEqual(audience, ["mikkel"])

    async def test_housemate_home_is_included(self):
        app = _make_app()
        app.states["person.kristine"] = "home"
        app.states["person.claudia"] = "not_home"
        audience = await app._episode_audience(FIXED_NOW)
        self.assertEqual(audience, ["kristine", "mikkel"])

    async def test_housemate_not_home_or_unavailable_excluded(self):
        app = _make_app()
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "unavailable"
        audience = await app._episode_audience(FIXED_NOW)
        self.assertEqual(audience, ["mikkel"])

    async def test_nobody_home_always_only_yields_mikkel_only(self):
        app = _make_app(alarm_nobody_home="always_only")
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        audience = await app._episode_audience(FIXED_NOW)
        self.assertEqual(audience, ["mikkel"])

    async def test_nobody_home_everyone_yields_all_three(self):
        app = _make_app(alarm_nobody_home="everyone")
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        audience = await app._episode_audience(FIXED_NOW)
        self.assertEqual(audience, ["claudia", "kristine", "mikkel"])

    async def test_departed_housemate_kept_via_episode_notified(self):
        app = _make_app(episode_notified=["mikkel", "kristine"])
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        audience = await app._episode_audience(FIXED_NOW)
        self.assertEqual(audience, ["kristine", "mikkel"])

    async def test_alarm_push_sets_episode_notified(self):
        app = _make_app(test_audience=None)
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        app.states["person.kristine"] = "home"
        await app._evaluate()
        self.assertEqual(app.episode_notified, ["kristine", "mikkel"])
        self.assertEqual(app.mobile_notifier.calls[0]["test_audience"], ["kristine", "mikkel"])

    async def test_departed_housemate_still_gets_all_clear_push(self):
        app = _make_app(
            test_audience=None,
            phase="cooldown", since=FIXED_NOW - timedelta(minutes=16),
            episode_id="E1", episode_started_at=FIXED_NOW - timedelta(minutes=20),
            episode_notified=["mikkel", "kristine"],
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        await app._evaluate()
        self.assertEqual(app.phase, "clear")
        push = app.mobile_notifier.calls[-1]
        self.assertEqual(push["test_audience"], ["kristine", "mikkel"])
        self.assertIsNone(app.episode_notified)

    async def test_episode_notified_reset_on_new_episode(self):
        app = _make_app(test_audience=None, episode_notified=["mikkel", "kristine", "claudia"])
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        self.assertEqual(app.episode_notified, ["mikkel"])


class PushPayloadShape(_FrozenTimeTestCase):
    async def test_alarm_push_payload_shape(self):
        app = _make_app()
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await app._evaluate()
        call = app.mobile_notifier.calls[0]
        self.assertEqual(call["data"], {"data": {"tag": "fire_alarm"}})
        self.assertEqual(call["target"], "all")
        self.assertEqual(call["category"], "fire_alarm")
        self.assertTrue(call["critical"])
        actions = call["per_person_actions"]("mikkel")
        self.assertEqual(actions[0]["action"], f"FIRE_HUSH_{app.episode_id}_mikkel")
        self.assertEqual(actions[1]["action"], f"FIRE_ACK_{app.episode_id}_mikkel")
        self.assertEqual(actions[2], {"action": "URI", "title": "Call 112", "uri": "tel:112"})


class SelfTestSuppression(_FrozenTimeTestCase):
    async def test_siren_alarm_value_ignored_during_window_with_smoke_off(self):
        app = _make_app(phase="clear", self_test_until=FIXED_NOW + timedelta(minutes=2))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "fire"
        await app._evaluate()
        self.assertEqual(app.phase, "clear")

    async def test_smoke_on_during_self_test_still_alarms(self):
        app = _make_app(phase="clear", self_test_until=FIXED_NOW + timedelta(minutes=2))
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")

    async def test_self_test_window_expires_and_stops_suppressing(self):
        app = _make_app(phase="clear", self_test_until=FIXED_NOW - timedelta(seconds=1))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "fire"
        await app._evaluate()
        self.assertEqual(app.phase, "alarm")
        self.assertIsNone(app.self_test_until)

    async def test_live_self_test_sets_last_self_test_at_and_suppresses(self):
        app = _make_app(phase="clear", last_siren="clear", last_self_test_at=None)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "self_test"
        await app._evaluate()
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.last_self_test_at, FIXED_NOW)

    async def test_live_self_test_does_not_reset_last_self_test_at_every_tick(self):
        earlier = FIXED_NOW - timedelta(minutes=1)
        app = _make_app(phase="clear", last_siren="self_test", last_self_test_at=earlier)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "self_test"
        await app._evaluate()
        self.assertEqual(app.last_self_test_at, earlier)


class CookingModeSuppression(_FrozenTimeTestCase):
    async def test_pre_alarm_suppressed_while_cooking_active(self):
        app = _make_app(phase="clear", cooking_until=FIXED_NOW + timedelta(minutes=10))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        await app._evaluate()
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.sonos_notifier.calls, [])

    async def test_pre_alarm_resumes_and_boolean_turned_off_after_cooking_expires(self):
        app = _make_app(phase="clear", cooking_until=FIXED_NOW - timedelta(seconds=1))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        await app._evaluate()
        self.assertEqual(app.phase, "pre_alarm")
        self.assertIsNone(app.cooking_until)
        off_calls = [c for c in app.call_service.call_args_list if c.args[0] == "input_boolean/turn_off"]
        self.assertEqual(len(off_calls), 1)
        self.assertEqual(off_calls[0].kwargs.get("entity_id"), app.cooking_mode_entity)

    async def test_cooking_on_button_sets_cooking_until(self):
        app = _make_app(phase="clear", cooking_until=None)
        app._on_cooking_on(app.cooking_mode_entity, None, "off", "on", {})
        await asyncio.gather(*app._test_tasks)
        self.assertIsNotNone(app.cooking_until)
        self.assertAlmostEqual(
            (app.cooking_until - FIXED_NOW).total_seconds(), 45 * 60, delta=1,
        )


class FaultThrottle(_FrozenTimeTestCase):
    async def test_battery_low_pushes_and_throttles_48h(self):
        # last_self_test_at recent so only the battery_low fault is in play (a fresh
        # install's default last_self_test_at=None would also fire test_overdue here).
        app = _make_app(last_self_test_at=FIXED_NOW - timedelta(days=1))
        app.states[app.battery_entity] = "10"
        await app._check_faults(FIXED_NOW)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        await app._check_faults(FIXED_NOW + timedelta(hours=1))
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        await app._check_faults(FIXED_NOW + timedelta(hours=49))
        self.assertEqual(len(app.mobile_notifier.calls), 2)

    async def test_test_overdue_when_never_tested(self):
        app = _make_app(last_self_test_at=None)
        await app._check_faults(FIXED_NOW)
        messages = [c["message"] for c in app.mobile_notifier.calls]
        self.assertTrue(any("self-test" in m for m in messages))

    async def test_no_fault_push_when_healthy(self):
        app = _make_app(last_self_test_at=FIXED_NOW - timedelta(days=1))
        app.states[app.battery_entity] = "90"
        await app._check_faults(FIXED_NOW)
        self.assertEqual(app.mobile_notifier.calls, [])


class PublishAttributes(_FrozenTimeTestCase):
    async def test_publish_includes_bands_reason_and_dry_run_flag(self):
        app = _make_app(phase="clear", dry_run=True)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.states[app.aqi_entity] = "30"
        app.states[app.eco2_entity] = "900"
        app.states[app.battery_entity] = "15"
        await app._evaluate()
        attrs = app.set_state.call_args.kwargs["attributes"]
        self.assertEqual(attrs["iaq_band"], "fresh")
        self.assertEqual(attrs["eco2_band"], "good")
        self.assertTrue(attrs["battery_low"])
        self.assertTrue(attrs["dry_run"])
        self.assertEqual(attrs["source_entities"], app._source_entities)
        self.assertEqual(attrs["device_available"], True)


class InitResume(_FrozenTimeTestCase):
    """No special "resume alarm on restart" code exists - initialize() + one ordinary
    evaluate() tick must independently re-derive alarm from a live smoke=on reading."""

    async def test_smoke_already_on_at_boot_resumes_alarm_on_first_tick(self):
        app = fs.FireSafety.__new__(fs.FireSafety)
        app.log_calls = []
        app.log = lambda *a, **kw: app.log_calls.append((a, kw))
        app.args = {"dry_run": False, "state_file": "/nonexistent/dir/fire_safety_state.json"}

        mobile_notifier = FakeMobileNotifier()
        sonos_notifier = FakeSonosNotifier()
        app.get_app = MagicMock(side_effect=lambda name: {
            "MobileNotifier": mobile_notifier, "SonosNotifier": sonos_notifier,
        }.get(name))
        app.listen_state = MagicMock()
        app.listen_event = MagicMock()
        app.run_daily = MagicMock()
        app.run_every = MagicMock()
        app.call_service = AsyncMock()
        app.set_state = AsyncMock()
        app.fire_event = AsyncMock()
        app.run_in = AsyncMock(return_value="handle")
        app.submit_to_executor = MagicMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))
        app.create_task = lambda coro: asyncio.ensure_future(coro)

        states = {
            "binary_sensor.kitchen_smoke_alarm_smoke": "on",
            "sensor.kitchen_smoke_alarm_siren_state": "clear",
        }

        async def get_state(entity_id, attribute=None):
            return states.get(entity_id)

        app.get_state = get_state

        app.initialize()
        self.assertEqual(app.phase, "clear")  # fresh install default, before the first tick

        await app._evaluate()

        self.assertEqual(app.phase, "alarm")
        self.assertEqual(len(mobile_notifier.calls), 1)


class PersistenceRoundTrip(unittest.TestCase):
    def _app(self, path):
        app = fs.FireSafety.__new__(fs.FireSafety)
        app.state_file = path
        app.log = lambda *a, **kw: None
        return app

    def test_round_trip(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        app = self._app(path)
        app.phase = "hushed"
        app.since = datetime(2026, 9, 11, 20, 14, tzinfo=timezone.utc)
        app.episode_id = "20260911201000"
        app.episode_started_at = datetime(2026, 9, 11, 20, 10, tzinfo=timezone.utc)
        app.hushed_until = datetime(2026, 9, 11, 20, 24, tzinfo=timezone.utc)
        app.hushed_by = "Kristine"
        app.hush_count = 1
        app.ack_by = None
        app.last_push_at = app.since
        app.last_announce_at = app.since
        app.last_lights_assert_at = app.since
        app.last_self_test_at = None
        app.last_smoke = "on"
        app.last_siren = "fire"
        app.self_test_until = None
        app.cooking_until = None
        app.unavailable_since = None
        app.off_since = None
        app.last_fault_push_at = {"battery_low": app.since}
        app.light_snapshot = {"light.hallway_lights": {"state": "on", "brightness": 128}}
        app.light_snapshot_episode = "20260911201000"
        app.episode_notified = ["mikkel", "kristine"]
        app._save_state()

        reloaded = self._app(path)
        reloaded._load_state()
        self.assertEqual(reloaded.phase, "hushed")
        self.assertEqual(reloaded.episode_id, "20260911201000")
        self.assertEqual(reloaded.episode_started_at, app.episode_started_at)
        self.assertEqual(reloaded.hushed_by, "Kristine")
        self.assertEqual(reloaded.hush_count, 1)
        self.assertEqual(reloaded.since, app.since)
        self.assertEqual(reloaded.hushed_until, app.hushed_until)
        self.assertEqual(reloaded.last_fault_push_at["battery_low"], app.since)
        self.assertEqual(reloaded.light_snapshot, app.light_snapshot)
        self.assertEqual(reloaded.light_snapshot_episode, "20260911201000")
        self.assertEqual(reloaded.episode_notified, ["mikkel", "kristine"])

    def test_missing_file_defaults_to_clear(self):
        app = self._app("/nonexistent/dir/fire_safety_state.json")
        app._load_state()
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.hush_count, 0)
        self.assertEqual(app.last_fault_push_at, {})
        self.assertEqual(app.light_snapshot, {})
        self.assertIsNone(app.light_snapshot_episode)
        self.assertIsNone(app.episode_notified)

    def test_save_leaves_no_tmp_file_behind(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app = self._app(path)
        app.phase = "clear"
        app.since = None
        app.episode_id = None
        app.episode_started_at = None
        app.hushed_until = None
        app.hushed_by = None
        app.hush_count = 0
        app.ack_by = None
        app.last_push_at = None
        app.last_announce_at = None
        app.last_lights_assert_at = None
        app.last_self_test_at = None
        app.last_smoke = None
        app.last_siren = None
        app.self_test_until = None
        app.cooking_until = None
        app.unavailable_since = None
        app.off_since = None
        app.last_fault_push_at = {}
        app.light_snapshot = {}
        app.light_snapshot_episode = None
        app.episode_notified = None
        app._save_state()
        self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
