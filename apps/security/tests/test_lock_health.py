# tests/test_lock_health.py - unit tests for LockHealth's pure functions
# (classify_transition, arbitrate, can_act) plus the startup plug-restore path,
# which is exercised on a LockHealth built with __new__ + fake AD callables.
# Run from repo root: python3 -m unittest discover -s apps/security/tests -q
# Imports the real module by stubbing the appdaemon package (not installed locally),
# so the code under test is the deployed code, not a duplicate.

import asyncio
import sys
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
             bridge_down_since=NOW - timedelta(minutes=20)):
    """LockHealth with fake AD callables, without running initialize(). The startup
    plug restore runs synchronously from initialize() before any listener, so calling
    _restore_plug_if_interrupted() directly reproduces it exactly. Instance fields are
    seeded the way _load_state leaves them (notified latches restored from JSON,
    heal_stage derived from them, clear_since deliberately never restored)."""
    app = lh.LockHealth.__new__(lh.LockHealth)
    app.bridge_plug = "switch.extender"
    app.bridge_down_heal_seconds = 90.0
    app.recovery_debounce_minutes = 2.0

    app._plug_off_at = plug_off_at
    app._bridge_down_since = bridge_down_since
    app._last_power_cycle_at = None
    app._currently_diverged = False
    app._invalid_since = {"ble": None, "cloud": None}
    app._active_ladder = None
    app._clear_since = {"divergence": None, "ble_invalid": None, "cloud_invalid": None, "bridge_down": None}
    app._notified = {"divergence": False, "ble_invalid": False, "cloud_invalid": False,
                     "bridge_down": bridge_down_notified, "flapping_ble": False, "flapping_cloud": False,
                     "plug_unavailable": False, "plug_unresponsive": False, "plug_no_load": False}
    app._heal_stage = "gave_up" if bridge_down_notified else "idle"

    app.states = {app.bridge_plug: plug_state}
    app.service_calls = []
    app.scheduled = []
    app.notifications = []
    app.saves = []

    app.log = lambda *a, **kw: None
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.call_service = lambda service, **kw: app.service_calls.append((service, kw))
    app._save_state = lambda: app.saves.append(True)

    def run_in(cb, delay, **kw):
        app.scheduled.append((cb, delay, kw))
        return object()

    async def notify(message):
        app.notifications.append(message)

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


if __name__ == "__main__":
    unittest.main()
