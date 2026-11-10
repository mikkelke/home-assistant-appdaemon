"""AssistSttFailover: the pipeline follows the primary STT server's health and nothing else changes."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

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

import assist_stt_failover as afo  # noqa: E402

PRIMARY = "stt.fast"
FALLBACK = "stt.local"
LIST = "assist_pipeline/pipeline/list"
UPDATE = "assist_pipeline/pipeline/update"
TOKEN = "not-a-real-token"
PIPELINE_FIELDS = ("conversation_engine", "conversation_language", "language", "name", "stt_engine",
                   "stt_language", "tts_engine", "tts_language", "tts_voice", "wake_word_entity",
                   "wake_word_id")
YAML = Path(__file__).resolve().parents[1] / "assist_stt_failover.yaml"


class FakeWyoming:
    """A Wyoming speech-to-text server that answers like wyoming-faster-whisper, or misbehaves."""

    def __init__(self, mode="transcript", delay=0.0, framing="inline"):
        self.mode = mode
        self.delay = delay
        self.framing = framing
        self.events = []
        self.audio = bytearray()

    async def _read(self, reader):
        line = await reader.readline()
        if not line:
            return None
        header = json.loads(line)
        data = dict(header.get("data") or {})
        if header.get("data_length"):
            data.update(json.loads(await reader.readexactly(header["data_length"])))
        size = header.get("payload_length")
        payload = await reader.readexactly(size) if size else b""
        return header["type"], data, payload

    def _reply(self, kind, data):
        if self.framing == "inline":
            return json.dumps({"type": kind, "data": data}).encode() + b"\n"
        body = json.dumps(data).encode()
        return json.dumps({"type": kind, "data_length": len(body)}).encode() + b"\n" + body

    async def handle(self, reader, writer):
        try:
            while True:
                event = await self._read(reader)
                if event is None:
                    return
                kind, data, payload = event
                self.events.append((kind, data))
                self.audio += payload
                if kind != "audio-stop":
                    continue
                if self.mode == "close":
                    return
                if self.mode == "silent":
                    continue
                await asyncio.sleep(self.delay)
                if self.mode == "garbage":
                    writer.write(b"this is not json\n")
                elif self.mode == "error":
                    writer.write(self._reply("error", {"text": "model failed", "code": "RuntimeError"}))
                else:
                    writer.write(self._reply("transcript", {"text": "The."}))
                await writer.drain()
        finally:
            writer.close()


async def probe_against(server, timeout=2.0):
    listener = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        return await afo.probe_wyoming("127.0.0.1", port, timeout, "en")
    finally:
        listener.close()
        await listener.wait_closed()


class ProbeWyoming(unittest.TestCase):
    def test_streams_half_a_second_of_silence_and_accepts_the_transcript(self):
        server = FakeWyoming()
        probe = asyncio.run(probe_against(server))
        self.assertTrue(probe.ok)
        self.assertEqual(probe.detail, "transcript")
        kinds = [kind for kind, _ in server.events]
        self.assertEqual(kinds[0], "transcribe")
        self.assertEqual(server.events[0][1], {"language": "en"})
        self.assertEqual(kinds[1], "audio-start")
        self.assertEqual(server.events[1][1], {"rate": 16000, "width": 2, "channels": 1})
        self.assertTrue(all(kind == "audio-chunk" for kind in kinds[2:-1]))
        self.assertEqual(kinds[-1], "audio-stop")
        self.assertEqual(len(server.audio), 16000)
        self.assertEqual(set(server.audio), {0})

    def test_a_reply_with_its_data_after_the_header_is_accepted_too(self):
        probe = asyncio.run(probe_against(FakeWyoming(framing="data_length")))
        self.assertTrue(probe.ok)

    def test_an_error_event_fails_the_probe(self):
        probe = asyncio.run(probe_against(FakeWyoming(mode="error")))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, "server error RuntimeError")

    def test_nothing_listening_fails_at_once(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        probe = asyncio.run(afo.probe_wyoming("127.0.0.1", port, 2.0))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, "ConnectionRefusedError")
        self.assertLess(probe.seconds, 1.0)

    def test_a_server_that_never_answers_fails_when_the_timeout_runs_out(self):
        probe = asyncio.run(probe_against(FakeWyoming(mode="silent"), timeout=0.3))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, "no transcript within 0.3 s")
        self.assertLess(probe.seconds, 1.5)

    def test_an_answer_after_the_timeout_is_a_failure(self):
        probe = asyncio.run(probe_against(FakeWyoming(delay=0.6), timeout=0.2))
        self.assertFalse(probe.ok)

    def test_a_server_that_hangs_up_fails(self):
        probe = asyncio.run(probe_against(FakeWyoming(mode="close")))
        self.assertFalse(probe.ok)

    def test_a_reply_that_is_not_json_fails(self):
        probe = asyncio.run(probe_against(FakeWyoming(mode="garbage")))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, "JSONDecodeError")


class VerdictRules(unittest.TestCase):
    def setUp(self):
        self.verdict = afo.Verdict(down_after=2, up_after=3)

    def feed(self, *results):
        return [self.verdict.observe(ok) for ok in results]

    def test_unknown_until_enough_probes_agree(self):
        self.assertEqual(self.feed(True, True), [None, None])
        self.assertEqual(self.feed(True), [True])

    def test_failing_probes_in_a_row_make_it_unhealthy(self):
        self.assertEqual(self.feed(False, False), [None, False])

    def test_one_failure_does_not_flip_a_healthy_verdict(self):
        self.feed(True, True, True)
        self.assertEqual(self.feed(False, True, False, True), [True, True, True, True])

    def test_a_failure_restarts_the_count_of_successes(self):
        self.feed(False, False)
        self.assertEqual(self.feed(True, True, False, True, True), [False, False, False, False, False])
        self.assertEqual(self.feed(True), [True])


class Plan(unittest.TestCase):
    def test_a_failing_primary_moves_the_pipeline_to_the_fallback(self):
        self.assertEqual(afo.plan(PRIMARY, PRIMARY, FALLBACK, False, True), FALLBACK)

    def test_a_healthy_primary_with_a_loaded_entity_takes_the_pipeline_back(self):
        self.assertEqual(afo.plan(FALLBACK, PRIMARY, FALLBACK, True, True), PRIMARY)

    def test_the_pipeline_does_not_go_back_to_an_entity_that_is_not_loaded(self):
        self.assertIsNone(afo.plan(FALLBACK, PRIMARY, FALLBACK, True, False))

    def test_an_unknown_verdict_changes_nothing(self):
        self.assertIsNone(afo.plan(PRIMARY, PRIMARY, FALLBACK, None, True))
        self.assertIsNone(afo.plan(FALLBACK, PRIMARY, FALLBACK, None, True))

    def test_settled_pipelines_stay_put(self):
        self.assertIsNone(afo.plan(PRIMARY, PRIMARY, FALLBACK, True, True))
        self.assertIsNone(afo.plan(FALLBACK, PRIMARY, FALLBACK, False, True))

    def test_another_engine_is_never_touched(self):
        for healthy in (True, False, None):
            self.assertIsNone(afo.plan("stt.chosen_by_hand", PRIMARY, FALLBACK, healthy, True))
            self.assertIsNone(afo.plan(None, PRIMARY, FALLBACK, healthy, True))


def pipeline(engine=PRIMARY, **extra):
    return {
        "id": "pipe1", "name": "Home Assistant", "language": "en",
        "conversation_engine": "conversation.agent", "conversation_language": "en",
        "stt_engine": engine, "stt_language": "en",
        "tts_engine": "tts.voice", "tts_language": "en_US", "tts_voice": "en_US-voice-medium",
        "wake_word_entity": None, "wake_word_id": None, "prefer_local_intents": True, **extra,
    }


def without_nulls(value):
    """What the HASS plugin does to every request before it sends it."""
    if isinstance(value, dict):
        return {key: without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [without_nulls(item) for item in value if item is not None]
    return value


class FakeSocket:
    """A websocket to Home Assistant: login first, then commands answered by the plugin's answer()."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.inbox = [{"type": "auth_required", "ha_version": "test"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def receive_json(self):
        while not self.inbox:
            await asyncio.sleep(0.01)
        return self.inbox.pop(0)

    async def send_json(self, message):
        message = json.loads(json.dumps(message))
        if message["type"] == "auth":
            granted = message.get("access_token") == TOKEN
            self.inbox.append({"type": "auth_ok"} if granted else {"type": "auth_invalid", "message": "no"})
        elif not self.plugin.mute:
            reply = self.plugin.answer({key: value for key, value in message.items() if key != "id"})
            self.inbox.append({"id": message["id"], "type": "result", **reply})


class FakeSession:
    """What the plugin's create_session returns: it opens websockets to the fake Home Assistant."""

    def __init__(self, plugin):
        self.plugin = plugin

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def ws_connect(self, url):
        self.plugin.opened.append(str(url))
        if self.plugin.refuse:
            raise self.plugin.refuse
        return FakeSocket(self.plugin)


class FakePlugin:
    """The HASS plugin and the Home Assistant behind it, as the app meets them.

    websocket_send_json drops null fields like the real plugin does. create_session opens a connection of
    its own, which keeps them. Home Assistant refuses a pipeline update that lacks any of its fields.
    """

    def __init__(self, *pipelines):
        self.pipelines = {item["id"]: item for item in pipelines}
        self.calls = []
        self.sent = []
        self.opened = []
        self.fail = {}
        self.refuse = None
        self.mute = False
        self.config = SimpleNamespace(websocket_url="ws://ha.example/api/websocket",
                                      auth_json={"type": "auth", "access_token": TOKEN})

    def updates(self):
        return [call for call in self.calls if call["type"] == UPDATE]

    def create_session(self):
        return FakeSession(self)

    async def websocket_send_json(self, **request):
        self.sent.append(request)
        return self.answer(without_nulls(request))

    def answer(self, request):
        self.calls.append(request)
        kind = request["type"]
        if kind in self.fail:
            return self.fail[kind]
        if kind == LIST:
            pipelines = [dict(item) for item in self.pipelines.values()]
            return {"success": True, "result": {"pipelines": pipelines, "preferred_pipeline": "pipe1"}}
        if kind == UPDATE:
            fields = {key: value for key, value in request.items() if key not in ("type", "pipeline_id")}
            missing = [key for key in PIPELINE_FIELDS if key not in fields]
            if missing:
                text = "".join(f"required key not provided at '{key}'. Got None\n" for key in missing)
                return {"success": False, "error": {"code": "invalid_format", "message": text}}
            self.pipelines[request["pipeline_id"]] = {"id": request["pipeline_id"], **fields}
            return {"success": True, "result": self.pipelines[request["pipeline_id"]]}
        return {"success": False, "error": {"code": "unknown_command", "message": kind}}


def make_app(plugin, **args):
    app = afo.AssistSttFailover.__new__(afo.AssistSttFailover)
    app.args = {"pipeline_id": "pipe1", "primary_entity": PRIMARY, "fallback_entity": FALLBACK,
                "primary_host": "stt.example", **args}
    app.namespace = "default"
    app.AD = SimpleNamespace(plugins=SimpleNamespace(get_plugin_object=lambda namespace: plugin))
    app.log = MagicMock()
    app.run_every = MagicMock()
    app.get_state = MagicMock(return_value="unknown")
    app.create_task = MagicMock(side_effect=lambda coro: coro.close())
    app.initialize()
    return app


def probe(ok, detail=None):
    return afo.Probe(ok, 0.1, detail or ("transcript" if ok else "ConnectionRefusedError"))


def cycle(app, *results, state="unknown"):
    """Run one cycle per scripted probe result."""
    with patch.object(afo, "probe_wyoming", AsyncMock(side_effect=[probe(ok) for ok in results])):
        for _ in results:
            app._busy = True
            asyncio.run(app._cycle(state))


def logged(app, level):
    return [call.args[0] for call in app.log.call_args_list if call.kwargs.get("level") == level]


class Cycles(unittest.TestCase):
    def test_a_healthy_primary_leaves_the_pipeline_alone(self):
        plugin = FakePlugin(pipeline())
        app = make_app(plugin)
        cycle(app, True, True, True, True)
        self.assertEqual(plugin.updates(), [])

    def test_a_failing_primary_moves_only_the_stt_engine_to_the_fallback(self):
        before = pipeline()
        plugin = FakePlugin(before)
        app = make_app(plugin)
        cycle(app, False)
        self.assertEqual(plugin.updates(), [])
        cycle(app, False)
        self.assertEqual(len(plugin.updates()), 1)
        self.assertEqual(plugin.pipelines["pipe1"], {**before, "stt_engine": FALLBACK})
        self.assertEqual(plugin.updates()[0]["pipeline_id"], "pipe1")
        self.assertNotIn("id", plugin.updates()[0])
        self.assertEqual(logged(app, "WARNING"),
                         [f"{PRIMARY} is not answering (ConnectionRefusedError)"])
        self.assertIn(f"Assist pipeline speech-to-text is now {FALLBACK}", logged(app, "INFO"))

    def test_further_failures_do_not_write_again(self):
        plugin = FakePlugin(pipeline())
        app = make_app(plugin)
        cycle(app, False, False, False, False, False)
        self.assertEqual(len(plugin.updates()), 1)

    def test_the_primary_takes_the_pipeline_back_after_three_good_probes(self):
        plugin = FakePlugin(pipeline())
        app = make_app(plugin)
        cycle(app, False, False)
        cycle(app, True, True)
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], FALLBACK)
        cycle(app, True)
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], PRIMARY)
        self.assertEqual(len(plugin.updates()), 2)
        self.assertIn(f"{PRIMARY} is answering", logged(app, "INFO"))

    def test_a_flapping_primary_does_not_move_the_pipeline_back_and_forth(self):
        plugin = FakePlugin(pipeline())
        app = make_app(plugin)
        cycle(app, False, False)
        cycle(app, True, True, False, True, True, False, True, True)
        self.assertEqual(len(plugin.updates()), 1)
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], FALLBACK)

    def test_the_pipeline_waits_for_the_primary_entity_to_load_before_going_back(self):
        plugin = FakePlugin(pipeline(FALLBACK))
        app = make_app(plugin)
        cycle(app, True, True, True, True, state="unavailable")
        cycle(app, True, state=None)
        self.assertEqual(plugin.updates(), [])
        cycle(app, True, state="unknown")
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], PRIMARY)

    def test_the_fallback_does_not_wait_for_the_primary_entity(self):
        plugin = FakePlugin(pipeline())
        app = make_app(plugin)
        cycle(app, False, False, state="unavailable")
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], FALLBACK)

    def test_a_restart_with_the_pipeline_on_the_fallback_and_a_healthy_primary_goes_back(self):
        plugin = FakePlugin(pipeline(FALLBACK))
        app = make_app(plugin)
        cycle(app, True, True)
        self.assertEqual(plugin.updates(), [])
        cycle(app, True)
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], PRIMARY)

    def test_an_engine_chosen_by_hand_is_left_alone(self):
        plugin = FakePlugin(pipeline("stt.chosen_by_hand"))
        app = make_app(plugin)
        cycle(app, False, False, False, True, True, True)
        self.assertEqual(plugin.updates(), [])

    def test_a_missing_pipeline_is_warned_about_once(self):
        plugin = FakePlugin(pipeline())
        app = make_app(plugin, pipeline_id="gone")
        cycle(app, True, True, True)
        self.assertEqual(plugin.updates(), [])
        self.assertEqual(logged(app, "WARNING"), ["Assist pipeline gone does not exist"])

    def test_a_failed_write_is_warned_about_once_and_retried(self):
        plugin = FakePlugin(pipeline())
        plugin.fail[UPDATE] = {"success": False, "error": {"code": "not_allowed"}}
        app = make_app(plugin)
        cycle(app, False, False, False, False)
        self.assertEqual(len(plugin.updates()), 3)
        self.assertEqual(logged(app, "WARNING").count(f"{UPDATE} failed: not_allowed"), 1)
        del plugin.fail[UPDATE]
        cycle(app, False)
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], FALLBACK)

    def test_a_warning_that_cleared_is_warned_about_again_when_it_returns(self):
        plugin = FakePlugin(pipeline())
        plugin.fail[LIST] = {"success": False, "ad_status": "disconnected"}
        app = make_app(plugin)
        cycle(app, True, True)
        del plugin.fail[LIST]
        cycle(app, True)
        plugin.fail[LIST] = {"success": False, "ad_status": "disconnected"}
        cycle(app, True)
        self.assertEqual(logged(app, "WARNING"), [f"{LIST} failed: disconnected"] * 2)

    def test_home_assistant_answering_nothing_is_not_a_crash(self):
        plugin = FakePlugin(pipeline())
        plugin.fail[LIST] = None
        app = make_app(plugin)
        cycle(app, False, False)
        self.assertEqual(plugin.updates(), [])
        failures = [text for text in logged(app, "WARNING") if "failed" in text]
        self.assertEqual(failures, [f"{LIST} failed: no reply"])

    def test_a_plugin_without_websocket_is_a_warning_not_a_crash(self):
        app = make_app(object())
        cycle(app, False, False)
        missing = [text for text in logged(app, "WARNING") if "websocket_send_json" in text]
        self.assertEqual(len(missing), 1)

    def test_an_unexpected_error_is_logged_and_the_next_cycle_still_runs(self):
        plugin = FakePlugin(pipeline())
        app = make_app(plugin)
        results = [RuntimeError("boom"), probe(False), probe(False)]
        with patch.object(afo, "probe_wyoming", AsyncMock(side_effect=results)):
            for _ in range(3):
                app._busy = True
                asyncio.run(app._cycle("unknown"))
                self.assertFalse(app._busy)
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], FALLBACK)
        self.assertTrue(any("boom" in text for text in logged(app, "WARNING")))


class Writes(unittest.TestCase):
    def test_the_update_goes_over_a_connection_of_its_own_with_the_null_fields(self):
        plugin = FakePlugin(pipeline())
        app = make_app(plugin)
        cycle(app, False, False)
        self.assertEqual(plugin.opened, ["ws://ha.example/api/websocket"])
        self.assertEqual([request["type"] for request in plugin.sent], [LIST, LIST])
        update = plugin.updates()[0]
        self.assertIn("wake_word_entity", update)
        self.assertIsNone(update["wake_word_id"])
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], FALLBACK)

    def test_reading_the_pipeline_needs_no_connection_of_its_own(self):
        plugin = FakePlugin(pipeline())
        cycle(make_app(plugin), True, True, True)
        self.assertEqual(plugin.opened, [])

    def test_a_refused_login_is_warned_about_once_and_retried(self):
        plugin = FakePlugin(pipeline())
        plugin.config.auth_json = {"type": "auth", "access_token": "wrong"}
        app = make_app(plugin)
        cycle(app, False, False, False)
        self.assertEqual(plugin.updates(), [])
        self.assertEqual(len(plugin.opened), 2)
        self.assertEqual(logged(app, "WARNING").count(f"{UPDATE} failed: auth_invalid"), 1)

    def test_a_connection_that_fails_is_warned_about_by_its_kind_and_never_its_details(self):
        plugin = FakePlugin(pipeline())
        plugin.refuse = ConnectionRefusedError(f"Bearer {TOKEN}")
        app = make_app(plugin)
        cycle(app, False, False, False)
        self.assertEqual(logged(app, "WARNING").count(f"{UPDATE} failed: ConnectionRefusedError"), 1)
        self.assertNotIn(TOKEN, repr(app.log.call_args_list))
        plugin.refuse = None
        cycle(app, False)
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], FALLBACK)

    def test_an_update_that_gets_no_answer_gives_up_and_the_next_cycle_runs(self):
        plugin = FakePlugin(pipeline())
        plugin.mute = True
        app = make_app(plugin)
        with patch.object(afo, "WRITE_TIMEOUT_S", 0.05):
            cycle(app, False, False)
        self.assertFalse(app._busy)
        self.assertEqual(logged(app, "WARNING").count(f"{UPDATE} failed: timeout"), 1)
        plugin.mute = False
        cycle(app, False)
        self.assertEqual(plugin.pipelines["pipe1"]["stt_engine"], FALLBACK)

    def test_the_reason_home_assistant_gives_is_in_the_warning(self):
        plugin = FakePlugin(pipeline())
        plugin.fail[UPDATE] = {"success": False, "error": {
            "code": "invalid_format", "message": "required key not provided at 'x'.\nGot None\n"}}
        app = make_app(plugin)
        cycle(app, False, False)
        self.assertEqual(logged(app, "WARNING")[-1],
                         f"{UPDATE} failed: invalid_format: required key not provided at 'x'. Got None")


class Scheduling(unittest.TestCase):
    def test_probes_every_ten_seconds_after_a_short_start_delay(self):
        app = make_app(FakePlugin(pipeline()))
        app.run_every.assert_called_once_with(app._on_tick, "now+5", 10)
        self.assertEqual((app.port, app.probe_timeout_s), (10300, 2.0))
        self.assertEqual((app.verdict.down_after, app.verdict.up_after), (2, 3))

    def test_the_timing_comes_from_the_config(self):
        app = make_app(FakePlugin(pipeline()), interval_s=5, probe_timeout_s=1.5, down_after=3,
                       up_after=4, primary_port=10310)
        app.run_every.assert_called_once_with(app._on_tick, "now+5", 5)
        self.assertEqual((app.port, app.probe_timeout_s), (10310, 1.5))
        self.assertEqual((app.verdict.down_after, app.verdict.up_after), (3, 4))

    def test_missing_settings_are_an_error_and_schedule_nothing(self):
        app = afo.AssistSttFailover.__new__(afo.AssistSttFailover)
        app.args = {"pipeline_id": "pipe1", "primary_entity": PRIMARY}
        app.log = MagicMock()
        app.run_every = MagicMock()
        app.initialize()
        app.run_every.assert_not_called()
        self.assertEqual(logged(app, "ERROR"),
                         ["AssistSttFailover is missing fallback_entity, primary_host"])

    def test_a_tick_runs_one_cycle_with_the_primary_entity_state_and_never_two_at_once(self):
        app = make_app(FakePlugin(pipeline()))
        cycles = []
        app.create_task = cycles.append
        app.get_state.return_value = "unavailable"
        app._on_tick({})
        app._on_tick({})
        app.get_state.assert_called_once_with(PRIMARY)
        self.assertEqual(len(cycles), 1)
        with patch.object(afo, "probe_wyoming", AsyncMock(return_value=probe(True))):
            asyncio.run(cycles[0])
        app._on_tick({})
        self.assertEqual(len(cycles), 2)
        cycles[1].close()

    def test_an_entity_state_that_cannot_be_read_does_not_stop_the_ticks(self):
        app = make_app(FakePlugin(pipeline()))
        cycles = []
        app.create_task = cycles.append
        app.get_state.side_effect = RuntimeError("gone")
        app._on_tick({})
        self.assertEqual(len(cycles), 1)
        cycles[0].close()


class Config(unittest.TestCase):
    @staticmethod
    def load():
        loader = yaml.SafeLoader
        loader.add_constructor("!secret", lambda ld, node: "secret:" + ld.construct_scalar(node))
        return yaml.load(YAML.read_text(encoding="utf-8"), Loader=loader)["AssistSttFailover"]

    def test_the_yaml_names_the_module_and_every_required_setting(self):
        config = self.load()
        self.assertEqual((config["module"], config["class"]),
                         ("assist_stt_failover", "AssistSttFailover"))
        for key in afo.REQUIRED:
            self.assertTrue(config.get(key), key)

    def test_the_host_is_a_secret_and_no_address_is_in_the_repo(self):
        self.assertTrue(self.load()["primary_host"].startswith("secret:"))
        address = re.compile(r"\d+\.\d+\.\d+\.\d+")
        for path in (YAML, Path(afo.__file__)):
            self.assertIsNone(address.search(path.read_text(encoding="utf-8")), path.name)


if __name__ == "__main__":
    unittest.main()
