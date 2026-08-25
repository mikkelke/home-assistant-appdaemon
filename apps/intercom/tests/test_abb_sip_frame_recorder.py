# tests/test_abb_sip_frame_recorder.py - unit tests for abb_sip_frame_recorder.py
# (the abb_welcome_sip_frame counter/last-inbound-MESSAGE recorder). Run from repo
# root: python3 -m unittest discover -s apps/intercom/tests
#
# Same stub-appdaemon-then-import-standalone trick as
# apps/notify/tests/test_appdaemon_release_watch.py: appdaemon isn't installed in
# the test env, so appdaemon.plugins.hass.hassapi is faked just enough (Hass =
# object) before the module under test is imported. The harness app is built with
# __new__ (bypassing initialize()), mirroring make_app() there.

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from collections import deque
from pathlib import Path

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

import abb_sip_frame_recorder as asr  # noqa: E402


def _fresh_state_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    return path


def make_app(buffer_size=200, state_file=None):
    """AbbSipFrameRecorder with fakes for logging/set_state, without running
    initialize() - mirrors make_app() in test_appdaemon_release_watch.py."""
    app = asr.AbbSipFrameRecorder.__new__(asr.AbbSipFrameRecorder)
    app.sip_frame_event = asr.DEFAULT_EVENT
    app.buffer_size = buffer_size
    app.summary_entity = asr.DEFAULT_SUMMARY_ENTITY
    app.state_file = state_file or _fresh_state_path()

    app.logs = []
    app.log = lambda msg, level="INFO": app.logs.append((level, msg))

    app.published = []
    app.set_state = lambda entity_id, **kw: app.published.append((entity_id, kw))

    state = app._load_state()
    app.counters = state["counters"]
    app.total_in = state["total_in"]
    app.total_out = state["total_out"]
    app.last_inbound_message = state["last_inbound_message"]
    app.buffer = deque(state["buffer"], maxlen=app.buffer_size)
    return app


def _frame(direction="in", is_response=False, method=None, status_code=None,
           content_type=None, body_bytes=None, received_at=1700000000.0,
           start_line="INVITE sip:redacted SIP/2.0"):
    data = {
        "direction": direction,
        "received_at": received_at,
        "is_response": is_response,
        "protocol": "SIP/2.0",
        "header_count": 8,
        "via_count": 1,
        "body_bytes": body_bytes,
        "raw_bytes": 200,
        "start_line": start_line,
    }
    if not is_response and method is not None:
        data["method"] = method
    if is_response:
        data["status_code"] = status_code
    if content_type is not None:
        data["content_type"] = content_type
    return data


class MethodOrStatusKey(unittest.TestCase):
    def test_request_uses_method(self):
        self.assertEqual(asr.method_or_status(_frame(method="INVITE")), "INVITE")

    def test_response_uses_status_code_as_string(self):
        self.assertEqual(
            asr.method_or_status(_frame(is_response=True, status_code=200)), "200"
        )

    def test_missing_method_falls_back_to_unknown(self):
        self.assertEqual(asr.method_or_status({"is_response": False}), "UNKNOWN")

    def test_missing_status_code_falls_back_to_unknown(self):
        self.assertEqual(
            asr.method_or_status({"is_response": True, "status_code": None}), "UNKNOWN"
        )


class IsoTimestamp(unittest.TestCase):
    def test_converts_epoch_float(self):
        self.assertEqual(asr.iso_timestamp(1700000000.0), "2023-11-14T22:13:20+00:00")

    def test_none_input_is_none(self):
        self.assertIsNone(asr.iso_timestamp(None))


class CountersByDirectionAndKey(unittest.TestCase):
    def test_counts_split_by_direction_and_method_or_status(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        app._handle_frame(_frame(direction="out", method="INVITE"))
        app._handle_frame(_frame(direction="in", is_response=True, status_code=200))
        app._handle_frame(_frame(direction="in", is_response=True, status_code=200))

        self.assertEqual(app.counters["out"]["INVITE"], 1)
        self.assertEqual(app.counters["in"]["200"], 2)
        self.assertEqual(app.total_out, 1)
        self.assertEqual(app.total_in, 2)


class RollingBufferCap(unittest.TestCase):
    def test_buffer_never_exceeds_configured_size(self):
        app = make_app(buffer_size=5)
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        for i in range(20):
            app._handle_frame(_frame(direction="out", method="OPTIONS", received_at=float(i)))

        self.assertEqual(len(app.buffer), 5)
        # FIFO: only the most recent 5 survive.
        self.assertEqual([r["received_at"] for r in app.buffer], [15.0, 16.0, 17.0, 18.0, 19.0])

    def test_buffer_records_expected_fields(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        app._handle_frame(_frame(direction="in", method="MESSAGE", content_type="text/plain",
                                  body_bytes=42, received_at=123.0))

        record = app.buffer[-1]
        self.assertEqual(record["direction"], "in")
        self.assertEqual(record["method_or_status"], "MESSAGE")
        self.assertEqual(record["content_type"], "text/plain")
        self.assertEqual(record["body_bytes"], 42)
        self.assertEqual(record["received_at"], 123.0)


class InboundMessageDetection(unittest.TestCase):
    def test_inbound_message_logs_warning_with_marker(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        app._handle_frame(_frame(direction="in", method="MESSAGE", content_type="application/vnd.dds.foo",
                                  body_bytes=64, received_at=1700000000.0))

        warnings = [msg for level, msg in app.logs if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn(asr.INBOUND_MESSAGE_MARKER, warnings[0])
        self.assertIn("application/vnd.dds.foo", warnings[0])
        self.assertIn("64", warnings[0])

    def test_inbound_message_recorded_as_last_inbound_message(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        app._handle_frame(_frame(direction="in", method="MESSAGE", content_type="text/plain",
                                  body_bytes=10, received_at=1700000000.0))

        self.assertIsNotNone(app.last_inbound_message)
        self.assertEqual(app.last_inbound_message["content_type"], "text/plain")
        self.assertEqual(app.last_inbound_message["body_bytes"], 10)
        self.assertEqual(app.last_inbound_message["received_at_iso"], "2023-11-14T22:13:20+00:00")

    def test_outbound_message_is_not_detected_as_inbound(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        app._handle_frame(_frame(direction="out", method="MESSAGE"))

        self.assertIsNone(app.last_inbound_message)
        self.assertEqual([msg for level, msg in app.logs if level == "WARNING"], [])

    def test_inbound_response_is_not_detected_as_message(self):
        """is_response frames never carry `method` per the event contract, but
        this guards the direction/is_response check regardless."""
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        app._handle_frame(_frame(direction="in", is_response=True, status_code=200))

        self.assertIsNone(app.last_inbound_message)
        self.assertEqual([msg for level, msg in app.logs if level == "WARNING"], [])

    def test_inbound_non_message_method_is_not_detected(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        app._handle_frame(_frame(direction="in", method="INVITE"))

        self.assertIsNone(app.last_inbound_message)


class StatePersistence(unittest.TestCase):
    def test_counters_and_last_inbound_message_survive_a_reload(self):
        state_file = _fresh_state_path()
        self.addCleanup(lambda: os.path.exists(state_file) and os.remove(state_file))

        app1 = make_app(state_file=state_file)
        app1._handle_frame(_frame(direction="out", method="INVITE"))
        app1._handle_frame(_frame(direction="in", method="MESSAGE", content_type="text/plain",
                                   body_bytes=5, received_at=1700000000.0))

        app2 = make_app(state_file=state_file)

        self.assertEqual(app2.counters["out"]["INVITE"], 1)
        self.assertEqual(app2.counters["in"]["MESSAGE"], 1)
        self.assertEqual(app2.total_in, 1)
        self.assertEqual(app2.total_out, 1)
        self.assertIsNotNone(app2.last_inbound_message)
        self.assertEqual(app2.last_inbound_message["content_type"], "text/plain")
        self.assertEqual(list(app2.buffer), list(app1.buffer))

    def test_missing_state_file_yields_empty_defaults(self):
        app = make_app(state_file=_fresh_state_path())
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        self.assertEqual(app.counters, {})
        self.assertEqual(app.total_in, 0)
        self.assertEqual(app.total_out, 0)
        self.assertIsNone(app.last_inbound_message)
        self.assertEqual(len(app.buffer), 0)

    def test_corrupt_state_file_falls_back_to_defaults_without_raising(self):
        state_file = _fresh_state_path()
        with open(state_file, "w") as f:
            f.write("{not valid json")
        self.addCleanup(lambda: os.path.exists(state_file) and os.remove(state_file))

        app = make_app(state_file=state_file)

        self.assertEqual(app.counters, {})
        self.assertEqual(app.total_in, 0)


class SummaryPublication(unittest.TestCase):
    def test_publish_summary_sets_total_as_state_and_counts_as_attributes(self):
        app = make_app()
        self.addCleanup(lambda: os.path.exists(app.state_file) and os.remove(app.state_file))

        app._handle_frame(_frame(direction="out", method="INVITE"))
        app._handle_frame(_frame(direction="in", method="MESSAGE", content_type="text/plain",
                                  body_bytes=5, received_at=1700000000.0))

        entity_id, kwargs = app.published[-1]
        self.assertEqual(entity_id, asr.DEFAULT_SUMMARY_ENTITY)
        self.assertEqual(kwargs["state"], "2")
        attrs = kwargs["attributes"]
        self.assertEqual(attrs["total_in"], 1)
        self.assertEqual(attrs["total_out"], 1)
        self.assertEqual(attrs["counts_out"], {"INVITE": 1})
        self.assertTrue(attrs["inbound_message_seen"])
        self.assertNotIn("buffer", attrs)  # full buffer deliberately not published
        self.assertTrue(kwargs["replace"])


if __name__ == "__main__":
    unittest.main()
