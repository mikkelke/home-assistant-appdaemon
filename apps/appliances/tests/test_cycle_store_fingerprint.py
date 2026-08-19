# tests/test_cycle_store_fingerprint.py - the shared code_fingerprint stamp (FIX 1) is additive.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# FIX 1 stamps an md5 of the monitor's own .py file into every CycleStore payload as
# code_fingerprint. Washer CONSUMES it on restore (a mismatch forces the uncorroborated path);
# dryer and dishwasher MUST only stamp it - their restore behaviour stays byte-for-byte unchanged.
# This is the light "additive, and inert for the other two" check: the envelope still carries its
# four original keys plus the new one, and neither dryer_monitor.py nor dishwasher_monitor.py so
# much as mentions code_fingerprint (they inherit the stamp from the shared mixin, never read it).

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import cycle_persistence as cp  # noqa: E402


class FakeMonitor(cp.CyclePersistenceMixin):
    """Minimal mixin host: just enough for _init_cycle_store + _build_cycle_store_payload."""

    def __init__(self, state_file):
        self.args = {"state_file": state_file}
        self.state = "Running"
        self.state_since = None
        self.cycle_id = "cycle-1"
        self.log = lambda *a, **kw: None
        self._init_cycle_store(appliance="washer", default_path=state_file)

    def _cycle_store_field_map(self):
        return {"foo": lambda: "bar"}


class EnvelopeIsAdditive(unittest.TestCase):
    def test_payload_keeps_original_keys_and_adds_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            mon = FakeMonitor(os.path.join(d, "x_state.json"))
            payload = mon._build_cycle_store_payload("Running")
        for key in ("state", "state_since", "cycle_id", "entity_recreated_at", "foo"):
            self.assertIn(key, payload)  # nothing dropped
        self.assertIn("code_fingerprint", payload)  # and the new field is present
        self.assertEqual(payload["state"], "Running")
        self.assertEqual(payload["foo"], "bar")

    def test_fingerprint_is_md5_of_the_hosting_module_file(self):
        with tempfile.TemporaryDirectory() as d:
            mon = FakeMonitor(os.path.join(d, "x_state.json"))
        # FakeMonitor lives in this test module, so the fingerprint is this file's md5.
        expected = hashlib.md5(Path(__file__).read_bytes()).hexdigest()
        self.assertEqual(mon._code_fingerprint, expected)
        self.assertEqual(mon._build_cycle_store_payload("Running")["code_fingerprint"], expected)

    def test_compute_fingerprint_never_raises(self):
        # A host whose module cannot be resolved to a file just gets None, never an exception.
        no_module = type("NoModule", (cp.CyclePersistenceMixin,), {"__module__": "does.not.exist"})()
        self.assertIsNone(no_module._compute_code_fingerprint())


class OtherMonitorsAreInert(unittest.TestCase):
    def test_dryer_and_dishwasher_never_read_the_fingerprint(self):
        """Inertness: only washer_monitor.py references code_fingerprint (it consumes it on
        restore); dryer/dishwasher inherit the stamp from the mixin and never mention it, so
        their restore behaviour is unchanged by this fix."""
        dryer = (APP_DIR / "dryer_monitor.py").read_text()
        dishwasher = (APP_DIR / "dishwasher_monitor.py").read_text()
        washer = (APP_DIR / "washer_monitor.py").read_text()
        self.assertNotIn("code_fingerprint", dryer)
        self.assertNotIn("code_fingerprint", dishwasher)
        self.assertIn("code_fingerprint", washer)


if __name__ == "__main__":
    unittest.main()
