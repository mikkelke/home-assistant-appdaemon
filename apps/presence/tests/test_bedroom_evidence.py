"""Passive evidence capture: fidelity, bounded storage, gaps and reload lifecycle.

Run: python3 -m unittest discover -s apps/presence/tests -p test_bedroom_evidence.py -v
"""
from __future__ import annotations

import ast
import base64
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
        sys.modules.setdefault(name, types.ModuleType(name))
    module = types.ModuleType("appdaemon.plugins.hass.hassapi")
    module.Hass = object
    sys.modules[module.__name__] = module

import bedroom_evidence as be

T0 = 1792746000.0


def record(kind="ha_change", entity=be.ROOM, old="on", new="off", seconds=0, **fields):
    return {"kind": kind, "source": "ha_listen_state", "session_id": "test",
            "sequence": int(seconds) + 1, "received_epoch": T0 + seconds,
            "received_utc": be.utc_stamp(T0 + seconds), "received_monotonic": 500 + seconds,
            "entity": entity, "old": {"state": old}, "new": {"state": new}, **fields}


def bare_app():
    app = be.BedroomEvidence.__new__(be.BedroomEvidence)
    app._queue = queue.Queue(maxsize=be.QUEUE_SIZE)
    app._lock = threading.Lock()
    app._stop = threading.Event()
    app._session_id = "test"
    app._sequence = 0
    app._dropped = app._oversize = app._write_failures = app._written = 0
    app._mqtt_status = "connecting"
    app.log = MagicMock()
    return app


def mqtt_message(payload=b'{"presence":false,"last_seen":"2026-10-23T12:00:00+02:00"}', **fields):
    return types.SimpleNamespace(topic=be.TOPICS[0], payload=payload, retain=False, qos=0, dup=False, **fields)


def read_records(directory):
    return [json.loads(line) for path in sorted(Path(directory).glob("capture-*.jsonl"))
            for line in path.read_text().splitlines()]


class ProjectionTests(unittest.TestCase):
    def test_only_expected_entities_and_fields(self):
        self.assertEqual(len(be.ENTITIES), 11)
        self.assertEqual(len(be.WITNESSES), 5)
        self.assertEqual(len(be.TOPICS), 3)
        state = {"state": "on", "last_changed": "earlier", "last_updated": "now",
                 "context": {"id": "context", "parent_id": "parent", "user_id": None, "token": "secret"},
                 "attributes": {"reason": "channel:pir", "active_witnesses": [be.WITNESSES[1]], "sensitive": "secret"}}
        value = be.project_state(be.ROOM, state)
        self.assertEqual(value["attributes"]["reason"], "channel:pir")
        self.assertNotIn("secret", json.dumps(value))
        self.assertNotIn("attributes", be.project_state(be.LIGHTS[0], state))

    def test_missing_unavailable_and_oversized_attributes(self):
        self.assertEqual(be.project_state(be.ROOM, None), {"state": None})
        self.assertEqual(be.project_state(be.ROOM, {"state": "unavailable"})["state"], "unavailable")
        value = be.project_state(be.ROOM, {"state": "on", "attributes": {
            "reason": "x" * 100000, "witness_states": ["y" * 10000] * 1000,
            "confidence": {"arbitrary": "data"}, "computed_at": float("nan")}})
        self.assertLess(len(json.dumps(value)), 4000)
        self.assertIsNone(value["attributes"]["computed_at"])

    def test_no_actuation_or_wildcard_subscription_in_source(self):
        tree = ast.parse(Path(be.__file__).read_text())
        forbidden = {"call_service", "set_state", "turn_on", "turn_off", "publish", "mqtt_publish", "notify", "fire_event"}
        called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(called & forbidden)
        self.assertTrue(all("#" not in t and "+" not in t and not t.endswith(("/set", "/get")) for t in be.TOPICS))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "log":
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        self.assertTrue(child.value.isascii())


class CorrelationTests(unittest.TestCase):
    def test_pir_return_has_active_witness_reason_and_door_states(self):
        c = be.Correlator()
        for e in be.ENTITIES:
            c.process(record("ha_initial", entity=e, new="off"))
        c.process(record(entity=be.ROOM, seconds=1))
        c.process(record(entity=be.WITNESSES[1], old="off", new="on", seconds=11))
        edge = record(entity=be.ROOM, old="off", new="on", seconds=11.1)
        edge["new"]["attributes"] = {"reason": "channel:" + be.WITNESSES[1], "active_witnesses": [be.WITNESSES[1]]}
        marker = c.process(edge)[0]
        self.assertEqual(marker["kind"], "off_on")
        self.assertAlmostEqual(marker["off_to_on_seconds"], 10.1)
        self.assertTrue(marker["within_120s"])
        self.assertEqual(marker["snapshot"]["active_witnesses_observed"], [be.WITNESSES[1]])
        self.assertEqual(marker["snapshot"]["doors"][be.DOORS[0]]["state"], "off")
        self.assertEqual(marker["snapshot"]["room_active"]["attributes"], edge["new"]["attributes"])

    def test_clock_rollback_uses_monotonic_elapsed(self):
        c = be.Correlator()
        c.process(record(seconds=10))
        edge = record(old="off", new="on", seconds=20)
        edge["received_epoch"] = T0 - 3600
        edge["received_utc"] = be.utc_stamp(T0 - 3600)
        self.assertEqual(c.process(edge)[0]["off_to_on_seconds"], 10)

    def test_unavailable_interrupts_and_long_windows_expire(self):
        c = be.Correlator()
        c.process(record())
        self.assertEqual(c.process(record(old="off", new="unavailable", seconds=10))[0]["kind"], "window_interrupted")
        self.assertEqual(c.process(record(old="unavailable", new="on", seconds=20)), [])
        c.process(record(seconds=30))
        markers = c.process(record(old="off", new="on", seconds=151))
        self.assertEqual([m["kind"] for m in markers], ["window_expired", "off_on"])
        self.assertIsNone(markers[1]["off_to_on_seconds"])
        self.assertFalse(markers[1]["within_120s"])

    def test_restart_off_state_is_not_an_observed_off_edge(self):
        c = be.Correlator()
        c.process(record("ha_initial", new="off"))
        marker = c.process(record(old="off", new="on", seconds=10))[0]
        self.assertIsNone(marker["off_received_utc"])
        self.assertFalse(marker["within_120s"])

    def test_stale_initial_snapshot_cannot_replace_callback_even_after_queue_gap(self):
        c = be.Correlator()
        c.process(record(entity=be.WITNESSES[1], new="on"))
        c.process(record("ha_initial", entity=be.WITNESSES[1], new="off", seconds=1))
        self.assertEqual(c.states[be.WITNESSES[1]]["state"], "on")
        c.process(record(entity=be.ROOM, queue_dropped_before=1, seconds=2))
        c.process(record("ha_initial", entity=be.WITNESSES[1], new="off", seconds=3))
        self.assertNotIn(be.WITNESSES[1], c.states)

    def test_queue_gap_clears_witness_cache_and_window(self):
        c = be.Correlator()
        c.process(record(entity=be.WITNESSES[1], new="on"))
        c.process(record())
        markers = c.process(record(old="off", new="on", seconds=10, queue_dropped_before=3))
        self.assertEqual([m["kind"] for m in markers], ["capture_gap", "off_on"])
        self.assertEqual(markers[0]["queue_dropped_total"], 3)
        self.assertIsNone(markers[1]["off_received_utc"])
        self.assertEqual(markers[1]["snapshot"]["active_witnesses_observed"], [])


class QueueAndMqttTests(unittest.TestCase):
    def test_callbacks_never_write_files_or_fetch_state_and_topics_are_exact(self):
        app = bare_app()
        app.get_state = MagicMock(side_effect=AssertionError("callback read"))
        app._archive = MagicMock()
        app._on_ha("sensor.unrelated", "all", {}, {}, {})
        message = mqtt_message()
        message.topic += "/set"
        app._on_message(None, None, message)
        self.assertTrue(app._queue.empty())
        app._on_ha(be.WITNESSES[0], "all", {"state": "off"}, {"state": "on"}, {})
        app._on_message(None, None, mqtt_message())
        self.assertEqual(app._queue.qsize(), 2)
        app._archive.append.assert_not_called()
        app.get_state.assert_not_called()

    def test_retained_payload_is_exact_and_last_seen_is_not_receive_time(self):
        app = bare_app()
        message = mqtt_message(b'{ "pir_detection": true, "last_seen": "old" }')
        message.retain = True
        message.dup = True
        app._on_message(None, None, message)
        row = app._queue.get_nowait()
        app._prepare_mqtt(row)
        self.assertTrue(row["retained"])
        self.assertTrue(row["duplicate"])
        self.assertEqual(row["payload_utf8"].encode(), message.payload)
        self.assertEqual(row["payload_last_seen"], "old")
        self.assertNotEqual(row["received_utc"], "old")
        self.assertEqual(row["source"], "mqtt_published_payload")

    def test_invalid_json_utf8_scalar_and_deep_payloads_are_retained(self):
        app = bare_app()
        for payload, status in ((b'{bad', "invalid_json"), (b'\xff\x00', "invalid_utf8"),
                                (b'null', "non_object"), (b'{"last_seen": NaN}', "invalid_json")):
            with self.subTest(status=status, length=len(payload)):
                app._on_message(None, None, mqtt_message(payload))
                row = app._queue.get_nowait()
                app._prepare_mqtt(row)
                self.assertEqual(row["json_status"], status)
                captured = base64.b64decode(row["payload_base64"]) if status == "invalid_utf8" else row["payload_utf8"].encode()
                self.assertEqual(captured, payload)

    def test_deep_json_and_parser_recursion_failure_do_not_crash_capture(self):
        app = bare_app()
        payload = b'[' * 2000 + b']' * 2000
        app._on_message(None, None, mqtt_message(payload))
        row = app._queue.get_nowait()
        app._prepare_mqtt(row)
        self.assertIn(row["json_status"], ("non_object", "invalid_json"))
        self.assertEqual(row["payload_utf8"].encode(), payload)
        app._on_message(None, None, mqtt_message(payload))
        row = app._queue.get_nowait()
        with patch.object(be.json, "loads", side_effect=RecursionError):
            app._prepare_mqtt(row)
        self.assertEqual(row["json_status"], "invalid_json")

    def test_oversized_payload_is_counted_without_saving_prefix(self):
        app = bare_app()
        app._on_message(None, None, mqtt_message(b"SECRET" * be.MAX_PAYLOAD_BYTES))
        row = app._queue.get_nowait()
        self.assertEqual(row["kind"], "mqtt_oversize")
        self.assertNotIn("SECRET", str(row))
        self.assertEqual(app._oversize, 1)

    def test_queue_is_bounded_and_next_record_reports_loss(self):
        app = bare_app()
        for _ in range(be.QUEUE_SIZE + 7):
            app._on_message(None, None, mqtt_message())
        self.assertEqual(app._queue.qsize(), be.QUEUE_SIZE)
        self.assertEqual(app._dropped, 7)
        while not app._queue.empty():
            app._queue.get_nowait()
        app._on_message(None, None, mqtt_message())
        self.assertEqual(app._queue.get_nowait()["queue_dropped_before"], 7)

    def test_subscribe_only_exact_topics_and_reconnect(self):
        app, client = bare_app(), MagicMock()
        app._on_connect(client, None, None, 0, None)
        app._on_connect(client, None, None, 0, None)
        self.assertEqual(client.subscribe.call_count, 2)
        client.subscribe.assert_called_with([(t, 0) for t in be.TOPICS])
        client.publish.assert_not_called()
        app._on_subscribe(client, None, 1, [types.SimpleNamespace(is_failure=False)] * 3, None)
        self.assertEqual(app._mqtt_status, "subscribed")
        app._on_subscribe(client, None, 1, [types.SimpleNamespace(is_failure=True)] * 3, None)
        self.assertEqual(app._mqtt_status, "subscription_failed")
        app._on_disconnect(client, None, None, 1, None)
        self.assertEqual(app._mqtt_status, "disconnected")
        app._on_connect(client, None, None, 5, None)
        self.assertEqual(app._mqtt_status, "connection_refused")
        self.assertEqual(client.subscribe.call_count, 2)

    def test_no_callbacks_after_stop(self):
        app = bare_app()
        app._stop.set()
        app._on_message(None, None, mqtt_message())
        self.assertTrue(app._queue.empty())
        client = MagicMock()
        app._on_connect(client, None, None, 0, None)
        client.subscribe.assert_not_called()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "evidence"
        self.archives = []

    def tearDown(self):
        for archive in self.archives:
            archive.close()
        self.temp.cleanup()

    def archive(self, **kwargs):
        archive = be.RollingArchive(self.directory, **kwargs)
        self.archives.append(archive)
        return archive

    def test_total_bytes_and_file_count_are_hard_caps(self):
        archive = self.archive(max_bytes=131072, segment_bytes=65536, max_files=2)
        for n in range(20):
            archive.append({"n": n, "payload": "x" * 40000}, T0 + n)
        paths = list(self.directory.glob("capture-*.jsonl"))
        self.assertEqual(len(paths), 2)
        self.assertLessEqual(sum(p.stat().st_size for p in paths), 131072)
        self.assertEqual([r["n"] for r in read_records(self.directory)], [18, 19])
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in paths))

    def test_days_expire_and_clock_rollback_does_not_extend_retention(self):
        archive = self.archive(retention_days=1)
        archive.append({"old": True}, T0)
        old = archive.current
        archive.append({"rollback": True}, T0 - 3600)
        self.assertEqual(old.stat().st_mtime, T0)
        archive.maintain(T0 + 86401)
        self.assertFalse(old.exists())
        archive.append({"new": True}, T0 + 86401)
        self.assertEqual(read_records(self.directory), [{"new": True}])

    def test_restart_preserves_corrupt_tail_but_never_appends_to_it(self):
        archive = self.archive()
        archive.append({"old": True}, T0)
        old = archive.current
        archive.close()
        with old.open("ab") as handle:
            handle.write(b'{"partial":')
        original = old.read_bytes()
        fresh = self.archive()
        fresh.append({"new": True}, T0 + 1)
        self.assertNotEqual(fresh.current, old)
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(json.loads(fresh.current.read_text()), {"new": True})

    def test_nonblocking_writer_ownership_prevents_reload_overlap(self):
        first, second = self.archive(), self.archive()
        first.append({"first": True}, T0)
        with self.assertRaises(BlockingIOError):
            second.append({"second": True}, T0)
        first.close()
        second.append({"second": True}, T0 + 1)
        self.assertEqual(len(read_records(self.directory)), 2)

    def test_symlink_and_unrelated_files_are_not_changed(self):
        self.directory.mkdir()
        unrelated = self.directory / "unrelated.jsonl"
        unrelated.write_text("keep")
        target = Path(self.temp.name) / "target"
        target.write_text("never touch")
        (self.directory / "capture-000000000001.jsonl").symlink_to(target)
        archive = self.archive()
        with self.assertRaises(OSError):
            archive.append({"new": True}, T0)
        self.assertEqual(target.read_text(), "never touch")
        self.assertEqual(unrelated.read_text(), "keep")

    def test_full_size_escaped_payload_is_recorded(self):
        app = bare_app()
        app._on_message(None, None, mqtt_message(b'\x00' * be.MAX_PAYLOAD_BYTES))
        row = app._queue.get_nowait()
        app._prepare_mqtt(row)
        archive = self.archive()
        archive.append(row, T0)
        self.assertEqual(read_records(self.directory)[0]["payload_utf8"], "\x00" * be.MAX_PAYLOAD_BYTES)

    def test_corrupt_or_oversized_prior_segment_still_obeys_caps(self):
        self.directory.mkdir()
        old = self.directory / "capture-000000000042.jsonl"
        old.write_bytes(b"corrupt" * 30000)
        os.utime(old, (T0, T0))
        archive = self.archive(max_bytes=131072, segment_bytes=65536)
        archive.append({"new": True}, T0)
        self.assertFalse(old.exists())
        self.assertEqual(archive.current.name, "capture-000000000043.jsonl")


class LifecycleTests(unittest.TestCase):
    def test_adjacent_secret_link_passes_strict_installed_appdaemon_parser(self):
        # A subprocess avoids this unit suite's fake AppDaemon modules. Locally
        # optional; also run against the real installed parser in the container.
        script = r"""
import sys
import tempfile
from pathlib import Path
try:
    from appdaemon import utils
    from appdaemon.app_management import AllAppConfig
except ModuleNotFoundError:
    sys.exit(77)
source = sys.stdin.read()
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    secrets = root / "secrets.yaml"
    secrets.write_text("bedroom_evidence_mqtt_user: fixture-user\n"
                       "bedroom_evidence_mqtt_password: fixture-password\n"
                       "unrelated_live_secret: preserve-this-key\n")
    app_dir = root / "apps"
    presence_dir = app_dir / "presence"
    presence_dir.mkdir(parents=True)
    (presence_dir / "secrets.yaml").symlink_to("../../secrets.yaml")
    config = presence_dir / "bedroom_evidence.yaml"
    config.write_text(source)
    utils.secrets = {"unrelated_cached_key": "stale"}
    # Match AppManagement.read_config_file's strict chain exactly. The tolerant
    # AllAppConfig.from_config_files method can hide an invalid top-level key.
    parsed = utils.read_config_file(config, app_config=True)
    assert parsed["BedroomEvidence"]["mqtt_user"] == "fixture-user"
    assert parsed["BedroomEvidence"]["mqtt_password"] == "fixture-password"
    assert set(parsed) == {"BedroomEvidence"}
    apps = AllAppConfig.model_validate(parsed)
    assert apps.app_names() == {"BedroomEvidence"}
    assert utils.secrets["unrelated_live_secret"] == "preserve-this-key"
    scanned = set(utils.recursive_get_files(
        base=app_dir.resolve(), suffix={".yaml", ".toml"},
        exclude={"ruff.toml", "pyproject.toml", "secrets.yaml"}))
    assert scanned == {config}
print("Strict installed parser loaded BedroomEvidence; adjacent secret link excluded from scan")
"""
        result = subprocess.run([sys.executable, "-c", script],
                                input=Path(be.__file__).with_suffix(".yaml").read_text(),
                                text=True, capture_output=True, timeout=10)
        if result.returncode == 77:
            self.skipTest("AppDaemon is not installed in this local interpreter")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_full_lifecycle_reads_only_whitelist_and_flushes_stop(self):
        with tempfile.TemporaryDirectory() as root, patch.object(be, "PRIVATE_LOG_ROOT", Path(root)):
            app = be.BedroomEvidence.__new__(be.BedroomEvidence)
            app.args = {"archive_dir": str(Path(root) / "evidence")}
            app.log = MagicMock()
            app.get_state = MagicMock(return_value={"state": "off", "attributes": {"unrelated": "secret"}})
            app.listen_state = MagicMock(side_effect=lambda callback, entity, **kw: entity)
            app.cancel_listen_state = MagicMock()
            app.initialize()
            app._on_ha(be.WITNESSES[1], "all", {"state": "off"}, {"state": "on"}, {})
            app._on_message(None, None, mqtt_message())
            app.terminate()
            self.assertFalse(app._writer.is_alive())
            rows = read_records(Path(root) / "evidence")
            self.assertEqual(rows[0]["kind"], "session_start")
            self.assertEqual(rows[-1]["kind"], "session_stop")
            self.assertEqual(len([r for r in rows if r["kind"] == "ha_initial"]), len(be.ENTITIES))
            self.assertEqual(len([r for r in rows if r["kind"] == "mqtt_message"]), 1)
            self.assertEqual({call.args[0] for call in app.get_state.call_args_list}, set(be.ENTITIES))
            self.assertEqual(app.cancel_listen_state.call_count, len(be.ENTITIES))
            self.assertNotIn("secret", json.dumps(rows))
            self.assertTrue(any("write_failures=" in call.args[0] for call in app.log.call_args_list))

    def test_shutdown_rejects_producer_already_waiting_for_enqueue_lock(self):
        app = bare_app()
        checked = threading.Event()
        original_is_set = app._stop.is_set

        def observe_check():
            stopped = original_is_set()
            if not stopped:
                checked.set()
            return stopped

        with patch.object(app._stop, "is_set", side_effect=observe_check):
            with app._lock:
                producer = threading.Thread(target=app._enqueue, args=("test", "test", {}), daemon=True)
                producer.start()
                self.assertTrue(checked.wait(timeout=1))
                app._stop.set()
            producer.join(timeout=1)
        self.assertFalse(producer.is_alive())
        self.assertTrue(app._queue.empty())

    def test_shutdown_waits_for_producer_inside_enqueue_critical_section(self):
        app = bare_app()
        app._listeners = []
        app._mqtt = None
        app._writer = MagicMock()
        app._writer.is_alive.return_value = False
        entered_put, release_put, stopping = (threading.Event() for _ in range(3))
        real_lock = threading.Lock()
        original_put = app._queue.put_nowait

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == "evidence-test-stopper":
                    stopping.set()
                real_lock.acquire()

            def __exit__(self, *args):
                real_lock.release()

        def blocked_put(row):
            entered_put.set()
            if not release_put.wait(timeout=2):
                raise AssertionError("producer was not released")
            original_put(row)

        app._lock = ObservedLock()
        with patch.object(app._queue, "put_nowait", side_effect=blocked_put):
            producer = threading.Thread(target=app._enqueue, args=("test", "test", {}), daemon=True)
            producer.start()
            self.assertTrue(entered_put.wait(timeout=1))
            stopper = threading.Thread(target=app.terminate, name="evidence-test-stopper", daemon=True)
            stopper.start()
            try:
                self.assertTrue(stopping.wait(timeout=1))
                self.assertFalse(app._stop.is_set())
            finally:
                release_put.set()
                producer.join(timeout=1)
                stopper.join(timeout=1)
        self.assertFalse(producer.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertTrue(app._stop.is_set())
        self.assertEqual(app._queue.qsize(), 1)
        app._enqueue("too_late", "test", {})
        self.assertEqual(app._queue.qsize(), 1)

    def test_archive_path_inside_apps_is_rejected(self):
        app = be.BedroomEvidence.__new__(be.BedroomEvidence)
        app.args = {"archive_dir": "/conf/apps/presence/evidence"}
        with self.assertRaises(ValueError):
            app.initialize()

    def test_write_failure_is_counted_and_recovery_records_the_gap(self):
        app = bare_app()
        app._archive = MagicMock()
        app._archive.append.side_effect = [OSError("disk unavailable"), None]
        app._persist(record())
        self.assertEqual(app._write_failures, 1)
        later = record(seconds=1)
        app._persist(later)
        self.assertEqual(later["write_failures_before"], 1)
        self.assertEqual(app._written, 1)

    def test_missing_mqtt_credentials_leaves_ha_capture_and_logs_no_exception_text(self):
        app = bare_app()
        app.args = {"mqtt_enabled": True}
        module = types.ModuleType("paho.mqtt")
        module.client = MagicMock()
        with patch.dict(sys.modules, {"paho": types.ModuleType("paho"), "paho.mqtt": module}):
            app._start_mqtt()
        self.assertEqual(app._mqtt_status, "setup_failed")
        module.client.Client.assert_not_called()
        row = app._queue.get_nowait()
        self.assertEqual(row["error_type"], "ValueError")
        self.assertNotIn("missing MQTT credentials", json.dumps(row))


if __name__ == "__main__":
    unittest.main()
