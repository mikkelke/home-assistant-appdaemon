"""Restore provenance through real DryerShadow.initialize(), with only AppDaemon I/O faked.

The v2 entity's public contract has no cycle-start/energy tracking. An active label, a
last_changed timestamp, or a newly minted identity cannot replace the durable cycle record.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_dryer_shadow import ENERGY, LIVE, V2, fire_shortest, make_shadow, push_power
import dryer_policy as dp
from cycle_store import CycleStore, format_utc, parse_utc


class ShadowRestoreValidation(unittest.TestCase):
    def _seed(self, app, *, state="Running", **changes):
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=40)
        payload = {
            "state": state, "state_since": format_utc(start), "cycle_id": "original-cycle",
            "cycle_start_time": format_utc(start), "energy_at_start": 10.0,
            "programme_duration_min": 120, "last_high_energy_at": format_utc(start),
            "code_fingerprint": app._compute_fingerprint(),
        }
        payload.update(changes)
        store = CycleStore(app.args["state_file"], "dryer_shadow")
        self.assertTrue(store.save(payload))
        # Corrupt/old persistence envelopes cannot be constructed via save() (it stamps now).
        if "saved_at" in changes:
            payload = store.load()
            payload["saved_at"] = changes["saved_at"]
            Path(app.args["state_file"]).write_text(json.dumps(payload))
        return parse_utc(payload["cycle_start_time"])

    def _assert_off(self, app, entities):
        self.assertEqual(app.fsm.state.name, "OFF")
        self.assertEqual(entities[V2]["state"], "Off")
        self.assertIsNone(app._policy.start_time)
        self.assertIsNone(app.fsm.cycle_id)
        self.assertIsNone(app._policy.watchdogs["running"]._handle)
        self.assertEqual(app._actions.feedback, [])
        self.assertEqual(app._actions.announced, [])
        self.assertEqual(app._actions.pushed, [])
        self.assertTrue(all(entity == V2 for entity, _ in app.set_state_calls))
        self.assertEqual(entities[LIVE]["state"], "Off")

    def test_running_and_paused_entity_without_tracking_never_restore_or_mint(self):
        for state in ("Running", "Paused"):
            for watts in ("0", "unavailable"):
                with self.subTest(state=state, watts=watts), tempfile.TemporaryDirectory() as tmp:
                    app, entities, _ = make_shadow(
                        tmp, v2_state=state, v2_attrs={"cycle_id": "old-cycle"}, power_w=watts,
                        v2_last_changed=format_utc(datetime.now(timezone.utc)),
                    )
                    app.initialize()
                    self._assert_off(app, entities)

    def test_corrupt_or_wrong_appliance_store_cannot_support_an_active_entity(self):
        for contents in ("{broken", "[]", json.dumps({"schema": 1, "appliance": "dryer"})):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as tmp:
                app, entities, _ = make_shadow(tmp, v2_state="Running")
                Path(app.args["state_file"]).write_text(contents)
                app.initialize()
                self._assert_off(app, entities)

    def test_current_store_preserves_clock_identity_and_valid_source_precedence(self):
        for entity in (None, "unknown", "unavailable", "Running", "Paused"):
            with self.subTest(entity=entity), tempfile.TemporaryDirectory() as tmp:
                app, entities, _ = make_shadow(
                    tmp, v2_state=entity, v2_attrs={"cycle_id": "original-cycle"}, power_w="600",
                )
                start = self._seed(app)
                app.initialize()
                self.assertEqual(app.fsm.state.name, "PAUSED" if entity == "Paused" else "RUNNING")
                self.assertEqual(app._policy.start_time, start)
                self.assertEqual(app.fsm.cycle_id, "original-cycle")
                self.assertFalse(app.fsm.hypothesis)
                self.assertIsNotNone(app._policy.watchdogs["running"]._handle)
                self.assertEqual(app._actions.announced, [])

    def test_all_active_sources_need_corroboration_at_zero_or_unavailable_power(self):
        for entity in (None, "unknown", "Running", "Paused"):
            for watts in ("0", "unavailable"):
                with self.subTest(entity=entity, watts=watts), tempfile.TemporaryDirectory() as tmp:
                    app, _, _ = make_shadow(tmp, v2_state=entity, power_w=watts)
                    self._seed(app)
                    app.initialize()
                    self.assertEqual(app.fsm.state.name, "PAUSED" if entity == "Paused" else "RUNNING")
                    self.assertTrue(app.fsm.hypothesis)
                    self.assertEqual(app._actions.announced, [])
                    self.assertEqual(app._actions.pushed, [])
                    self.assertEqual(app._actions.feedback, [])

    def test_recent_valid_high_energy_can_corroborate_an_entity_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = make_shadow(tmp, v2_state="Running", power_w="0")
            self._seed(app, last_high_energy_at=format_utc(datetime.now(timezone.utc) - timedelta(minutes=2)))
            app.initialize()
            self.assertEqual(app.fsm.state.name, "RUNNING")
            self.assertFalse(app.fsm.hypothesis)

    def test_future_high_inside_clock_skew_cannot_corroborate_a_zero_power_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = make_shadow(tmp, v2_state="Running", power_w="0")
            self._seed(app, last_high_energy_at=format_utc(datetime.now(timezone.utc) + timedelta(seconds=30)))
            app.initialize()
            self.assertEqual(app.fsm.state.name, "RUNNING")
            self.assertTrue(app.fsm.hypothesis)

    def test_changed_engine_keeps_entity_restore_hypothesis_despite_high_power(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = make_shadow(tmp, v2_state="Running", power_w="600")
            self._seed(app, code_fingerprint="previous-engine")
            app.initialize()
            self.assertEqual(app.fsm.state.name, "RUNNING")
            self.assertTrue(app.fsm.hypothesis)

    def test_missing_corrupt_stale_and_future_clocks_are_rejected_for_both_sources(self):
        now = datetime.now(timezone.utc)
        invalid = (
            {"cycle_start_time": None}, {"cycle_start_time": "broken"},
            {"cycle_start_time": format_utc(now - timedelta(hours=6))},
            {"cycle_start_time": format_utc(now + timedelta(minutes=5))},
            {"saved_at": None}, {"saved_at": "broken"},
            {"saved_at": format_utc(now - timedelta(hours=13))},
            {"saved_at": format_utc(now + timedelta(minutes=5))},
            {"state_since": "broken"}, {"state_since": format_utc(now + timedelta(minutes=5))},
            {"last_high_energy_at": "broken"},
            {"last_high_energy_at": format_utc(now + timedelta(minutes=5))},
            {"cycle_id": None}, {"cycle_id": ""},
            {"energy_at_start": "broken"}, {"energy_at_start": float("nan")},
            {"programme_duration_min": "120"}, {"max_power_w": float("inf")},
            {"keep_fresh_detected": "false"}, {"detected_programme": {}},
        )
        for entity in (None, "Running", "Paused"):
            for change in invalid:
                with self.subTest(entity=entity, change=change), tempfile.TemporaryDirectory() as tmp:
                    app, entities, _ = make_shadow(tmp, v2_state=entity)
                    self._seed(app, **change)
                    app.initialize()
                    self._assert_off(app, entities)

    def test_entity_identity_conflict_cannot_fall_through_to_the_other_active_cycle(self):
        for state in ("Running", "Paused"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                app, entities, _ = make_shadow(tmp, v2_state=state, v2_attrs={"cycle_id": "different-cycle"})
                self._seed(app)
                app.initialize()
                self._assert_off(app, entities)

    def test_completed_store_cannot_resurrect_an_active_entity(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, _ = make_shadow(tmp, v2_state="Running", v2_attrs={"cycle_id": "original-cycle"})
            self._seed(app, state="Unemptied")
            app.initialize()
            self.assertEqual(app.fsm.state.name, "FINISHED")
            self.assertEqual(entities[V2]["state"], "Unemptied")
            self.assertEqual(app._actions.announced, [])
            self.assertEqual(app._actions.feedback, [])

    def test_v2_attributes_do_not_claim_an_unsupported_tracking_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            start = format_utc(datetime.now(timezone.utc) - timedelta(minutes=30))
            app, entities, _ = make_shadow(tmp, v2_state="Running", v2_attrs={
                "cycle_id": "old-cycle", "start_time": start, "cycle_start_time": start,
                "energy_at_start": 10.0,
            })
            app.initialize()
            self._assert_off(app, entities)

    def test_rejected_entity_can_recover_on_a_real_confirmed_power_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, _ = make_shadow(tmp, v2_state="Running", v2_attrs={"cycle_id": "old-cycle"})
            app.initialize()
            self._assert_off(app, entities)
            push_power(app, entities, "600")
            fire_shortest(app)
            self.assertEqual(app.fsm.state.name, "RUNNING")
            self.assertIsNotNone(app._policy.start_time)
            self.assertIsNotNone(app.fsm.cycle_id)
            self.assertNotEqual(app.fsm.cycle_id, "old-cycle")

    def test_valid_entity_restore_keeps_existing_low_power_finish_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, _ = make_shadow(tmp, v2_state="Running", power_w="0")
            start = format_utc(datetime.now(timezone.utc) - timedelta(minutes=130))
            self._seed(app, cycle_start_time=start, state_since=start, last_high_energy_at=start)
            app.initialize()
            self.assertTrue(app.fsm.hypothesis)
            entities[ENERGY]["state"] = "10.5"
            push_power(app, entities, "0")
            fire_shortest(app)
            self.assertEqual(app.fsm.state.name, "FINISHED")
            self.assertEqual(app._actions.announced, [])
            self.assertEqual(len(app._actions.pushed), 1)
            self.assertEqual(len(app._actions.feedback), 1)

    def test_code_change_resets_cohort_without_carrying_old_five_clean_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, _ = make_shadow(tmp)
            Path(app.args["progress_file"]).write_text(json.dumps({
                "clean_cycles": 5, "divergence_count": 0, "divergence_count_at_last_off": 0,
                "code_fingerprint": "previous-engine-fingerprint",
            }))
            app.initialize()
            self.assertEqual(app._clean_cycles, 0)
            self.assertEqual(entities[V2]["attributes"]["clean_cycles"], "0")
            self.assertTrue(any("Cutover evidence starts at 0" in args[0] for args, _ in app.log_calls))


class CandidateBoundaries(unittest.TestCase):
    def test_clock_skew_and_staleness_limits_keep_existing_inclusive_boundaries(self):
        now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        for field, allowed, rejected in (
            ("cycle_start_time", now + timedelta(seconds=60), now + timedelta(seconds=61)),
            ("cycle_start_time", now - timedelta(hours=5), now - timedelta(hours=5, seconds=1)),
            ("saved_at", now + timedelta(seconds=60), now + timedelta(seconds=61)),
            ("saved_at", now - timedelta(hours=12), now - timedelta(hours=12, seconds=1)),
        ):
            with self.subTest(field=field, allowed=allowed):
                args = {
                    "start_time": now - timedelta(hours=1), "saved_at": now, "now": now,
                    "future_skew_s": 60, "max_running_hours": 5, "max_downtime_hours": 12,
                }
                key = "start_time" if field == "cycle_start_time" else field
                self.assertTrue(dp._validate_running_candidate(**{**args, key: allowed}))
                self.assertFalse(dp._validate_running_candidate(**{**args, key: rejected}))

    def test_helper_labels_use_same_gate_and_only_restore_clock_free_states(self):
        for helper, expected in (("Running", None), ("Paused", None), ("Unemptied", "Unemptied"), ("Emptied", "Emptied")):
            with self.subTest(helper=helper):
                snap = dp.resolve_boot_snapshot(
                    entity_state=None, entity_attrs={}, entity_last_changed=None, store_data=None,
                    helper_state=helper, now=datetime.now(timezone.utc), cfg=dp.DEFAULTS,
                )
                self.assertEqual(snap["state"] if snap else None, expected)


if __name__ == "__main__":
    unittest.main()
