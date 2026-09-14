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
# apps/notify - so NotifierDeliveryResult can exercise the REAL MobileNotifier, not a
# fake that would hide a "delivered to nobody" bug.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "notify"))

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
import mobile_notifier as mn  # noqa: E402

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
        return 1  # simulates one service delivered - see the real MobileNotifier

    async def clear_notification(self, **kwargs):
        pass


class FakeSonosNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, **kwargs):
        self.calls.append(kwargs)


def _make_app(**overrides):
    """A fully-wired FireSafety instance with every yaml-configurable attribute defaulted
    (mirrors initialize()'s defaults, which mirror the live fire_safety.yaml) and a
    fresh-install state (mirrors _load_state() with no state file). AppDaemon APIs are
    mocked; get_state reads from `app.states` (a plain dict the test populates);
    submit_to_executor runs its target inline; create_task queues onto app._test_tasks for
    tests that exercise the sync-listener wiring (or a backgrounded alarm push) to
    `await asyncio.gather(*app._test_tasks)` - see the _tick() helper below."""
    app = fs.FireSafety.__new__(fs.FireSafety)

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.call_service = AsyncMock()

    async def _set_state(entity_id, state=None, **kw):
        # Mirrors real HA: a set_state() makes that state visible to a later get_state()
        # ("is the published entity still there" checks rely on this).
        app.states[entity_id] = state

    app.set_state = AsyncMock(side_effect=_set_state)
    app.fire_event = AsyncMock()
    app.run_in = AsyncMock(return_value="handle")
    app.run_every = MagicMock()
    app.run_daily = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.get_app = MagicMock(return_value=None)
    app.submit_to_executor = MagicMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))

    app._eval_lock = asyncio.Lock()
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
    app.test_audience = None
    app.hush_minutes = 10
    app.max_hushes_per_episode = 2
    app.hush_confirm_s = 20
    app.hush_confirm_max_s = 120
    app.pre_alarm_timeout_min = 10
    app.cooldown_confirm_s = 60
    app.cooldown_clear_min = 5
    app.offline_after_min = 30
    app.cooking_mode_minutes = 45
    app.self_test_floor_s = 60
    app.self_test_max_min = 10
    app.reannounce_interval_s = 45
    app.repush_interval_s = 120
    app.repush_interval_acked_s = 180
    app.relights_interval_s = 60
    app.tick_interval_s = 5
    app.stale_grace_s = 120
    app.smoke_fallback_s = 30
    app.episode_reuse_min = 30
    app.battery_low_pct = 20
    app.test_overdue_days = 35
    app.fault_push_throttle_hours = 48
    app.push_tag = "fire_alarm"
    app.push_category = "fire_alarm"
    app.health_category = "fire_health"
    app.health_notify_target = ["mikkel"]
    app.alarm_always_notify = ["mikkel"]
    app.alarm_notify_if_home = {"kristine": "person.kristine", "claudia": "person.claudia"}
    app.alarm_nobody_home = "everyone"
    app.monthly_test_person_entities = ["person.mikkel", "person.kristine", "person.claudia"]
    app.monthly_test_max_retry_days = 7
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
    app.light_restore_attempts = 0
    app.episode_notified = None
    app.last_clear_at = None
    app.last_episode = None
    app.hush_limit_notified = False
    app.monthly_test_month_key = None
    app.monthly_test_resolved = False
    app.stale_alarm_notified = False
    app.generation = 0
    app.pending_hush = None
    app.smoke_fallback_since = None
    app.pre_alarm_stuck = False
    app._last_published_state = None
    app._last_published_attrs = None
    app._last_published_at = None

    for key, value in overrides.items():
        setattr(app, key, value)
    return app


async def _tick(app):
    """await app._evaluate() and drain any task it queued via create_task (notably a
    backgrounded alarm push) so assertions can see the resulting side effects."""
    await app._evaluate()
    await asyncio.gather(*app._test_tasks)


async def _confirm_pending_hush(app):
    """A remote hush schedules its confirmation check via run_in (mocked in tests, so it
    never fires on its own) - extract that scheduled call and run it to completion,
    mirroring what AppDaemon would do once hush_confirm_s elapses."""
    args, callback_kwargs = app.run_in.call_args
    callback = args[0]
    callback(callback_kwargs)
    await asyncio.gather(*app._test_tasks)


def _make_real_mobile_notifier(**overrides):
    """A real (not faked) MobileNotifier for NotifierDeliveryResult - a fake notify() can
    hide a "delivered to nobody" bug that only the real resolve/send path would surface."""
    notifier = mn.MobileNotifier.__new__(mn.MobileNotifier)
    notifier.log = lambda *a, **kw: None
    notifier.call_service = AsyncMock()
    notifier.person_entities = []
    notifier.device_mapping = {}
    notifier.category_audience = {}
    notifier.platform_map = {}
    notifier.user_notification_service = None
    for key, value in overrides.items():
        setattr(notifier, key, value)
    return notifier


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
        # smoke_fallback is debounced - pre-seed the timer past smoke_fallback_s so this
        # test still exercises "smoke alone raises alarm" without a real siren="fire".
        app = _make_app(smoke_fallback_since=FIXED_NOW - timedelta(seconds=31))
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertIsNotNone(app.episode_id)

    async def test_clear_to_alarm_on_siren_alarm_value(self):
        app = _make_app()
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "Fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_clear_to_pre_alarm_on_siren_pre_alarm_value(self):
        app = _make_app()
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        await _tick(app)
        self.assertEqual(app.phase, "pre_alarm")

    async def test_clear_stays_clear_when_nothing_matches(self):
        app = _make_app()
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "clear")

    async def test_pre_alarm_stays_pre_alarm_when_smoke_on_and_siren_still_pre_alarm(self):
        # Twinguard's own smoke bit is SET during a real pre_alarm, so smoke=="on" must
        # NOT alone escalate - only the siren value can.
        app = _make_app(phase="pre_alarm", since=FIXED_NOW)
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "pre_alarm"
        await _tick(app)
        self.assertEqual(app.phase, "pre_alarm")
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_pre_alarm_to_alarm_on_siren_fire(self):
        app = _make_app(phase="pre_alarm", since=FIXED_NOW, last_siren="pre_alarm", last_smoke="on")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_clear_to_pre_alarm_when_smoke_already_on(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "pre_alarm"
        await _tick(app)
        self.assertEqual(app.phase, "pre_alarm")
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_burglar_siren_ignored_for_phases(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "burglar"
        await _tick(app)
        self.assertEqual(app.phase, "clear")

    async def test_burglar_edge_logs_once_at_info(self):
        app = _make_app(phase="clear", last_siren="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "burglar"
        await _tick(app)
        await _tick(app)

        def is_burglar_info(call):
            args, kw = call
            return kw.get("level") == "INFO" and "burglar" in args[0].lower()

        self.assertEqual(len(list(filter(is_burglar_info, app.log_calls))), 1)

    async def test_pre_alarm_to_clear_on_siren_clear(self):
        app = _make_app(phase="pre_alarm", since=FIXED_NOW)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "clear")

    async def test_pre_alarm_to_clear_on_10min_timeout(self):
        app = _make_app(phase="pre_alarm", since=FIXED_NOW - timedelta(minutes=11))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        await _tick(app)
        self.assertEqual(app.phase, "clear")
        self.assertTrue(app.pre_alarm_stuck)

    async def test_pre_alarm_stays_before_timeout(self):
        app = _make_app(phase="pre_alarm", since=FIXED_NOW - timedelta(minutes=5))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        await _tick(app)
        self.assertEqual(app.phase, "pre_alarm")

    async def test_alarm_to_cooldown_after_60s_smoke_off(self):
        app = _make_app(
            phase="alarm", episode_id="20260911180000",
            off_since=FIXED_NOW - timedelta(seconds=61),
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "cooldown")

    async def test_alarm_stays_before_60s_smoke_off(self):
        app = _make_app(
            phase="alarm", episode_id="20260911180000",
            off_since=FIXED_NOW - timedelta(seconds=30),
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_hushed_to_alarm_on_expiry_while_smoke_still_on(self):
        # Hush expiry uses the debounced smoke_fallback (like everywhere else) instead of
        # a raw, immediate smoke=="on" check - pre-seed the debounce so this still
        # exercises "smoke corroborated on for a while -> alarm".
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1, last_smoke="on",
            hushed_until=FIXED_NOW - timedelta(seconds=1),
            smoke_fallback_since=FIXED_NOW - timedelta(seconds=31),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_hushed_expiry_with_pre_alarm_siren_goes_to_pre_alarm_not_alarm(self):
        """smoke=="on" is also the steady state of pre_alarm (the device's own bit is set
        in both) - hush expiry must not read that alone as a full critical alarm."""
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1, last_smoke="on",
            hushed_until=FIXED_NOW - timedelta(seconds=1),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "pre_alarm"
        await _tick(app)
        self.assertEqual(app.phase, "pre_alarm")
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_hushed_expiry_goes_to_cooldown_when_clear(self):
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1, last_smoke="off",
            hushed_until=FIXED_NOW - timedelta(seconds=1),
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "cooldown")

    async def test_hushed_realarms_on_siren_fire_edge(self):
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1, last_siren="silenced",
            hushed_until=FIXED_NOW + timedelta(minutes=9),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_hushed_stays_hushed_with_smoke_off_while_silenced(self):
        # Device fact: the smoke bit clears while siren=="silenced" - this is the realistic
        # steady state for a hushed episode, not "smoke on".
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1, last_siren="silenced",
            hushed_until=FIXED_NOW + timedelta(minutes=9),
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "silenced"
        await _tick(app)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_hushed_stays_hushed_despite_smoke_off_before_expiry(self):
        # Never sends the all-clear before hushed_until, regardless of the smoke bit or how
        # long it's read "off" - only expiry (or a real re-alarm) may leave hushed.
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1, last_smoke="off",
            hushed_until=FIXED_NOW + timedelta(minutes=9),
            off_since=FIXED_NOW - timedelta(seconds=61),
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_hushed_does_not_realarm_before_smoke_fallback_debounce(self):
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1,
            hushed_until=FIXED_NOW + timedelta(minutes=9),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "hushed")

    async def test_hushed_realarms_once_smoke_fallback_debounce_elapses(self):
        # A smoke-on/siren-not-corroborating condition while hushed can only re-alarm once
        # it has held for smoke_fallback_s, same as everywhere else.
        app = _make_app(
            phase="hushed", episode_id="X", hush_count=1,
            hushed_until=FIXED_NOW + timedelta(minutes=9),
            smoke_fallback_since=FIXED_NOW - timedelta(seconds=31),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.episode_id, "X")
        self.assertEqual(app.hush_count, 1)

    async def test_cooldown_to_alarm_immediately_on_smoke_same_episode(self):
        app = _make_app(phase="cooldown", episode_id="X", hush_count=1)
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.episode_id, "X")
        self.assertEqual(app.hush_count, 1)

    async def test_cooldown_to_clear_after_5min(self):
        app = _make_app(
            phase="cooldown", since=FIXED_NOW - timedelta(minutes=6),
            episode_id="X", episode_started_at=FIXED_NOW - timedelta(minutes=20),
            hush_count=1,
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "clear")
        self.assertIsNone(app.episode_id)
        self.assertEqual(app.hush_count, 0)

    async def test_cooldown_stays_before_5min(self):
        app = _make_app(phase="cooldown", since=FIXED_NOW - timedelta(minutes=3), episode_id="X")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "cooldown")


class SmokeFallbackDebounce(_FrozenTimeTestCase):
    """smoke on but the siren doesn't corroborate must hold smoke_fallback_s (30s
    default) before it alone raises an alarm; siren=="fire" always bypasses this."""

    async def test_no_alarm_before_debounce_elapsed(self):
        app = _make_app(phase="clear", smoke_fallback_since=FIXED_NOW - timedelta(seconds=10))
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "clear")

    async def test_alarms_once_debounce_elapsed(self):
        app = _make_app(phase="clear", smoke_fallback_since=FIXED_NOW - timedelta(seconds=31))
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_fresh_condition_starts_the_timer_without_alarming(self):
        app = _make_app(phase="clear", smoke_fallback_since=None)
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.smoke_fallback_since, FIXED_NOW)

    async def test_timer_resets_when_condition_breaks(self):
        app = _make_app(phase="clear", smoke_fallback_since=FIXED_NOW - timedelta(seconds=20))
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "pre_alarm"
        await _tick(app)
        self.assertIsNone(app.smoke_fallback_since)
        self.assertEqual(app.phase, "pre_alarm")

    async def test_siren_fire_alarms_immediately_regardless_of_timer(self):
        app = _make_app(phase="clear", smoke_fallback_since=None)
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")


class IndependentEvidence(_FrozenTimeTestCase):
    """siren=="fire" is independent evidence and must raise/keep an alarm even while the
    smoke entity itself is unavailable; an outage must not corrupt the smoke_fallback/off
    debounce; the self-test floor/window must never mask it."""

    async def test_siren_fire_with_smoke_unknown_raises_alarm(self):
        app = _make_app(phase="clear")
        app.states[app.siren_state_entity] = "fire"
        # smoke_entity intentionally left unset - unknown/unavailable this tick.
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertIsNotNone(app.episode_id)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertIsNone(app.unavailable_since)

    async def test_smoke_fallback_debounce_resets_across_an_outage(self):
        app = _make_app(phase="clear", smoke_fallback_since=FIXED_NOW - timedelta(seconds=31))
        app.states[app.smoke_entity] = None
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertIsNone(app.smoke_fallback_since)
        self.assertEqual(app.phase, "clear")

        app.states[app.smoke_entity] = "on"
        await _tick(app)
        # Must NOT alarm instantly off the stale pre-outage elapsed time - the debounce
        # restarted at the outage and hasn't held smoke_fallback_s again yet.
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.smoke_fallback_since, FIXED_NOW)

    async def test_floor_never_masks_fire_when_smoke_not_confirmed_off(self):
        app = _make_app(phase="clear", self_test_until=FIXED_NOW + timedelta(seconds=30))
        app.states[app.siren_state_entity] = "fire"
        # smoke_entity left unset - unknown, not a positive "off" corroborating the floor.
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_self_test_suppression_expires_after_max_min_allows_smoke_fallback(self):
        app = _make_app(
            phase="clear", last_siren="self_test",
            last_self_test_at=FIXED_NOW - timedelta(minutes=11),
            smoke_fallback_since=FIXED_NOW - timedelta(seconds=31),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "self_test"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_self_test_suppression_still_applies_within_max_min(self):
        app = _make_app(
            phase="clear", last_siren="self_test",
            last_self_test_at=FIXED_NOW - timedelta(minutes=5),
            smoke_fallback_since=FIXED_NOW - timedelta(seconds=31),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "self_test"
        await _tick(app)
        self.assertEqual(app.phase, "clear")


class PreAlarmLoopGuard(_FrozenTimeTestCase):
    """Once a pre_alarm timeout forces a clear while the siren is still reporting
    pre_alarm, don't re-chime every tick - wait for the siren to actually leave pre_alarm."""

    async def test_stuck_pre_alarm_does_not_re_enter_until_siren_actually_clears(self):
        app = _make_app(phase="pre_alarm", since=FIXED_NOW - timedelta(minutes=11))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"

        await _tick(app)
        self.assertEqual(app.phase, "clear")
        self.assertTrue(app.pre_alarm_stuck)

        # Still stuck on the very next tick - must not re-chime/re-enter pre_alarm.
        await _tick(app)
        self.assertEqual(app.phase, "clear")

        # Siren finally reports something other than pre_alarm - guard lifts.
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "clear")
        self.assertFalse(app.pre_alarm_stuck)

        # A genuinely new pre_alarm event now re-enters normally.
        app.states[app.siren_state_entity] = "pre_alarm"
        await _tick(app)
        self.assertEqual(app.phase, "pre_alarm")


class StaleDeviceMidEpisode(_FrozenTimeTestCase):
    """Device stops reporting mid-alarm/hushed."""

    async def test_not_triggered_before_stale_grace(self):
        # ~65s unknown right after an HA restart must not fire anything.
        app = _make_app(phase="alarm", episode_id="E1", unavailable_since=FIXED_NOW - timedelta(seconds=65))
        app.states[app.smoke_entity] = None
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.mobile_notifier.calls, [])
        self.assertEqual(app.sonos_notifier.calls, [])
        self.assertFalse(app.stale_alarm_notified)

    async def test_past_stale_grace_sends_exactly_one_push_and_announce(self):
        app = _make_app(phase="alarm", episode_id="E1", unavailable_since=FIXED_NOW - timedelta(seconds=121))
        app.states[app.smoke_entity] = None
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertNotIn("critical", app.mobile_notifier.calls[0])
        self.assertIn("stopped reporting", app.mobile_notifier.calls[0]["message"])
        self.assertEqual(len(app.sonos_notifier.calls), 1)
        self.assertTrue(app.sonos_notifier.calls[0].get("override_quiet_hours"))
        self.assertTrue(app.stale_alarm_notified)

        # Still stale on the next tick - must not resend, and repeats stay stopped.
        await _tick(app)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertEqual(len(app.sonos_notifier.calls), 1)

    async def test_past_stale_grace_while_hushed_also_notifies(self):
        app = _make_app(
            phase="hushed", episode_id="E1", hushed_until=FIXED_NOW + timedelta(minutes=5),
            unavailable_since=FIXED_NOW - timedelta(seconds=121),
        )
        app.states[app.smoke_entity] = None
        await _tick(app)
        self.assertEqual(len(app.mobile_notifier.calls), 1)

    async def test_clear_button_ends_stale_episode_despite_smoke_not_off(self):
        app = _make_app(phase="alarm", episode_id="E1", unavailable_since=FIXED_NOW - timedelta(seconds=121))
        app.states[app.smoke_entity] = None
        await app._on_clear_pressed()
        self.assertEqual(app.phase, "clear")

    async def test_clear_button_ignored_before_stale_grace_elapsed(self):
        app = _make_app(phase="alarm", episode_id="E1", unavailable_since=FIXED_NOW - timedelta(seconds=30))
        app.states[app.smoke_entity] = None
        await app._on_clear_pressed()
        self.assertEqual(app.phase, "alarm")

    async def test_offline_after_offline_after_min_still_applies_mid_episode(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=1, unavailable_since=FIXED_NOW - timedelta(minutes=31))
        app.states[app.smoke_entity] = None
        await _tick(app)
        self.assertEqual(app.phase, "offline")
        self.assertEqual(app.episode_id, "E1")
        self.assertEqual(app.hush_count, 1)


class EpisodeReuseAndOfflineContinuity(_FrozenTimeTestCase):
    async def test_alarm_within_reuse_window_reuses_last_episode(self):
        app = _make_app(
            phase="clear",
            last_clear_at=FIXED_NOW - timedelta(minutes=10),
            last_episode={
                "episode_id": "OLDEP",
                "hush_count": 1,
                "episode_notified": ["mikkel", "kristine"],
                "light_snapshot": {"light.hallway_lights": {"state": "on", "brightness": 50}},
                "light_snapshot_episode": "OLDEP",
            },
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.episode_id, "OLDEP")
        self.assertEqual(app.hush_count, 1)
        self.assertEqual(app.light_snapshot_episode, "OLDEP")

    async def test_reuse_keeps_hush_count_but_starts_audience_fresh(self):
        """Episode reuse carries over hush accounting only - not the audience list.
        alarm_nobody_home="always_only" isolates the effect (no "everyone" fallback
        muddying the result)."""
        app = _make_app(
            phase="clear", alarm_nobody_home="always_only",
            last_clear_at=FIXED_NOW - timedelta(minutes=10),
            last_episode={
                "episode_id": "OLDEP",
                "hush_count": 1,
                "episode_notified": ["mikkel", "kristine"],
                "light_snapshot": {"light.hallway_lights": {"state": "on", "brightness": 50}},
                "light_snapshot_episode": "OLDEP",
            },
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.episode_id, "OLDEP")
        self.assertEqual(app.hush_count, 1)
        self.assertEqual(app.episode_notified, ["mikkel"])

    async def test_reuse_does_not_resurrect_stale_light_snapshot_contents(self):
        """Episode reuse must not reuse a light snapshot - a fresh one is taken from
        CURRENT light state, not the stale values stashed at the last clear."""
        app = _make_app(
            phase="clear",
            last_clear_at=FIXED_NOW - timedelta(minutes=10),
            last_episode={
                "episode_id": "OLDEP", "hush_count": 0, "episode_notified": None,
                "light_snapshot": {"light.hallway_lights": {"state": "on", "brightness": 50}},
                "light_snapshot_episode": "OLDEP",
            },
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        app.states["light.hallway_lights"] = {"state": "off", "attributes": {}}
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(
            app.light_snapshot, {"light.hallway_lights": {"state": "off", "brightness": None}}
        )

    async def test_alarm_outside_reuse_window_starts_fresh_episode(self):
        app = _make_app(
            phase="clear",
            last_clear_at=FIXED_NOW - timedelta(minutes=31),
            last_episode={
                "episode_id": "OLDEP", "hush_count": 2, "episode_notified": None,
                "light_snapshot": {}, "light_snapshot_episode": None,
            },
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertNotEqual(app.episode_id, "OLDEP")
        self.assertEqual(app.hush_count, 0)

    async def test_offline_resumes_same_episode_when_entered_from_alarm(self):
        app = _make_app(
            alarm_nobody_home="always_only",
            phase="offline", episode_id="E1", hush_count=1,
            episode_notified=["mikkel", "kristine"],
            light_snapshot={"light.hallway_lights": {"state": "on", "brightness": 80}},
            light_snapshot_episode="E1",
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.episode_id, "E1")
        self.assertEqual(app.hush_count, 1)
        self.assertEqual(app.episode_notified, ["kristine", "mikkel"])
        # No re-snapshot: light_snapshot_episode already matches episode_id.
        self.assertEqual(app.light_snapshot_episode, "E1")

    async def test_clear_stashes_episode_for_reuse(self):
        app = _make_app(
            phase="cooldown", since=FIXED_NOW - timedelta(minutes=6),
            episode_id="E9", episode_started_at=FIXED_NOW - timedelta(minutes=10),
            hush_count=1, episode_notified=["mikkel"],
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.last_episode["episode_id"], "E9")
        self.assertEqual(app.last_episode["hush_count"], 1)
        self.assertEqual(app.last_clear_at, FIXED_NOW)


class OfflineHandling(_FrozenTimeTestCase):
    async def test_first_unavailable_tick_records_timestamp_without_offline(self):
        app = _make_app(phase="clear", unavailable_since=None)
        app.states[app.smoke_entity] = None
        await _tick(app)
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.unavailable_since, FIXED_NOW)

    async def test_not_offline_before_30_minutes(self):
        app = _make_app(phase="clear", unavailable_since=FIXED_NOW - timedelta(minutes=29))
        app.states[app.smoke_entity] = None
        await _tick(app)
        self.assertEqual(app.phase, "clear")

    async def test_offline_after_30_minutes(self):
        app = _make_app(phase="clear", unavailable_since=FIXED_NOW - timedelta(minutes=31))
        app.states[app.smoke_entity] = None
        await _tick(app)
        self.assertEqual(app.phase, "offline")

    async def test_offline_recovers_to_alarm_when_smoke_on(self):
        app = _make_app(phase="offline")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_offline_recovers_to_clear_when_smoke_off(self):
        app = _make_app(phase="offline")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "clear")


class HushBehavior(_FrozenTimeTestCase):
    async def test_hush_from_alarm_sends_stop_then_confirms_via_siren_leaving_fire(self):
        # A remote hush is not trusted just because the select call was accepted - it
        # sends "stop", stays "alarm" and doesn't consume hush_count until confirmed.
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0)
        app.states[app.siren_state_entity] = "silenced"
        app.states[app.smoke_entity] = "on"
        await app._hush(FIXED_NOW, "the dashboard")
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.hush_count, 0)
        stop_calls = [c for c in app.call_service.call_args_list if c.args[0] == "select/select_option"]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(stop_calls[0].kwargs.get("option"), "stop")
        self.assertEqual(app.run_in.call_args.kwargs.get("episode_id"), "E1")

        await _confirm_pending_hush(app)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hush_count, 1)
        self.assertEqual(app.hushed_by, "the dashboard")
        # Confirmation itself must not send a second "stop".
        stop_calls = [c for c in app.call_service.call_args_list if c.args[0] == "select/select_option"]
        self.assertEqual(len(stop_calls), 1)

    async def test_hush_confirmed_when_siren_leaves_fire_for_clear(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0)
        app.states[app.siren_state_entity] = "fire"
        app.states[app.smoke_entity] = "on"
        await app._hush(FIXED_NOW, "the dashboard")
        self.assertEqual(app.phase, "alarm")

        app.states[app.siren_state_entity] = "clear"
        app.states[app.smoke_entity] = "off"
        await _confirm_pending_hush(app)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hush_count, 1)

    async def test_hush_with_failed_stop_call_stays_alarm_and_pushes_failure_once(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0)

        async def failing_call_service(service, **kwargs):
            if service == "select/select_option":
                raise RuntimeError("boom")

        app.call_service = AsyncMock(side_effect=failing_call_service)
        await app._hush(FIXED_NOW, "the dashboard")
        await asyncio.gather(*app._test_tasks)

        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.hush_count, 0)
        app.run_in.assert_not_called()
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertIn("Couldn't silence", app.mobile_notifier.calls[0]["message"])

    async def test_hush_unconfirmed_after_timeout_stays_alarm_and_pushes_once(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0)
        app.states[app.siren_state_entity] = "fire"
        app.states[app.smoke_entity] = "on"
        await app._hush(FIXED_NOW, "the dashboard")

        # Neither siren nor smoke corroborate the hush by the time it's checked.
        await _confirm_pending_hush(app)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.hush_count, 0)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertIn("Couldn't silence", app.mobile_notifier.calls[0]["message"])

    async def test_hush_ignored_outside_alarm_phase(self):
        app = _make_app(phase="clear")
        await app._hush(FIXED_NOW, "the dashboard")
        self.assertEqual(app.phase, "clear")
        app.call_service.assert_not_called()

    async def test_hush_bounded_rejects_at_limit(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=2, max_hushes_per_episode=2)
        await app._hush(FIXED_NOW, "the dashboard")
        await asyncio.gather(*app._test_tasks)  # the hush-limit push runs detached
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.hush_count, 2)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertIn("Hush limit reached", app.mobile_notifier.calls[0]["message"])

    async def test_hush_limit_push_sent_once_per_episode(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=2, max_hushes_per_episode=2)
        await app._hush(FIXED_NOW, "the dashboard")
        await app._hush(FIXED_NOW, "the dashboard")
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertTrue(app.hush_limit_notified)

    async def test_second_hush_within_limit_succeeds_once_confirmed(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=1, max_hushes_per_episode=2)
        app.states[app.siren_state_entity] = "clear"
        app.states[app.smoke_entity] = "off"
        await app._hush(FIXED_NOW, "the dashboard")
        await _confirm_pending_hush(app)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hush_count, 2)

    async def test_re_alarm_during_hush_escalates(self):
        app = _make_app(
            phase="hushed", episode_id="E1", hush_count=1, last_smoke="on",
            hushed_until=FIXED_NOW - timedelta(seconds=1),
            smoke_fallback_since=FIXED_NOW - timedelta(seconds=31),
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.episode_id, "E1")
        self.assertEqual(app.hush_count, 1)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertEqual(len(app.sonos_notifier.calls), 1)
        light_calls = [c for c in app.call_service.call_args_list if c.args[0] == "light/turn_on"]
        self.assertEqual(len(light_calls), 1)

    async def test_siren_unknown_then_fire_while_hushed_is_a_realarm_edge(self):
        """An unknown siren reading must reset last_siren to None (not linger at the
        pre-outage value) so a later return to "fire" is always a fresh edge - otherwise
        a re-ignition coinciding with an HA restart could be swallowed as "still fire, no
        edge"."""
        app = _make_app(
            phase="hushed", episode_id="E1", hush_count=1, last_siren="silenced",
            hushed_until=FIXED_NOW + timedelta(minutes=9),
        )
        # Both entities unknown this tick - e.g. an HA restart mid-hush.
        await _tick(app)
        self.assertEqual(app.phase, "hushed")
        self.assertIsNone(app.last_siren)

        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_physical_button_inferred_hush(self):
        # Edge into the "silenced" siren value - see _physical_hush_signal. smoke is
        # "off" here because the device's own smoke bit clears while silenced.
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0, last_siren="fire")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "silenced"
        await _tick(app)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hushed_by, "the button on the alarm")

    async def test_siren_clear_while_alarming_is_not_inferred_hush(self):
        # "clear" is not "silenced" - must not misfire on any non-silenced reading.
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0, last_siren="fire")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
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
        app.states[app.siren_state_entity] = "clear"
        app.states[app.smoke_entity] = "off"
        app._on_button_state_changed(
            "state_changed", self._button_press_data(app.hush_button_entity), {}
        )
        await asyncio.gather(*app._test_tasks)
        await _confirm_pending_hush(app)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hushed_by, "the dashboard")

    async def test_hush_button_resolves_actor_from_person_entity(self):
        app = _make_app(phase="alarm", episode_id="E1")
        app.states[app.siren_state_entity] = "clear"
        app.states[app.smoke_entity] = "off"
        app.states["person"] = ["person.kristine"]
        app.states["person.kristine"] = {"attributes": {"user_id": "uid-123", "friendly_name": "Kristine"}}
        app._on_button_state_changed(
            "state_changed", self._button_press_data(app.hush_button_entity, "uid-123"), {}
        )
        await asyncio.gather(*app._test_tasks)
        await _confirm_pending_hush(app)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hushed_by, "Kristine")

    async def test_hush_button_resolves_actor_from_fallback_map(self):
        app = _make_app(phase="alarm", episode_id="E1", user_name_fallback={"uid456": "Claudia"})
        app.states[app.siren_state_entity] = "clear"
        app.states[app.smoke_entity] = "off"
        app._on_button_state_changed(
            "state_changed", self._button_press_data(app.hush_button_entity, "uid-456"), {}
        )
        await asyncio.gather(*app._test_tasks)
        await _confirm_pending_hush(app)
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
        app.states[app.siren_state_entity] = "clear"
        app.states[app.smoke_entity] = "off"
        app._on_notification_action(
            "mobile_app_notification_action",
            {"action": "FIRE_HUSH_20260911180000_mikkel"},
            {},
        )
        await asyncio.gather(*app._test_tasks)
        await _confirm_pending_hush(app)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hushed_by, "Mikkel")

    async def test_notification_hush_action_revalidated_at_lock_time(self):
        """The episode id travels with the task and is rechecked once the lock is
        actually acquired, not just at event-handling time - a new episode could start
        (still phase=="alarm") before the queued hush task actually runs."""
        app = _make_app(phase="alarm", episode_id="20260911180000")
        app._on_notification_action(
            "mobile_app_notification_action",
            {"action": "FIRE_HUSH_20260911180000_mikkel"},
            {},
        )
        # A new alarm episode replaces this one before the queued hush task runs.
        app.episode_id = "20260911190000"
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.phase, "alarm")
        self.assertIsNone(app.hushed_by)
        app.run_in.assert_not_called()

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


class PendingHushConfirmation(_FrozenTimeTestCase):
    """Unknown siren/smoke must never confirm a hush, and only one remote hush attempt
    may be pending confirmation at a time."""

    async def test_confirm_reschedules_when_siren_and_smoke_unknown(self):
        app = _make_app(
            phase="alarm", episode_id="E1", hush_count=0,
            pending_hush={"requested_at": FIXED_NOW, "by": "the dashboard", "episode": "E1", "generation": 0},
        )
        # siren/smoke left unset in app.states - unknown, as in the ~65s post-restart gap.
        await app._confirm_hush("E1", "the dashboard")
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.hush_count, 0)
        self.assertIsNotNone(app.pending_hush)
        self.assertEqual(app.mobile_notifier.calls, [])
        self.assertEqual(app.run_in.call_args.args[1], fs.HUSH_UNKNOWN_RETRY_S)

    async def test_confirm_gives_up_after_max_s_of_unknown(self):
        app = _make_app(
            phase="alarm", episode_id="E1", hush_count=0,
            pending_hush={
                "requested_at": FIXED_NOW - timedelta(seconds=121), "by": "the dashboard",
                "episode": "E1", "generation": 0,
            },
        )
        await app._confirm_hush("E1", "the dashboard")
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.hush_count, 0)
        self.assertIsNone(app.pending_hush)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertIn("Couldn't silence", app.mobile_notifier.calls[0]["message"])

    async def test_second_press_while_pending_is_ignored(self):
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0, max_hushes_per_episode=2)
        app.states[app.siren_state_entity] = "fire"
        app.states[app.smoke_entity] = "on"
        await app._hush(FIXED_NOW, "the dashboard")
        first_pending = app.pending_hush
        self.assertIsNotNone(first_pending)
        run_in_calls_before = app.run_in.call_count

        await app._hush(FIXED_NOW, "someone else")
        self.assertEqual(app.pending_hush, first_pending)
        self.assertEqual(app.run_in.call_count, run_in_calls_before)

        app.states[app.siren_state_entity] = "clear"
        await _confirm_pending_hush(app)
        self.assertEqual(app.phase, "hushed")
        self.assertEqual(app.hush_count, 1)

    async def test_pending_hush_resumes_on_init(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        seed = fs.FireSafety.__new__(fs.FireSafety)
        seed.state_file = path
        seed.log = lambda *a, **kw: None
        seed.phase = "alarm"
        seed.since = FIXED_NOW
        seed.episode_id = "E1"
        seed.episode_started_at = FIXED_NOW
        seed.hushed_until = None
        seed.hushed_by = None
        seed.hush_count = 0
        seed.ack_by = None
        seed.last_push_at = FIXED_NOW
        seed.last_announce_at = FIXED_NOW
        seed.last_lights_assert_at = FIXED_NOW
        seed.last_self_test_at = None
        seed.last_smoke = "on"
        seed.last_siren = "fire"
        seed.self_test_until = None
        seed.cooking_until = None
        seed.unavailable_since = None
        seed.off_since = None
        seed.last_fault_push_at = {}
        seed.light_snapshot = {}
        seed.light_snapshot_episode = None
        seed.light_restore_attempts = 0
        seed.episode_notified = None
        seed.last_clear_at = None
        seed.last_episode = None
        seed.hush_limit_notified = False
        seed.monthly_test_month_key = None
        seed.monthly_test_resolved = False
        seed.stale_alarm_notified = False
        seed.generation = 1
        seed.pending_hush = {
            "requested_at": FIXED_NOW, "by": "the dashboard", "episode": "E1", "generation": 1,
        }
        seed._save_state()

        app = fs.FireSafety.__new__(fs.FireSafety)
        app.state_file = path
        app.log = lambda *a, **kw: None
        app._load_state()
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.pending_hush["by"], "the dashboard")

        app.run_in = AsyncMock(return_value="handle")
        app._test_tasks = []

        def create_task(coro):
            t = asyncio.ensure_future(coro)
            app._test_tasks.append(t)
            return t

        app.create_task = create_task

        app._maybe_resume_pending_hush()
        await asyncio.gather(*app._test_tasks)
        self.assertTrue(app.run_in.called)
        self.assertEqual(app.run_in.call_args.kwargs.get("episode_id"), "E1")
        self.assertEqual(app.run_in.call_args.kwargs.get("by_text"), "the dashboard")


class ButtonRestoreGuard(_FrozenTimeTestCase):
    """After an HA restart, input_button.* goes unavailable then RESTORES its last press
    timestamp - that restore must never be read as a fresh press."""

    async def test_restored_hush_press_after_unavailable_is_ignored(self):
        app = _make_app(phase="alarm", episode_id="E1")
        data = {
            "entity_id": app.hush_button_entity,
            "old_state": {"state": "unavailable"},
            "new_state": {"state": "2026-09-11T18:00:00+00:00", "context": {}},
        }
        app._on_button_state_changed("state_changed", data, {})
        self.assertEqual(app._test_tasks, [])
        self.assertEqual(app.phase, "alarm")
        self.assertEqual(app.hush_count, 0)

    async def test_missing_old_state_is_ignored(self):
        app = _make_app(phase="alarm", episode_id="E1")
        data = {
            "entity_id": app.hush_button_entity,
            "new_state": {"state": "2026-09-11T18:00:00+00:00", "context": {}},
        }
        app._on_button_state_changed("state_changed", data, {})
        self.assertEqual(app._test_tasks, [])
        self.assertEqual(app.phase, "alarm")

    async def test_delayed_legitimate_press_is_accepted(self):
        """No freshness window - a real prior state (not unavailable) that's simply old
        is still a legitimate press, not a restart-restore artifact."""
        app = _make_app(phase="alarm", episode_id="E1")
        app.states[app.siren_state_entity] = "silenced"
        app.states[app.smoke_entity] = "off"
        data = {
            "entity_id": app.hush_button_entity,
            "old_state": {"state": "2026-09-11T16:00:00+00:00"},
            "new_state": {"state": "2026-09-11T17:58:00+00:00", "context": {}},  # 2 min old
        }
        app._on_button_state_changed("state_changed", data, {})
        await asyncio.gather(*app._test_tasks)
        await _confirm_pending_hush(app)
        self.assertEqual(app.phase, "hushed")

    def test_test_button_restored_after_unavailable_is_ignored(self):
        app = _make_app(phase="clear")
        app._on_test_button(
            app.test_button_entity, None, "unavailable", "2026-09-11T18:00:00+00:00", {}
        )
        self.assertEqual(app._test_tasks, [])

    def test_test_button_delayed_legitimate_press_is_accepted(self):
        app = _make_app(phase="clear")
        app._on_test_button(
            app.test_button_entity, None, "2026-09-11T16:00:00+00:00",
            "2026-09-11T17:58:00+00:00", {},
        )
        self.assertEqual(len(app._test_tasks), 1)


class SelfTestGuardConditions(_FrozenTimeTestCase):
    """_run_self_test refuses unless the alarm is idle and the device itself is clear - a
    self-test sounds the siren, so it must never fire mid-episode or onto ambiguous
    device state."""

    async def test_refused_when_phase_not_clear(self):
        app = _make_app(phase="alarm", episode_id="E1")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        ran = await app._run_self_test(FIXED_NOW)
        self.assertFalse(ran)
        self.assertIsNone(app.self_test_until)
        app.call_service.assert_not_called()

    async def test_refused_when_smoke_not_off(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        ran = await app._run_self_test(FIXED_NOW)
        self.assertFalse(ran)
        self.assertIsNone(app.self_test_until)

    async def test_refused_when_siren_not_clear(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        ran = await app._run_self_test(FIXED_NOW)
        self.assertFalse(ran)
        self.assertIsNone(app.self_test_until)

    async def test_manual_test_allowed_regardless_of_presence(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.states["person.mikkel"] = "home"
        ran = await app._run_self_test(FIXED_NOW)
        self.assertTrue(ran)
        self.assertIsNotNone(app.self_test_until)

    async def test_automatic_test_refused_while_someone_home(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.states["person.mikkel"] = "home"
        ran = await app._run_self_test(FIXED_NOW, automatic=True)
        self.assertFalse(ran)
        self.assertIsNone(app.self_test_until)


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


class LightRestoreDurability(_FrozenTimeTestCase):
    """Only drop the snapshot once every restore call in it succeeded; otherwise keep it
    and retry on a later cooldown/clear tick, giving up after 5 attempts."""

    async def test_failed_restore_keeps_snapshot_and_retries_next_tick(self):
        app = _make_app(
            phase="cooldown", since=FIXED_NOW - timedelta(minutes=1), episode_id="E1",
            light_snapshot={"light.hallway_lights": {"state": "on", "brightness": 80}},
            light_snapshot_episode="E1",
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"

        async def failing_call_service(service, **kwargs):
            if service == "light/turn_on":
                raise RuntimeError("boom")

        app.call_service = AsyncMock(side_effect=failing_call_service)

        await _tick(app)
        self.assertEqual(app.phase, "cooldown")
        self.assertEqual(app.light_snapshot, {"light.hallway_lights": {"state": "on", "brightness": 80}})
        self.assertEqual(app.light_restore_attempts, 1)

        await _tick(app)
        self.assertEqual(app.light_restore_attempts, 2)
        self.assertTrue(app.light_snapshot)

    async def test_gives_up_after_max_attempts(self):
        app = _make_app(
            phase="cooldown", since=FIXED_NOW - timedelta(minutes=1), episode_id="E1",
            light_snapshot={"light.hallway_lights": {"state": "on", "brightness": 80}},
            light_snapshot_episode="E1",
            light_restore_attempts=fs.LIGHT_RESTORE_MAX_ATTEMPTS - 1,
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"

        async def failing_call_service(service, **kwargs):
            if service == "light/turn_on":
                raise RuntimeError("boom")

        app.call_service = AsyncMock(side_effect=failing_call_service)

        await _tick(app)
        self.assertEqual(app.light_snapshot, {})
        self.assertIsNone(app.light_snapshot_episode)
        self.assertEqual(app.light_restore_attempts, 0)
        warnings = [a for a, kw in app.log_calls if kw.get("level") == "WARNING"]
        self.assertTrue(any("giving up" in str(a[0]).lower() for a in warnings))

    async def test_successful_restore_resets_attempts_counter(self):
        app = _make_app(
            phase="cooldown", episode_id="E1",
            light_snapshot={"light.hallway_lights": {"state": "off", "brightness": None}},
            light_snapshot_episode="E1",
            light_restore_attempts=2,
        )
        await app._clear_lights()
        self.assertEqual(app.light_snapshot, {})
        self.assertEqual(app.light_restore_attempts, 0)

    async def test_no_new_snapshot_taken_while_unrestored_snapshot_pending(self):
        app = _make_app(
            phase="alarm", episode_id="E2",
            light_snapshot={"light.hallway_lights": {"state": "on", "brightness": 40}},
            light_snapshot_episode="E1",  # stale, from a previous un-restored episode
        )
        app.states["light.hallway_lights"] = {"state": "off", "attributes": {}}
        await app._assert_lights(FIXED_NOW)
        self.assertEqual(app.light_snapshot, {"light.hallway_lights": {"state": "on", "brightness": 40}})
        self.assertEqual(app.light_snapshot_episode, "E1")


class RepeatCadence(_FrozenTimeTestCase):
    async def test_repush_fires_once_interval_elapsed(self):
        app = _make_app(
            phase="alarm", episode_id="E1",
            last_push_at=FIXED_NOW - timedelta(seconds=121),
            last_announce_at=FIXED_NOW, last_lights_assert_at=FIXED_NOW,
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(len(app.mobile_notifier.calls), 1)

    async def test_no_repush_before_interval(self):
        app = _make_app(
            phase="alarm", episode_id="E1",
            last_push_at=FIXED_NOW - timedelta(seconds=30),
            last_announce_at=FIXED_NOW, last_lights_assert_at=FIXED_NOW,
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(len(app.mobile_notifier.calls), 0)

    async def test_repush_interval_extends_once_acked(self):
        app = _make_app(
            phase="alarm", episode_id="E1", ack_by="Mikkel",
            last_push_at=FIXED_NOW - timedelta(seconds=130),
            last_announce_at=FIXED_NOW, last_lights_assert_at=FIXED_NOW,
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(len(app.mobile_notifier.calls), 0)  # 130s < 180s acked interval


class PushLockNonBlocking(_FrozenTimeTestCase):
    """Repeat/hush-confirmation/stale pushes run detached from _eval_lock (via
    create_task with a snapshot), so a slow send can never block a concurrent hush/clear,
    and a push that resolves after the episode has moved on must not mutate state."""

    async def test_slow_repeat_push_does_not_block_a_concurrent_hush(self):
        app = _make_app(
            phase="alarm", episode_id="E1",
            last_push_at=FIXED_NOW - timedelta(seconds=121),
            last_announce_at=FIXED_NOW, last_lights_assert_at=FIXED_NOW,
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"

        push_started = asyncio.Event()
        release = asyncio.Event()

        async def slow_notify(**kwargs):
            push_started.set()
            await release.wait()
            return 1

        app.mobile_notifier.notify = slow_notify

        # The repeat push must be dispatched (create_task) and _evaluate() must return
        # promptly - it must NOT await the slow notify while holding _eval_lock.
        await asyncio.wait_for(app._evaluate(), timeout=1)
        await asyncio.wait_for(push_started.wait(), timeout=1)

        # The lock must already be free - a hush must not have to wait on the push.
        await asyncio.wait_for(app._hush(FIXED_NOW, "the dashboard"), timeout=1)

        release.set()
        await asyncio.gather(*app._test_tasks)

    async def test_push_completing_after_clear_does_not_touch_episode_notified(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"

        push_started = asyncio.Event()
        release = asyncio.Event()
        call_count = 0

        async def slow_notify(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                push_started.set()
                await release.wait()
            return 1

        app.mobile_notifier.notify = slow_notify

        await asyncio.wait_for(app._evaluate(), timeout=1)  # enters alarm; initial push pending
        self.assertEqual(app.phase, "alarm")
        self.assertIsNone(app.episode_notified)
        # Make sure the initial push has actually started (and is now parked on release)
        # before moving the episode on - otherwise it may not run until later, landing
        # its call_count==1 branch on the all-clear push instead.
        await asyncio.wait_for(push_started.wait(), timeout=1)

        # Smoke clears and stays off long enough to fall through cooldown into clear,
        # all before the original alarm push's notify() resolves.
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.off_since = FIXED_NOW - timedelta(seconds=61)
        await asyncio.wait_for(app._evaluate(), timeout=1)
        self.assertEqual(app.phase, "cooldown")
        app.since = FIXED_NOW - timedelta(minutes=10)  # force the cooldown_clear_min timeout
        await asyncio.wait_for(app._evaluate(), timeout=1)
        self.assertEqual(app.phase, "clear")
        self.assertIsNone(app.episode_id)

        release.set()
        await asyncio.gather(*app._test_tasks)
        # The stale initial push must not resurrect episode_notified for a cleared episode.
        self.assertIsNone(app.episode_notified)

    async def test_slow_all_clear_push_does_not_block_a_new_evaluation(self):
        app = _make_app(
            phase="cooldown", since=FIXED_NOW - timedelta(minutes=6),
            episode_id="E1", episode_started_at=FIXED_NOW - timedelta(minutes=20),
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"

        push_started = asyncio.Event()
        release = asyncio.Event()

        async def slow_notify(**kwargs):
            push_started.set()
            await release.wait()
            return 1

        app.mobile_notifier.notify = slow_notify

        await asyncio.wait_for(app._evaluate(), timeout=1)  # enters clear; all-clear pending
        self.assertEqual(app.phase, "clear")
        await asyncio.wait_for(push_started.wait(), timeout=1)

        # The lock must already be free - a fresh fire must not wait on the all-clear push.
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await asyncio.wait_for(app._evaluate(), timeout=1)
        self.assertEqual(app.phase, "alarm")

        release.set()
        await asyncio.gather(*app._test_tasks)

    async def test_stale_hush_confirmation_push_dropped_after_realarm(self):
        """A queued hush-confirmation push must not fire once a re-alarm has superseded
        it - it would otherwise replace a live alarm notification (same tag) with a
        stale "silenced" message."""
        app = _make_app(phase="alarm", episode_id="E1", hush_count=0, last_siren="fire")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "silenced"

        read_started = asyncio.Event()
        release = asyncio.Event()
        underlying_get_state = app.get_state

        async def slow_get_state(entity_id, attribute=None):
            if entity_id == "person.kristine":
                read_started.set()
                await release.wait()
            return await underlying_get_state(entity_id, attribute=attribute)

        app.get_state = slow_get_state

        # Physical hush commits immediately and dispatches the (slow-audience) confirmation.
        await asyncio.wait_for(app._evaluate(), timeout=1)
        self.assertEqual(app.phase, "hushed")
        await asyncio.wait_for(read_started.wait(), timeout=1)

        # Re-alarm before the queued confirmation push reaches its pre-notify guard. This
        # itself dispatches a fresh (legitimate) alarm push, which is expected to send.
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await asyncio.wait_for(app._evaluate(), timeout=1)
        self.assertEqual(app.phase, "alarm")

        release.set()
        await asyncio.gather(*app._test_tasks)
        messages = [c["message"] for c in app.mobile_notifier.calls]
        self.assertFalse(any("silenced" in m for m in messages))

    async def test_queued_repeat_dropped_after_hush(self):
        """A repeat push dispatched while still "alarm" must not fire once a hush has
        since committed - a queued critical repeat must not undo a hush the household
        already saw confirmed."""
        app = _make_app(
            phase="alarm", episode_id="E1", hush_count=0,
            last_push_at=FIXED_NOW - timedelta(seconds=121),
            last_announce_at=FIXED_NOW, last_lights_assert_at=FIXED_NOW,
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "clear"

        read_started = asyncio.Event()
        release = asyncio.Event()
        underlying_get_state = app.get_state
        call_count = 0

        async def slow_get_state(entity_id, attribute=None):
            nonlocal call_count
            if entity_id == "person.kristine":
                call_count += 1
                if call_count == 1:
                    read_started.set()
                    await release.wait()
            return await underlying_get_state(entity_id, attribute=attribute)

        app.get_state = slow_get_state

        await asyncio.wait_for(app._evaluate(), timeout=1)  # dispatches the (slow) repeat
        await asyncio.wait_for(read_started.wait(), timeout=1)

        await asyncio.wait_for(app._hush(FIXED_NOW, "the dashboard"), timeout=1)
        args, callback_kwargs = app.run_in.call_args
        before = set(app._test_tasks)
        args[0](callback_kwargs)
        confirm_tasks = [t for t in app._test_tasks if t not in before]
        await asyncio.gather(*confirm_tasks)
        self.assertEqual(app.phase, "hushed")

        release.set()
        await asyncio.gather(*app._test_tasks)
        messages = [c["message"] for c in app.mobile_notifier.calls]
        self.assertNotIn("The alarm is sounding. Check the kitchen.", messages)
        self.assertTrue(any("silenced the kitchen alarm" in m for m in messages))


class DryRunGating(_FrozenTimeTestCase):
    async def test_dry_run_alarm_makes_zero_call_service_and_zero_notify(self):
        app = _make_app(dry_run=True)
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        app.call_service.assert_not_called()
        self.assertEqual(app.mobile_notifier.calls, [])
        self.assertEqual(app.sonos_notifier.calls, [])

    async def test_dry_run_hush_makes_zero_call_service(self):
        app = _make_app(phase="alarm", episode_id="E1", dry_run=True)
        app.states[app.siren_state_entity] = "clear"
        app.states[app.smoke_entity] = "off"
        await app._hush(FIXED_NOW, "the dashboard")
        app.call_service.assert_not_called()
        self.assertEqual(app.phase, "alarm")  # dry-run still waits for confirmation

        await _confirm_pending_hush(app)
        app.call_service.assert_not_called()
        self.assertEqual(app.phase, "hushed")

    async def test_dry_run_self_test_makes_zero_call_service_and_zero_notify(self):
        app = _make_app(dry_run=True)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._run_self_test(FIXED_NOW)
        app.call_service.assert_not_called()
        self.assertEqual(app.mobile_notifier.calls, [])


class TestAudiencePassthrough(_FrozenTimeTestCase):
    async def test_alarm_push_carries_test_audience(self):
        app = _make_app(test_audience=["mikkel"])
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.mobile_notifier.calls[0]["test_audience"], ["mikkel"])

    async def test_health_push_carries_test_audience(self):
        app = _make_app(test_audience=["mikkel"])
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._run_self_test(FIXED_NOW)
        await asyncio.gather(*app._test_tasks)  # self-test's health push runs detached
        self.assertEqual(app.mobile_notifier.calls[0]["test_audience"], ["mikkel"])

    async def test_non_null_test_audience_overrides_computed_audience(self):
        # Kristine is home (would join the computed audience) but the override still wins.
        app = _make_app(test_audience=["mikkel"])
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        app.states["person.kristine"] = "home"
        await _tick(app)
        self.assertEqual(app.mobile_notifier.calls[0]["test_audience"], ["mikkel"])


class AlarmAudience(_FrozenTimeTestCase):
    async def test_mikkel_always_included(self):
        app = _make_app(alarm_nobody_home="always_only")
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        audience = await app._episode_audience(FIXED_NOW, include_history=True)
        self.assertEqual(audience, ["mikkel"])

    async def test_housemate_home_is_included(self):
        app = _make_app()
        app.states["person.kristine"] = "home"
        app.states["person.claudia"] = "not_home"
        audience = await app._episode_audience(FIXED_NOW, include_history=True)
        self.assertEqual(audience, ["kristine", "mikkel"])

    async def test_housemate_not_home_or_unavailable_excluded(self):
        app = _make_app(alarm_nobody_home="always_only")
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "unavailable"
        audience = await app._episode_audience(FIXED_NOW, include_history=True)
        self.assertEqual(audience, ["mikkel"])

    async def test_nobody_home_always_only_yields_mikkel_only(self):
        app = _make_app(alarm_nobody_home="always_only")
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        audience = await app._episode_audience(FIXED_NOW, include_history=True)
        self.assertEqual(audience, ["mikkel"])

    async def test_nobody_home_everyone_yields_all_three(self):
        app = _make_app(alarm_nobody_home="everyone")
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        audience = await app._episode_audience(FIXED_NOW, include_history=True)
        self.assertEqual(audience, ["claudia", "kristine", "mikkel"])

    async def test_unknown_presence_does_not_trigger_everyone_fallback(self):
        """The "everyone" fallback only applies once every tracked person is CONFIRMED
        not_home - an unknown/unavailable reading is ambiguous, not evidence of absence,
        so that person is simply left out rather than pulling in the whole household."""
        app = _make_app(alarm_nobody_home="everyone")
        app.states["person.kristine"] = "not_home"
        # person.claudia left unset - unknown/unavailable, not confirmed away.
        audience = await app._episode_audience(FIXED_NOW, include_history=True)
        self.assertEqual(audience, ["mikkel"])

    async def test_departed_housemate_kept_via_episode_notified(self):
        app = _make_app(alarm_nobody_home="always_only", episode_notified=["mikkel", "kristine"])
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        audience = await app._episode_audience(FIXED_NOW, include_history=True)
        self.assertEqual(audience, ["kristine", "mikkel"])

    async def test_alarm_push_sets_episode_notified(self):
        app = _make_app(test_audience=None, alarm_nobody_home="always_only")
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        app.states["person.kristine"] = "home"
        await _tick(app)
        self.assertEqual(app.episode_notified, ["kristine", "mikkel"])
        self.assertEqual(app.mobile_notifier.calls[0]["test_audience"], ["kristine", "mikkel"])

    async def test_departed_housemate_still_gets_all_clear_push(self):
        app = _make_app(
            test_audience=None, alarm_nobody_home="always_only",
            phase="cooldown", since=FIXED_NOW - timedelta(minutes=6),
            episode_id="E1", episode_started_at=FIXED_NOW - timedelta(minutes=20),
            episode_notified=["mikkel", "kristine"],
        )
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        await _tick(app)
        self.assertEqual(app.phase, "clear")
        push = app.mobile_notifier.calls[-1]
        self.assertEqual(push["test_audience"], ["kristine", "mikkel"])
        self.assertIsNone(app.episode_notified)

    async def test_episode_notified_reset_on_new_episode(self):
        app = _make_app(
            test_audience=None, alarm_nobody_home="always_only",
            episode_notified=["mikkel", "kristine", "claudia"],
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.episode_notified, ["mikkel"])

    async def test_repeat_own_targeting_ignores_history_but_recipients_are_recorded(self):
        """A repeat's own targeting is always_notify ∪ home-NOW only (include_history=False)
        - someone who has since left isn't re-targeted by a later repeat. But every
        recipient of ANY alarm push (initial or repeat) is unioned into episode_notified,
        so someone only ever seen home during a repeat is still sticky for hush/all-clear
        afterward."""
        app = _make_app(
            test_audience=None, alarm_nobody_home="always_only",
            phase="alarm", episode_id="E1", episode_notified=["mikkel"],
            last_push_at=FIXED_NOW - timedelta(seconds=121),
            last_announce_at=FIXED_NOW, last_lights_assert_at=FIXED_NOW,
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        app.states["person.kristine"] = "home"
        await _tick(app)
        push = app.mobile_notifier.calls[-1]
        self.assertEqual(push["test_audience"], ["kristine", "mikkel"])
        self.assertEqual(app.episode_notified, ["kristine", "mikkel"])  # now sticky

        app.states["person.kristine"] = "not_home"
        app.last_push_at = FIXED_NOW - timedelta(seconds=121)
        await _tick(app)
        push2 = app.mobile_notifier.calls[-1]
        # This repeat's OWN targeting excludes her (she's not home now)...
        self.assertEqual(push2["test_audience"], ["mikkel"])
        # ...but she stays recorded from the earlier repeat for hush/all-clear purposes.
        self.assertEqual(app.episode_notified, ["kristine", "mikkel"])

    async def test_departed_housemate_notified_initially_excluded_from_later_repeats(self):
        """The INITIAL alarm push's audience (via always_notify ∪ home-now) does not, by
        itself, re-target a repeat once that person has left - only the always-notify set
        is re-targeted, though she remains recorded."""
        app = _make_app(
            test_audience=None, alarm_nobody_home="always_only",
            phase="clear",
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        app.states["person.kristine"] = "home"
        await _tick(app)
        self.assertEqual(app.episode_notified, ["kristine", "mikkel"])

        app.states["person.kristine"] = "not_home"
        app.last_push_at = FIXED_NOW - timedelta(seconds=121)
        await _tick(app)
        push2 = app.mobile_notifier.calls[-1]
        self.assertEqual(push2["test_audience"], ["mikkel"])
        self.assertEqual(app.episode_notified, ["kristine", "mikkel"])

    async def test_repeat_recipient_gets_all_clear_after_leaving(self):
        """Someone first notified via a REPEAT (not the initial push) who leaves before
        the episode ends must still receive the all-clear."""
        app = _make_app(
            test_audience=None, alarm_nobody_home="always_only",
            phase="alarm", episode_id="E1", episode_notified=["mikkel"],
            episode_started_at=FIXED_NOW - timedelta(minutes=20),
            last_push_at=FIXED_NOW - timedelta(seconds=121),
            last_announce_at=FIXED_NOW, last_lights_assert_at=FIXED_NOW,
        )
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        app.states["person.kristine"] = "home"
        await _tick(app)  # repeat push while she's home
        self.assertEqual(app.episode_notified, ["kristine", "mikkel"])

        app.states["person.kristine"] = "not_home"
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.off_since = FIXED_NOW - timedelta(seconds=61)
        await _tick(app)
        self.assertEqual(app.phase, "cooldown")
        app.since = FIXED_NOW - timedelta(minutes=10)
        await _tick(app)
        self.assertEqual(app.phase, "clear")
        push = app.mobile_notifier.calls[-1]
        self.assertEqual(push["test_audience"], ["kristine", "mikkel"])


class PushPayloadShape(_FrozenTimeTestCase):
    async def test_alarm_push_payload_shape(self):
        app = _make_app()
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
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
        await _tick(app)
        self.assertEqual(app.phase, "clear")

    async def test_smoke_on_during_self_test_still_alarms(self):
        app = _make_app(phase="clear", self_test_until=FIXED_NOW + timedelta(minutes=2))
        app.states[app.smoke_entity] = "on"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")

    async def test_self_test_window_expires_and_stops_suppressing(self):
        app = _make_app(phase="clear", self_test_until=FIXED_NOW - timedelta(seconds=1))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "fire"
        await _tick(app)
        self.assertEqual(app.phase, "alarm")
        self.assertIsNone(app.self_test_until)

    async def test_live_self_test_sets_last_self_test_at_and_suppresses(self):
        app = _make_app(phase="clear", last_siren="clear", last_self_test_at=None)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "self_test"
        await _tick(app)
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.last_self_test_at, FIXED_NOW)

    async def test_live_self_test_does_not_reset_last_self_test_at_every_tick(self):
        earlier = FIXED_NOW - timedelta(minutes=1)
        app = _make_app(phase="clear", last_siren="self_test", last_self_test_at=earlier)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "self_test"
        await _tick(app)
        self.assertEqual(app.last_self_test_at, earlier)

    async def test_manual_run_self_test_does_not_stamp_last_self_test_at(self):
        # Only the observed siren edge into self_test stamps last_self_test_at -
        # triggering the switch here is not itself confirmation the device actually ran it.
        app = _make_app(last_self_test_at=None)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await app._run_self_test(FIXED_NOW)
        self.assertIsNone(app.last_self_test_at)


class CookingModeSuppression(_FrozenTimeTestCase):
    async def test_pre_alarm_suppressed_while_cooking_active(self):
        app = _make_app(phase="clear", cooking_until=FIXED_NOW + timedelta(minutes=10))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        await _tick(app)
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.sonos_notifier.calls, [])

    async def test_pre_alarm_resumes_and_boolean_turned_off_after_cooking_expires(self):
        app = _make_app(phase="clear", cooking_until=FIXED_NOW - timedelta(seconds=1))
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "pre_alarm"
        await _tick(app)
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
        await asyncio.gather(*app._test_tasks)  # fault pushes run detached
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        await app._check_faults(FIXED_NOW + timedelta(hours=1))
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        await app._check_faults(FIXED_NOW + timedelta(hours=49))
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(len(app.mobile_notifier.calls), 2)

    async def test_test_overdue_when_never_tested(self):
        app = _make_app(last_self_test_at=None)
        await app._check_faults(FIXED_NOW)
        await asyncio.gather(*app._test_tasks)
        messages = [c["message"] for c in app.mobile_notifier.calls]
        self.assertTrue(any("self-test" in m for m in messages))

    async def test_no_fault_push_when_healthy(self):
        app = _make_app(last_self_test_at=FIXED_NOW - timedelta(days=1))
        app.states[app.battery_entity] = "90"
        await app._check_faults(FIXED_NOW)
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_health_push_targets_mikkel_directly_not_home(self):
        app = _make_app(last_self_test_at=FIXED_NOW - timedelta(days=1))
        app.states[app.battery_entity] = "10"
        await app._check_faults(FIXED_NOW)
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.mobile_notifier.calls[0]["target"], ["mikkel"])

    async def test_throttle_not_stamped_on_push_failure_and_retries_next_call(self):
        app = _make_app(last_self_test_at=FIXED_NOW - timedelta(days=1))
        app.states[app.battery_entity] = "10"

        async def failing_notify(**kwargs):
            raise RuntimeError("boom")

        working_notify = app.mobile_notifier.notify
        app.mobile_notifier.notify = failing_notify
        await app._check_faults(FIXED_NOW)
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.last_fault_push_at, {})
        self.assertEqual(app.mobile_notifier.calls, [])

        app.mobile_notifier.notify = working_notify
        await app._check_faults(FIXED_NOW + timedelta(seconds=1))
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertIn("battery_low", app.last_fault_push_at)

    async def test_throttle_not_stamped_when_real_notifier_delivers_to_nobody(self):
        """MobileNotifier.notify() returning 0 (delivered to nobody) must be treated as
        a failure by _notify/the throttle - using the REAL notifier here (not a fake) so
        a "resolved zero services" bug can't hide behind a mock that always reports
        success."""
        app = _make_app(last_self_test_at=FIXED_NOW - timedelta(days=1))
        app.states[app.battery_entity] = "10"
        app.mobile_notifier = _make_real_mobile_notifier(device_mapping={})
        await app._check_faults(FIXED_NOW)
        await asyncio.gather(*app._test_tasks)
        self.assertEqual(app.last_fault_push_at, {})

    async def test_throttle_stamped_when_real_notifier_delivers(self):
        app = _make_app(last_self_test_at=FIXED_NOW - timedelta(days=1))
        app.states[app.battery_entity] = "10"
        app.mobile_notifier = _make_real_mobile_notifier(
            device_mapping={"mikkel": ["notify.mobile_app_mikkels_phone"]}
        )
        await app._check_faults(FIXED_NOW)
        await asyncio.gather(*app._test_tasks)
        self.assertIn("battery_low", app.last_fault_push_at)


class MonthlySelfTest(_FrozenTimeTestCase):
    """Automatic monthly self-test only while nobody's home, retried daily through day
    monthly_test_max_retry_days, then a give-up health push."""

    async def test_runs_when_nobody_home(self):
        app = _make_app()
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.states["person.mikkel"] = "not_home"
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        now = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
        await app._maybe_run_monthly_test(now)
        await asyncio.gather(*app._test_tasks)  # self-test's health push runs detached
        self.assertIsNotNone(app.self_test_until)
        self.assertTrue(app.monthly_test_resolved)
        messages = [c["message"] for c in app.mobile_notifier.calls]
        self.assertTrue(any("self-test ran" in m for m in messages))

    async def test_refused_when_presence_unknown_even_if_nobody_confirmed_home(self):
        """Unknown/unavailable presence counts as "home" for this gate - a self-test
        sounds the siren, so an unclear reading must never read as "away"."""
        app = _make_app()
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        # person.mikkel left unset - unknown/unavailable.
        now = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
        await app._maybe_run_monthly_test(now)
        self.assertIsNone(app.self_test_until)
        self.assertFalse(app.monthly_test_resolved)
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_skips_and_retries_while_someone_home(self):
        app = _make_app()
        app.states["person.mikkel"] = "home"
        now = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
        await app._maybe_run_monthly_test(now)
        self.assertIsNone(app.self_test_until)
        self.assertFalse(app.monthly_test_resolved)
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_gives_up_after_max_retry_days_with_someone_home(self):
        app = _make_app()
        app.states["person.mikkel"] = "home"
        now = datetime(2026, 9, 7, 11, 0, tzinfo=timezone.utc)
        await app._maybe_run_monthly_test(now)
        await asyncio.gather(*app._test_tasks)  # give-up health push runs detached
        self.assertIsNone(app.self_test_until)
        self.assertTrue(app.monthly_test_resolved)
        self.assertEqual(len(app.mobile_notifier.calls), 1)
        self.assertIn("someone was home", app.mobile_notifier.calls[0]["message"])
        self.assertEqual(app.mobile_notifier.calls[0]["target"], ["mikkel"])

    async def test_does_not_rerun_once_resolved_this_month(self):
        app = _make_app(monthly_test_month_key="2026-09", monthly_test_resolved=True)
        now = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
        await app._maybe_run_monthly_test(now)
        self.assertIsNone(app.self_test_until)
        self.assertEqual(app.mobile_notifier.calls, [])

    async def test_new_month_resets_resolution(self):
        app = _make_app(monthly_test_month_key="2026-08", monthly_test_resolved=True)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.states["person.mikkel"] = "not_home"
        app.states["person.kristine"] = "not_home"
        app.states["person.claudia"] = "not_home"
        now = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
        await app._maybe_run_monthly_test(now)
        self.assertEqual(app.monthly_test_month_key, "2026-09")
        self.assertIsNotNone(app.self_test_until)

    async def test_past_max_retry_day_does_nothing(self):
        app = _make_app()
        app.states["person.mikkel"] = "home"
        now = datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)
        await app._maybe_run_monthly_test(now)
        self.assertFalse(app.monthly_test_resolved)
        self.assertEqual(app.mobile_notifier.calls, [])


class PublishAttributes(_FrozenTimeTestCase):
    async def test_publish_includes_bands_reason_and_dry_run_flag(self):
        app = _make_app(phase="clear", dry_run=True)
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.states[app.aqi_entity] = "30"
        app.states[app.eco2_entity] = "900"
        app.states[app.battery_entity] = "15"
        await _tick(app)
        attrs = app.set_state.call_args.kwargs["attributes"]
        self.assertEqual(attrs["iaq_band"], "fresh")
        self.assertEqual(attrs["eco2_band"], "good")
        self.assertTrue(attrs["battery_low"])
        self.assertTrue(attrs["dry_run"])
        self.assertEqual(attrs["source_entities"], app._source_entities)
        self.assertEqual(attrs["device_available"], True)

    async def test_publishes_on_first_call_after_init(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.set_state.call_count, 1)

    async def test_skips_republish_when_unchanged_within_heartbeat_window(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.set_state.call_count, 1)
        await _tick(app)
        self.assertEqual(app.set_state.call_count, 1)

    async def test_republishes_immediately_when_published_entity_missing(self):
        """HA may lose our published entity (e.g. an HA-core restart wiped it while
        AppDaemon kept running) - don't wait out the 5-minute heartbeat to notice."""
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.set_state.call_count, 1)

        app.states[app.publish_entity] = None
        await _tick(app)
        self.assertEqual(app.set_state.call_count, 2)

    async def test_heartbeat_republishes_after_5_minutes_unchanged(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        await _tick(app)
        self.assertEqual(app.set_state.call_count, 1)
        app._last_published_at = FIXED_NOW - timedelta(minutes=6)
        await _tick(app)
        self.assertEqual(app.set_state.call_count, 2)

    async def test_republishes_immediately_on_attribute_change(self):
        app = _make_app(phase="clear")
        app.states[app.smoke_entity] = "off"
        app.states[app.siren_state_entity] = "clear"
        app.states[app.battery_entity] = "90"
        await _tick(app)
        self.assertEqual(app.set_state.call_count, 1)
        app.states[app.battery_entity] = "50"
        await _tick(app)
        self.assertEqual(app.set_state.call_count, 2)


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

        app._test_tasks = []

        def create_task(coro):
            t = asyncio.ensure_future(coro)
            app._test_tasks.append(t)
            return t

        app.create_task = create_task

        states = {
            "binary_sensor.kitchen_smoke_alarm_smoke": "on",
            "sensor.kitchen_smoke_alarm_siren_state": "fire",
        }

        async def get_state(entity_id, attribute=None):
            return states.get(entity_id)

        app.get_state = get_state

        app.initialize()
        self.assertEqual(app.phase, "clear")  # fresh install default, before the first tick

        await app._evaluate()
        await asyncio.gather(*app._test_tasks)

        self.assertEqual(app.phase, "alarm")
        self.assertEqual(len(mobile_notifier.calls), 1)


class ButtonListenerWiring(unittest.TestCase):
    """state_changed is filtered per button entity_id at registration, not by a manual
    entity check inside a single house-wide listener."""

    def test_hush_and_clear_buttons_each_get_their_own_entity_filtered_listener(self):
        app = fs.FireSafety.__new__(fs.FireSafety)
        app.log = lambda *a, **kw: None
        app.args = {"dry_run": True, "state_file": "/nonexistent/dir/fire_safety_state.json"}
        app.get_app = MagicMock(return_value=None)
        app.listen_state = MagicMock()
        app.listen_event = MagicMock()
        app.run_daily = MagicMock()
        app.run_every = MagicMock()
        app.create_task = MagicMock()

        app.initialize()

        state_changed_calls = [
            c for c in app.listen_event.call_args_list if c.args[1:2] == ("state_changed",)
        ]
        entity_ids = {c.kwargs.get("entity_id") for c in state_changed_calls}
        self.assertEqual(entity_ids, {app.hush_button_entity, app.clear_button_entity})
        self.assertEqual(len(state_changed_calls), 2)


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
        app.light_restore_attempts = 2
        app.episode_notified = ["mikkel", "kristine"]
        app.last_clear_at = datetime(2026, 9, 11, 19, 40, tzinfo=timezone.utc)
        app.last_episode = {"episode_id": "20260911193000", "hush_count": 2}
        app.hush_limit_notified = True
        app.monthly_test_month_key = "2026-09"
        app.monthly_test_resolved = True
        app.stale_alarm_notified = True
        app.generation = 3
        app.pending_hush = {
            "requested_at": datetime(2026, 9, 11, 20, 13, tzinfo=timezone.utc),
            "by": "Kristine", "episode": "20260911201000", "generation": 2,
        }
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
        self.assertEqual(reloaded.light_restore_attempts, 2)
        self.assertEqual(reloaded.episode_notified, ["mikkel", "kristine"])
        self.assertEqual(reloaded.last_clear_at, app.last_clear_at)
        self.assertEqual(reloaded.last_episode, app.last_episode)
        self.assertTrue(reloaded.hush_limit_notified)
        self.assertEqual(reloaded.monthly_test_month_key, "2026-09")
        self.assertTrue(reloaded.monthly_test_resolved)
        # stale_alarm_notified persists (an AppDaemon restart mid-alarm must not repeat
        # the once-only stale announcement) - unlike the truly transient fields below.
        self.assertTrue(reloaded.stale_alarm_notified)
        self.assertIsNone(reloaded.smoke_fallback_since)
        self.assertFalse(reloaded.pre_alarm_stuck)
        self.assertEqual(reloaded.generation, 3)
        self.assertEqual(reloaded.pending_hush, app.pending_hush)

    def test_missing_file_defaults_to_clear(self):
        app = self._app("/nonexistent/dir/fire_safety_state.json")
        app._load_state()
        self.assertEqual(app.phase, "clear")
        self.assertEqual(app.hush_count, 0)
        self.assertEqual(app.last_fault_push_at, {})
        self.assertEqual(app.light_snapshot, {})
        self.assertIsNone(app.light_snapshot_episode)
        self.assertIsNone(app.episode_notified)
        self.assertIsNone(app.last_clear_at)
        self.assertIsNone(app.last_episode)
        self.assertFalse(app.hush_limit_notified)
        self.assertIsNone(app.monthly_test_month_key)
        self.assertFalse(app.monthly_test_resolved)
        self.assertEqual(app.light_restore_attempts, 0)
        self.assertFalse(app.stale_alarm_notified)
        self.assertEqual(app.generation, 0)
        self.assertIsNone(app.pending_hush)

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
        app.light_restore_attempts = 0
        app.episode_notified = None
        app.last_clear_at = None
        app.last_episode = None
        app.hush_limit_notified = False
        app.monthly_test_month_key = None
        app.monthly_test_resolved = False
        app.stale_alarm_notified = False
        app.generation = 0
        app.pending_hush = None
        app._save_state()
        self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
