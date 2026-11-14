# tests/test_lock_health.py - unit tests for LockHealth's pure functions
# (classify_transition, arbitrate, can_act) plus the startup plug-restore path, the
# optional-bridge-plug paths, the heal ladders' first steps, the suppression guards, the
# episode log and the weekly report, which are exercised on a LockHealth built with __new__
# + fake AD callables.
# Run from repo root: python3 -m unittest discover -s apps/security/tests -q
# Imports the real module by stubbing the appdaemon package (not installed locally),
# so the code under test is the deployed code, not a duplicate.

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Stub appdaemon.plugins.hass.hassapi before importing the app module.
_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
_hassapi.Hass = object
for name, mod in (
    ("appdaemon", types.ModuleType("appdaemon")),
    ("appdaemon.plugins", types.ModuleType("appdaemon.plugins")),
    ("appdaemon.plugins.hass", types.ModuleType("appdaemon.plugins.hass")),
    ("appdaemon.plugins.hass.hassapi", _hassapi),
):
    sys.modules.setdefault(name, mod)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lock_health as lh  # noqa: E402

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def side(state, changed=None, changed_by=None, valid=True, just_recovered=False, flapping=False):
    return {
        "state": state,
        "changed": changed,
        "changed_by": changed_by,
        "valid": valid,
        "just_recovered": just_recovered,
        "flapping": flapping,
    }


class ClassifyTransitionTests(unittest.TestCase):
    def test_real(self):
        self.assertEqual(lh.classify_transition("locked", "unlocked"), "real")
        self.assertEqual(lh.classify_transition("unlocked", "locking"), "real")

    def test_recovery(self):
        self.assertEqual(lh.classify_transition("unavailable", "locked"), "recovery")
        self.assertEqual(lh.classify_transition(None, "locked"), "recovery")
        self.assertEqual(lh.classify_transition("unknown", "unlocked"), "recovery")

    def test_loss(self):
        self.assertEqual(lh.classify_transition("locked", "unavailable"), "loss")
        self.assertEqual(lh.classify_transition("unlocked", None), "loss")
        self.assertEqual(lh.classify_transition("jammed", "unknown"), "loss")

    def test_noop_same_state(self):
        self.assertEqual(lh.classify_transition("locked", "locked"), "noop")

    def test_noop_both_invalid(self):
        self.assertEqual(lh.classify_transition("unknown", "unavailable"), "noop")
        self.assertEqual(lh.classify_transition(None, None), "noop")
        self.assertEqual(lh.classify_transition(None, "unknown"), "noop")


class ArbitrateBothInvalidTests(unittest.TestCase):
    def test_both_invalid(self):
        bt = side(None, valid=False)
        cloud = side("unavailable", valid=False)
        result = lh.arbitrate(bt, cloud, prev_state=None)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["source"], "none")
        self.assertFalse(result["diverged"])
        self.assertIsNone(result["stale_side"])
        self.assertFalse(result["needs_reconcile"])


class ArbitrateOneInvalidTests(unittest.TestCase):
    def test_ble_invalid_cloud_wins(self):
        bt = side("unavailable", valid=False)
        cloud = side("locked", changed=NOW)
        result = lh.arbitrate(bt, cloud, prev_state="locked")
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["source"], "cloud")
        self.assertFalse(result["diverged"])
        self.assertIsNone(result["stale_side"])

    def test_cloud_invalid_ble_wins(self):
        bt = side("unlocked", changed=NOW)
        cloud = side(None, valid=False)
        result = lh.arbitrate(bt, cloud, prev_state="unlocked")
        self.assertEqual(result["state"], "unlocked")
        self.assertEqual(result["source"], "ble")
        self.assertFalse(result["diverged"])
        self.assertIsNone(result["stale_side"])


class ArbitrateAgreementTests(unittest.TestCase):
    def test_agree(self):
        bt = side("locked", changed=NOW)
        cloud = side("locked", changed=NOW - timedelta(seconds=2))
        result = lh.arbitrate(bt, cloud, prev_state="locked")
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["source"], "both")
        self.assertFalse(result["diverged"])


class ArbitrateTransitionalTests(unittest.TestCase):
    def test_transitional_never_beats_settled_and_no_diverge_flag(self):
        bt = side("unlocking", changed=NOW)
        cloud = side("locked", changed=NOW - timedelta(minutes=5))
        result = lh.arbitrate(bt, cloud, prev_state="locked")
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["source"], "cloud")
        self.assertFalse(result["diverged"])
        self.assertIsNone(result["stale_side"])

    def test_settled_wins_regardless_of_which_side_is_transitional(self):
        bt = side("locked", changed=NOW - timedelta(minutes=5))
        cloud = side("unlocking", changed=NOW)
        result = lh.arbitrate(bt, cloud, prev_state="locked")
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["source"], "ble")
        self.assertFalse(result["diverged"])


class ArbitrateFreshestWinsTests(unittest.TestCase):
    # The two live incidents this app exists to fix - see the module docstring.
    def test_cloud_stuck_unlocked_ble_later_locked(self):
        # 2026-07-12 shape: cloud stuck "unlocked" 1.5h, BLE reports the real/fresher "locked".
        bt = side("locked", changed=NOW)
        cloud = side("unlocked", changed=NOW - timedelta(hours=1, minutes=30))
        result = lh.arbitrate(bt, cloud, prev_state="unlocked")
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["source"], "ble")
        self.assertTrue(result["diverged"])
        self.assertEqual(result["stale_side"], "cloud")

    def test_ble_stuck_unlocked_cloud_later_locked_auto_lock(self):
        # 2026-07-22 live incident: BLE stuck "unlocked" 1.5h while cloud correctly saw
        # the auto-lock (changed_by "Auto Lock"). Must NOT hardcode "prefer BLE".
        bt = side("unlocked", changed=NOW - timedelta(hours=1, minutes=30))
        cloud = side("locked", changed=NOW, changed_by="Auto Lock")
        result = lh.arbitrate(bt, cloud, prev_state="unlocked")
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["source"], "cloud")
        self.assertTrue(result["diverged"])
        self.assertEqual(result["stale_side"], "ble")


class ArbitrateJustRecoveredTests(unittest.TestCase):
    def test_just_recovered_fresher_does_not_win(self):
        # BLE just reconnected (restart artifact -> very fresh last_changed), but that
        # freshness must not let it beat a legitimately-reported cloud state.
        bt = side("unlocked", changed=NOW, just_recovered=True)
        cloud = side("locked", changed=NOW - timedelta(minutes=10))
        result = lh.arbitrate(bt, cloud, prev_state="unlocked")
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["source"], "cloud")
        self.assertEqual(result["stale_side"], "ble")

    def test_both_just_recovered_falls_through_to_recency(self):
        # Filtering must not remove every candidate - if BOTH just recovered, recency
        # (or the tie-break cascade) still applies instead of a dead end.
        bt = side("unlocked", changed=NOW, just_recovered=True)
        cloud = side("locked", changed=NOW - timedelta(minutes=10), just_recovered=True)
        result = lh.arbitrate(bt, cloud, prev_state="unlocked")
        self.assertEqual(result["source"], "ble")
        self.assertTrue(result["diverged"])


class ArbitrateFlappingTests(unittest.TestCase):
    def test_flapping_side_demoted(self):
        bt = side("unlocked", changed=NOW, flapping=True)
        cloud = side("locked", changed=NOW - timedelta(minutes=10))
        result = lh.arbitrate(bt, cloud, prev_state="unlocked")
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["source"], "cloud")
        self.assertEqual(result["stale_side"], "ble")

    def test_both_flapping_falls_through_to_recency(self):
        bt = side("unlocked", changed=NOW, flapping=True)
        cloud = side("locked", changed=NOW - timedelta(minutes=10), flapping=True)
        result = lh.arbitrate(bt, cloud, prev_state="unlocked")
        self.assertEqual(result["source"], "ble")


class ArbitrateTieTests(unittest.TestCase):
    def test_tie_prev_state_continuity_wins(self):
        bt = side("locked", changed=NOW)
        cloud = side("unlocked", changed=NOW - timedelta(seconds=5))
        result = lh.arbitrate(bt, cloud, prev_state="locked")
        self.assertEqual(result["source"], "ble")
        self.assertEqual(result["state"], "locked")
        self.assertFalse(result["diverged"])
        self.assertFalse(result["needs_reconcile"])

    def test_tie_changed_by_wins_when_no_continuity(self):
        bt = side("locked", changed=NOW, changed_by=None)
        cloud = side("unlocked", changed=NOW - timedelta(seconds=5), changed_by="Mikkel")
        result = lh.arbitrate(bt, cloud, prev_state="jammed")  # matches neither side
        self.assertEqual(result["source"], "cloud")
        self.assertEqual(result["state"], "unlocked")
        self.assertFalse(result["needs_reconcile"])

    def test_tie_unknown_and_needs_reconcile_not_a_ble_default(self):
        bt = side("locked", changed=NOW)
        cloud = side("unlocked", changed=NOW - timedelta(seconds=5))
        result = lh.arbitrate(bt, cloud, prev_state="jammed")  # no continuity, no changed_by
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["source"], "none")
        self.assertTrue(result["needs_reconcile"])
        self.assertNotEqual(result["source"], "ble")

    def test_missing_timestamp_treated_as_tie(self):
        bt = side("locked", changed=None)
        cloud = side("unlocked", changed=NOW)
        result = lh.arbitrate(bt, cloud, prev_state="locked")
        self.assertEqual(result["source"], "ble")  # continuity, not a timestamp-missing default

    def test_tie_within_15s_boundary(self):
        bt = side("locked", changed=NOW)
        cloud = side("unlocked", changed=NOW - timedelta(seconds=15))
        result = lh.arbitrate(bt, cloud, prev_state="unlocked")
        self.assertEqual(result["source"], "cloud")  # exactly at tie_seconds -> still a tie

    def test_gap_just_over_15s_is_not_a_tie(self):
        bt = side("locked", changed=NOW)
        cloud = side("unlocked", changed=NOW - timedelta(seconds=16))
        result = lh.arbitrate(bt, cloud, prev_state="unlocked")
        self.assertEqual(result["source"], "ble")
        self.assertTrue(result["diverged"])
        self.assertEqual(result["stale_side"], "cloud")


class CanActTests(unittest.TestCase):
    def test_cooldown_blocks(self):
        history = [NOW - timedelta(seconds=30)]
        self.assertFalse(lh.can_act(history, NOW, cooldown_s=60, cap_n=10, cap_window_s=7200))

    def test_cooldown_elapsed_allows(self):
        history = [NOW - timedelta(seconds=120)]
        self.assertTrue(lh.can_act(history, NOW, cooldown_s=60, cap_n=10, cap_window_s=7200))

    def test_cap_blocks_over_rolling_window(self):
        history = [NOW - timedelta(minutes=m) for m in (10, 20, 30)]
        self.assertFalse(lh.can_act(history, NOW, cooldown_s=0, cap_n=3, cap_window_s=7200))

    def test_cap_allows_under_limit(self):
        history = [NOW - timedelta(minutes=10)]
        self.assertTrue(lh.can_act(history, NOW, cooldown_s=0, cap_n=3, cap_window_s=7200))

    def test_prunes_old_entries(self):
        history = [NOW - timedelta(hours=3), NOW - timedelta(minutes=10)]
        lh.can_act(history, NOW, cooldown_s=0, cap_n=10, cap_window_s=7200)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0], NOW - timedelta(minutes=10))

    def test_does_not_append_now(self):
        history = []
        lh.can_act(history, NOW, cooldown_s=0, cap_n=10, cap_window_s=7200)
        self.assertEqual(history, [])

    def test_exhausted_cap_recovers_once_old_entries_age_out(self):
        history = [NOW - timedelta(hours=1, minutes=59)]
        self.assertFalse(lh.can_act(history, NOW, cooldown_s=0, cap_n=1, cap_window_s=7200))
        later = NOW + timedelta(minutes=2)
        self.assertTrue(lh.can_act(history, later, cooldown_s=0, cap_n=1, cap_window_s=7200))


class IsValidLockStateTests(unittest.TestCase):
    def test_valid_states(self):
        for s in ("locked", "unlocked", "locking", "unlocking", "jammed"):
            self.assertTrue(lh.is_valid_lock_state(s))

    def test_invalid_states(self):
        for s in (None, "unknown", "unavailable", "open"):
            self.assertFalse(lh.is_valid_lock_state(s))


class PlugRestoreDecisionTests(unittest.TestCase):
    # _save_state/_load_state round-trip of plug_off_at is NOT covered here (that
    # needs the real filesystem + AD logger); plug_restore_decision is the pure
    # surface of the same question, and RestorePlugAlertingTests below covers what
    # the restore path is allowed to touch once the decision says "restore".
    def test_no_marker_is_none(self):
        self.assertEqual(lh.plug_restore_decision(None, "on"), "none")
        self.assertEqual(lh.plug_restore_decision(None, "off"), "none")
        self.assertEqual(lh.plug_restore_decision(None, None), "none")

    def test_marker_and_plug_on_clears(self):
        self.assertEqual(lh.plug_restore_decision(NOW, "on"), "clear")

    def test_marker_and_plug_not_on_restores(self):
        for plug_state in ("off", "unavailable", "unknown", None):
            self.assertEqual(lh.plug_restore_decision(NOW, plug_state), "restore")


def make_app(plug_off_at=NOW - timedelta(minutes=5), plug_state="off", bridge_down_notified=True,
             bridge_down_since=NOW - timedelta(minutes=20), bridge_plug="switch.extender",
             plug_power_entity=None):
    """LockHealth with fake AD callables, without running initialize(). The startup
    plug restore runs synchronously from initialize() before any listener, so calling
    _restore_plug_if_interrupted() directly reproduces it exactly. Instance fields are
    seeded the way _load_state leaves them (notified latches restored from JSON,
    heal_stage derived from them, clear_since deliberately never restored). The plug
    entity switch.extender always exists in the fake HA (reading plug_state); whether
    the app is CONFIGURED with it is the bridge_plug argument. `queried` records every
    entity get_state was asked for. `attrs[(entity, attribute)]` answers attribute reads
    such as last_changed; an attribute read with no entry falls back to the entity's
    state. `events` records fire_event calls (the house feed). Episode lines go to /dev/null
    unless a test points `_episode_file` at a file of its own."""
    app = lh.LockHealth.__new__(lh.LockHealth)
    app.bridge_plug = bridge_plug
    app.plug_power_entity = plug_power_entity
    app.bridge_ping = "binary_sensor.yale_bridge"
    app.lock_bt = "lock.yale_bt"
    app.lock_cloud = "lock.yale"
    app.wake_button = "button.yale_wake"
    app.door = "binary_sensor.apartment_door_open"
    app.report_weekday = 5
    app.bridge_down_heal_seconds = 90.0
    app.recovery_debounce_minutes = 2.0
    app.settle_guard_seconds = 45.0
    app.startup_grace_seconds = 150.0
    app.action_cooldown_minutes = 5.0
    app.cloud_action_cooldown_minutes = 20.0
    app.reload_max_per_2h = 4
    app.plug_power_floor_w = 0.5
    app.plug_cycle_cooldown_minutes = 15.0
    app.plug_cycle_max_per_2h = 3

    app._plug_off_at = plug_off_at
    app._bridge_down_since = bridge_down_since
    app._last_bridge_down_at = None
    app._last_power_cycle_at = None
    app._last_action_at = None
    app._last_cloud_api_at = None
    app._last_source = None
    app._diverged_since = None
    app._plug_cycle_history = []
    app._reload_history = []
    app._last_reload_at = {"ble": None, "cloud": None}
    app._history = {"ble": [], "cloud": []}
    app._startup_at = NOW - timedelta(hours=1)
    app._last_state = "locked"
    app._currently_diverged = False
    app._invalid_since = {"ble": None, "cloud": None}
    app._active_ladder = None
    app._ladder_handle = None
    app._ladder_key = None
    app._ladder_cause = None
    app._episode = None
    app._episode_file = Path(os.devnull)
    app._clear_since = {"divergence": None, "ble_invalid": None, "cloud_invalid": None, "bridge_down": None}
    app._notified = {"divergence": False, "ble_invalid": False, "cloud_invalid": False,
                     "bridge_down": bridge_down_notified, "flapping_ble": False, "flapping_cloud": False,
                     "plug_unavailable": False, "plug_unresponsive": False, "plug_no_load": False}
    app._heal_stage = "gave_up" if bridge_down_notified else "idle"

    app.states = {"switch.extender": plug_state}
    app.attrs = {}
    app.queried = []
    app.service_calls = []
    app.scheduled = []
    app.notifications = []
    app.events = []
    app.cancelled = []
    app.saves = []

    app.log = lambda *a, **kw: None
    app.get_now = lambda: NOW
    app.call_service = lambda service, **kw: app.service_calls.append((service, kw))
    app.fire_event = lambda *a, **kw: app.events.append((a, kw))
    app.create_task = asyncio.run
    app.timer_running = lambda handle: True
    app.cancel_timer = lambda handle: app.cancelled.append(handle)
    app._save_state = lambda: app.saves.append(True)

    def get_state(entity, **kw):
        app.queried.append(entity)
        key = (entity, kw.get("attribute"))
        if key in app.attrs:
            return app.attrs[key]
        return app.states.get(entity)

    async def state(entity):
        return get_state(entity)

    def run_in(cb, delay, **kw):
        app.scheduled.append((cb, delay, kw))
        return object()

    async def notify(message):
        app.notifications.append(message)

    app.get_state = get_state
    app._state = state
    app.run_in = run_in
    app._notify = notify
    return app


class RestorePlugAlertingTests(unittest.TestCase):
    """The restore path is a power-only safety action. It ran on a live box on every
    AD restart (and, since lock_health.yaml declares a MobileNotifier dependency, on
    every MobileNotifier reload too), so anything it clears is cleared over and over."""

    def test_restore_turns_the_plug_back_on(self):
        app = make_app(plug_state="off")
        app._restore_plug_if_interrupted()
        self.assertEqual(app.service_calls,
                         [("switch/turn_on", {"entity_id": "switch.extender"})])
        self.assertIsNotNone(app._plug_off_at)  # marker stays set until the 60s confirm
        self.assertEqual([delay for _cb, delay, _kw in app.scheduled], [60])

    def test_restore_leaves_the_bridge_down_latch_alone(self):
        # One incident -> one alert: _gave_up already pushed "still unreachable after a
        # power-cycle". Re-arming the latch here would re-run the ladder after the startup
        # grace and push a byte-identical second alert on every single restart.
        app = make_app(bridge_down_notified=True)
        app._restore_plug_if_interrupted()
        self.assertTrue(app._notified["bridge_down"])
        self.assertFalse(app._bridge_down_due(NOW))

    def test_restore_does_not_touch_any_notified_flag(self):
        app = make_app(bridge_down_notified=True)
        app._notified["cloud_invalid"] = True
        app._notified["plug_unresponsive"] = True
        before = dict(app._notified)
        app._restore_plug_if_interrupted()
        self.assertEqual(app._notified, before)

    def test_restore_does_not_reset_heal_stage(self):
        app = make_app(bridge_down_notified=True)
        app._restore_plug_if_interrupted()
        self.assertEqual(app._heal_stage, "gave_up")  # episode still open, still published

    def test_restore_works_with_nothing_notified(self):
        app = make_app(bridge_down_notified=False)
        app._restore_plug_if_interrupted()
        self.assertEqual(app.service_calls,
                         [("switch/turn_on", {"entity_id": "switch.extender"})])
        self.assertFalse(any(app._notified.values()))

    def test_repeated_restarts_never_re_arm_the_ladder(self):
        app = make_app(bridge_down_notified=True)
        for _ in range(5):
            app._restore_plug_if_interrupted()
        self.assertTrue(app._notified["bridge_down"])
        self.assertFalse(app._bridge_down_due(NOW))
        self.assertEqual(len(app.service_calls), 5)  # power is re-asserted every time

    def test_plug_already_on_clears_only_the_marker(self):
        app = make_app(plug_state="on", bridge_down_notified=True)
        app._restore_plug_if_interrupted()
        self.assertIsNone(app._plug_off_at)
        self.assertEqual(app.service_calls, [])
        self.assertTrue(app._notified["bridge_down"])

    def test_no_marker_does_nothing(self):
        app = make_app(plug_off_at=None, bridge_down_notified=True)
        app._restore_plug_if_interrupted()
        self.assertEqual(app.service_calls, [])
        self.assertEqual(app.saves, [])
        self.assertTrue(app._notified["bridge_down"])


class AllClearAfterRestoreTests(unittest.TestCase):
    """The all-clear is the only thing that closes a notified episode - and it skips
    keys whose latch is already False, so the latch surviving the restore is what makes
    the closing push (and the heal_stage reset) possible at all."""

    def test_bridge_recovery_closes_the_episode_the_restore_left_open(self):
        app = make_app(bridge_down_notified=True)
        app._restore_plug_if_interrupted()
        app._bridge_down_since = None  # bridge answered again
        asyncio.run(app._check_all_clear(NOW))
        self.assertTrue(app._notified["bridge_down"])  # debounce started, not yet clear
        asyncio.run(app._check_all_clear(NOW + timedelta(minutes=2)))
        self.assertFalse(app._notified["bridge_down"])
        self.assertEqual(app._heal_stage, "idle")
        self.assertEqual(app.notifications, ["Yale lock: the bridge is reachable again."])

    def test_all_clear_skips_a_cleared_latch(self):
        # Guards the regression from the other side: with the latch cleared, the user
        # never gets the closing push for the alert they DID receive.
        app = make_app(bridge_down_notified=False)
        app._bridge_down_since = None
        asyncio.run(app._check_all_clear(NOW))
        asyncio.run(app._check_all_clear(NOW + timedelta(minutes=2)))
        self.assertEqual(app.notifications, [])


class NoBridgePlugTests(unittest.TestCase):
    """bridge_plug is optional: without one the app never touches a plug, and the
    ladder's power-cycle rung has nothing left to do but give up. switch.extender
    still exists in the fake HA (reading "off"), so any stray use of it would show."""

    def test_power_cycle_rung_gives_up_without_any_switch_call(self):
        app = make_app(bridge_plug=None, plug_off_at=None, bridge_down_notified=False)
        app._active_ladder = "bridge"
        app._ladder_key = "bridge_down"
        app._power_cycle_rung("bridge")
        self.assertEqual(app.service_calls, [])
        self.assertEqual(app.scheduled, [])
        self.assertEqual(app.notifications, ["Yale bridge is still unreachable."])
        self.assertTrue(app._notified["bridge_down"])
        self.assertFalse(app._notified["plug_unavailable"])  # no "plug unavailable" alert either
        self.assertEqual(app._heal_stage, "gave_up")
        self.assertIsNone(app._active_ladder)
        self.assertEqual(app._plug_cycle_history, [])
        self.assertNotIn(None, app.queried)

    def test_startup_restore_is_a_noop_and_drops_a_stale_marker(self):
        app = make_app(bridge_plug=None, plug_state="off")  # marker set, plug entity reads off
        app._restore_plug_if_interrupted()
        app._plug_restore_confirm({})
        self.assertEqual(app.service_calls, [])
        self.assertEqual(app.scheduled, [])
        self.assertIsNone(app._plug_off_at)
        self.assertEqual(app.queried, [])
        self.assertTrue(app._notified["bridge_down"])  # latches stay exactly as loaded

    def test_startup_restore_without_a_marker_saves_nothing(self):
        app = make_app(bridge_plug=None, plug_off_at=None)
        app._restore_plug_if_interrupted()
        self.assertEqual(app.service_calls, [])
        self.assertEqual(app.saves, [])

    def test_track_bridge_never_reads_a_plug(self):
        app = make_app(bridge_plug=None, plug_off_at=None, bridge_down_since=None)
        app.states["binary_sensor.yale_bridge"] = "off"
        asyncio.run(app._track_bridge(NOW))
        self.assertEqual(app._bridge_down_since, NOW)
        self.assertEqual(app.queried, ["binary_sensor.yale_bridge"])

    def test_gave_up_only_claims_a_power_cycle_when_there_is_a_plug(self):
        cases = (
            (None, "cloud", "cloud_invalid",
             "Cloud side of the Yale lock is stuck and reload didn't help. Trust 'locked'."),
            (None, "bridge", "bridge_down", "Yale bridge is still unreachable."),
            ("switch.extender", "cloud", "cloud_invalid",
             "Cloud side of the Yale lock is stuck and reload/power-cycle didn't help. Trust 'locked'."),
            ("switch.extender", "bridge", "bridge_down",
             "Yale bridge is still unreachable after a power-cycle."),
        )
        for plug, issue, key, expected in cases:
            with self.subTest(plug=plug, issue=issue):
                app = make_app(bridge_plug=plug, plug_off_at=None, bridge_down_notified=False)
                app._ladder_key = key
                app._gave_up(issue)
                self.assertEqual(app.notifications, [expected])


class PlugWithoutMeterTests(unittest.TestCase):
    """plug_power_entity is optional too: a plug without a meter still power-cycles,
    and no meter read may ever go out as get_state(None)."""

    def test_power_cycle_and_verify_work_without_a_meter_entity(self):
        app = make_app(plug_off_at=None, plug_state="on", bridge_down_notified=False)
        app._active_ladder = "bridge"
        app._ladder_key = "bridge_down"
        app._power_cycle_rung("bridge")
        self.assertEqual(app.service_calls, [("switch/turn_off", {"entity_id": "switch.extender"})])
        self.assertEqual([delay for _cb, delay, _kw in app.scheduled],
                         [lh.PLUG_OFF_WAIT_S, lh.PLUG_VERIFY_WAIT_S, lh.POWER_CYCLE_WAIT_S])
        app._plug_cycle_verify({"pre_switch_lc": None, "pre_meter_lu": None})
        self.assertNotIn(None, app.queried)


LADDER_START = NOW - timedelta(minutes=3)


def make_stale_ble_app():
    """The BLE twin sits on 'unlocked' while the cloud twin saw the auto-lock 5 min ago:
    BLE is the stale side and its ladder is the active one."""
    app = make_app(plug_off_at=None, bridge_down_notified=False, bridge_down_since=None)
    app.states.update({"lock.yale_bt": "unlocked", "lock.yale": "locked"})
    app.attrs[("lock.yale_bt", "last_changed")] = (NOW - timedelta(minutes=90)).isoformat()
    app.attrs[("lock.yale", "last_changed")] = (NOW - timedelta(minutes=5)).isoformat()
    app.attrs[("lock.yale", "changed_by")] = "Auto Lock"
    app._active_ladder = "ble"
    app._ladder_key = "divergence"
    app._ladder_cause = "Yale Bluetooth twin stale 1 min (diverged)"
    app._episode = {"start": LADDER_START, "side": "ble", "key": "divergence", "actions": []}
    return app


def fire_next_timer(app):
    """Run the most recently scheduled run_in callback, as AppDaemon does when it fires."""
    cb, _delay, kw = app.scheduled.pop()
    cb(dict(kw))


def scheduled_steps(app):
    return [(cb.__name__, delay, kw) for cb, delay, kw in app.scheduled]


def called_services(app):
    return [service for service, _kw in app.service_calls]


def capture_tasks(app):
    """Replaces create_task with a recorder that closes each coroutine unrun (the evaluation
    a lock change schedules needs a live AppDaemon)."""
    tasks = []

    def create_task(coro):
        tasks.append(coro)
        coro.close()

    app.create_task = create_task
    return tasks


class GuardTests(unittest.TestCase):
    """Only the settle guard and the startup grace gate a rung."""

    def test_an_open_door_does_not_block_healing(self):
        app = make_app()
        app.states["binary_sensor.apartment_door_open"] = "on"
        self.assertTrue(app._guards_ok(NOW))
        self.assertNotIn("binary_sensor.apartment_door_open", app.queried)

    def test_a_twin_that_just_changed_blocks(self):
        app = make_app()
        app.attrs[("lock.yale", "last_changed")] = (NOW - timedelta(seconds=10)).isoformat()
        self.assertFalse(app._guards_ok(NOW))

    def test_startup_grace_blocks_unless_exempted(self):
        app = make_app()
        app._startup_at = NOW - timedelta(seconds=30)
        self.assertFalse(app._guards_ok(NOW))
        self.assertTrue(app._guards_ok(NOW, allow_in_startup_grace=True))


class BleLadderTests(unittest.TestCase):
    """The Bluetooth ladder is local end to end: refresh, one reload, give up. It never
    presses the wake button (a Yale cloud API call) and never spends the cloud allowance."""

    def assert_backed_off(self, app):
        self.assertEqual(app.service_calls, [])
        self.assertEqual(app.scheduled, [])
        self.assertIsNone(app._active_ladder)
        self.assertEqual(app._heal_stage, "idle")

    def test_first_step_refreshes_the_ble_twin_and_never_presses_wake(self):
        app = make_stale_ble_app()
        spent = NOW - timedelta(minutes=1)  # cloud allowance just used: irrelevant here
        app._last_cloud_api_at = spent
        app._ladder_first_rung({"issue": "ble"})
        self.assertEqual(app.service_calls,
                         [("homeassistant/update_entity", {"entity_id": "lock.yale_bt"})])
        self.assertEqual(app._heal_stage, "refresh")
        self.assertEqual(app._last_action_at, NOW)
        self.assertEqual(app._last_cloud_api_at, spent)  # not re-stamped
        self.assertEqual(app.events, [])  # no house-feed line for the refresh
        self.assertEqual(scheduled_steps(app),
                         [("_after_first_rung", lh.REFRESH_WAIT_S, {"issue": "ble"})])

    def test_a_refresh_that_works_ends_the_ladder_silently(self):
        app = make_stale_ble_app()
        app._ladder_first_rung({"issue": "ble"})
        app.states["lock.yale_bt"] = "locked"  # the twin caught up
        app.attrs[("lock.yale_bt", "last_changed")] = (NOW - timedelta(seconds=30)).isoformat()
        fire_next_timer(app)
        self.assertEqual(called_services(app), ["homeassistant/update_entity"])
        self.assertIsNone(app._active_ladder)
        self.assertEqual(app._heal_stage, "idle")
        self.assertEqual(app.events, [])
        self.assertEqual(app.notifications, [])

    def test_still_stale_after_the_refresh_reloads_once_then_gives_up(self):
        app = make_stale_ble_app()
        app._ladder_first_rung({"issue": "ble"})

        fire_next_timer(app)  # the refresh did not help
        self.assertEqual(app.service_calls[-1],
                         ("homeassistant/reload_config_entry", {"entity_id": "lock.yale_bt"}))
        self.assertEqual(app._heal_stage, "reload")
        self.assertEqual(len(app.events), 1)
        self.assertEqual(app.events[0][1]["cause"], "Yale Bluetooth twin still not right after the refresh")
        self.assertEqual(app.events[0][1]["effect"], "reloaded the Bluetooth integration entry")
        self.assertEqual(scheduled_steps(app), [("_after_reload", lh.RELOAD_WAIT_S, {"issue": "ble"})])

        fire_next_timer(app)  # the reload did not help either
        self.assertEqual(called_services(app),
                         ["homeassistant/update_entity", "homeassistant/reload_config_entry"])
        self.assertEqual(app.scheduled, [])
        self.assertEqual(app._reload_history, [NOW])
        self.assertIsNone(app._active_ladder)
        self.assertEqual(app._heal_stage, "gave_up")
        self.assertTrue(app._notified["divergence"])
        self.assertEqual(app.notifications, [])  # a lagging twin is logged, never pushed
        self.assertNotIn("button/press", called_services(app))
        self.assertIsNone(app._last_cloud_api_at)

    def test_refresh_backs_off_when_a_twin_just_changed(self):
        app = make_stale_ble_app()
        app.attrs[("lock.yale", "last_changed")] = (NOW - timedelta(seconds=10)).isoformat()
        app._ladder_first_rung({"issue": "ble"})
        self.assert_backed_off(app)

    def test_refresh_respects_the_action_cooldown(self):
        app = make_stale_ble_app()
        app._last_action_at = NOW - timedelta(minutes=1)
        app._ladder_first_rung({"issue": "ble"})
        self.assert_backed_off(app)


class CloudLadderTests(unittest.TestCase):
    """The cloud ladder starts with a reload: it restarts the cloud integration's push
    connection, which can die for good at an HA start, and a wake press cannot."""

    def make_cloud_app(self):
        app = make_app(plug_off_at=None, plug_state="on", bridge_down_notified=False, bridge_down_since=None)
        app._active_ladder = "cloud"
        app._ladder_key = "divergence"
        app._ladder_cause = "Yale cloud twin stale 1 min (diverged)"
        app._episode = {"start": LADDER_START, "side": "cloud", "key": "divergence", "actions": []}
        return app

    def test_first_step_reloads_the_cloud_entry_and_never_presses_wake(self):
        app = self.make_cloud_app()
        app._ladder_first_rung({"issue": "cloud"})
        self.assertEqual(app.service_calls,
                         [("homeassistant/reload_config_entry", {"entity_id": "lock.yale"})])
        self.assertEqual(app._heal_stage, "reload")
        self.assertEqual(app._last_cloud_api_at, NOW)
        self.assertEqual(app.events[0][1]["cause"], "Yale cloud twin stale 1 min (diverged)")
        self.assertEqual(app.events[0][1]["effect"], "reloaded the cloud integration entry")
        self.assertEqual(scheduled_steps(app), [("_after_reload", lh.RELOAD_WAIT_S, {"issue": "cloud"})])
        self.assertEqual(app.notifications, [])

    def test_a_recent_wake_press_does_not_block_the_reload(self):
        app = self.make_cloud_app()
        app._last_cloud_api_at = NOW - timedelta(minutes=10)  # e.g. the reconcile wake after a restart
        app._ladder_first_rung({"issue": "cloud"})
        self.assertEqual(called_services(app), ["homeassistant/reload_config_entry"])

    def test_the_first_reload_waits_for_the_action_cooldown(self):
        app = self.make_cloud_app()
        app._last_action_at = NOW - timedelta(minutes=1)
        app._ladder_first_rung({"issue": "cloud"})
        self.assertEqual(app.service_calls, [])
        self.assertIsNone(app._active_ladder)
        self.assertEqual(app.notifications, [])

    def test_a_reload_that_did_not_help_power_cycles(self):
        app = self.make_cloud_app()
        app._ladder_first_rung({"issue": "cloud"})
        fire_next_timer(app)  # still diverged after the reload
        self.assertEqual(app.service_calls[-1], ("switch/turn_off", {"entity_id": "switch.extender"}))
        self.assertEqual(app._heal_stage, "power_cycle")


class StateFileTests(unittest.TestCase):
    """The state file outlives code versions: a key a newer build no longer knows must
    load without complaint, and must not be written back."""

    def make_file_app(self, tmp):
        app = make_app(plug_off_at=None, bridge_down_notified=False, bridge_down_since=None)
        app._state_file = Path(tmp) / "lock_health_state.json"
        app.logs = []
        app.log = lambda msg, **kw: app.logs.append((kw.get("level"), msg))
        return app

    def test_load_tolerates_the_retired_wake_nudge_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_file_app(tmp)
            app._state_file.write_text(json.dumps({
                "last_state": "unlocked",
                "last_action_at": NOW.isoformat(),
                "last_wake_nudge_at": NOW.isoformat(),
            }))
            app._load_state()
            self.assertEqual(app.logs, [])
            self.assertEqual(app._last_state, "unlocked")
            self.assertEqual(app._last_action_at, NOW)

    def test_save_does_not_write_the_retired_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_file_app(tmp)
            lh.LockHealth._save_state(app)  # the real one: make_app stubs the instance attribute
            self.assertEqual(app.logs, [])
            data = json.loads(app._state_file.read_text())
            self.assertNotIn("last_wake_nudge_at", data)
            self.assertIn("last_cloud_api_at", data)


class EpisodeLogTests(unittest.TestCase):
    """Every ladder ends in exactly one line of the episode file: when it started and ended,
    which rungs it took, and how it ended."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "lock_health_episodes.jsonl"

    def lines(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def expected(self, side, key, actions, outcome):
        return {"kind": "episode", "start": LADDER_START.isoformat(timespec="seconds"),
                "end": NOW.isoformat(timespec="seconds"), "side": side, "key": key,
                "actions": actions, "outcome": outcome}

    def stale_ble(self):
        app = make_stale_ble_app()
        app._episode_file = self.path
        return app

    def test_a_refresh_that_works_logs_healed_by_refresh(self):
        app = self.stale_ble()
        app._ladder_first_rung({"issue": "ble"})
        app.states["lock.yale_bt"] = "locked"  # the twin caught up
        fire_next_timer(app)
        self.assertEqual(self.lines(), [self.expected("ble", "divergence", ["refresh"], "healed_by_refresh")])
        self.assertIsNone(app._episode)

    def test_a_reload_that_works_logs_healed_by_reload(self):
        app = self.stale_ble()
        app._ladder_first_rung({"issue": "ble"})
        fire_next_timer(app)  # the refresh did not help: reload
        app.states["lock.yale_bt"] = "locked"
        fire_next_timer(app)
        self.assertEqual(self.lines(),
                         [self.expected("ble", "divergence", ["refresh", "reload"], "healed_by_reload")])

    def test_a_ladder_that_gives_up_logs_gave_up(self):
        app = self.stale_ble()
        app._ladder_first_rung({"issue": "ble"})
        fire_next_timer(app)
        fire_next_timer(app)
        self.assertEqual(self.lines(), [self.expected("ble", "divergence", ["refresh", "reload"], "gave_up")])
        self.assertEqual(app.notifications, [])  # the line is the only trace of a lagging twin

    def test_a_ladder_that_backs_off_logs_backed_off(self):
        app = self.stale_ble()
        app.attrs[("lock.yale", "last_changed")] = (NOW - timedelta(seconds=10)).isoformat()
        app._ladder_first_rung({"issue": "ble"})
        self.assertEqual(self.lines(), [self.expected("ble", "divergence", [], "backed_off")])
        self.assertIsNone(app._episode)

    def test_a_back_off_after_a_rung_keeps_the_actions_taken(self):
        app = self.stale_ble()
        app._ladder_first_rung({"issue": "ble"})
        app.attrs[("lock.yale", "last_changed")] = (NOW - timedelta(seconds=10)).isoformat()
        fire_next_timer(app)  # still stale, but a twin just changed: the reload waits
        self.assertEqual(self.lines(), [self.expected("ble", "divergence", ["refresh"], "backed_off")])

    def test_a_cloud_ladder_that_power_cycles_logs_healed_by_power_cycle(self):
        app = make_app(plug_off_at=None, plug_state="on", bridge_down_notified=False, bridge_down_since=None)
        app._episode_file = self.path
        app._active_ladder = "cloud"
        app._ladder_key = "cloud_invalid"
        app._episode = {"start": LADDER_START, "side": "cloud", "key": "cloud_invalid", "actions": []}
        app._ladder_first_rung({"issue": "cloud"})
        fire_next_timer(app)  # the reload did not help: power-cycle
        app.states.update({"lock.yale_bt": "locked", "lock.yale": "locked"})
        fire_next_timer(app)
        self.assertEqual(self.lines(), [self.expected("cloud", "cloud_invalid", ["reload", "power_cycle"],
                                                      "healed_by_power_cycle")])

    def test_a_bridge_ladder_without_a_plug_logs_gave_up_with_no_actions(self):
        app = make_app(bridge_plug=None, plug_off_at=None, bridge_down_notified=False)
        app._episode_file = self.path
        app._active_ladder = "bridge"
        app._ladder_key = "bridge_down"
        app._episode = {"start": LADDER_START, "side": "bridge", "key": "bridge_down", "actions": []}
        app._power_cycle_rung("bridge")
        self.assertEqual(self.lines(), [self.expected("bridge", "bridge_down", [], "gave_up")])

    def test_a_started_ladder_carries_its_start_and_key_to_the_line(self):
        app = make_app(plug_off_at=None, bridge_down_notified=False, bridge_down_since=None)
        app._episode_file = self.path

        async def run_in(cb, delay, **kw):
            return object()

        app.run_in = run_in
        asyncio.run(app._start_ladder("cloud", "cloud_invalid", "Yale cloud twin invalid 5 min", LADDER_START))
        self.assertEqual(app._episode,
                         {"start": LADDER_START, "side": "cloud", "key": "cloud_invalid", "actions": []})
        app._abort_ladder("cloud", "test")
        self.assertEqual(self.lines(), [self.expected("cloud", "cloud_invalid", [], "backed_off")])


class LadderStartTests(unittest.TestCase):
    """Whichever trigger starts a ladder opens its episode at that moment."""

    def start(self, **setup):
        app = make_app(bridge_down_notified=False, bridge_down_since=None)
        app.divergence_heal_minutes = 1.0
        app.invalid_heal_minutes = 5.0
        app._notified = {}
        for name, value in setup.items():
            setattr(app, name, value)

        async def run_in(cb, delay, **kw):
            return object()

        app.run_in = run_in
        asyncio.run(app._maybe_start_ladder(NOW))
        return app._episode

    def test_each_trigger_opens_an_episode(self):
        stale = NOW - timedelta(minutes=10)
        cases = (
            ("ble", "divergence", dict(_currently_diverged=True, _current_stale_side="ble", _diverged_since=stale)),
            ("cloud", "divergence", dict(_currently_diverged=True, _current_stale_side="cloud", _diverged_since=stale)),
            ("ble", "ble_invalid", dict(_invalid_since={"ble": stale, "cloud": None})),
            ("cloud", "cloud_invalid", dict(_invalid_since={"ble": None, "cloud": stale})),
            ("bridge", "bridge_down", dict(_bridge_down_since=stale)),
        )
        for side, key, setup in cases:
            with self.subTest(key=key, side=side):
                self.assertEqual(self.start(**setup), {"start": NOW, "side": side, "key": key, "actions": []})

    def test_nothing_due_opens_nothing(self):
        self.assertIsNone(self.start())


class JamLineTests(unittest.TestCase):
    """Each time the Bluetooth twin goes to jammed, one line records the door's state."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "lock_health_episodes.jsonl"

    def change(self, entity, old, new, door="on"):
        app = make_app()
        app._episode_file = self.path
        if door is not None:
            app.states["binary_sensor.apartment_door_open"] = door
        tasks = capture_tasks(app)
        app._on_lock_change(entity, None, old, new, {})
        return tasks

    def lines(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_the_ble_twin_going_jammed_logs_the_door_state(self):
        self.change("lock.yale_bt", "unlocking", "jammed", door="on")
        self.change("lock.yale_bt", "unlocking", "jammed", door="off")
        t = NOW.isoformat(timespec="seconds")
        self.assertEqual(self.lines(), [{"kind": "jammed", "t": t, "door": "on"},
                                        {"kind": "jammed", "t": t, "door": "off"}])

    def test_a_door_that_cannot_be_read_logs_null(self):
        self.change("lock.yale_bt", "unlocking", "jammed", door=None)
        self.assertIsNone(self.lines()[0]["door"])

    def test_other_changes_log_nothing(self):
        for entity, old, new in (("lock.yale", "unlocking", "jammed"),
                                 ("lock.yale_bt", "jammed", "jammed"),
                                 ("lock.yale_bt", "jammed", "unlocked"),
                                 ("lock.yale_bt", "locked", "unlocked")):
            with self.subTest(entity=entity, old=old, new=new):
                self.change(entity, old, new)
        self.assertFalse(self.path.exists())

    def test_a_jam_still_reaches_the_evaluation(self):
        self.assertEqual(len(self.change("lock.yale_bt", "unlocking", "jammed")), 1)


class EpisodeWriteFailureTests(unittest.TestCase):
    """A write that fails is logged and nothing else: the ladder still ends the way it would."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A directory cannot be opened for appending.
        self.broken = Path(tmp.name) / "lock_health_episodes.jsonl"
        self.broken.mkdir()

    def warnings(self, app):
        return [msg for level, msg in app.logs if level == "WARNING" and "lock_health_episodes.jsonl" in msg]

    def logging_app(self, app):
        app._episode_file = self.broken
        app.logs = []
        app.log = lambda msg, **kw: app.logs.append((kw.get("level"), msg))
        return app

    def test_a_ladder_end_logs_a_warning_and_still_resets_the_ladder(self):
        app = self.logging_app(make_stale_ble_app())
        app._ladder_first_rung({"issue": "ble"})
        fire_next_timer(app)
        fire_next_timer(app)  # gives up
        self.assertEqual(len(self.warnings(app)), 1)
        self.assertEqual(app._heal_stage, "gave_up")
        self.assertTrue(app._notified["divergence"])
        self.assertIsNone(app._active_ladder)
        self.assertIsNone(app._episode)

    def test_a_back_off_logs_a_warning_and_still_resets_the_ladder(self):
        app = self.logging_app(make_stale_ble_app())
        app.attrs[("lock.yale", "last_changed")] = (NOW - timedelta(seconds=10)).isoformat()
        app._ladder_first_rung({"issue": "ble"})
        self.assertEqual(len(self.warnings(app)), 1)
        self.assertIsNone(app._active_ladder)
        self.assertEqual(app._heal_stage, "idle")

    def test_a_jam_logs_a_warning_and_still_reaches_the_evaluation(self):
        app = self.logging_app(make_app())
        tasks = capture_tasks(app)
        app._on_lock_change("lock.yale_bt", None, "unlocking", "jammed", {})
        self.assertEqual(len(self.warnings(app)), 1)
        self.assertEqual(len(tasks), 1)


SAT = datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone(timedelta(hours=1)))  # ISO week 46


def episode_line(days_ago, side, key, outcome, actions=()):
    end = SAT - timedelta(days=days_ago)
    return json.dumps({"kind": "episode", "start": (end - timedelta(minutes=2)).isoformat(timespec="seconds"),
                       "end": end.isoformat(timespec="seconds"), "side": side, "key": key,
                       "actions": list(actions), "outcome": outcome})


def jam_line(days_ago, door):
    return json.dumps({"kind": "jammed", "t": (SAT - timedelta(days=days_ago)).isoformat(timespec="seconds"),
                       "door": door})


BLE_REFRESH = ("ble", "divergence", "healed_by_refresh", ["refresh"])
BLE_RELOAD = ("ble", "divergence", "healed_by_reload", ["refresh", "reload"])
CLOUD_RELOAD = ("cloud", "divergence", "healed_by_reload", ["reload"])


class WeeklyReportTests(unittest.TestCase):
    """The report is built from the file alone, so it covers whatever the app wrote."""

    def test_a_week_with_every_common_failure(self):
        lines = [episode_line(6.5, *BLE_REFRESH), episode_line(5.2, *BLE_REFRESH),
                 episode_line(4.1, *BLE_REFRESH), episode_line(3.3, *BLE_REFRESH),
                 episode_line(2.4, *BLE_RELOAD), episode_line(1.5, *CLOUD_RELOAD),
                 jam_line(5.0, "on"), jam_line(0.5, "off")]
        self.assertEqual(lh.weekly_report(lines, SAT),
                         "Yale uge 46: Bluetooth bagud 5 (refresh løste 4, reload 1). Cloud bagud 1 (reload løste). "
                         "Fastlåst 2 (1 med døren åben).")

    def test_every_kind_and_outcome_has_its_wording(self):
        lines = [episode_line(1, "ble", "divergence", "gave_up", ["refresh", "reload"]),
                 episode_line(2, "ble", "divergence", "gave_up", ["refresh", "reload"]),
                 episode_line(1, "ble", "ble_invalid", "gave_up", ["refresh", "reload"]),
                 episode_line(1, "cloud", "cloud_invalid", "healed_by_reload", ["reload"]),
                 episode_line(2, "cloud", "cloud_invalid", "healed_by_power_cycle", ["reload", "power_cycle"]),
                 jam_line(1, "off"),
                 episode_line(1, "bridge", "bridge_down", "healed_by_power_cycle", ["power_cycle"]),
                 episode_line(2, "bridge", "bridge_down", "healed_by_power_cycle", ["power_cycle"]),
                 episode_line(3, "bridge", "bridge_down", "gave_up", ["power_cycle"])]
        self.assertEqual(lh.weekly_report(lines, SAT),
                         "Yale uge 46: Bluetooth bagud 2 (opgivet 2). Bluetooth udfald 1 (opgivet). "
                         "Cloud udfald 2 (reload løste 1, power-cycle 1). Fastlåst 1. "
                         "Bridge nede 3 (power-cycle løste 2, opgivet 1).")

    def test_a_week_with_nothing_still_says_so(self):
        self.assertEqual(lh.weekly_report([], SAT), "Yale uge 46: ingen fejl.")

    def test_only_the_last_seven_days_count(self):
        lines = [episode_line(7.5, *BLE_REFRESH), jam_line(8, "on"), episode_line(6.9, *CLOUD_RELOAD)]
        self.assertEqual(lh.weekly_report(lines, SAT), "Yale uge 46: Cloud bagud 1 (reload løste).")

    def test_a_ladder_counts_in_the_week_it_ended(self):
        line = json.loads(episode_line(7, *BLE_REFRESH))
        line["start"] = (SAT - timedelta(days=7, minutes=1)).isoformat(timespec="seconds")
        line["end"] = (SAT - timedelta(days=7) + timedelta(minutes=1)).isoformat(timespec="seconds")
        self.assertEqual(lh.weekly_report([json.dumps(line)], SAT), "Yale uge 46: Bluetooth bagud 1 (refresh løste).")

    def test_backed_off_ladders_are_not_counted(self):
        lines = [episode_line(1, "ble", "divergence", "backed_off"),
                 episode_line(1, "cloud", "divergence", "backed_off", ["reload"])]
        self.assertEqual(lh.weekly_report(lines, SAT), "Yale uge 46: ingen fejl.")

    def test_lines_that_do_not_parse_are_skipped(self):
        naive = json.loads(episode_line(1, *BLE_REFRESH))
        naive["end"] = "2026-11-13T10:00:00"
        lines = ["", "{not json", "[1, 2]", "7", '{"kind": "episode"}', json.dumps(naive),
                 '{"kind": "jammed", "t": "yesterday", "door": "on"}',
                 episode_line(1, *BLE_REFRESH)]
        self.assertEqual(lh.weekly_report(lines, SAT), "Yale uge 46: Bluetooth bagud 1 (refresh løste).")

    def test_jams_without_an_open_door_have_no_detail(self):
        lines = [jam_line(1, "off"), jam_line(2, None), jam_line(3, "unavailable")]
        self.assertEqual(lh.weekly_report(lines, SAT), "Yale uge 46: Fastlåst 3.")


class WeeklyReportPushTests(unittest.TestCase):
    """The report reads the episode file, is always logged, and goes out through the app's own
    _notify only in a week where a ladder gave up."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "lock_health_episodes.jsonl"
        self.app = make_app()
        self.app._episode_file = self.path
        self.app.get_now = lambda: SAT
        self.app.logs = []
        self.app.log = lambda msg, **kw: self.app.logs.append((kw.get("level"), msg))

    def test_a_week_with_a_give_up_is_pushed(self):
        self.path.write_text(episode_line(2, *BLE_REFRESH) + "\n" + jam_line(1, "on") + "\n"
                             + episode_line(1, "ble", "divergence", "gave_up", ["refresh", "reload"]) + "\n")
        self.app._send_report(SAT)
        self.assertEqual(self.app.notifications,
                         ["Yale uge 46: Bluetooth bagud 2 (refresh løste 1, opgivet 1). Fastlåst 1 (1 med døren åben)."])

    def test_a_week_that_healed_by_itself_is_logged_not_pushed(self):
        self.path.write_text(episode_line(2, *BLE_REFRESH) + "\n" + jam_line(1, "on") + "\n")
        self.app._send_report(SAT)
        self.assertEqual(self.app.notifications, [])
        self.assertEqual(self.app.logs, [("INFO", "LockHealth: Yale uge 46: Bluetooth bagud 1 (refresh løste). "
                                                  "Fastlåst 1 (1 med døren åben).")])

    def test_a_give_up_outside_the_week_is_not_pushed(self):
        self.path.write_text(episode_line(8, "ble", "divergence", "gave_up", ["refresh", "reload"]) + "\n")
        self.app._send_report(SAT)
        self.assertEqual(self.app.notifications, [])

    def test_no_file_yet_is_a_quiet_week(self):
        self.app._send_report(SAT)
        self.assertEqual(self.app.notifications, [])
        self.assertEqual(self.app.logs, [("INFO", "LockHealth: Yale uge 46: ingen fejl.")])

    def test_an_empty_file_is_a_quiet_week(self):
        self.path.write_text("")
        self.app._send_report(SAT)
        self.assertEqual(self.app.notifications, [])

    def test_the_push_is_titled_front_door_and_goes_to_the_notify_target(self):
        calls = []

        class Notifier:
            async def notify(self, **kwargs):
                calls.append(kwargs)

        self.app._notify = lh.LockHealth._notify.__get__(self.app)  # the real one: make_app stubs it
        self.app._notifier = Notifier()
        self.app.notify_target = "mikkel"
        self.path.write_text(episode_line(1, "bridge", "bridge_down", "gave_up", ["power_cycle"]) + "\n")
        self.app._send_report(SAT)
        self.assertEqual(calls, [{"title": "Front door", "message": "Yale uge 46: Bridge nede 1 (opgivet).",
                                  "target": "mikkel"}])

    def test_an_unreadable_file_warns_and_sends_nothing(self):
        self.path.mkdir()
        self.app._send_report(SAT)
        self.assertEqual(self.app.notifications, [])
        self.assertEqual([level for level, _msg in self.app.logs], ["WARNING"])

    def test_the_tick_reports_only_on_the_report_day(self):
        for days, sent in ((0, 1), (1, 0), (-1, 0), (-3, 0), (7, 1)):
            with self.subTest(days=days):
                self.app.logs.clear()
                now = SAT + timedelta(days=days)
                self.app.get_now = lambda now=now: now
                self.app._on_report_tick({})
                self.assertEqual(len(self.app.logs), sent)


class ReportScheduleTests(unittest.TestCase):
    """report_day and report_time come from the yaml; initialize() schedules the push."""

    def initialize(self, **args):
        app = lh.LockHealth.__new__(lh.LockHealth)
        app.args = args
        app.logs = []
        app.daily = []
        app.log = lambda msg, **kw: app.logs.append((kw.get("level"), msg))
        app.get_app = lambda name: object()
        app.get_now = lambda: NOW
        app.listen_state = lambda *a, **kw: None
        app.run_every = lambda *a, **kw: None
        app.run_daily = lambda cb, start, **kw: app.daily.append((cb.__name__, start))
        app._load_state = lambda: None
        app.initialize()
        return app

    def test_the_default_is_saturday_at_ten(self):
        app = self.initialize()
        self.assertEqual(app.report_weekday, 5)
        self.assertEqual(app.daily, [("_on_report_tick", "10:00:00")])
        self.assertEqual(app.door, "binary_sensor.apartment_door_open")

    def test_the_day_and_time_are_configurable(self):
        app = self.initialize(report_day="Sunday", report_time="18:30:00", door="binary_sensor.front_door")
        self.assertEqual(app.report_weekday, 6)
        self.assertEqual(app.daily, [("_on_report_tick", "18:30:00")])
        self.assertEqual(app.door, "binary_sensor.front_door")

    def test_an_unknown_day_falls_back_to_saturday_with_a_warning(self):
        app = self.initialize(report_day="lørdag")
        self.assertEqual(app.report_weekday, 5)
        warnings = [msg for level, msg in app.logs if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("lørdag", warnings[0])


if __name__ == "__main__":
    unittest.main()


class DivergenceThresholdTests(unittest.TestCase):
    """Either twin heals after 1 min of divergence."""

    def make(self, side, minutes):
        app = make_app(bridge_down_notified=False, bridge_down_since=None)
        app.divergence_heal_minutes = 1.0
        app._currently_diverged = True
        app._current_stale_side = side
        app._diverged_since = NOW - timedelta(minutes=minutes)
        app._notified = {}
        return app

    def test_not_due_before_one_minute(self):
        self.assertFalse(self.make("cloud", 0.5)._divergence_due(NOW))

    def test_ble_is_due_after_one_minute(self):
        self.assertTrue(self.make("ble", 1.5)._divergence_due(NOW))

    def test_cloud_is_due_after_one_minute(self):
        self.assertTrue(self.make("cloud", 1.5)._divergence_due(NOW))
