"""AssistTurnLog: a durable, append-only record of every Assist voice turn.

Home Assistant keeps the debug trace of only the last ten runs per pipeline, in memory, so a
failed or odd turn is gone after a restart. This app copies each finished run into log_file, one
JSON line per run, so voice failures can be reviewed later. It decides nothing and drives nothing.

Runs are read over AppDaemon's own authenticated Home Assistant websocket (the HASS plugin's
websocket_send_json), so no token is read, kept or logged here. A poll runs settle_s after any
assist_satellite state change, and every poll_every_s as a fallback, which also covers runs
that have no satellite. A run is written once it has emitted run-end, or once it is older than
grace_s (then finished is false). Every poll first reads the run ids in the tail of log_file, so a
run is never written twice, across polls, reloads and restarts.

Each line is a JSON object; v is the schema version and a part that did not happen is null:
  run_id, timestamp, pipeline, satellite, conversation_id
  heard                  the speech-to-text result, or the typed text for text input
  engine                 the conversation agent
  processed_locally      matched by local intents, not answered by the agent's model
  tool_calls             [{name, args}] the agent called
  spoken                 the answer text
  response_type          the intent response type
  response_code          the intent error code, e.g. no_intent_match
  continue_conversation  the agent asked a follow-up question
  error                  {code, message} of a pipeline error event
  finished               the run emitted run-end
  stt_s                  start of listening to the transcript
  stt_latency_s          end of speech to the transcript
  intent_s, tts_s, total_s
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

SCHEMA = 1
TAIL_BYTES = 256 * 1024
FIRST_POLL_S = 10
QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def _dig(obj, *keys):
    for key in keys:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def _first(events, kind):
    return next((event for event in events if event.get("type") == kind), None)


def _moment(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _seconds(events, start, end):
    began = _moment(_dig(_first(events, start), "timestamp"))
    ended = _moment(_dig(_first(events, end), "timestamp"))
    return round((ended - began).total_seconds(), 3) if began and ended else None


def parse_turn(pipeline_id, run, events):
    """One log record from a pipeline_debug/list entry and its pipeline_debug/get events."""
    events = [event for event in events or [] if isinstance(event, dict)]

    def data(kind):
        value = _dig(_first(events, kind), "data")
        return value if isinstance(value, dict) else {}

    run_start, intent_start, intent_end = data("run-start"), data("intent-start"), data("intent-end")
    response = _dig(intent_end, "intent_output", "response")
    failure = _first(events, "error")

    tool_calls = []
    for event in events:
        calls = _dig(event, "data", "chat_log_delta", "tool_calls")
        if event.get("type") == "intent-progress" and isinstance(calls, list):
            tool_calls += [{"name": call.get("tool_name"), "args": call.get("tool_args")}
                           for call in calls if isinstance(call, dict)]

    return {
        "v": SCHEMA,
        "run_id": run.get("pipeline_run_id"),
        "timestamp": run.get("timestamp"),
        "pipeline": pipeline_id,
        "satellite": run_start.get("satellite_id") or intent_start.get("satellite_id"),
        "conversation_id": (run_start.get("conversation_id") or intent_start.get("conversation_id")
                            or _dig(intent_end, "intent_output", "conversation_id")),
        "heard": _dig(data("stt-end"), "stt_output", "text") or intent_start.get("intent_input"),
        "engine": intent_start.get("engine"),
        "processed_locally": intent_end.get("processed_locally"),
        "tool_calls": tool_calls,
        "spoken": _dig(response, "speech", "plain", "speech"),
        "response_type": _dig(response, "response_type"),
        "response_code": _dig(response, "data", "code"),
        "continue_conversation": _dig(intent_end, "intent_output", "continue_conversation"),
        "error": ({"code": _dig(failure, "data", "code"), "message": _dig(failure, "data", "message")}
                  if failure else None),
        "finished": _first(events, "run-end") is not None,
        "stt_s": _seconds(events, "stt-start", "stt-end"),
        "stt_latency_s": _seconds(events, "stt-vad-end", "stt-end"),
        "intent_s": _seconds(events, "intent-start", "intent-end"),
        "tts_s": _seconds(events, "tts-start", "tts-end"),
        "total_s": _seconds(events, "run-start", "run-end"),
    }


def is_final(events, started, now, grace_s):
    """A run is final once it has ended, or has been going for grace_s without ending."""
    if any(isinstance(event, dict) and event.get("type") == "run-end" for event in events):
        return True
    began = _moment(started)
    return began is None or (now - began).total_seconds() >= grace_s


def _clip(value, limit=200):
    text = " ".join(str(value).translate(QUOTES).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def summary(turn):
    """The one-line app log entry for a turn, ASCII only."""
    heard = f"'{_clip(turn['heard'])}'" if turn.get("heard") else "nothing"
    said = f"'{_clip(turn['spoken'])}'" if turn.get("spoken") else "nothing"
    local = turn.get("processed_locally")
    notes = [_clip(turn.get("engine") or "no engine"),
             {True: "local", False: "not local"}[local] if isinstance(local, bool) else "locality unknown"]
    tools = [str(call.get("name")) for call in turn.get("tool_calls") or []]
    if tools:
        notes.append("tools " + ",".join(tools))
    problem = _dig(turn, "error", "code")
    if not problem and turn.get("response_type") == "error":
        problem = turn.get("response_code") or "error response"
    if problem:
        notes.append(f"error {problem}")
    for label, key in (("stt", "stt_latency_s"), ("intent", "intent_s")):
        if turn.get(key) is not None:
            notes.append(f"{label} {turn[key]:.1f}s")
    satellite = (turn.get("satellite") or "no satellite").split(".", 1)[-1]
    line = f"Assist turn on {satellite}: heard {heard} -> said {said} ({', '.join(notes)})"
    return line.encode("ascii", "backslashreplace").decode("ascii")


def read_seen(path, tail_bytes=TAIL_BYTES):
    """Run ids in the last tail_bytes of the log. A line that does not parse, such as the first
    one when the tail starts mid-line, is skipped."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - tail_bytes))
            tail = handle.read()
    except FileNotFoundError:
        return set()
    seen = set()
    for line in tail.splitlines():
        try:
            seen.add(json.loads(line)["run_id"])
        except (ValueError, KeyError, TypeError):
            continue
    return seen


def append_jsonl(path, records):
    """Append one JSON line per record in a single fsynced write. When the file does not end in a
    newline, an earlier write was cut short, so the batch starts on a fresh line."""
    data = "".join(json.dumps(record, separators=(",", ":"), default=str) + "\n"
                   for record in records).encode("ascii")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _failure(reply):
    if not isinstance(reply, dict):
        return "no reply"
    return str(_dig(reply, "error", "code") or reply.get("ad_status") or "unsuccessful")


class AssistTurnLog(hass.Hass):
    def initialize(self):
        self.log_file = str(self.args.get("log_file") or Path(__file__).with_name("assist_turn_log.jsonl"))
        self.settle_s = float(self.args.get("settle_s", 5))
        self.poll_every_s = int(self.args.get("poll_every_s", 300))
        self.grace_s = float(self.args.get("grace_s", 600))
        self._lock = asyncio.Lock()
        self._poll_pending = False
        self.listen_state(self._on_satellite_state, "assist_satellite")
        self.run_every(self._on_tick, f"now+{FIRST_POLL_S}", self.poll_every_s)
        self.log(f"Logging Assist turns to {self.log_file}", level="INFO")

    def _on_satellite_state(self, entity, attribute, old, new, kwargs):
        if old == new or self._poll_pending:
            return
        self._poll_pending = True
        self.run_in(self._on_settled, self.settle_s)

    def _on_settled(self, kwargs):
        self._poll_pending = False
        self.create_task(self._poll())

    def _on_tick(self, kwargs):
        self.create_task(self._poll())

    async def _poll(self):
        async with self._lock:
            try:
                turns = await self._new_turns()
                if turns:
                    await self.run_in_executor(append_jsonl, self.log_file, turns)
            except Exception as err:
                self.log(f"Assist turn log poll failed: {err!r}", level="WARNING")
                return
            for turn in turns:
                self.log(summary(turn), level="INFO")

    async def _new_turns(self):
        seen = await self.run_in_executor(read_seen, self.log_file)
        listing = await self._call(type="assist_pipeline/pipeline/list")
        now = datetime.now(timezone.utc)
        turns = []
        for pipeline in (listing or {}).get("pipelines") or []:
            pipeline_id = pipeline.get("id")
            listed = await self._call(type="assist_pipeline/pipeline_debug/list", pipeline_id=pipeline_id)
            for run in (listed or {}).get("pipeline_runs") or []:
                run_id = run.get("pipeline_run_id")
                if run_id in seen:
                    continue
                got = await self._call(type="assist_pipeline/pipeline_debug/get",
                                       pipeline_id=pipeline_id, pipeline_run_id=run_id)
                events = (got or {}).get("events")
                if events is not None and is_final(events, run.get("timestamp"), now, self.grace_s):
                    turns.append(parse_turn(pipeline_id, run, events))
        return sorted(turns, key=lambda turn: turn["timestamp"] or "")

    async def _call(self, **request):
        """The result of one websocket command, or None (logged) when it did not succeed."""
        plugin = self.AD.plugins.get_plugin_object(self.namespace)
        send = getattr(plugin, "websocket_send_json", None)
        if send is None:
            self.log("The HASS plugin has no websocket_send_json; cannot read Assist runs", level="ERROR")
            return None
        reply = await send(**request)
        if not isinstance(reply, dict) or reply.get("success") is not True:
            self.log(f"{request['type']} failed: {_failure(reply)}", level="WARNING")
            return None
        return reply.get("result")
