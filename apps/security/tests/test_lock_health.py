# tests/test_lock_health.py - unit tests for LockHealth's pure functions
# (classify_transition, arbitrate, can_act).
# Run from repo root: python3 -m unittest discover -s apps/security/tests -q
# Imports the real module by stubbing the appdaemon package (not installed locally),
# so the code under test is the deployed code, not a duplicate.

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


if __name__ == "__main__":
    unittest.main()
