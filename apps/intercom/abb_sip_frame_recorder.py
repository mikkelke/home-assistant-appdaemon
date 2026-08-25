"""ABB SIP frame recorder - diagnostic-only observer (2026-08-25).

Open question this app exists to answer: does the ABB Welcome gateway ever send
an INBOUND SIP MESSAGE (some gateways use that frame type for door-open
broadcasts)? Log-level investigation was inconclusive - the integration's own
``_LOGGER.debug`` calls for this sit on paths that were never exercised while
watching logs live. The integration instead fires an ``abb_welcome_sip_frame``
HA event for EVERY SIP frame in BOTH directions, so this app just listens,
counts, and remembers - it never calls a service and holds no reference to (or
dependency on) abb_welcome_bridge.py / intercom.py.

Event payload (bodies/headers deliberately never cross the event bus, so there
is nothing else to look at):
    direction      "in" | "out"
    received_at    float, time.time()
    is_response    bool
    protocol       "SIP/2.0"
    header_count, via_count, body_bytes, raw_bytes   int
    start_line     str, already redacted
    method         str  - only present when is_response is False
    status_code    int | None - only present when is_response is True
    content_type   str  - optional
    cseq_method    str  - optional

What this app does, per frame:
    1. counters["in"|"out"][method-or-status] += 1 (method for a request, the
       status code as a string for a response).
    2. appends (received_at, direction, method_or_status, content_type,
       body_bytes) to a rolling buffer capped at ``buffer_size`` frames
       (collections.deque(maxlen=...)) - bounded so a chatty gateway (e.g.
       periodic SIP OPTIONS keepalives) cannot grow this without limit.
    3. if the frame is inbound, not a response, and method == "MESSAGE": logs
       one WARNING line carrying a stable, greppable marker plus content_type/
       body_bytes/start_line, and records it as "last_inbound_message" with a
       full ISO-8601 timestamp. That single fact is the entire point of this
       app.
    4. persists counters + last_inbound_message + the buffer to a JSON state
       file next to this app (same load/save-with-tmp-then-replace pattern as
       apps/notify/appdaemon_release_watch.py) so a restart never loses
       evidence.
    5. republishes a summary onto an HA sensor (same set_state(..., replace=
       True) pattern as abb_welcome_bridge.py's sensor.abb_esp_ring_agreement)
       so the answer is readable without SSH. The full 200-frame buffer is
       deliberately NOT put in the entity's attributes (HA's per-entity
       attribute size is not meant for that much data) - only counts and the
       last-inbound-message fact are published; the buffer stays in the state
       file for SSH-level forensics if ever needed.

Threading: everything here runs synchronously inside the listen_event callback
on this app's own pinned thread (default AppDaemon behaviour) - no network
calls, no executor offload needed, same assumption apps/intercom/
abb_welcome_bridge.py documents for its own callbacks.
"""

import json
import os
from collections import deque
from datetime import datetime, timezone

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

DEFAULT_EVENT = "abb_welcome_sip_frame"
DEFAULT_BUFFER_SIZE = 200
DEFAULT_STATE_FILE = "/conf/apps/intercom/abb_sip_frame_recorder_state.json"
DEFAULT_SUMMARY_ENTITY = "sensor.abb_sip_frame_recorder"

# Stable marker so a ring's log output is one grep away from a yes/no answer.
INBOUND_MESSAGE_MARKER = "ABB SIP INBOUND MESSAGE DETECTED"


def method_or_status(data):
    """Counter/buffer key for one frame: the method name for a request, the
    status code (as a string) for a response. Falls back to "UNKNOWN" instead
    of raising if the expected field is missing - a malformed/unexpected frame
    still gets counted rather than crashing the listener."""
    if data.get("is_response"):
        status = data.get("status_code")
        return str(status) if status is not None else "UNKNOWN"
    method = data.get("method")
    return method if method else "UNKNOWN"


def iso_timestamp(received_at):
    """``received_at`` (epoch float) -> UTC ISO-8601 string, or None if it is
    missing/malformed. Pure and self-free so it is directly unit-testable."""
    try:
        return datetime.fromtimestamp(float(received_at), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


class AbbSipFrameRecorder(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.sip_frame_event = str(a("sip_frame_event", DEFAULT_EVENT))
        self.buffer_size = int(a("buffer_size", DEFAULT_BUFFER_SIZE))
        self.summary_entity = str(a("summary_entity", DEFAULT_SUMMARY_ENTITY))
        self.state_file = str(a("state_file", DEFAULT_STATE_FILE))

        state = self._load_state()
        self.counters = state["counters"]
        self.total_in = state["total_in"]
        self.total_out = state["total_out"]
        self.last_inbound_message = state["last_inbound_message"]
        self.buffer = deque(state["buffer"], maxlen=self.buffer_size)

        self.listen_event(self._on_sip_frame, self.sip_frame_event)

        self._publish_summary()

        last_seen = (
            self.last_inbound_message.get("received_at_iso")
            if self.last_inbound_message else "never"
        )
        self.log(
            f"AbbSipFrameRecorder: listening for '{self.sip_frame_event}', "
            f"buffer_size={self.buffer_size}, resumed at {self.total_in} in / "
            f"{self.total_out} out frames seen so far, last inbound MESSAGE: {last_seen}"
        )

    # -- event handling (pinned thread) --------------------------------------
    def _on_sip_frame(self, event_name, data, kwargs):
        try:
            self._handle_frame(data or {})
        except Exception as e:
            self.log(f"SIP frame handling failed: {e}", level="ERROR")

    def _handle_frame(self, data):
        direction = data.get("direction", "unknown")
        is_response = bool(data.get("is_response"))
        key = method_or_status(data)
        content_type = data.get("content_type")
        body_bytes = data.get("body_bytes")
        received_at = data.get("received_at")

        bucket = self.counters.setdefault(direction, {})
        bucket[key] = bucket.get(key, 0) + 1
        if direction == "in":
            self.total_in += 1
        elif direction == "out":
            self.total_out += 1

        self.buffer.append({
            "received_at": received_at,
            "direction": direction,
            "method_or_status": key,
            "content_type": content_type,
            "body_bytes": body_bytes,
        })

        # The entire point of this app: some gateways use an inbound MESSAGE
        # for door-open broadcasts.
        if direction == "in" and not is_response and data.get("method") == "MESSAGE":
            self._record_inbound_message(data, received_at, content_type, body_bytes)

        self._save_state()
        self._publish_summary()

    def _record_inbound_message(self, data, received_at, content_type, body_bytes):
        iso_ts = iso_timestamp(received_at)
        start_line = data.get("start_line")  # already redacted by the integration
        self.last_inbound_message = {
            "received_at": received_at,
            "received_at_iso": iso_ts,
            "content_type": content_type,
            "body_bytes": body_bytes,
            "start_line": start_line,
        }
        self.log(
            f"{INBOUND_MESSAGE_MARKER}: content_type={content_type!r} "
            f"body_bytes={body_bytes!r} start_line={start_line!r} received_at={iso_ts}",
            level="WARNING",
        )

    # -- HA-visible summary ---------------------------------------------------
    def _publish_summary(self):
        try:
            total = self.total_in + self.total_out
            self.set_state(
                self.summary_entity,
                state=str(total),
                attributes={
                    "friendly_name": "ABB Welcome SIP frames",
                    "icon": "mdi:phone-log",
                    "sip_frame_event": self.sip_frame_event,
                    "total_in": self.total_in,
                    "total_out": self.total_out,
                    "counts_in": self.counters.get("in", {}),
                    "counts_out": self.counters.get("out", {}),
                    "inbound_message_seen": self.last_inbound_message is not None,
                    "last_inbound_message": self.last_inbound_message,
                    "buffered_frames": len(self.buffer),
                    "buffer_capacity": self.buffer_size,
                },
                replace=True,
            )
        except Exception as e:
            self.log(f"Summary publish failed: {e}", level="WARNING")

    # -- state persistence ------------------------------------------------------
    def _load_state(self):
        default = {
            "counters": {},
            "total_in": 0,
            "total_out": 0,
            "last_inbound_message": None,
            "buffer": [],
        }
        try:
            with open(self.state_file) as f:
                d = json.load(f)
            return {
                "counters": d.get("counters", {}),
                "total_in": int(d.get("total_in", 0)),
                "total_out": int(d.get("total_out", 0)),
                "last_inbound_message": d.get("last_inbound_message"),
                "buffer": d.get("buffer", []),
            }
        except Exception:
            return default

    def _save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "counters": self.counters,
                    "total_in": self.total_in,
                    "total_out": self.total_out,
                    "last_inbound_message": self.last_inbound_message,
                    "buffer": list(self.buffer),
                }, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"state save failed ({e}) - continuing in-memory", level="WARNING")
