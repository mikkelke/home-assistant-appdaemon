# tests/test_appliance_cycle_store.py - Unit tests for apps/appliances/cycle_store.py.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# CycleStore is the durable on-disk shadow of a monitor's cycle state - see the module
# docstring in cycle_store.py for why it exists: HA erases AppDaemon set_state entities,
# and the cycle clock living in their attributes, on every HA restart. cycle_store.py has
# no AppDaemon dependency, so these tests instantiate CycleStore directly - no
# WasherMonitor.__new__-style harness needed. The appdaemon.plugins.hass.hassapi stub
# below is copied from test_dryer_unavailable_grace.py's harness purely for import-path
# consistency with the rest of this suite; cycle_store.py itself never touches it.

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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

import cycle_store as cs  # noqa: E402


def make_log():
    """A minimal AppDaemon-shaped log recorder: log(message, level="INFO")."""
    calls = []

    def log(message, level="INFO"):
        calls.append((message, level))

    return log, calls


def _warning_count(calls):
    return sum(1 for _message, level in calls if level == "WARNING")


def _blocked_path(tmpdir):
    """A path that can never be written to, regardless of which user runs the test
    (unlike a chmod'd read-only directory, which root silently bypasses): a plain file
    sits where a directory is expected, so opening anything "under" it always raises
    NotADirectoryError."""
    blocker = Path(tmpdir) / "not_a_directory"
    blocker.write_text("blocking file")
    return blocker / "washer_state.json"


class SaveLoadRoundTrip(unittest.TestCase):
    def test_round_trip_returns_payload_with_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "washer_state.json"
            log, calls = make_log()
            store = cs.CycleStore(path, "washer", log=log)
            payload = {"cycle_start_time": "2026-08-12T10:00:00Z", "cycle_number": 2}

            self.assertTrue(store.save(payload))
            loaded = store.load()

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["cycle_start_time"], "2026-08-12T10:00:00Z")
            self.assertEqual(loaded["cycle_number"], 2)
            self.assertEqual(loaded["schema"], cs.DEFAULT_SCHEMA)
            self.assertEqual(loaded["appliance"], "washer")
            self.assertIsNotNone(cs.parse_utc(loaded.get("saved_at")))
            self.assertEqual(calls, [])  # a clean round trip logs nothing


class LoadValidation(unittest.TestCase):
    """load() must never raise, and must reject anything it can't trust: bad JSON, a
    non-dict top level, or an envelope that doesn't match this store's identity."""

    def _store(self, tmp, appliance="washer"):
        log, calls = make_log()
        store = cs.CycleStore(Path(tmp) / "washer_state.json", appliance, log=log)
        return store, calls

    def test_missing_file_returns_none_and_logs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, calls = self._store(tmp)

            self.assertIsNone(store.load())
            self.assertEqual(calls, [])  # missing file is the normal steady state

    def test_corrupt_json_returns_none_and_logs_one_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "washer_state.json").write_text("{not valid json")
            store, calls = self._store(tmp)

            self.assertIsNone(store.load())
            self.assertEqual(_warning_count(calls), 1)

    def test_truncated_json_returns_none_and_logs_one_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "washer_state.json"
            log, calls = make_log()
            store = cs.CycleStore(path, "washer", log=log)
            store.save({"cycle_start_time": "2026-08-12T10:00:00Z", "note": "x" * 200})
            raw = path.read_text()
            path.write_text(raw[: len(raw) // 2])  # chop the file in half

            self.assertIsNone(store.load())
            self.assertEqual(_warning_count(calls), 1)

    def test_schema_mismatch_returns_none_and_logs_one_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "washer_state.json"
            path.write_text(json.dumps({
                "schema": cs.DEFAULT_SCHEMA + 1,
                "appliance": "washer",
                "saved_at": "2026-08-12T10:00:00Z",
            }))
            store, calls = self._store(tmp)

            self.assertIsNone(store.load())
            self.assertEqual(_warning_count(calls), 1)

    def test_appliance_mismatch_returns_none_and_logs_one_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "washer_state.json"
            path.write_text(json.dumps({
                "schema": cs.DEFAULT_SCHEMA,
                "appliance": "dryer",
                "saved_at": "2026-08-12T10:00:00Z",
            }))
            store, calls = self._store(tmp)

            self.assertIsNone(store.load())
            self.assertEqual(_warning_count(calls), 1)

    def test_non_dict_top_level_returns_none_and_logs_one_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "washer_state.json").write_text(json.dumps([1, 2, 3]))
            store, calls = self._store(tmp)

            self.assertIsNone(store.load())
            self.assertEqual(_warning_count(calls), 1)


class AtomicWrite(unittest.TestCase):
    def test_no_tmp_file_left_after_successful_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "washer_state.json"
            store = cs.CycleStore(path, "washer")

            self.assertTrue(store.save({"cycle_start_time": "2026-08-12T10:00:00Z"}))

            self.assertTrue(path.exists())
            self.assertFalse((Path(tmp) / "washer_state.json.tmp").exists())

    def test_unwritable_directory_returns_false_and_logs_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _blocked_path(tmp)
            log, calls = make_log()
            store = cs.CycleStore(path, "washer", log=log)

            ok = store.save({"cycle_start_time": "2026-08-12T10:00:00Z"})

            self.assertFalse(ok)
            self.assertEqual(_warning_count(calls), 1)

    def test_interrupted_replace_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "washer_state.json"
            log, calls = make_log()
            store = cs.CycleStore(path, "washer", log=log)
            self.assertTrue(store.save({"cycle_start_time": "2026-08-12T10:00:00Z"}))
            original = store.load()
            self.assertIsNotNone(original)

            with patch("cycle_store.os.replace", side_effect=OSError("simulated replace failure")):
                ok = store.save({"cycle_start_time": "2026-08-12T23:59:00Z"})

            self.assertFalse(ok)
            self.assertEqual(_warning_count(calls), 1)
            self.assertEqual(store.load(), original)  # previous good file untouched
            self.assertFalse((Path(tmp) / "washer_state.json.tmp").exists())  # cleaned up


class ClearBehavior(unittest.TestCase):
    def test_clear_removes_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "washer_state.json"
            store = cs.CycleStore(path, "washer")
            store.save({"cycle_start_time": "2026-08-12T10:00:00Z"})
            self.assertTrue(path.exists())

            self.assertTrue(store.clear())
            self.assertFalse(path.exists())

    def test_clear_on_absent_file_is_also_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "washer_state.json"
            store = cs.CycleStore(path, "washer")
            self.assertFalse(path.exists())

            self.assertTrue(store.clear())


class EnvelopeIntegrity(unittest.TestCase):
    def test_payload_cannot_override_schema_or_appliance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "washer_state.json"
            store = cs.CycleStore(path, "washer")

            store.save({"schema": 999, "appliance": "evil-dryer", "cycle_number": 1})
            loaded = store.load()

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["schema"], cs.DEFAULT_SCHEMA)
            self.assertEqual(loaded["appliance"], "washer")
            self.assertEqual(loaded["cycle_number"], 1)  # non-envelope keys pass through


class TimestampHelpers(unittest.TestCase):
    def test_format_then_parse_preserves_the_instant(self):
        dt = datetime(2026, 8, 12, 10, 15, 30, tzinfo=timezone.utc)

        formatted = cs.format_utc(dt)

        self.assertTrue(formatted.endswith("Z"))
        self.assertEqual(cs.parse_utc(formatted), dt)

    def test_parse_utc_accepts_z_suffix(self):
        parsed = cs.parse_utc("2026-08-12T10:15:30Z")

        self.assertIsNotNone(parsed)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed, datetime(2026, 8, 12, 10, 15, 30, tzinfo=timezone.utc))

    def test_parse_utc_accepts_explicit_offset(self):
        parsed = cs.parse_utc("2026-08-12T12:15:30+02:00")

        self.assertIsNotNone(parsed)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed, datetime(2026, 8, 12, 10, 15, 30, tzinfo=timezone.utc))

    def test_parse_utc_returns_none_on_garbage(self):
        self.assertIsNone(cs.parse_utc("not-a-timestamp"))
        self.assertIsNone(cs.parse_utc(""))
        self.assertIsNone(cs.parse_utc(None))
        self.assertIsNone(cs.parse_utc(12345))

    def test_parse_utc_result_is_tz_aware_and_comparable(self):
        parsed = cs.parse_utc("2026-08-12T10:15:30Z")

        self.assertIsNotNone(parsed.tzinfo)
        # A tz-aware datetime must subtract cleanly against datetime.now(timezone.utc)
        # without raising - the documented past-incident failure mode (naive vs. aware
        # TypeError) this guarantee exists to prevent.
        delta = datetime.now(timezone.utc) - parsed
        self.assertIsInstance(delta, timedelta)


if __name__ == "__main__":
    unittest.main()
