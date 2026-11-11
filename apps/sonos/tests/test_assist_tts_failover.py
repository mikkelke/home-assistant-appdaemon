"""AssistTtsFailover: the pipeline's voice and the announcement voice follow the primary TTS server's health."""

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
import assist_tts_failover as atf  # noqa: E402
from test_assist_stt_failover import FakePlugin, pipeline as stt_pipeline  # noqa: E402

PRIMARY = "tts.fast"
FALLBACK = "tts.local"
UPDATE = "assist_pipeline/pipeline/update"
VOICES = {"primary_voice": "fast-voice", "primary_language": "en",
          "fallback_voice": "local-voice", "fallback_language": "en_US"}
PRIMARY_VOICE = {"tts_platform": "fast", "options": {"voice": "fast-voice"}, "language": "en"}
FALLBACK_VOICE = {"tts_platform": "local", "options": {"voice": "local-voice"}, "language": "en_US"}
SONOS = Path(__file__).resolve().parents[1]
YAML = SONOS / "assist_tts_failover.yaml"


class FakeTts:
    """A Wyoming text-to-speech server that answers like wyoming-piper, or misbehaves."""

    def __init__(self, mode="audio", delay=0.0, framing="inline", chunks=3):
        self.mode = mode
        self.delay = delay
        self.framing = framing
        self.chunks = chunks
        self.events = []

    async def _read(self, reader):
        line = await reader.readline()
        if not line:
            return None
        header = json.loads(line)
        data = dict(header.get("data") or {})
        if header.get("data_length"):
            data.update(json.loads(await reader.readexactly(header["data_length"])))
        return header["type"], data

    def _event(self, kind, data=None, payload=b""):
        header = {"type": kind}
        if self.framing == "inline":
            header["data"] = data or {}
            body = b""
        else:
            body = json.dumps(data or {}).encode()
            header["data_length"] = len(body)
        if payload:
            header["payload_length"] = len(payload)
        return json.dumps(header).encode() + b"\n" + body + payload

    async def handle(self, reader, writer):
        try:
            event = await self._read(reader)
            if event is None:
                return
            self.events.append(event)
            if self.mode == "close":
                return
            if self.mode == "silent":
                await reader.read()
                return
            await asyncio.sleep(self.delay)
            audio = {"rate": 22050, "width": 2, "channels": 1}
            if self.mode == "garbage":
                writer.write(b"this is not json\n")
            elif self.mode == "error":
                writer.write(self._event("error", {"text": "voice not found", "code": "RuntimeError"}))
            elif self.mode == "no_start":
                writer.write(self._event("audio-stop"))
            else:
                writer.write(self._event("audio-start", audio))
                for _ in range(self.chunks):
                    writer.write(self._event("audio-chunk", audio, bytes(1024)))
                if self.mode != "no_stop":
                    writer.write(self._event("audio-stop"))
            await writer.drain()
            if self.mode == "no_stop":
                await reader.read()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()


async def probe_against(server, timeout=2.0, voice="fast-voice"):
    listener = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        return await atf.probe_synthesize("127.0.0.1", port, timeout, voice)
    finally:
        listener.close()
        await listener.wait_closed()


class ProbeSynthesize(unittest.TestCase):
    def test_asks_for_a_short_text_in_the_primary_voice_and_accepts_the_audio(self):
        server = FakeTts()
        probe = asyncio.run(probe_against(server))
        self.assertTrue(probe.ok)
        self.assertEqual(probe.detail, "audio")
        self.assertEqual(server.events, [("synthesize", {"text": atf.PROBE_TEXT,
                                                         "voice": {"name": "fast-voice"}})])

    def test_the_chunks_between_the_start_and_the_end_are_read_and_dropped(self):
        for chunks in (0, 1, 40):
            with self.subTest(chunks=chunks):
                self.assertTrue(asyncio.run(probe_against(FakeTts(chunks=chunks))).ok)

    def test_a_reply_with_its_data_after_the_header_is_accepted_too(self):
        self.assertTrue(asyncio.run(probe_against(FakeTts(framing="data_length"))).ok)

    def test_an_error_event_fails_the_probe(self):
        probe = asyncio.run(probe_against(FakeTts(mode="error")))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, "server error RuntimeError")

    def test_an_end_without_a_start_fails(self):
        probe = asyncio.run(probe_against(FakeTts(mode="no_start")))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, "audio-stop without audio-start")

    def test_nothing_listening_fails_at_once(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        probe = asyncio.run(atf.probe_synthesize("127.0.0.1", port, 2.0, "fast-voice"))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, "ConnectionRefusedError")
        self.assertLess(probe.seconds, 1.0)

    def test_a_server_that_never_answers_fails_when_the_timeout_runs_out(self):
        probe = asyncio.run(probe_against(FakeTts(mode="silent"), timeout=0.3))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, "no audio within 0.3 s")
        self.assertLess(probe.seconds, 1.5)

    def test_audio_that_never_ends_fails_when_the_timeout_runs_out(self):
        probe = asyncio.run(probe_against(FakeTts(mode="no_stop"), timeout=0.3))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, "no audio within 0.3 s")

    def test_audio_that_ends_after_the_timeout_is_a_failure(self):
        probe = asyncio.run(probe_against(FakeTts(delay=0.6), timeout=0.2))
        self.assertFalse(probe.ok)

    def test_a_server_that_hangs_up_fails(self):
        self.assertFalse(asyncio.run(probe_against(FakeTts(mode="close"))).ok)

    def test_a_reply_that_is_not_json_fails(self):
        probe = asyncio.run(probe_against(FakeTts(mode="garbage")))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, "JSONDecodeError")


def pipeline(engine=PRIMARY, voice="fast-voice", language="en", **extra):
    return stt_pipeline(tts_engine=engine, tts_voice=voice, tts_language=language, **extra)


def make_app(plugin, **args):
    app = atf.AssistTtsFailover.__new__(atf.AssistTtsFailover)
    app.args = {"pipeline_id": "pipe1", "primary_entity": PRIMARY, "fallback_entity": FALLBACK,
                "primary_host": "tts.example", **VOICES, **args}
    app.namespace = "default"
    app.AD = SimpleNamespace(plugins=SimpleNamespace(get_plugin_object=lambda namespace: plugin))
    app.log = MagicMock()
    app.run_every = MagicMock()
    app.run_in = AsyncMock(return_value="handle")
    app.get_state = MagicMock(return_value="unknown")
    app.get_app = MagicMock(return_value=None)
    app.create_task = MagicMock(side_effect=lambda coro: coro.close())
    app.initialize()
    return app


def probe(ok, detail=None):
    return afo.Probe(ok, 0.1, detail or ("audio" if ok else "ConnectionRefusedError"))


def cycle(app, *results, state="unknown"):
    """Run one cycle per scripted probe result."""
    with patch.object(atf, "probe_synthesize", AsyncMock(side_effect=[probe(ok) for ok in results])):
        for _ in results:
            app._busy = True
            asyncio.run(app._cycle(state))


def logged(app, level):
    return [call.args[0] for call in app.log.call_args_list if call.kwargs.get("level") == level]


class Cycles(unittest.TestCase):
    def test_the_probe_asks_the_primary_server_for_the_primary_voice(self):
        app = make_app(FakePlugin(pipeline()))
        with patch.object(atf, "probe_synthesize", AsyncMock(return_value=probe(True))) as mocked:
            asyncio.run(app._cycle("unknown"))
        mocked.assert_awaited_once_with("tts.example", 10200, 2.0, "fast-voice")

    def test_a_healthy_primary_leaves_the_pipeline_alone(self):
        plugin = FakePlugin(pipeline())
        cycle(make_app(plugin), True, True, True, True)
        self.assertEqual(plugin.updates(), [])

    def test_a_failing_primary_moves_only_the_three_tts_fields_to_the_fallback(self):
        before = pipeline()
        plugin = FakePlugin(before)
        app = make_app(plugin)
        cycle(app, False)
        self.assertEqual(plugin.updates(), [])
        cycle(app, False)
        self.assertEqual(len(plugin.updates()), 1)
        self.assertEqual(plugin.pipelines["pipe1"],
                         {**before, "tts_engine": FALLBACK, "tts_voice": "local-voice",
                          "tts_language": "en_US"})
        update = plugin.updates()[0]
        self.assertEqual(update["pipeline_id"], "pipe1")
        self.assertNotIn("id", update)
        changed = {key for key, value in update.items()
                   if key not in ("type", "pipeline_id") and value != before[key]}
        self.assertEqual(changed, {"tts_engine", "tts_voice", "tts_language"})
        self.assertIn("wake_word_entity", update)
        self.assertIsNone(update["wake_word_id"])
        self.assertEqual(logged(app, "WARNING"),
                         [f"{PRIMARY} is not answering (ConnectionRefusedError)"])
        self.assertIn(f"Assist pipeline text-to-speech is now {FALLBACK}", logged(app, "INFO"))

    def test_the_speech_to_text_side_of_the_pipeline_is_never_touched(self):
        before = pipeline(stt_engine="stt.chosen", stt_language="da")
        plugin = FakePlugin(before)
        cycle(make_app(plugin), False, False)
        after = plugin.pipelines["pipe1"]
        self.assertEqual((after["stt_engine"], after["stt_language"]), ("stt.chosen", "da"))

    def test_the_primary_takes_the_pipeline_and_its_voice_back_after_three_good_probes(self):
        before = pipeline()
        plugin = FakePlugin(before)
        app = make_app(plugin)
        cycle(app, False, False)
        cycle(app, True, True)
        self.assertEqual(plugin.pipelines["pipe1"]["tts_engine"], FALLBACK)
        cycle(app, True)
        self.assertEqual(plugin.pipelines["pipe1"], before)
        self.assertEqual(len(plugin.updates()), 2)
        self.assertIn(f"{PRIMARY} is answering", logged(app, "INFO"))

    def test_a_flapping_primary_does_not_move_the_pipeline_back_and_forth(self):
        plugin = FakePlugin(pipeline())
        app = make_app(plugin)
        cycle(app, False, False)
        cycle(app, True, True, False, True, True, False, True, True)
        self.assertEqual(len(plugin.updates()), 1)

    def test_the_pipeline_waits_for_the_primary_entity_to_load_before_going_back(self):
        plugin = FakePlugin(pipeline(FALLBACK, "local-voice", "en_US"))
        app = make_app(plugin)
        cycle(app, True, True, True, True, state="unavailable")
        self.assertEqual(plugin.updates(), [])
        cycle(app, True, state="unknown")
        self.assertEqual(plugin.pipelines["pipe1"]["tts_engine"], PRIMARY)
        self.assertEqual(plugin.pipelines["pipe1"]["tts_voice"], "fast-voice")

    def test_an_engine_chosen_by_hand_is_left_alone(self):
        plugin = FakePlugin(pipeline("tts.chosen_by_hand", "hand", "da"))
        cycle(make_app(plugin), False, False, False, True, True, True)
        self.assertEqual(plugin.updates(), [])

    def test_an_unknown_verdict_changes_nothing(self):
        plugin = FakePlugin(pipeline(FALLBACK, "local-voice", "en_US"))
        cycle(make_app(plugin), True, True)
        self.assertEqual(plugin.updates(), [])



class AnnouncementVoice(unittest.TestCase):
    def app(self, **kwargs):
        return make_app(FakePlugin(pipeline(**kwargs)))

    def test_an_unknown_verdict_counts_as_the_primary(self):
        self.assertEqual(self.app().announcement_voice(), PRIMARY_VOICE)

    def test_a_healthy_primary_voices_the_announcement(self):
        app = self.app()
        cycle(app, True, True, True)
        self.assertEqual(app.announcement_voice(), PRIMARY_VOICE)

    def test_a_down_primary_hands_the_announcement_to_the_fallback_voice(self):
        app = self.app()
        cycle(app, False)
        self.assertEqual(app.announcement_voice(), PRIMARY_VOICE)
        cycle(app, False)
        self.assertEqual(app.announcement_voice(), FALLBACK_VOICE)

    def test_the_primary_voice_comes_back_with_the_verdict(self):
        app = self.app()
        cycle(app, False, False, True, True)
        self.assertEqual(app.announcement_voice(), FALLBACK_VOICE)
        cycle(app, True)
        self.assertEqual(app.announcement_voice(), PRIMARY_VOICE)

    def test_it_follows_the_probes_and_not_the_pipeline(self):
        app = self.app(engine="tts.chosen_by_hand", voice="hand", language="da")
        cycle(app, True, True, True)
        self.assertEqual(app.announcement_voice(), PRIMARY_VOICE)
        cycle(app, False, False)
        self.assertEqual(app.announcement_voice(), FALLBACK_VOICE)
        parked = make_app(FakePlugin(pipeline(FALLBACK, "local-voice", "en_US")))
        cycle(parked, True, True, True, state="unavailable")
        self.assertEqual(parked.announcement_voice(), PRIMARY_VOICE)

    def test_chime_tts_gets_the_engine_without_its_domain_and_the_pipeline_the_entity_id(self):
        plugin = FakePlugin(pipeline())
        app = make_app(plugin)
        self.assertEqual(app.announcement_voice()["tts_platform"], "fast")
        cycle(app, False, False)
        self.assertEqual(app.announcement_voice()["tts_platform"], "local")
        self.assertEqual(plugin.pipelines["pipe1"]["tts_engine"], FALLBACK)

    def test_every_call_returns_a_dict_of_its_own(self):
        app = self.app()
        app.announcement_voice()["options"]["voice"] = "changed"
        self.assertEqual(app.announcement_voice(), PRIMARY_VOICE)

    def test_the_voices_to_render_ahead_are_both_while_the_primary_is_not_known_to_be_down(self):
        app = self.app()
        self.assertEqual(app.precache_voices(), [PRIMARY_VOICE, FALLBACK_VOICE])
        cycle(app, True, True, True)
        self.assertEqual(app.precache_voices(), [PRIMARY_VOICE, FALLBACK_VOICE])

    def test_only_the_fallback_is_rendered_ahead_while_the_primary_is_down(self):
        app = self.app()
        cycle(app, False, False)
        self.assertEqual(app.precache_voices(), [FALLBACK_VOICE])


class Recovery(unittest.TestCase):
    """A primary that comes back asks SonosNotifier to render its announcements again."""

    def awaited(self, app):
        return [call.args for call in app.run_in.await_args_list]

    def test_the_verdict_flipping_back_to_healthy_asks_for_a_render_on_a_worker_thread(self):
        app = make_app(FakePlugin(pipeline()))
        cycle(app, False, False, True, True)
        self.assertEqual(self.awaited(app), [])
        cycle(app, True)
        self.assertEqual(self.awaited(app), [(app._precache_announcements, 0)])

    def test_nothing_else_asks(self):
        app = make_app(FakePlugin(pipeline()))
        cycle(app, True, True, True, True, False, True, False, False, False)
        self.assertEqual(self.awaited(app), [])

    def test_the_callback_calls_the_notifiers_precache(self):
        app = make_app(FakePlugin(pipeline()))
        notifier = MagicMock()
        app.get_app.return_value = notifier
        app._precache_announcements({})
        app.get_app.assert_called_once_with("SonosNotifier")
        notifier.precache.assert_called_once_with()

    def test_a_missing_notifier_is_fine(self):
        app = make_app(FakePlugin(pipeline()))
        app.get_app.return_value = None
        app._precache_announcements({})
        self.assertEqual(logged(app, "WARNING"), [])

    def test_a_notifier_that_fails_is_a_warning_and_not_a_crash(self):
        app = make_app(FakePlugin(pipeline()))
        app.get_app.return_value = MagicMock(precache=MagicMock(side_effect=RuntimeError("boom")))
        app._precache_announcements({})
        self.assertTrue(any("boom" in text for text in logged(app, "WARNING")))
        app.get_app.side_effect = RuntimeError("gone")
        app._precache_announcements({})
        self.assertTrue(any("gone" in text for text in logged(app, "WARNING")))

    def test_a_schedule_that_fails_does_not_stop_the_cycle(self):
        app = make_app(FakePlugin(pipeline()))
        app.run_in.side_effect = RuntimeError("no scheduler")
        cycle(app, False, False, True, True, True)
        self.assertTrue(any("no scheduler" in text for text in logged(app, "WARNING")))
        self.assertFalse(app._busy)


class Scheduling(unittest.TestCase):
    def test_probes_every_ten_seconds_after_a_short_start_delay(self):
        app = make_app(FakePlugin(pipeline()))
        app.run_every.assert_called_once_with(app._on_tick, "now+5", 10)
        self.assertEqual((app.port, app.probe_timeout_s), (10200, 2.0))
        self.assertEqual((app.verdict.down_after, app.verdict.up_after), (2, 3))
        self.assertIn(f"Assist text-to-speech: {PRIMARY} while it answers, else {FALLBACK}",
                      logged(app, "INFO"))

    def test_the_timing_comes_from_the_config(self):
        app = make_app(FakePlugin(pipeline()), interval_s=5, probe_timeout_s=1.5, down_after=3,
                       up_after=4, primary_port=10210)
        app.run_every.assert_called_once_with(app._on_tick, "now+5", 5)
        self.assertEqual((app.port, app.probe_timeout_s), (10210, 1.5))
        self.assertEqual((app.verdict.down_after, app.verdict.up_after), (3, 4))

    def test_missing_settings_are_an_error_and_schedule_nothing(self):
        app = atf.AssistTtsFailover.__new__(atf.AssistTtsFailover)
        app.args = {"pipeline_id": "pipe1", "primary_entity": PRIMARY, "primary_voice": "fast-voice"}
        app.log = MagicMock()
        app.run_every = MagicMock()
        app.initialize()
        app.run_every.assert_not_called()
        self.assertEqual(logged(app, "ERROR"), [
            "AssistTtsFailover is missing fallback_entity, primary_host, primary_language, "
            "fallback_voice, fallback_language"])

    def test_a_tick_runs_one_cycle_with_the_primary_entity_state_and_never_two_at_once(self):
        app = make_app(FakePlugin(pipeline()))
        cycles = []
        app.create_task = cycles.append
        app.get_state.return_value = "unavailable"
        app._on_tick({})
        app._on_tick({})
        app.get_state.assert_called_once_with(PRIMARY)
        self.assertEqual(len(cycles), 1)
        with patch.object(atf, "probe_synthesize", AsyncMock(return_value=probe(True))):
            asyncio.run(cycles[0])
        app._on_tick({})
        self.assertEqual(len(cycles), 2)
        cycles[1].close()


class Config(unittest.TestCase):
    @staticmethod
    def load(path=YAML, name="AssistTtsFailover"):
        class Loader(yaml.SafeLoader):
            pass

        Loader.add_constructor("!secret", lambda ld, node: "secret:" + ld.construct_scalar(node))
        return yaml.load(path.read_text(encoding="utf-8"), Loader=Loader)[name]

    def test_the_yaml_names_the_module_and_every_required_setting(self):
        config = self.load()
        self.assertEqual((config["module"], config["class"]),
                         ("assist_tts_failover", "AssistTtsFailover"))
        self.assertEqual(config["log"], "sonos_log")
        for key in atf.AssistTtsFailover.REQUIRED:
            self.assertTrue(config.get(key), key)

    def test_the_engines_voices_and_languages_are_the_ones_the_house_runs(self):
        config = self.load()
        self.assertEqual((config["primary_entity"], config["primary_voice"], config["primary_language"]),
                         ("tts.acer_tts", "af_heart", "en"))
        self.assertEqual((config["fallback_entity"], config["fallback_voice"], config["fallback_language"]),
                         ("tts.piper", "en_US-hfc_female-medium", "en_US"))
        self.assertEqual(config["primary_port"], 10210)

    def test_it_watches_the_same_pipeline_as_the_speech_to_text_failover(self):
        stt = self.load(SONOS / "assist_stt_failover.yaml", "AssistSttFailover")
        self.assertEqual(self.load()["pipeline_id"], stt["pipeline_id"])
        self.assertEqual(self.load()["primary_host"], stt["primary_host"])

    def test_the_host_is_a_secret_and_no_address_is_in_the_repo(self):
        self.assertTrue(self.load()["primary_host"].startswith("secret:"))
        address = re.compile(r"\d+\.\d+\.\d+\.\d+")
        for path in (YAML, Path(atf.__file__)):
            self.assertIsNone(address.search(path.read_text(encoding="utf-8")), path.name)

    def test_the_shipped_config_starts_the_app(self):
        app = make_app(FakePlugin(pipeline()), **{key: value for key, value in self.load().items()
                                                  if key not in ("module", "class", "log")})
        app.run_every.assert_called_once()
        self.assertEqual(logged(app, "ERROR"), [])
        self.assertEqual(app.announcement_voice(),
                         {"tts_platform": "acer_tts", "options": {"voice": "af_heart"},
                          "language": "en"})
        self.assertEqual(app.precache_voices()[1],
                         {"tts_platform": "piper", "options": {"voice": "en_US-hfc_female-medium"},
                          "language": "en_US"})


if __name__ == "__main__":
    unittest.main()
