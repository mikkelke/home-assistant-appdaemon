"""Real host initialization and replay regressions for the inert presentation boundary."""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_dryer_shadow import ENERGY, LIVE, POWER, V2, ds, fire_shortest, make_shadow, push_door, push_power
from replay import FakeClock, Replay, build_engine_adapter, load_tape
from dryer_monitor import DryerMonitor
from dryer_shadow_contract import compare_attributes, compare_effect_intents


class HostContract(unittest.TestCase):
    def boot(self, tmp, **kwargs):
        clock = FakeClock(datetime.now(timezone.utc))
        app, entities, calls = make_shadow(tmp, **kwargs)
        with patch.object(ds, "SystemClock", return_value=clock):
            app.initialize()
        return app, entities, clock

    def running(self, tmp, **kwargs):
        app, entities, clock = self.boot(tmp, **kwargs)
        push_power(app, entities, "620")
        fire_shortest(app)
        self.assertEqual(app.fsm.published, "Running")
        return app, entities, clock

    def finish(self, app, entities, clock):
        clock.advance(130 * 60)
        entities[ENERGY]["state"] = "10.5"
        push_power(app, entities, "620")  # live activity anchor, so no stale-finish fallback
        push_power(app, entities, "2")
        fire_shortest(app)
        self.assertEqual(app.fsm.published, "Unemptied")

    def test_initialize_loads_same_human_confirmed_learning_as_legacy_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            feedback = Path(tmp) / "feedback.json"
            payload = {"cycles": [
                {"confirmed": "unknown", "duration_min": 200, "programme_confirmed_by_human": True},
                {"confirmed": "unknown", "duration_min": 1},
                {"confirmed": "bomuld_eco__skabstoert", "duration_min": 180, "programme_confirmed_by_human": True},
                {"confirmed": "bomuld_eco__skabstoert", "duration_min": 200, "programme_user_confirmed": True},
                {"confirmed": "bomuld_eco__skabstoert", "duration_min": 40},
            ]}
            feedback.write_text(json.dumps(payload))
            original = feedback.read_bytes()
            app, entities, clock = self.running(tmp, extra_args={
                "feedback_file": str(feedback), "programme_entity": "input_select.programme",
            })
            legacy = DryerMonitor.__new__(DryerMonitor)
            legacy.feedback_file, legacy.log = str(feedback), lambda *a, **kw: None
            legacy._load_and_apply_feedback()
            self.assertEqual(app._policy.learned, legacy._learned_durations)
            entities["input_select.programme"] = {"state": "Bomuld eco"}
            app._presentation_tick({})
            self.assertEqual(entities[V2]["attributes"]["programme_duration_min"], 155)
            self.assertEqual(feedback.read_bytes(), original)
            self.assertTrue(all(entity == V2 for entity, _ in app.set_state_calls))

    def test_effective_inputs_reset_cohort_but_json_reformat_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feedback.json"
            data = {"cycles": [{"confirmed": "eco", "duration_min": 100, "programme_confirmed_by_human": True}]}
            path.write_text(json.dumps(data))
            args = {"feedback_file": str(path)}
            first, _, _ = self.boot(tmp, extra_args=args)
            first._clean_cycles = 4
            first._save_progress()
            path.write_text(json.dumps(data, indent=4, sort_keys=True))
            same, _, _ = self.boot(tmp, extra_args=args)
            self.assertEqual(same._clean_cycles, 4)
            data["cycles"][0]["duration_min"] = 150
            path.write_text(json.dumps(data))
            changed, entities, _ = self.boot(tmp, extra_args=args)
            self.assertEqual(changed._clean_cycles, 0)
            self.assertEqual(changed._code_fingerprint, first._code_fingerprint)
            self.assertNotEqual(changed._input_fingerprint, first._input_fingerprint)
            self.assertEqual(entities[V2]["attributes"]["input_fingerprint"], changed._input_fingerprint)
            changed._clean_cycles = 2
            changed._save_progress()
            configured, _, _ = self.boot(tmp, extra_args={**args, "stop_w": 4})
            self.assertEqual(configured._clean_cycles, 0)

    def test_malformed_feedback_is_skipped_without_aborting_real_initialize(self):
        for data in ([], {"cycles": {}}, {"cycles": [None, "bad", {
            "confirmed": "eco", "duration_min": "100", "programme_confirmed_by_human": True,
        }, {"confirmed": "eco", "duration_min": float("nan"), "programme_user_confirmed": True}]}):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "feedback.json"
                path.write_text(json.dumps(data))
                original = path.read_bytes()
                app, _, _ = self.boot(tmp, extra_args={"feedback_file": str(path)})
                self.assertEqual(app._policy.learned, {})
                self.assertEqual(path.read_bytes(), original)

    def test_next_cycle_refreshes_learning_and_starts_new_input_cohort(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feedback.json"
            data = {"cycles": [{"confirmed": "bomuld_eco__skabstoert", "duration_min": 80,
                                "programme_confirmed_by_human": True}]}
            path.write_text(json.dumps(data))
            app, entities, clock = self.running(tmp, extra_args={
                "feedback_file": str(path), "programme_entity": "input_select.programme",
            })
            entities["input_select.programme"] = {"state": "Bomuld eco"}
            app._presentation_tick({})
            self.assertEqual(entities[V2]["attributes"]["programme_duration_min"], 108)
            original = app._input_fingerprint
            self.finish(app, entities, clock)
            # Legacy learning changes when its completed feedback is written.
            data["cycles"].append({**data["cycles"][0], "duration_min": 160})
            path.write_text(json.dumps(data))
            app._clean_cycles = 4
            app._divergence_count_at_last_off = app._divergence_count
            push_door(app, entities, "on")
            push_door(app, entities, "off")
            self.assertEqual(app._clean_cycles, 5)
            push_power(app, entities, "620")
            fire_shortest(app)
            self.assertEqual(app.fsm.published, "Running")
            self.assertEqual(app._clean_cycles, 0)
            self.assertNotEqual(app._input_fingerprint, original)
            self.assertEqual(entities[V2]["attributes"]["programme_duration_min"], 120)
            self.assertEqual(entities[V2]["attributes"]["learning_input_status"], "observed")
            # Identical effective learning cannot reset the new cohort again.
            app._clean_cycles = 2
            app._refresh_learning()
            self.assertEqual(app._clean_cycles, 2)

    def test_failed_boundary_refresh_retains_previous_learning_and_reports_unobserved(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "feedback.json"
                path.write_text(json.dumps({"cycles": [{"confirmed": "eco", "duration_min": 100,
                                                      "programme_confirmed_by_human": True}]}))
                app, entities, clock = self.running(tmp, extra_args={"feedback_file": str(path)})
                original = app._input_fingerprint
                learned = copy.deepcopy(app._policy.learned)
                self.finish(app, entities, clock)
                if unavailable:
                    path.unlink()
                else:
                    path.write_text('{"cycles": [')
                push_door(app, entities, "on")
                push_door(app, entities, "off")
                push_power(app, entities, "620")
                fire_shortest(app)
                self.assertEqual(app.fsm.published, "Running")
                self.assertEqual(app._policy.learned, learned)
                self.assertEqual(app._input_fingerprint, original)
                self.assertEqual(entities[V2]["attributes"]["learning_input_status"], "unobserved")

    def test_progress_matches_legacy_and_refresh_has_no_lifecycle_or_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, clock = self.running(tmp)
            clock.advance(30 * 60)
            entities[ENERGY]["state"] = "10.3"
            since, cycle_id = app.fsm.state_since, app.fsm.cycle_id
            store_path = Path(app.args["state_file"])
            effects, store = list(app._actions.calls), store_path.read_bytes()
            app._presentation_tick({})
            self.assertEqual((app.fsm.state_since, app.fsm.cycle_id), (since, cycle_id))
            self.assertEqual(list(app._actions.calls), effects)
            self.assertEqual(store_path.read_bytes(), store)

            legacy = DryerMonitor.__new__(DryerMonitor)
            legacy.start_time, legacy.energy_start = app._policy.start_time, 10.0
            legacy._now_utc, legacy.get_state = clock.now, app.get_state
            legacy.state_entity = V2
            legacy._get_confirmed_programme_key = lambda: None
            legacy.power_sensor, legacy.energy_sensor = POWER, ENERGY
            legacy.keep_fresh_detected, legacy._learned_durations = False, app._policy.learned
            legacy.main_cycle_power, legacy.overrun_floor_min = 400, 10
            legacy._cycle_actor = None
            captured = []
            legacy._set_state_entity = lambda **kw: captured.append(kw)
            with patch.object(DryerMonitor, "PROGRAMME_PROFILES", app._policy.profiles):
                legacy._update_running_attributes()
            comparison = compare_attributes("Running", entities[V2]["attributes"], "Running", captured[-1]["attributes"])
            self.assertEqual(comparison["status"], "match", comparison)

    def test_overrun_clears_after_duration_grows_and_pause_keeps_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, clock = self.running(tmp)
            clock.advance(121 * 60)
            app._presentation_tick({})
            attrs = entities[V2]["attributes"]
            self.assertEqual((attrs["progress_pct"], attrs["estimated_remaining_min"]), (99, 10))
            self.assertIn("overrun_min", attrs)
            app._policy.profiles = {**app._policy.profiles, "unknown": {
                **app._policy.profiles["unknown"], "duration_min": 180,
            }}
            app._presentation_tick({})
            self.assertNotIn("overrun_min", entities[V2]["attributes"])
            push_door(app, entities, "on")
            self.assertEqual(entities[V2]["state"], "Paused")
            self.assertIn("estimated_end_time", entities[V2]["attributes"])

    def test_completion_freezes_across_emptying_and_restart_without_progress_leaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, clock = self.running(tmp)
            self.finish(app, entities, clock)
            finished = dict(app._policy.completed_attributes)
            self.assertNotIn("estimated_end_time", entities[V2]["attributes"])
            self.assertEqual(finished["run_time_minutes"], app._actions.feedback[-1]["duration_min"])
            clock.advance(8 * 3600)
            entities[ENERGY]["state"] = "12.0"
            app._on_emptied_button("state_changed", {
                "old_state": {"state": "old"},
                "new_state": {"state": "new", "context": {"user_id": "actual-user"}},
            }, {})
            self.assertEqual(app._policy.completed_attributes, finished)
            self.assertEqual(entities[V2]["attributes"]["emptying_context"]["user_id"], "actual-user")
            restored, after, _ = self.boot(tmp, energy_kwh="99.0")
            self.assertEqual(after[V2]["state"], "Emptied")
            self.assertEqual(restored._policy.completed_attributes, finished)
            self.assertEqual(after[V2]["attributes"]["emptying_context"]["user_id"], "actual-user")
            self.assertEqual(restored._actions.feedback, [])
            push_door(restored, after, "off")
            push_power(restored, after, "620")
            fire_shortest(restored)
            self.assertEqual(after[V2]["state"], "Running")
            self.assertNotIn("cycle_complete", after[V2]["attributes"])
            self.assertNotIn("emptying_context", after[V2]["attributes"])

    def test_malformed_optional_store_fields_are_missing_not_remeasured(self):
        for completed, context in (
            ([], []), ({"run_time_minutes": "130"}, {"source": []}),
            ({"energy_used": float("nan")}, {"source": "button", "user_id": {}, "observed_at": "bad"}),
            ({"cycle_complete": "true"}, {"source": "button", "resolution": "resolved"}),
            ({"run_time_minutes": 10**400}, {"source": "button", "user_id": None,
                "observed_at": "0001-01-01T00:00:00+02:00", "resolution": "unresolved"}),
        ):
            with self.subTest(completed=completed), tempfile.TemporaryDirectory() as tmp:
                app, entities, clock = self.running(tmp)
                self.finish(app, entities, clock)
                saved = app._store.load()
                saved.update(completed_attributes=completed, emptying_context=context)
                app._store.save(saved)
                restored, after, _ = self.boot(tmp)
                self.assertEqual(after[V2]["state"], "Unemptied")
                self.assertEqual(restored._policy.completed_attributes, {})
                self.assertEqual(restored._policy.emptying_context, {})
                self.assertNotIn("run_time_minutes", after[V2]["attributes"])

    def test_entity_only_completed_snapshot_uses_shared_measurement_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, _ = self.boot(tmp, v2_state="Unemptied", v2_attrs={
                "run_time_minutes": 100.1, "energy_used": .978, "end_reason": "power",
            })
            app._on_force_emptied("dryer_force_emptied", {}, {})
            self.assertEqual(entities[V2]["attributes"]["run_time_minutes"], 100.1)
            self.assertEqual(entities[V2]["attributes"]["energy_used"], .978)
            self.assertNotIn("estimated_end_time", entities[V2]["attributes"])

    def test_restored_snapshot_cannot_inject_progress_or_resolved_actor_attributes(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, clock = self.running(tmp)
            self.finish(app, entities, clock)
            saved = app._store.load()
            saved["completed_attributes"].update(progress_pct=100, started_by="invented")
            saved["emptying_context"] = {"source": "button", "user_id": "known-id",
                "observed_at": clock.now().isoformat(), "resolution": "unresolved", "person": "invented"}
            app._store.save(saved)
            restored, after, _ = self.boot(tmp)
            self.assertNotIn("progress_pct", after[V2]["attributes"])
            self.assertEqual(after[V2]["attributes"]["started_by"], "")
            self.assertNotIn("person", restored._policy.emptying_context)

    def test_immediate_comparison_exposes_missing_different_and_unobserved_without_90s_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, _ = self.running(tmp)
            attrs = copy.deepcopy(entities[V2]["attributes"])
            del attrs["programme_label"]
            attrs["progress_pct"] = 45
            app._on_live_changed(LIVE, "all", None, {"state": "Running", "attributes": attrs}, {})
            reported = entities[V2]["attributes"]
            differences = reported["attribute_comparison"]["differences"]
            self.assertEqual(differences["programme_label"]["kind"], "missing_live")
            self.assertEqual(differences["progress_pct"]["kind"], "different")
            self.assertEqual(reported["effect_comparison"], "unobserved")
            self.assertEqual(reported["actor_resolution"], "unimplemented")
            self.assertEqual(app._divergence_count, 0)
            app._on_live_changed(LIVE, "all", None, {"state": "Paused", "attributes": attrs}, {})
            self.assertEqual(entities[V2]["attributes"]["attribute_comparison"]["status"], "state_mismatch")
            app._on_live_changed(LIVE, "all", None, {"state": "unavailable"}, {})
            self.assertEqual(entities[V2]["attributes"]["attribute_comparison"]["status"], "unobserved")

    def test_intent_recording_is_bounded_and_does_not_dispatch_boolean_selectors(self):
        sink = ds.ShadowActions(lambda *a, **kw: None)
        for _ in range(100):
            sink.select_option("input_boolean.dryer_announce", "on")
        self.assertEqual(len(sink.calls), 64)
        mismatch = compare_effect_intents([sink.calls[-1]], [{"kind": "turn_on", "entity": "input_boolean.dryer_announce"}])
        self.assertEqual(mismatch["status"], "different")
        self.assertEqual(compare_effect_intents(list(sink.calls), None)["status"], "unobserved")

    def test_live_deletion_is_authoritative_even_if_cache_still_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, _ = self.running(tmp)
            entities[LIVE] = copy.deepcopy(entities[V2])
            app._on_live_changed(LIVE, "all", None, entities[LIVE], {})
            self.assertEqual(entities[V2]["attributes"]["attribute_comparison"]["status"], "match")
            app._on_live_changed(LIVE, "all", entities[LIVE], None, {})
            self.assertEqual(entities[V2]["attributes"]["attribute_comparison"]["status"], "unobserved")


class ReplayContract(unittest.TestCase):
    def test_real_replay_result_contains_completed_attributes_and_actual_effects(self):
        tape = load_tape(Path(__file__).with_name("tapes") / "t3_feedback_once.json")
        last = tape["events"][-1]["t"]
        tape["events"].append({"t": last + 1, "kind": "force_emptied", "user_id": "replay-user"})
        replay = Replay(tape)
        engine = build_engine_adapter(tape, replay.clock, replay.scheduler)
        try:
            result = replay.run(engine)
            self.assertEqual(result.actions, engine.actions.calls)
            self.assertTrue(result.actions, "must not return the replay driver's disconnected empty sink")
            done = next(s for s in result.snapshots if s["published"] == "Unemptied")
            self.assertIn("run_time_minutes", done["attributes"])
            emptied = result.snapshots[-1]
            self.assertEqual(emptied["attributes"]["run_time_minutes"], done["attributes"]["run_time_minutes"])
            self.assertEqual(emptied["attributes"]["emptying_context"]["user_id"], "replay-user")
            omitted = compare_effect_intents(result.actions, result.actions[:-1])
            self.assertEqual(omitted["differences"][-1]["kind"], "unexpected_shadow")
            absent = compare_attributes("Running", {}, "Running", {})
            self.assertEqual(absent["differences"]["progress_pct"]["kind"], "missing_both")
        finally:
            engine._tmpdir.cleanup()
