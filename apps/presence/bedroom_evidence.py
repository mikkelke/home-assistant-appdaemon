"""Temporary, passive bedroom evidence recorder; remove after useful episodes are captured.

Only three exact Z2M device topics and eleven named HA entities are observed. MQTT
records contain the published application payload, NOT Zigbee/ZCL frames; a cached
last_seen is not proof that every property was freshly measured. No service calls,
state publication, device requests, notifications, or automation timing changes.

Callbacks only project bounded data into a bounded queue. One private writer thread
owns files and correlation state. JSONL segments rotate at 256 KiB or one UTC day;
oldest segments expire after seven days without a write, or sooner at the total
16 MiB/128-file caps. A partial/corrupt previous segment is never parsed or appended.
Every restart starts a new session/segment; gaps are explicit, never interpolated.

Receive UTC is paired with monotonic seconds and a session id. Clock rollback cannot
extend an in-process window or age retention; after restart the archive's newest
mtime is the retention clock floor. Forward clock jumps can expire evidence early.
OFF->ON markers on room_active and two light groups include the five witness states,
room_active's reported reason/active_witnesses, doors and bed-session state. This is
observed correlation, not proof of physical occupancy or an automation's cause.

Readiness: Paho MQTT 2.x is already installed on the box. Credentials live exclusively
in /conf/secrets.yaml; no new AD plugin or container restart is required. No named
AppDaemon log stream is used. Archive permission errors fail capture, not automation;
health records expose queue loss, oversize payloads and failed writes after recovery.
"""

from __future__ import annotations

import base64
import fcntl
import json
import math
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

TOPICS = (
    "zigbee2mqtt/Bedroom presence",
    "zigbee2mqtt/Bedroom door",
    "zigbee2mqtt/Bathroom door",
)
WITNESSES = (
    "binary_sensor.bedroom_presence_presence",
    "binary_sensor.bedroom_presence_pir_detection",
    "binary_sensor.left_bedside",
    "binary_sensor.right_bedside",
    "binary_sensor.bed_presence_6b9c94_bed_occupied_either",
)
ROOM = "binary_sensor.bedroom_active"
SESSION = "input_boolean.bedroom_bed_session"
DOORS = ("binary_sensor.bedroom_door_contact", "binary_sensor.bathroom_door_contact")
LIGHTS = ("light.bedroom_bed_lights", "light.bedroom_ceiling_lights")
ENTITIES = WITNESSES + (ROOM, SESSION) + DOORS + LIGHTS
TRIGGERS = (ROOM,) + LIGHTS
ROOM_ATTRIBUTES = (
    "reason", "tier", "confidence", "auto_on_ok", "active_witnesses",
    "source_entities", "witness_states", "settling", "stale", "computed_at",
)
PRIVATE_LOG_ROOT = Path("/conf/logs")
MAX_PAYLOAD_BYTES = 8192
MAX_RECORD_BYTES = 65536
QUEUE_SIZE = 512
WINDOW_SECONDS = 120
HEALTH_SECONDS = 60
_SEGMENT = re.compile(r"capture-([0-9]{12})\.jsonl\Z")


def utc_stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds")


def small(value):
    """Bound selected HA fields; never recurse through arbitrary attributes."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:384]
    if isinstance(value, (list, tuple)):
        return [small(v) if not isinstance(v, (list, tuple, dict)) else "<unsupported>"
                for v in value[:8]]
    return "<unsupported>"


def project_state(entity, state):
    if not isinstance(state, dict):
        return {"state": small(state)}
    result = {k: small(state.get(k)) for k in ("state", "last_changed", "last_updated")}
    context = state.get("context")
    if isinstance(context, dict):
        result["context"] = {k: small(context.get(k)) for k in ("id", "parent_id", "user_id")}
    attrs = state.get("attributes")
    if entity == ROOM and isinstance(attrs, dict):
        result["attributes"] = {k: small(attrs[k]) for k in ROOM_ATTRIBUTES if k in attrs}
    return result


class RollingArchive:
    """Single-writer bounded storage; touches only our strictly named regular files."""

    def __init__(self, directory, *, max_bytes=16 * 1024 * 1024,
                 segment_bytes=256 * 1024, retention_days=7, max_files=128):
        if not (MAX_RECORD_BYTES <= segment_bytes <= max_bytes <= 16 * 1024 * 1024):
            raise ValueError("invalid archive byte limits")
        if not (1 <= retention_days <= 7 and 2 <= max_files <= 128):
            raise ValueError("invalid archive retention limits")
        self.directory = Path(directory)
        self.max_bytes, self.segment_bytes = max_bytes, segment_bytes
        self.retention_s, self.max_files = retention_days * 86400, max_files
        self.current = None
        self.current_day = None
        self.high_water = 0.0
        self.sequence = 0
        self.files = []
        self._ready = False
        self._lock_fd = None

    def close(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self._ready = False
        self.current = None

    def _prepare(self):
        if self._ready:
            return
        if self.directory.is_symlink():
            raise OSError("archive directory must not be a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        if self._lock_fd is None:
            fd = os.open(self.directory / ".writer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise
            self._lock_fd = fd
        entries = []
        for path in self.directory.iterdir():
            match = _SEGMENT.fullmatch(path.name)
            if not match:
                continue
            if path.is_symlink() or not path.is_file():
                raise OSError("archive segment must be a regular file")
            stat = path.stat()
            entries.append([int(match[1]), path, stat.st_size, stat.st_mtime])
        self.files = sorted(entries)
        self.sequence = max((row[0] for row in entries), default=0)
        self.high_water = max((row[3] for row in entries), default=0.0)
        self._ready = True

    def _remove_oldest(self):
        entry = self.files[0]
        entry[1].unlink(missing_ok=True)
        self.files.pop(0)
        if entry[1] == self.current:
            self.current = None

    def maintain(self, now):
        self._prepare()
        self.high_water = max(now, self.high_water)
        cutoff = self.high_water - self.retention_s
        # Sequence is independent of wall time; remove expired entries even if a
        # clock correction made their mtimes differ from their sequence order.
        for entry in list(self.files):
            if entry[3] < cutoff:
                entry[1].unlink(missing_ok=True)
                self.files.remove(entry)
                if entry[1] == self.current:
                    self.current = None
        while self.files and (sum(r[2] for r in self.files) > self.max_bytes
                              or len(self.files) > self.max_files):
            self._remove_oldest()

    def append(self, record, now):
        data = (json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if len(data) > MAX_RECORD_BYTES:
            raise ValueError("evidence record exceeds hard cap")
        self.maintain(now)
        day = int(self.high_water // 86400)
        if self.current and (self.files[-1][2] + len(data) > self.segment_bytes
                             or day != self.current_day):
            self.current = None
        if self.current is None:
            while self.files and len(self.files) >= self.max_files:
                self._remove_oldest()
            self.sequence += 1
            self.current = self.directory / f"capture-{self.sequence:012d}.jsonl"
            fd = os.open(self.current, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self.current_day = day
            self.files.append([self.sequence, self.current, 0, self.high_water])
        while self.files and sum(row[2] for row in self.files) + len(data) > self.max_bytes:
            self._remove_oldest()
        # segment_bytes <= max_bytes, so the current (bounded) segment remains.
        fd = os.open(self.current, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            with os.fdopen(fd, "ab") as handle:
                handle.write(data)
        except OSError:
            # Never append after a potentially partial write. Re-scan actual sizes
            # at recovery, then continue in a fresh segment with valid JSONL lines.
            self.current = None
            self._ready = False
            raise
        self.files[-1][2] += len(data)
        self.files[-1][3] = self.high_water
        os.utime(self.current, (self.high_water, self.high_water), follow_symlinks=False)


class Correlator:
    """Worker-owned HA cache and monotonic OFF->ON windows; no HA API access."""

    def __init__(self):
        self.states = {}
        self.windows = {}
        self.changed = set()
        self.dropped = 0

    def snapshot(self):
        return {
            "witnesses": {e: self.states.get(e, {"state": None}) for e in WITNESSES},
            "active_witnesses_observed": [e for e in WITNESSES if self.states.get(e, {}).get("state") == "on"],
            "room_active": self.states.get(ROOM, {"state": None}),
            "bed_session": self.states.get(SESSION, {"state": None}),
            "doors": {e: self.states.get(e, {"state": None}) for e in DOORS},
            "lights": {e: self.states.get(e, {"state": None}) for e in LIGHTS},
            "interpretation": "observed_correlation_not_proven_cause",
        }

    def process(self, record):
        markers = []
        dropped = record.get("queue_dropped_before", self.dropped)
        if dropped > self.dropped:
            self.states.clear()
            self.windows.clear()
            markers.append(self._marker(record, "capture_gap", None, None))
            markers[-1]["queue_dropped_total"] = dropped
            self.dropped = dropped
        mono = record["received_monotonic"]
        for entity, start in list(self.windows.items()):
            if mono - start["received_monotonic"] > WINDOW_SECONDS:
                markers.append(self._marker(record, "window_expired", entity, start))
                del self.windows[entity]
        if record["kind"] not in ("ha_initial", "ha_change"):
            return markers
        entity = record["entity"]
        if record["kind"] == "ha_initial" and entity in self.changed:
            return markers
        if record["kind"] == "ha_change":
            self.changed.add(entity)
        self.states[entity] = record["new"]
        if record["kind"] == "ha_initial" or entity not in TRIGGERS:
            return markers
        old, new = record["old"].get("state"), record["new"].get("state")
        if old == new:
            return markers
        if new == "off":
            self.windows[entity] = record
            markers.append(self._marker(record, "window_open", entity, record))
        elif new == "on":
            start = self.windows.pop(entity, None)
            if old == "off":
                markers.append(self._marker(record, "off_on", entity, start))
        else:
            start = self.windows.pop(entity, None)
            if start:
                markers.append(self._marker(record, "window_interrupted", entity, start))
        return markers

    def _marker(self, record, kind, entity, start):
        elapsed = None if start is None else record["received_monotonic"] - start["received_monotonic"]
        return {k: record[k] for k in ("session_id", "received_utc", "received_epoch", "received_monotonic")} | {
            "kind": kind, "source": "correlation", "entity": entity,
            "off_received_utc": start["received_utc"] if start else None,
            "off_to_on_seconds": elapsed if kind == "off_on" else None,
            "within_120s": elapsed is not None and 0 <= elapsed <= WINDOW_SECONDS,
            "trigger_sequence": record.get("sequence"), "snapshot": self.snapshot(),
        }


class BedroomEvidence(hass.Hass):
    def initialize(self):
        directory = Path(self.args.get("archive_dir", PRIVATE_LOG_ROOT / "bedroom_evidence"))
        if not directory.is_absolute() or not directory.resolve().is_relative_to(PRIVATE_LOG_ROOT.resolve()):
            raise ValueError("archive_dir must be beneath /conf/logs")
        self._archive = RollingArchive(directory)
        self._queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._session_id = uuid.uuid4().hex
        self._sequence = 0
        self._dropped = self._oversize = self._write_failures = self._written = 0
        self._mqtt_status = "disabled"
        self._mqtt = None
        self._listeners = []
        self._correlator = Correlator()
        self._enqueue("session_start", "lifecycle", {
            "coverage": {"mqtt_topics": list(TOPICS), "ha_entities": list(ENTITIES)},
            "mqtt_payload_type": "published_application_payload_not_zcl_frame",
            "continuity": "new_session_no_reconstruction_across_restart",
        })
        # Register before seeding. Callback data (not a later get_state poll) is
        # authoritative after seeding; AppDaemon's central cache can be stale.
        for entity in ENTITIES:
            self._listeners.append(self.listen_state(self._on_ha, entity, attribute="all"))
        for entity in ENTITIES:
            try:
                state = self.get_state(entity, attribute="all")
            except Exception:
                state = None
            self._enqueue("ha_initial", "ha_state_cache_at_start", {
                "entity": entity, "new": project_state(entity, state),
            })
        self._writer = threading.Thread(target=self._write_loop, name="bedroom-evidence-writer", daemon=True)
        self._writer.start()
        if self.args.get("mqtt_enabled", False):
            self._start_mqtt()
        self.log("BedroomEvidence initialized: passive capture; bounded private archive; no device actions")

    def _enqueue(self, kind, source, fields):
        if self._stop.is_set():
            return
        with self._lock:
            now, mono = time.time(), time.monotonic()
            self._sequence += 1
            record = {"kind": kind, "source": source, "session_id": self._session_id,
                      "sequence": self._sequence, "received_utc": utc_stamp(now),
                      "received_epoch": now, "received_monotonic": mono,
                      "queue_dropped_before": self._dropped, **fields}
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self._dropped += 1

    def _on_ha(self, entity, attribute, old, new, kwargs):
        if entity not in ENTITIES:
            return
        before, after = project_state(entity, old), project_state(entity, new)
        if before == after:
            return
        self._enqueue("ha_change", "ha_listen_state", {"entity": entity, "old": before, "new": after})

    def _start_mqtt(self):
        try:
            from paho.mqtt import client as mqtt
            user, password = self.args.get("mqtt_user"), self.args.get("mqtt_password")
            if not user or not password:
                raise ValueError("missing MQTT credentials")
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id="bedroom-evidence-" + self._session_id[:12])
            client.username_pw_set(user, password)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            client.on_subscribe = self._on_subscribe
            client.reconnect_delay_set(min_delay=2, max_delay=60)
            self._mqtt = client
            self._mqtt_status = "connecting"
            client.connect_async(self.args.get("mqtt_host", "127.0.0.1"), int(self.args.get("mqtt_port", 1883)), 60)
            client.loop_start()
        except Exception as exc:
            self._mqtt_status = "setup_failed"
            # Exception messages may contain connection details or credentials.
            self._enqueue("mqtt_status", "mqtt_client", {"status": "setup_failed", "error_type": type(exc).__name__})
            self.log("BedroomEvidence MQTT setup failed; HA capture remains active", level="WARNING")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if self._stop.is_set():
            return
        if reason_code == 0:
            self._mqtt_status = "subscribing"
            client.subscribe([(topic, 0) for topic in TOPICS])
        else:
            self._mqtt_status = "connection_refused"
        self._enqueue("mqtt_status", "mqtt_client", {"status": self._mqtt_status})

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        self._mqtt_status = "subscribed" if len(reason_codes) == len(TOPICS) and all(not r.is_failure for r in reason_codes) else "subscription_failed"
        self._enqueue("mqtt_status", "mqtt_client", {"status": self._mqtt_status})

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._mqtt_status = "disconnected"
        self._enqueue("mqtt_status", "mqtt_client", {"status": self._mqtt_status})

    def _on_message(self, client, userdata, message):
        if message.topic not in TOPICS:
            return
        size = len(message.payload)
        if size > MAX_PAYLOAD_BYTES:
            with self._lock:
                self._oversize += 1
            self._enqueue("mqtt_oversize", "mqtt_published_payload", {"topic": message.topic, "payload_bytes": size})
            return
        self._enqueue("mqtt_message", "mqtt_published_payload", {
            "topic": message.topic, "_payload": bytes(message.payload),
            "retained": bool(message.retain), "qos": int(message.qos),
            "duplicate": bool(message.dup), "payload_bytes": size,
        })

    def _prepare_mqtt(self, record):
        raw = record.pop("_payload")
        try:
            payload = raw.decode("utf-8")
        except UnicodeDecodeError:
            record.update(payload_base64=base64.b64encode(raw).decode("ascii"), json_status="invalid_utf8")
            return
        record["payload_utf8"] = payload
        try:
            decoded = json.loads(payload, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            record["json_status"] = "object" if isinstance(decoded, dict) else "non_object"
            record["payload_last_seen"] = small(decoded.get("last_seen")) if isinstance(decoded, dict) else None
        except (ValueError, RecursionError):
            record["json_status"] = "invalid_json"

    def _health(self):
        with self._lock:
            return {"queue_dropped_total": self._dropped, "oversize_payloads_total": self._oversize,
                    "write_failures_total": self._write_failures, "queue_depth": self._queue.qsize(),
                    "mqtt_status": self._mqtt_status, "records_written_total": self._written}

    def _persist(self, record):
        record["write_failures_before"] = self._write_failures
        try:
            self._archive.append(record, record["received_epoch"])
            self._written += 1
        except (OSError, ValueError):
            self._write_failures += 1

    def _write_loop(self):
        next_health = 0.0
        while not self._stop.is_set() or not self._queue.empty():
            try:
                record = self._queue.get(timeout=0.5)
            except queue.Empty:
                record = None
            if record is not None:
                if record["kind"] == "mqtt_message":
                    self._prepare_mqtt(record)
                self._persist(record)
                for marker in self._correlator.process(record):
                    self._persist(marker)
                self._queue.task_done()
            now = time.monotonic()
            if now >= next_health:
                health = self._health()
                self._enqueue("capture_health", "recorder", health)
                self.log("BedroomEvidence health: mqtt=%s written=%d dropped=%d oversize=%d write_failures=%d" % (
                    health["mqtt_status"], health["records_written_total"], health["queue_dropped_total"],
                    health["oversize_payloads_total"], health["write_failures_total"]))
                next_health = now + HEALTH_SECONDS
        final = {"kind": "session_stop", "source": "lifecycle", "session_id": self._session_id,
                 "received_epoch": time.time(), "received_monotonic": time.monotonic(), **self._health()}
        final["received_utc"] = utc_stamp(final["received_epoch"])
        self._persist(final)
        self._archive.close()

    def terminate(self):
        self._stop.set()
        for listener in self._listeners:
            self.cancel_listen_state(listener)
        if self._mqtt is not None:
            self._mqtt.disconnect()
            self._mqtt.loop_stop()
        self._writer.join(timeout=3)
        if self._writer.is_alive():
            self.log("BedroomEvidence writer still stopping; queued tail may be lost", level="WARNING")
