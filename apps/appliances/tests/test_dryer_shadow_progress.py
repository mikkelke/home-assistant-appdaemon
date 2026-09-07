# tests/test_dryer_shadow_progress.py - the cutover evidence (clean_cycles) must survive an app
# reload, and must NOT survive an engine code change. Before 2026-09-07 the counter was in-memory
# only: 37 DryerShadow re-inits in the retained logs against ~0.8 dryer cycles a day meant it sat
# at 0 forever and the promotion gate was unreachable, even though the engine had never once
# diverged from the live monitor. Reuses test_dryer_shadow.py's harness (real initialize(), only
# AppDaemon primitives faked) so the reload path exercised here is the real one. Run from repo
# root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_dryer_shadow import V2, ds, make_shadow

import dryer_policy as policy_mod


def _progress_args(tmp):
    return {"progress_file": str(Path(tmp) / "dryer_shadow_progress_state.json")}


class ProgressResolution(unittest.TestCase):
    """resolve_shadow_progress in isolation - the pure precedence/invalidation rules."""

    def test_file_beats_entity(self):
        got = policy_mod.resolve_shadow_progress(
            file_data={"clean_cycles": 7, "code_fingerprint": "abc"},
            entity_attrs={"clean_cycles": "2", "code_fingerprint": "abc"},
            code_fingerprint="abc",
        )
        self.assertEqual(got["clean_cycles"], 7)
        self.assertEqual(got["source"], "file")
        self.assertIsNone(got["reset_reason"])

    def test_entity_is_the_fallback_when_no_file_yet(self):
        got = policy_mod.resolve_shadow_progress(
            file_data=None,
            entity_attrs={"clean_cycles": "4", "divergence_count": "1", "code_fingerprint": "abc"},
            code_fingerprint="abc",
        )
        self.assertEqual((got["clean_cycles"], got["divergence_count"]), (4, 1))
        self.assertEqual(got["source"], "entity")

    def test_code_change_invalidates_the_evidence(self):
        got = policy_mod.resolve_shadow_progress(
            file_data={"clean_cycles": 9, "code_fingerprint": "old"},
            entity_attrs={},
            code_fingerprint="new",
        )
        self.assertEqual(got["clean_cycles"], 0)
        self.assertIn("code changed", got["reset_reason"])

    def test_unfingerprinted_progress_is_refused(self):
        got = policy_mod.resolve_shadow_progress(
            file_data={"clean_cycles": 5}, entity_attrs={}, code_fingerprint="abc"
        )
        self.assertEqual(got["clean_cycles"], 0)
        self.assertIn("no fingerprint", got["reset_reason"])

    def test_garbage_counter_does_not_raise(self):
        got = policy_mod.resolve_shadow_progress(
            file_data={"clean_cycles": "not-a-number", "code_fingerprint": "abc"},
            entity_attrs=None,
            code_fingerprint="abc",
        )
        self.assertEqual(got["clean_cycles"], 0)
        self.assertEqual(got["reset_reason"], "no stored progress")

    def test_nothing_stored_starts_clean(self):
        got = policy_mod.resolve_shadow_progress(
            file_data=None, entity_attrs=None, code_fingerprint="abc"
        )
        self.assertEqual(got["clean_cycles"], 0)
        self.assertIsNone(got["source"])


class ProgressSurvivesReload(unittest.TestCase):
    """The regression itself: a second initialize() must not zero the counter."""

    def test_counter_survives_reload_with_the_entity_wiped(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _progress_args(tmp)
            first, _entities, _calls = make_shadow(tmp, extra_args=args)
            first.initialize()
            first._clean_cycles = 3
            first._divergence_count = 1
            first._divergence_count_at_last_off = 1
            first._save_progress()

            # An HA restart wipes AppDaemon-set entities, so the reloaded app sees V2 as absent -
            # the file is the only surviving source.
            second, entities, _ = make_shadow(tmp, extra_args=args)
            self.assertIsNone(entities[V2]["state"])
            second.initialize()

            self.assertEqual(second._clean_cycles, 3)
            self.assertEqual(second._divergence_count, 1)
            self.assertEqual(second._divergence_count_at_last_off, 1)

    def test_engine_code_change_resets_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _progress_args(tmp)
            Path(args["progress_file"]).write_text(
                json.dumps({"clean_cycles": 9, "code_fingerprint": "a-different-build"}),
                encoding="utf-8",
            )
            app, _entities, _ = make_shadow(tmp, extra_args=args)
            app.initialize()
            self.assertEqual(app._clean_cycles, 0)

    def test_unreadable_file_is_survivable(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _progress_args(tmp)
            Path(args["progress_file"]).write_text("{not json", encoding="utf-8")
            app, _entities, _ = make_shadow(tmp, extra_args=args)
            app.initialize()  # must not raise
            self.assertEqual(app._clean_cycles, 0)

    def test_publish_persists_without_an_explicit_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _progress_args(tmp)
            app, _entities, _ = make_shadow(tmp, extra_args=args)
            app.initialize()
            app._clean_cycles = 2
            app._publish(
                "Off", internal=ds.State.OFF, store_only=False,
                attrs={"internal_state": "OFF", "cycle_id": ""},
            )
            saved = json.loads(Path(args["progress_file"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["clean_cycles"], 2)
            self.assertEqual(saved["code_fingerprint"], app._code_fingerprint)


if __name__ == "__main__":
    unittest.main()
