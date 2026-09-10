# tests/test_dryer_shadow_audit_2026_09.py - regression coverage for the seven flaws confirmed by
# the 2026-09-07 dryer FSM shadow audit (see the audit itself for the full write-up). One class per
# flaw (F1-F7); each test FAILS against the pre-fix code and PASSES against the fix in this same
# change. Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# Reuses the two established harnesses rather than inventing a third:
#   - test_dryer_policy_table.Harness (+ CFG/covers/EXERCISED) for pure table/policy-level rows
#     (F1a/F1's new POWER_HIGH row, F6, F7b's pure-function half).
#   - test_dryer_shadow.make_shadow (+ push_power/push_door/fire_shortest) for anything that needs
#     the real DryerShadow.initialize() boot sequence (F1b's tick-driven reconciler, F2, F3, F4,
#     F5, F7a's watchdog re-arm).
# Importing test_dryer_shadow first (as this file's own boilerplate below does) already performs
# the appdaemon.plugins.hass.hassapi stub + sys.path setup every other dryer-shadow test file
# needs; this file additionally does its own sys.path.insert for robustness against import order.

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import types  # noqa: E402

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

import dryer_policy as dp  # noqa: E402
import dryer_shadow as ds  # noqa: E402
from appliance_fsm import EvidenceType, State  # noqa: E402
from cycle_store import CycleStore, format_utc  # noqa: E402
from test_dryer_policy_table import CFG, Harness, covers  # noqa: E402
from test_dryer_shadow import ENERGY, POWER, fire_shortest, make_shadow, push_door, push_power  # noqa: E402

E = EvidenceType
S = State


def _seed_store(state_file, **fields):
    store = CycleStore(state_file, "dryer_shadow")
    ok = store.save(fields)
    assert ok
    return state_file


# =================================================================================================
# F1: PAUSED had no power-driven or periodic exit
# =================================================================================================


@covers((S.PAUSED, E.POWER_HIGH))
class F1PausedDoorCloseGapAndPowerHigh(unittest.TestCase):
    """(a) 5W < live power < 8W previously matched NO PAUSED/DOOR_CLOSED row at all
    (G_power_high_live needs >= start_w=8, the old G_power_low_live needed <= stop_w=5) - the
    machine silently stayed PAUSED forever, no announce, no feedback. Fixed by making the
    "else" branch (below start_w) an unguarded catch-all, exactly mirroring dryer_monitor.py:
    1579-1584's `>= start_w` / `else` split. Also covers the new PAUSED/POWER_HIGH row."""

    def _to_paused(self, h):
        h.enter_running()
        h.clear_cooling()
        h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 500}))
        self.assertEqual(h.fsm.state, S.PAUSED)

    def test_door_closed_gap_watts_6_finishes_like_5(self):
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"min_cycle_minutes": 1})
        self._to_paused(h)
        h.states[CFG["power_sensor"]] = "6"
        h.states[CFG["energy_sensor"]] = "10.5"
        res = h.fsm.submit(h.ev(E.DOOR_CLOSED, {"power_w": 6}))
        self.assertEqual(h.fsm.state, S.FINISHED, "6W (between stop_w and start_w) must finish, not wedge in PAUSED")
        self.assertTrue(res.matched)
        self.assertEqual(h.sink.announces, ["Dryer is ready to be emptied"])

    def test_door_closed_gap_watts_7_finishes_like_5(self):
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"min_cycle_minutes": 1})
        self._to_paused(h)
        h.states[CFG["power_sensor"]] = "7"
        h.states[CFG["energy_sensor"]] = "10.5"
        res = h.fsm.submit(h.ev(E.DOOR_CLOSED, {"power_w": 7}))
        self.assertEqual(h.fsm.state, S.FINISHED, "7W (between stop_w and start_w) must finish, not wedge in PAUSED")
        self.assertTrue(res.matched)

    def test_door_closed_gap_invalid_cycle_still_goes_off(self):
        """Regression guard: the catch-all's OFF fallback (row 3) must still apply when the cycle
        itself is not valid - the fix only removes the power-window gap, not the validity gate."""
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"min_cycle_minutes": 999})
        self._to_paused(h)
        h.states[CFG["power_sensor"]] = "6"
        res = h.fsm.submit(h.ev(E.DOOR_CLOSED, {"power_w": 6}))
        self.assertEqual(h.fsm.state, S.OFF)

    def test_paused_power_high_resumes_running(self):
        """A live POWER_HIGH sample (PowerStartDetector emits it on any watts >= start_w
        regardless of state) was previously unlisted for PAUSED - silently dropped. Now resumes
        Running the same way a high-power door-close does."""
        h = Harness(state=S.OFF, cooling_period=0)
        self._to_paused(h)
        h.states[CFG["power_sensor"]] = "600"
        res = h.fsm.submit(h.ev(E.POWER_HIGH, {"watts": 600}))
        self.assertEqual(h.fsm.state, S.RUNNING)
        self.assertTrue(res.matched)


class F1bPausedPeriodicReconciler(unittest.TestCase):
    """(b) A restart while Paused with the door ALREADY closed never gets a fresh DOOR_CLOSED edge
    to react to - before this fix, appliance_fsm.py's tick() was a complete no-op (no detector
    overrode Detector.tick), so the machine stayed wedged in PAUSED (published "Paused") with a
    dryer that is actually running at 600W, until the 10-minute pause watchdog eventually force-
    wiped the whole in-progress cycle. PausedExitReconciler.tick() closes this within ONE tick."""

    def test_restart_paused_door_already_closed_high_power_resumes_with_original_start_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = str(Path(tmp) / "dryer_shadow_state.json")
            start = datetime.now(timezone.utc) - timedelta(minutes=45)
            _seed_store(
                state_file, state="Paused", state_since=format_utc(start), cycle_id="cid-f1b",
                cycle_start_time=format_utc(start), energy_at_start=10.0,
                detected_programme="unknown", programme_duration_min=120,
            )
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="600", door_state="off", live_state="Paused",
                extra_args={"min_cycle_minutes": 1, "cooling_period": 0},
            )
            app.initialize()
            self.assertEqual(app.fsm.state.name, "PAUSED")
            original_start_time = app._policy.start_time
            self.assertIsNotNone(original_start_time)

            app.fsm.tick()  # one periodic tick - the ONLY thing driving detector.tick()

            self.assertEqual(
                app.fsm.state.name, "RUNNING",
                "PausedExitReconciler must re-emit DOOR_CLOSED-equivalent evidence and resolve "
                "the stale restore within one tick",
            )
            self.assertEqual(app._policy.start_time, original_start_time, "start_time must survive untouched")


# =================================================================================================
# F2: progress-store fingerprint hashed only dryer_shadow.py, and a None fingerprint passed through
# =================================================================================================


class F2FingerprintCoverage(unittest.TestCase):
    def test_hash_changes_when_any_of_the_four_files_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = ds._FINGERPRINT_MODULES
            for name in names:
                (Path(tmp) / name).write_bytes(f"content of {name}\n".encode())
            baseline = ds._hash_engine_files(tmp)
            self.assertIsNotNone(baseline)

            for mutated in names:
                (Path(tmp) / mutated).write_bytes(f"MUTATED {mutated}\n".encode())
                changed = ds._hash_engine_files(tmp)
                self.assertNotEqual(
                    changed, baseline,
                    f"fingerprint did not change when {mutated} changed - not all four engine "
                    f"files are being hashed",
                )
                # restore for the next iteration
                (Path(tmp) / mutated).write_bytes(f"content of {mutated}\n".encode())

    def test_missing_file_yields_none_not_a_partial_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Only 3 of the 4 expected files present.
            for name in list(ds._FINGERPRINT_MODULES)[:3]:
                (Path(tmp) / name).write_bytes(b"x")
            self.assertIsNone(ds._hash_engine_files(tmp))

    def test_none_live_fingerprint_is_treated_as_a_reset_not_a_free_pass(self):
        """Before the fix, `if code_fingerprint and stored_fingerprint != code_fingerprint`
        short-circuited to False whenever the CURRENT fingerprint was None (this boot could not
        hash the engine files), silently trusting a stored counter with unverifiable provenance."""
        got = dp.resolve_shadow_progress(
            file_data={"clean_cycles": 9, "code_fingerprint": "some-real-hash"},
            entity_attrs={},
            code_fingerprint=None,
        )
        self.assertEqual(got["clean_cycles"], 0, "a None live fingerprint must reset, not pass a stored counter through")
        self.assertIsNotNone(got["reset_reason"])


# =================================================================================================
# F3: late-detection push reported total run time instead of latency since actual finish
# =================================================================================================


class F3LatencyNotRunTimeInPushMessage(unittest.TestCase):
    def test_boot_reconcile_130min_cycle_ended_4min_ago_reports_4_min(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = str(Path(tmp) / "dryer_shadow_state.json")
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=134)
            last_high = now - timedelta(minutes=4)
            _seed_store(
                state_file, state="Running", state_since=format_utc(start), cycle_id="cid-f3",
                cycle_start_time=format_utc(start), energy_at_start=10.0,
                detected_programme="unknown", programme_duration_min=120, max_power_w=600.0,
                last_high_energy_at=format_utc(last_high),
            )
            history = {
                POWER: [{"state": "2", "last_changed": (now - timedelta(minutes=1)).isoformat()}],
            }
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", live_state="Running", history=history,
                extra_args={
                    "min_cycle_minutes": 1, "restore_corroboration_window_minutes": 2,
                    "cooling_period": 0,
                },
            )
            app.initialize()

            self.assertEqual(app.fsm.state.name, "FINISHED", "boot RECONCILE must conclude on the sustained-low history")
            self.assertEqual(app._actions.announced, [], "a boot-time conclusion must never use Sonos")
            self.assertEqual(len(app._actions.pushed), 1)
            message = app._actions.pushed[0]
            self.assertIn("4 min ago", message, f"expected latency (~4 min), got: {message!r}")
            self.assertNotIn("134 min ago", message, "must not report total run time as the latency")


# =================================================================================================
# F4: selector reset / announce-toggle re-enable never ported; push wrongly gated behind the toggle
# =================================================================================================


class F4SelectorResetAndAnnounceToggle(unittest.TestCase):
    def test_off_landing_records_idle_selector_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp, extra_args={
                "programme_entity": "input_select.dryer_programme",
                "dryness_entity": "input_select.dryer_dryness",
                "skane_plus_entity": "input_boolean.dryer_skane_plus",
                "min_cycle_minutes": 1,
            })
            app.initialize()
            push_power(app, entities, "620")
            fire_shortest(app)  # start-confirm -> RUNNING
            entities[ENERGY]["state"] = "10.5"
            push_power(app, entities, "2")
            app._policy.start_time = app._policy.start_time - timedelta(minutes=130)
            fire_shortest(app)  # end-confirm -> FINISHED
            push_door(app, entities, "on")  # -> EMPTIED
            push_door(app, entities, "off")  # -> OFF

            self.assertEqual(app.fsm.state.name, "OFF")
            unconfirmed = dp.DEFAULTS["selectors"]["programme_unconfirmed_option"]
            self.assertIn(("input_select.dryer_programme", unconfirmed), app._actions.selected)
            self.assertTrue(app._actions.resets, "OFF landing must record a selector reset")

    def test_emptied_landing_records_announce_reenable(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp, extra_args={
                "announce_entity": "input_boolean.dryer_announce", "min_cycle_minutes": 1,
            })
            app.initialize()
            push_power(app, entities, "620")
            fire_shortest(app)
            entities[ENERGY]["state"] = "10.5"
            push_power(app, entities, "2")
            app._policy.start_time = app._policy.start_time - timedelta(minutes=130)
            fire_shortest(app)
            self.assertEqual(app.fsm.state.name, "FINISHED")

            push_door(app, entities, "on")  # FINISHED -> EMPTIED
            self.assertEqual(app.fsm.state.name, "EMPTIED")
            self.assertIn(("input_boolean.dryer_announce", "on"), app._actions.selected)

    def test_stale_finish_pushes_even_when_announce_toggle_is_off_but_never_announces(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = str(Path(tmp) / "dryer_shadow_state.json")
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=180)
            _seed_store(
                state_file, state="Running", state_since=format_utc(start), cycle_id="cid-f4",
                cycle_start_time=format_utc(start), energy_at_start=10.0,
                detected_programme="unknown", programme_duration_min=120, max_power_w=600.0,
                last_high_energy_at=format_utc(start),
            )
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", live_state="Running",
                extra_args={
                    "min_cycle_minutes": 1, "restore_corroboration_window_minutes": 10,
                    "cooling_period": 0, "announce_entity": "input_boolean.dryer_announce",
                },
            )
            entities["input_boolean.dryer_announce"] = {"state": "off", "attributes": {}, "last_changed": None}
            app.initialize()
            self.assertTrue(app.fsm.hypothesis, "absent history - boot RECONCILE must not conclude")

            entities[ENERGY]["state"] = "10.5"
            push_power(app, entities, "0")  # live 0W -> lands RUNNING->ENDING, clears hypothesis
            fire_shortest(app)  # PowerEndDetector's stop_for confirm -> POWER_END_CONFIRMED -> FINISHED

            self.assertEqual(app.fsm.state.name, "FINISHED")
            self.assertEqual(len(app._actions.pushed), 1, "push must fire regardless of the announce toggle")
            self.assertEqual(app._actions.announced, [], "announce must stay gated behind the toggle")


# =================================================================================================
# F5: cycle_id is None dropped feedback entirely (entity Running/Paused, store missing/unreadable)
# =================================================================================================


class F5NoneCycleIdStillFeedsBackOnce(unittest.TestCase):
    def test_entity_running_no_store_finish_produces_one_feedback_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No CycleStore.save() at all - state_file points at a path that never gets written,
            # so self._store.load() returns None (missing file) exactly like a corrupt/unreadable
            # store would after logging its own WARNING.
            app, entities, calls = make_shadow(
                tmp, v2_state="Paused", v2_attrs={}, power_w="2",
                extra_args={"min_cycle_minutes": 1, "cooling_period": 0},
            )
            app.initialize()
            self.assertEqual(app.fsm.state.name, "PAUSED")
            self.assertIsNone(app._policy.start_time, "precondition: no store means no restored physics either")
            self.assertIsNotNone(app.fsm.cycle_id, "a restored active cycle must never carry a None cycle_id")

            # keep_fresh short-circuits G_valid_cycle regardless of start_time/energy being unknown
            # (is_valid_completed_cycle: "if keep_fresh: return True") - isolates this test to the
            # cycle_id fix itself, not a second, unrelated physics-restore gap.
            app._policy.keep_fresh_detected = True
            push_door(app, entities, "off")  # PAUSED/DOOR_CLOSED, power=2 (< start_w) -> FINISHED

            self.assertEqual(app.fsm.state.name, "FINISHED")
            self.assertEqual(len(app._actions.feedback), 1, "a None-cycle_id restore must still save exactly once")

    def test_request_feedback_saves_when_cycle_id_is_none(self):
        """Engine-level pin of the exact appliance_fsm.py:686 condition, isolated from the shadow's
        boot sequence entirely."""
        h = Harness(state=S.RUNNING, cooling_period=0, cfg_overrides={"min_cycle_minutes": 1})
        h.fsm.set_cycle_id(None)
        h.policy.start_time = h.clock.now() - timedelta(minutes=200)
        h.policy.energy_start = 10.0
        h.states[CFG["power_sensor"]] = "2"
        h.states[CFG["energy_sensor"]] = "10.5"
        res = h.fsm.submit(h.ev(E.POWER_LOW, {"watts": 2}))
        self.assertEqual(h.fsm.state, S.ENDING)
        res = h.fsm.submit(h.ev(E.POWER_END_CONFIRMED, {"watts": 2}))
        self.assertEqual(h.fsm.state, S.FINISHED)
        self.assertEqual(len(h.sink.feedbacks), 1, "cycle_id=None must degrade to 'no guard', not drop the record")


# =================================================================================================
# F6: PLUG_OUTAGE routed only from RUNNING - ENDING/PAUSED published a live state with a dead plug
# =================================================================================================


@covers((S.ENDING, E.PLUG_OUTAGE), (S.PAUSED, E.PLUG_OUTAGE))
class F6PlugOutageRoutedFromEndingAndPaused(unittest.TestCase):
    def test_ending_plug_outage_forces_off(self):
        h = Harness(state=S.OFF, cooling_period=0)
        h.enter_running()
        h.enter_ending(watts=2)
        self.assertEqual(h.fsm.state, S.ENDING)
        res = h.fsm.submit(h.ev(E.PLUG_OUTAGE))
        self.assertEqual(h.fsm.state, S.OFF)
        self.assertTrue(res.matched)

    def test_paused_plug_outage_forces_off(self):
        h = Harness(state=S.OFF, cooling_period=0)
        h.enter_running()
        h.clear_cooling()
        h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 500}))
        self.assertEqual(h.fsm.state, S.PAUSED)
        res = h.fsm.submit(h.ev(E.PLUG_OUTAGE))
        self.assertEqual(h.fsm.state, S.OFF)
        self.assertTrue(res.matched)


# =================================================================================================
# F7: restart re-arm anchored the 5h running watchdog to state_since, not cycle start; the entity
# branch's state_since had no fallback to the store's own state_since
# =================================================================================================


class F7RunningWatchdogAnchorAndStateSinceFallback(unittest.TestCase):
    def test_restore_running_watchdog_anchored_to_start_time_not_state_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = str(Path(tmp) / "dryer_shadow_state.json")
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=4)
            _seed_store(
                state_file, state="Running", state_since=format_utc(start), cycle_id="cid-f7",
                cycle_start_time=format_utc(start), energy_at_start=10.0,
            )
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, v2_state="Running", v2_attrs={"cycle_id": "cid-f7"},
                v2_last_changed=format_utc(now),  # entity-sourced, but freshly "changed" just now
                power_w="600", live_state="Running",
                extra_args={"min_cycle_minutes": 1, "cooling_period": 0},
            )
            app.initialize()
            self.assertEqual(app.fsm.state.name, "RUNNING")
            self.assertIsNotNone(app._policy.start_time)

            handle = app._policy.watchdogs["running"]._handle
            self.assertIsNotNone(handle, "running watchdog must be armed after a Running restore")
            entry = next(e for e in app.scheduled if e[0] == handle)
            remaining_s = entry[2]
            self.assertLess(
                remaining_s, 4000,
                f"running watchdog must anchor to start_time (~1h left), not state_since (~5h): got {remaining_s}s",
            )
            self.assertGreater(remaining_s, 3000)

    def test_entity_branch_falls_back_to_store_state_since_when_entity_last_changed_missing(self):
        snap = dp.resolve_boot_snapshot(
            entity_state="Running", entity_attrs={}, entity_last_changed=None,
            store_data={"state": "Running", "state_since": "2026-09-01T00:00:00+00:00", "cycle_id": "x"},
            helper_state=None, now=datetime.now(timezone.utc), cfg=CFG,
        )
        self.assertEqual(snap["source"], "entity")
        self.assertEqual(
            snap["state_since"], "2026-09-01T00:00:00+00:00",
            "entity_last_changed missing must fall back to the store's own state_since, not None",
        )


if __name__ == "__main__":
    unittest.main()
