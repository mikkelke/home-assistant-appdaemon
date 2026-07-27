# tests/test_intercom.py - unit tests for Intercom's pure/persistence surface
# (resume_decision, the ring-state save/load helpers, and _handle_trigger's
# clean-edge-vs-replay-edge branching) - mirrors test_lock_health.py's
# stub-and-import pattern.
# Run from repo root: python3 -m unittest discover -s apps/intercom/tests -q
# Imports the real module by stubbing the appdaemon package (not installed locally),
# so the code under test is the deployed code, not a duplicate.

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
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
import intercom  # noqa: E402

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


class ResumeDecisionTests(unittest.TestCase):
    def test_succeeded_discards_regardless_of_age(self):
        self.assertEqual(
            intercom.resume_decision({"succeeded": True}, age_s=5, max_age_s=60), "discard_succeeded")
        self.assertEqual(
            intercom.resume_decision({"succeeded": True}, age_s=99999, max_age_s=60), "discard_succeeded")

    def test_missing_succeeded_key_defaults_falsy(self):
        self.assertEqual(intercom.resume_decision({}, age_s=10, max_age_s=60), "alert")

    def test_negative_age_is_stale(self):
        self.assertEqual(intercom.resume_decision({"succeeded": False}, age_s=-1, max_age_s=60), "discard_stale")

    def test_within_window_is_alert(self):
        self.assertEqual(intercom.resume_decision({"succeeded": False}, age_s=0, max_age_s=60), "alert")
        self.assertEqual(intercom.resume_decision({"succeeded": False}, age_s=30, max_age_s=60), "alert")
        self.assertEqual(intercom.resume_decision({"succeeded": False}, age_s=60, max_age_s=60), "alert")  # boundary

    def test_over_max_age_is_stale(self):
        self.assertEqual(intercom.resume_decision({"succeeded": False}, age_s=61, max_age_s=60), "discard_stale")


def _bare_intercom(state_file):
    """A bare, un-initialized Intercom instance (bypasses initialize()/AD wiring
    entirely, via __new__ - hass.Hass is stubbed to `object` above) with just
    _state_file set - enough to exercise the ring-persistence helpers below, which
    only ever touch self._state_file and self.log."""
    app = intercom.Intercom.__new__(intercom.Intercom)
    app._state_file = state_file
    app.log = lambda *a, **kw: None
    return app


class RingStateRoundTripTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.state_file = Path(tmpdir.name) / "intercom_state.json"
        self.app = _bare_intercom(self.state_file)

    def test_missing_file_loads_empty_dict(self):
        self.assertEqual(self.app._load_ring_state(), {})

    def test_save_then_load_round_trip(self):
        records = {"binary_sensor.front": {"ring_ts": NOW.isoformat(), "ring_label": "front door",
                                            "lock_entity": "lock.front", "succeeded": False}}
        self.app._save_ring_state(records)
        self.assertEqual(self.app._load_ring_state(), records)

    def test_save_is_atomic_no_leftover_tmp_file(self):
        self.app._save_ring_state({"a": 1})
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        self.assertTrue(self.state_file.exists())
        self.assertFalse(tmp.exists())

    def test_persist_ring_writes_expected_schema(self):
        self.app._persist_ring("binary_sensor.front", NOW, "front door", "lock.front")
        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk, {
            "binary_sensor.front": {
                "ring_ts": NOW.isoformat(),
                "ring_label": "front door",
                "lock_entity": "lock.front",
                "succeeded": False,
            }
        })

    def test_mark_ring_succeeded_updates_existing_record_only(self):
        self.app._persist_ring("binary_sensor.front", NOW, "front door", "lock.front")
        self.app._mark_ring_succeeded("binary_sensor.front")
        self.assertTrue(self.app._load_ring_state()["binary_sensor.front"]["succeeded"])
        # No record for this entity yet - must not fabricate one.
        self.app._mark_ring_succeeded("binary_sensor.back")
        self.assertNotIn("binary_sensor.back", self.app._load_ring_state())

    def test_forget_ring_deletes_and_is_idempotent(self):
        self.app._persist_ring("binary_sensor.front", NOW, "front door", "lock.front")
        self.app._forget_ring("binary_sensor.front")
        self.assertEqual(self.app._load_ring_state(), {})
        self.app._forget_ring("binary_sensor.front")  # second call: no-op, must not raise
        self.assertEqual(self.app._load_ring_state(), {})

    def test_parse_ts_round_trip_and_unparseable(self):
        self.assertEqual(intercom.Intercom._parse_ts(NOW.isoformat()), NOW)
        self.assertIsNone(intercom.Intercom._parse_ts(None))
        self.assertIsNone(intercom.Intercom._parse_ts(""))
        self.assertIsNone(intercom.Intercom._parse_ts("not-a-timestamp"))


def _fake_handle_trigger_app(state_file, states):
    """A bare Intercom instance with the minimal duck-typed AD surface
    _handle_trigger touches, so its clean_edge/replay-edge branching can be
    exercised without a real AppDaemon runtime. run_in is captured, not executed -
    this does not exercise _perform_unlock/_verify_unlock or the real timer
    lifecycle, only what _handle_trigger itself decides to schedule/persist."""
    app = intercom.Intercom.__new__(intercom.Intercom)
    app._state_file = state_file
    app.trigger_map = {
        "binary_sensor.front": {
            "message": "Someone is at the front door",
            "lock": "lock.front",
            "followup": "I opened the front door",
            "door_sensor": None,
            "ring_label": "front door",
        }
    }
    app.last_trigger_at = {}
    app.pending_unlocks = {}
    app.unlock_outcomes = {}
    app.auto_open_entity = "input_boolean.auto_open"
    app.debounce_s = 5
    app.unlock_delay_s = 1
    app.unlock_repeat_count = 2
    app.unlock_repeat_interval_s = 7
    app.sonos_notifier = None  # skip TTS/submit_to_executor entirely - not under test here
    app.run_in_calls = []

    app.get_now = lambda: NOW
    app.log = lambda *a, **kw: None
    app.get_state = lambda entity, attribute=None: states.get(entity)
    app.fire_event = lambda *a, **kw: None
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None

    def fake_run_in(callback, delay, **kwargs):
        app.run_in_calls.append((delay, kwargs))
        return f"handle-{len(app.run_in_calls)}"
    app.run_in = fake_run_in
    return app


class HandleTriggerEdgeTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.state_file = Path(tmpdir.name) / "intercom_state.json"
        self.states = {"input_boolean.auto_open": "on", "lock.front": "locked"}
        self.app = _fake_handle_trigger_app(self.state_file, self.states)

    def test_clean_edge_schedules_unlocks_and_persists(self):
        self.app._handle_trigger("binary_sensor.front", "state", "off", "on", {})
        self.assertEqual(len(self.app.run_in_calls), self.app.unlock_repeat_count)
        on_disk = json.loads(self.state_file.read_text())
        self.assertIn("binary_sensor.front", on_disk)
        self.assertFalse(on_disk["binary_sensor.front"]["succeeded"])

    def test_replay_edge_never_schedules_unlocks_or_persists(self):
        self.app._handle_trigger("binary_sensor.front", "state", "unavailable", "on", {})
        self.assertEqual(self.app.run_in_calls, [])
        self.assertFalse(self.state_file.exists())

    def test_unknown_to_on_is_also_a_replay_edge(self):
        self.app._handle_trigger("binary_sensor.front", "state", "unknown", "on", {})
        self.assertEqual(self.app.run_in_calls, [])
        self.assertFalse(self.state_file.exists())

    def test_non_on_new_state_is_ignored_entirely(self):
        self.app._handle_trigger("binary_sensor.front", "state", "on", "off", {})
        self.assertEqual(self.app.run_in_calls, [])
        self.assertFalse(self.state_file.exists())


if __name__ == "__main__":
    unittest.main()
