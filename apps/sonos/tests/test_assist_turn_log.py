"""AssistTurnLog: pipeline runs to durable log lines, never twice, across polls and restarts."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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

import assist_turn_log as atl  # noqa: E402

SATELLITE = "assist_satellite.test_room"
PIPELINE = "pipeline_one"
LIST = "assist_pipeline/pipeline/list"
RUNS = "assist_pipeline/pipeline_debug/list"
GET = "assist_pipeline/pipeline_debug/get"


def event(kind, second, **data):
    stamp = f"2026-01-05T10:00:{second:06.3f}+00:00"
    return {"type": kind, "timestamp": stamp, **({"data": data} if data else {})}


def response(speech, response_type="action_done", **data):
    return {"speech": {"plain": {"speech": speech, "extra_data": None}}, "card": {}, "language": "en",
            "response_type": response_type, "data": data}


def llm_run():
    return [
        event("run-start", 0.0, pipeline=PIPELINE, language="en", conversation_id="conv-1",
              satellite_id=SATELLITE),
        event("stt-start", 0.1, engine="stt.test"),
        event("stt-vad-start", 1.0, timestamp=900),
        event("stt-vad-end", 3.0, timestamp=2900),
        event("stt-end", 4.5, stt_output={"text": "turn on the hallway lamp"}),
        event("intent-start", 4.5, engine="conversation.test_agent", language="en",
              intent_input="turn on the hallway lamp", conversation_id="conv-1",
              satellite_id=SATELLITE, prefer_local_intents=True),
        event("intent-progress", 5.0, chat_log_delta={"role": "assistant"}),
        event("intent-progress", 5.5, chat_log_delta={"tool_calls": [
            {"tool_name": "HassTurnOn", "tool_args": {"name": "hallway lamp"}, "id": "call_1",
             "external": False}]}),
        event("intent-progress", 5.6, chat_log_delta={
            "role": "tool_result", "tool_name": "HassTurnOn", "tool_call_id": "call_1",
            "result": {"data": {"result": "BULKY" * 1000}}}),
        event("intent-progress", 6.0, chat_log_delta={"content": "The"}),
        event("intent-end", 7.0, processed_locally=False, intent_output={
            "response": response("The hallway lamp is on.", success=[], failed=[]),
            "conversation_id": "conv-1", "continue_conversation": False}),
        event("tts-start", 7.0, engine="tts.test", tts_input="The hallway lamp is on."),
        event("tts-end", 7.25, tts_output={"url": "/api/tts_proxy/x.mp3"}),
        event("run-end", 7.5),
    ]


def local_run():
    return [
        event("run-start", 0.0, conversation_id="conv-2", satellite_id=SATELLITE),
        event("stt-start", 0.1),
        event("stt-end", 2.0, stt_output={"text": "what time is it"}),
        event("intent-start", 2.0, engine="conversation.home_assistant", intent_input="what time is it"),
        event("intent-end", 2.1, processed_locally=True, intent_output={
            "response": response("It is ten o'clock.", "query_answer"),
            "conversation_id": "conv-2", "continue_conversation": True}),
        event("tts-start", 2.1), event("tts-end", 2.2), event("run-end", 2.3),
    ]


def silent_run():
    return [
        event("run-start", 0.0, conversation_id="conv-3", satellite_id=SATELLITE),
        event("stt-start", 0.1),
        event("error", 8.0, code="stt-no-text-recognized", message="No text recognized"),
        event("run-end", 8.1),
    ]


def no_match_run():
    return [
        event("run-start", 0.0, satellite_id=SATELLITE),
        event("stt-end", 2.0, stt_output={"text": "mumble"}),
        event("intent-start", 2.0, engine="conversation.home_assistant", intent_input="mumble"),
        event("intent-end", 2.1, processed_locally=True, intent_output={
            "response": response("Sorry, I couldn't understand that.", "error", code="no_intent_match")}),
        event("run-end", 2.2),
    ]


def typed_run():
    return [
        event("run-start", 0.0, conversation_id="conv-4"),
        event("intent-start", 0.1, engine="conversation.home_assistant", intent_input="hello there"),
        event("intent-end", 0.2, processed_locally=True, intent_output={
            "response": response("Hi.", "query_answer")}),
        event("run-end", 0.3),
    ]


def unfinished(events):
    return [item for item in events if item["type"] != "run-end"]


def run(run_id, age_s=3600):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return {"pipeline_run_id": run_id, "timestamp": stamp.isoformat()}


FIXED_RUN = {"pipeline_run_id": "run-1", "timestamp": "2026-01-05T10:00:00+00:00"}


class ParseTurn(unittest.TestCase):
    def parse(self, events, pipeline=PIPELINE):
        return atl.parse_turn(pipeline, FIXED_RUN, events)

    def test_agent_turn_with_tool_call(self):
        turn = self.parse(llm_run())
        self.assertEqual(turn, {
            "v": 1, "run_id": "run-1", "timestamp": "2026-01-05T10:00:00+00:00", "pipeline": PIPELINE,
            "satellite": SATELLITE, "conversation_id": "conv-1", "heard": "turn on the hallway lamp",
            "engine": "conversation.test_agent", "processed_locally": False,
            "tool_calls": [{"name": "HassTurnOn", "args": {"name": "hallway lamp"}}],
            "spoken": "The hallway lamp is on.", "response_type": "action_done", "response_code": None,
            "continue_conversation": False, "error": None, "finished": True,
            "stt_s": 4.4, "stt_latency_s": 1.5, "intent_s": 2.5, "tts_s": 0.25, "total_s": 7.5,
        })
        self.assertNotIn("BULKY", json.dumps(turn))

    def test_local_turn_has_no_tool_calls_and_asks_a_follow_up(self):
        turn = self.parse(local_run())
        self.assertTrue(turn["processed_locally"])
        self.assertEqual(turn["tool_calls"], [])
        self.assertEqual(turn["response_type"], "query_answer")
        self.assertTrue(turn["continue_conversation"])
        self.assertIsNone(turn["stt_latency_s"])

    def test_no_speech_run_keeps_the_pipeline_error(self):
        turn = self.parse(silent_run())
        self.assertEqual(turn["error"], {"code": "stt-no-text-recognized", "message": "No text recognized"})
        self.assertIsNone(turn["heard"])
        self.assertIsNone(turn["spoken"])
        self.assertIsNone(turn["engine"])
        self.assertIsNone(turn["intent_s"])
        self.assertTrue(turn["finished"])

    def test_intent_error_response_keeps_its_code(self):
        turn = self.parse(no_match_run())
        self.assertEqual((turn["response_type"], turn["response_code"]), ("error", "no_intent_match"))
        self.assertIsNone(turn["error"])

    def test_typed_turn_uses_the_text_and_has_no_satellite(self):
        turn = self.parse(typed_run())
        self.assertEqual(turn["heard"], "hello there")
        self.assertIsNone(turn["satellite"])
        self.assertEqual(turn["conversation_id"], "conv-4")
        self.assertIsNone(turn["stt_s"])

    def test_run_without_run_end_is_unfinished(self):
        turn = self.parse(unfinished(llm_run()))
        self.assertFalse(turn["finished"])
        self.assertIsNone(turn["total_s"])
        self.assertEqual(turn["heard"], "turn on the hallway lamp")

    def test_malformed_events_do_not_raise(self):
        junk = [None, "text", 3, {}, {"type": "intent-progress"}, {"type": "intent-progress", "data": None},
                {"type": "intent-progress", "data": {"chat_log_delta": {"tool_calls": "oops"}}},
                {"type": "intent-progress", "data": {"chat_log_delta": {"tool_calls": ["x", None]}}},
                {"type": "stt-end", "data": {"stt_output": None}}, {"type": "error"},
                {"type": "run-start", "timestamp": "not a time"}, {"type": "run-end", "timestamp": None}]
        turn = self.parse(junk)
        self.assertEqual(turn["run_id"], "run-1")
        self.assertEqual(turn["tool_calls"], [])
        self.assertEqual(turn["error"], {"code": None, "message": None})
        self.assertIsNone(self.parse(None)["heard"])


class FinalRuns(unittest.TestCase):
    NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)

    def test_ended_run_is_final_at_once(self):
        self.assertTrue(atl.is_final(llm_run(), "2026-01-05T11:59:59+00:00", self.NOW, 600))

    def test_running_run_waits_until_the_grace_period_has_passed(self):
        events = unfinished(llm_run())
        self.assertFalse(atl.is_final(events, "2026-01-05T11:59:00+00:00", self.NOW, 600))
        self.assertTrue(atl.is_final(events, "2026-01-05T11:50:00+00:00", self.NOW, 600))

    def test_run_with_an_unreadable_start_is_final(self):
        self.assertTrue(atl.is_final(unfinished(llm_run()), None, self.NOW, 600))


class Summary(unittest.TestCase):
    def line(self, events):
        return atl.summary(atl.parse_turn(PIPELINE, FIXED_RUN, events))

    def test_heard_and_said_on_one_line(self):
        line = self.line(llm_run())
        self.assertIn("test_room: heard 'turn on the hallway lamp' -> said 'The hallway lamp is on.'", line)
        self.assertIn("conversation.test_agent, not local, tools HassTurnOn, stt 1.5s, intent 2.5s", line)

    def test_local_turn_says_so_and_skips_timings_it_lacks(self):
        self.assertTrue(self.line(local_run()).endswith("(conversation.home_assistant, local, intent 0.1s)"))

    def test_failures_show_their_code(self):
        quiet = self.line(silent_run())
        self.assertIn("heard nothing -> said nothing", quiet)
        self.assertIn("error stt-no-text-recognized", quiet)
        self.assertIn("error no_intent_match", self.line(no_match_run()))

    def test_line_is_ascii_single_line_and_clipped(self):
        turn = atl.parse_turn(PIPELINE, FIXED_RUN, llm_run())
        turn["heard"] = "caf\u00e9 it\u2019s \u201cd\u00f8r\u201d\nopen"
        turn["spoken"] = "word " * 200
        line = atl.summary(turn)
        self.assertTrue(line.isascii())
        self.assertNotIn("\n", line)
        self.assertIn("heard 'caf\\xe9 it's \"d\\xf8r\" open'", line)
        self.assertIn("...' (", line)
        self.assertLess(len(line), 450)


class LogFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "nested" / "turns.jsonl"

    def test_missing_file_has_no_runs(self):
        self.assertEqual(atl.read_seen(self.path), set())

    def test_append_creates_the_folder_and_writes_one_ascii_line_per_record(self):
        atl.append_jsonl(self.path, [{"run_id": "a", "heard": "caf\u00e9\nbar"}, {"run_id": "b"}])
        atl.append_jsonl(self.path, [{"run_id": "c"}])
        raw = self.path.read_bytes()
        self.assertTrue(raw.isascii())
        lines = raw.decode().splitlines()
        self.assertEqual([json.loads(line)["run_id"] for line in lines], ["a", "b", "c"])
        self.assertEqual(json.loads(lines[0])["heard"], "caf\u00e9\nbar")

    def test_append_after_a_cut_off_line_starts_a_fresh_line(self):
        atl.append_jsonl(self.path, [{"run_id": "a"}])
        with open(self.path, "ab") as handle:
            handle.write(b'{"run_id": "cut')
        atl.append_jsonl(self.path, [{"run_id": "b"}])
        self.assertEqual(atl.read_seen(self.path), {"a", "b"})

    def test_read_seen_skips_lines_that_are_not_records(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'not json\n[1, 2]\n{"other": 1}\n{"run_id": ["x"]}\n\xff\xfe\n{"run_id": "ok"}\n')
        self.assertEqual(atl.read_seen(self.path), {"ok"})

    def test_read_seen_reads_only_the_tail_and_skips_the_cut_line(self):
        records = [{"run_id": name, "pad": "x" * 50} for name in ("a", "b", "c")]
        atl.append_jsonl(self.path, records)
        lines = self.path.read_bytes().splitlines(keepends=True)
        tail = len(lines[1]) + len(lines[2]) + 10
        self.assertEqual(atl.read_seen(self.path, tail_bytes=tail), {"b", "c"})
        self.assertEqual(atl.read_seen(self.path), {"a", "b", "c"})


def ok(result):
    return {"id": 7, "type": "result", "success": True, "result": result, "ad_status": "OK"}


class FakePlugin:
    """What the app uses of the HASS plugin: websocket_send_json, answering like Home Assistant."""

    def __init__(self, pipelines):
        self.pipelines = pipelines
        self.calls = []
        self.fail = {}

    def fetches(self):
        return [call["pipeline_run_id"] for call in self.calls if call["type"] == GET]

    async def websocket_send_json(self, **request):
        self.calls.append(request)
        kind = request["type"]
        if kind in self.fail:
            return self.fail[kind]
        if kind == LIST:
            return ok({"pipelines": [{"id": name, "name": name} for name in self.pipelines],
                       "preferred_pipeline": None})
        if kind == RUNS:
            return ok({"pipeline_runs": [item for item, _ in self.pipelines[request["pipeline_id"]]]})
        for item, events in self.pipelines[request["pipeline_id"]]:
            if item["pipeline_run_id"] == request["pipeline_run_id"]:
                return ok({"events": events})
        return {"success": False, "error": {"code": "not_found", "message": "gone"}}


def make_app(path, plugin, **args):
    app = atl.AssistTurnLog.__new__(atl.AssistTurnLog)
    app.args = {"log_file": str(path), **args}
    app.namespace = "default"
    app.AD = SimpleNamespace(plugins=SimpleNamespace(get_plugin_object=lambda namespace: plugin))
    app.log = MagicMock()
    app.listen_state = MagicMock()
    app.run_every = MagicMock()
    app.run_in = MagicMock()
    app.create_task = MagicMock(side_effect=lambda coro: coro.close())

    async def run_in_executor(func, *func_args):
        return func(*func_args)

    app.run_in_executor = run_in_executor
    app.initialize()
    return app


def poll(app):
    asyncio.run(app._poll())


class Polling(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "turns.jsonl"

    def written(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def warnings(self, app):
        return [call.args[0] for call in app.log.call_args_list if call.kwargs.get("level") == "WARNING"]

    def test_new_runs_are_written_in_time_order_and_logged(self):
        plugin = FakePlugin({
            "p1": [(run("r2", 3600), llm_run()), (run("r1", 7200), local_run())],
            "p2": [(run("r3", 1800), silent_run())],
        })
        app = make_app(self.path, plugin)
        poll(app)
        self.assertEqual([turn["run_id"] for turn in self.written()], ["r1", "r2", "r3"])
        self.assertEqual([turn["pipeline"] for turn in self.written()], ["p1", "p1", "p2"])
        logged = [call.args[0] for call in app.log.call_args_list if call.args[0].startswith("Assist turn")]
        self.assertEqual(len(logged), 3)
        self.assertIn("heard 'what time is it'", logged[0])
        self.assertIn("heard 'turn on the hallway lamp' -> said 'The hallway lamp is on.'", logged[1])

    def test_a_run_is_never_written_twice_by_polls_or_restarts(self):
        plugin = FakePlugin({"p1": [(run("r1", 3600), llm_run())]})
        app = make_app(self.path, plugin)
        poll(app)
        self.assertEqual(plugin.fetches(), ["r1"])
        poll(app)
        self.assertEqual(plugin.fetches(), ["r1"])
        restarted = make_app(self.path, plugin)
        poll(restarted)
        self.assertEqual(plugin.fetches(), ["r1"])
        plugin.pipelines["p1"].append((run("r2", 60), local_run()))
        poll(restarted)
        self.assertEqual([turn["run_id"] for turn in self.written()], ["r1", "r2"])

    def test_a_running_run_waits_for_its_end_and_is_then_written_once(self):
        plugin = FakePlugin({"p1": [(run("r1", 5), unfinished(llm_run()))]})
        app = make_app(self.path, plugin)
        poll(app)
        self.assertEqual(self.written(), [])
        plugin.pipelines["p1"] = [(run("r1", 5), llm_run())]
        poll(app)
        poll(app)
        self.assertEqual([(turn["run_id"], turn["finished"]) for turn in self.written()], [("r1", True)])

    def test_a_run_that_never_ends_is_written_as_unfinished(self):
        plugin = FakePlugin({"p1": [(run("r1", 3600), unfinished(llm_run()))]})
        app = make_app(self.path, plugin)
        poll(app)
        self.assertEqual([(turn["run_id"], turn["finished"]) for turn in self.written()], [("r1", False)])

    def test_failed_listing_writes_nothing_then_recovers(self):
        plugin = FakePlugin({"p1": [(run("r1", 3600), llm_run())]})
        plugin.fail[LIST] = {"success": False, "error": {"code": "unauthorized", "message": "no"}}
        app = make_app(self.path, plugin)
        poll(app)
        self.assertEqual(self.written(), [])
        self.assertEqual(self.warnings(app), [f"{LIST} failed: unauthorized"])
        del plugin.fail[LIST]
        poll(app)
        self.assertEqual([turn["run_id"] for turn in self.written()], ["r1"])

    def test_websocket_not_connected_is_a_warning_not_a_crash(self):
        plugin = FakePlugin({"p1": [(run("r1", 3600), llm_run())]})
        plugin.fail[LIST] = None
        app = make_app(self.path, plugin)
        poll(app)
        self.assertEqual(self.warnings(app), [f"{LIST} failed: no reply"])
        self.assertEqual(self.written(), [])

    def test_failed_run_fetch_skips_only_that_run_and_retries_it(self):
        plugin = FakePlugin({"p1": [(run("gone", 3600), llm_run()), (run("r2", 3000), local_run())]})
        original = plugin.websocket_send_json

        async def fetch_fails_for_gone(**request):
            if request["type"] == GET and request["pipeline_run_id"] == "gone":
                plugin.calls.append(request)
                return {"success": False, "error": {"code": "not_found", "message": "gone"}}
            return await original(**request)

        plugin.websocket_send_json = fetch_fails_for_gone
        app = make_app(self.path, plugin)
        poll(app)
        self.assertEqual([turn["run_id"] for turn in self.written()], ["r2"])
        poll(app)
        self.assertEqual(plugin.fetches(), ["gone", "r2", "gone"])

    def test_plugin_without_websocket_is_an_error_not_a_crash(self):
        app = make_app(self.path, object())
        poll(app)
        self.assertEqual(self.written(), [])
        self.assertTrue(any(call.kwargs.get("level") == "ERROR" for call in app.log.call_args_list))

    def fail_first_append(self, app, after_writing):
        real = app.run_in_executor
        failures = []

        async def flaky(func, *func_args):
            if func is not atl.append_jsonl or failures:
                return await real(func, *func_args)
            failures.append(1)
            if after_writing:
                await real(func, *func_args)
            raise OSError("disk full")

        app.run_in_executor = flaky

    def test_a_write_that_failed_is_retried_and_written_once(self):
        app = make_app(self.path, FakePlugin({"p1": [(run("r1", 3600), llm_run())]}))
        self.fail_first_append(app, after_writing=False)
        poll(app)
        self.assertEqual(self.written(), [])
        self.assertEqual(len(self.warnings(app)), 1)
        poll(app)
        poll(app)
        self.assertEqual([turn["run_id"] for turn in self.written()], ["r1"])

    def test_a_write_that_landed_before_the_failure_is_not_repeated(self):
        app = make_app(self.path, FakePlugin({"p1": [(run("r1", 3600), llm_run())]}))
        self.fail_first_append(app, after_writing=True)
        poll(app)
        poll(app)
        self.assertEqual([turn["run_id"] for turn in self.written()], ["r1"])


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.app = make_app(Path(self.dir.name) / "turns.jsonl", FakePlugin({}))

    def change(self, old, new):
        self.app._on_satellite_state(SATELLITE, "state", old, new, {})

    def test_listens_to_every_satellite_and_polls_on_a_timer_as_fallback(self):
        self.app.listen_state.assert_called_once_with(self.app._on_satellite_state, "assist_satellite")
        self.app.run_every.assert_called_once_with(self.app._on_tick, "now+10", 300)

    def test_a_burst_of_state_changes_schedules_one_poll(self):
        for old, new in (("idle", "listening"), ("listening", "processing"), ("processing", "responding")):
            self.change(old, new)
        self.app.run_in.assert_called_once_with(self.app._on_settled, 5.0)
        self.app.create_task.assert_not_called()
        self.app._on_settled({})
        self.app.create_task.assert_called_once()
        self.change("responding", "idle")
        self.assertEqual(self.app.run_in.call_count, 2)

    def test_unchanged_state_schedules_nothing(self):
        self.change("idle", "idle")
        self.app.run_in.assert_not_called()

    def test_the_fallback_timer_polls(self):
        self.app._on_tick({})
        self.app.create_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
