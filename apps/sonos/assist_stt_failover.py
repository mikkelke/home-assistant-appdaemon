"""AssistSttFailover: Assist speech-to-text on the fast server while it answers, on the local one otherwise.

Home Assistant's Wyoming STT entity does not go unavailable when its server stops after setup. It only
does when the config entry fails to load, for example after a restart with the server down. So the entity
cannot be the health signal. Every interval_s this app streams half a second of silence to the primary
Wyoming server and expects a transcript within probe_timeout_s, which is the whole path a voice turn uses.
down_after failures in a row move the pipeline's stt_engine to fallback_entity; up_after successes in a
row, with the primary entity loaded in Home Assistant, move it back.

Only stt_engine is changed, on a pipeline read just before the write. An engine other than the two
configured is somebody's manual choice and is left alone; disable the app to pin one of them.
"""

import asyncio
import json
import time
from collections import namedtuple

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

RATE, WIDTH, CHANNELS = 16000, 2, 1
CHUNK_BYTES = 2048
PROBE_SECONDS = 0.5
FIRST_CYCLE_S = 5
WRITE_TIMEOUT_S = 10
REQUIRED = ("pipeline_id", "primary_entity", "fallback_entity", "primary_host")

Probe = namedtuple("Probe", "ok seconds detail")


def _event(kind, data=None, payload=b""):
    body = json.dumps(data).encode() if data is not None else b""
    header = {"type": kind}
    if body:
        header["data_length"] = len(body)
    if payload:
        header["payload_length"] = len(payload)
    return json.dumps(header).encode() + b"\n" + body + payload


async def _read_event(reader):
    line = await reader.readline()
    if not line:
        raise ConnectionError("closed")
    header = json.loads(line)
    data = dict(header.get("data") or {})
    if header.get("data_length"):
        data.update(json.loads(await reader.readexactly(header["data_length"])))
    if header.get("payload_length"):
        await reader.readexactly(header["payload_length"])
    return header["type"], data


async def run_probe(host, port, timeout, exchange, expected):
    """Run exchange(reader, writer) on a new connection to a Wyoming server; it returns (ok, detail).

    `expected` names what the server should send back, for the timeout's detail.
    """
    started = time.monotonic()

    async def connected():
        reader, writer = await asyncio.open_connection(host, port)
        try:
            return await exchange(reader, writer)
        finally:
            writer.close()

    try:
        ok, detail = await asyncio.wait_for(connected(), timeout)
    except TimeoutError:
        ok, detail = False, f"no {expected} within {timeout:g} s"
    except (OSError, ValueError, KeyError, EOFError) as err:
        ok, detail = False, type(err).__name__
    return Probe(ok, round(time.monotonic() - started, 3), detail)


async def probe_wyoming(host, port, timeout, language="en"):
    """Stream silence to a Wyoming STT server and wait for its transcript."""

    async def exchange(reader, writer):
        audio = {"rate": RATE, "width": WIDTH, "channels": CHANNELS}
        writer.write(_event("transcribe", {"language": language}))
        writer.write(_event("audio-start", audio))
        silence = bytes(int(RATE * WIDTH * PROBE_SECONDS))
        for start in range(0, len(silence), CHUNK_BYTES):
            writer.write(_event("audio-chunk", audio, silence[start:start + CHUNK_BYTES]))
        writer.write(_event("audio-stop", {}))
        await writer.drain()
        while True:
            kind, data = await _read_event(reader)
            if kind == "transcript":
                return True, "transcript"
            if kind == "error":
                return False, "server error " + str(data.get("code") or "")[:40]

    return await run_probe(host, port, timeout, exchange, "transcript")


class Verdict:
    """The primary server's health, debounced: None until enough probes in a row agree."""

    def __init__(self, down_after, up_after):
        self.down_after = down_after
        self.up_after = up_after
        self.healthy = None
        self._good = 0
        self._bad = 0

    def observe(self, ok):
        self._good, self._bad = (self._good + 1, 0) if ok else (0, self._bad + 1)
        if self._good >= self.up_after:
            self.healthy = True
        elif self._bad >= self.down_after:
            self.healthy = False
        return self.healthy


def plan(engine, primary, fallback, healthy, primary_loaded):
    """The engine the pipeline should move to, or None to leave it as it is."""
    if engine == primary and healthy is False:
        return fallback
    if engine == fallback and healthy is True and primary_loaded:
        return primary
    return None


def _failure(reply):
    if not isinstance(reply, dict):
        return "no reply"
    error = reply.get("error")
    if isinstance(error, dict) and error.get("code"):
        parts = [str(error[key]) for key in ("code", "message") if error.get(key)]
        return " ".join(": ".join(parts).split())[:160]
    return str(reply.get("ad_status") or "unsuccessful")


class AssistSttFailover(hass.Hass):
    """The failover for speech-to-text.

    AssistTtsFailover reuses it for text-to-speech: it sets the class attributes and overrides _probe and _fields.
    """

    SERVICE = "speech-to-text"
    ENGINE_FIELD = "stt_engine"
    DEFAULT_PORT = 10300
    REQUIRED = REQUIRED

    def initialize(self):
        missing = [key for key in self.REQUIRED if not self.args.get(key)]
        if missing:
            self.log(f"{type(self).__name__} is missing " + ", ".join(missing), level="ERROR")
            return
        self.pipeline_id = self.args["pipeline_id"]
        self.primary = self.args["primary_entity"]
        self.fallback = self.args["fallback_entity"]
        self.host = self.args["primary_host"]
        self.port = int(self.args.get("primary_port", self.DEFAULT_PORT))
        self.language = str(self.args.get("language", "en"))
        self.interval_s = int(self.args.get("interval_s", 10))
        self.probe_timeout_s = float(self.args.get("probe_timeout_s", 2))
        self.verdict = Verdict(int(self.args.get("down_after", 2)), int(self.args.get("up_after", 3)))
        self._busy = False
        self._warned = False
        self._last_warning = None
        self.run_every(self._on_tick, f"now+{FIRST_CYCLE_S}", self.interval_s)
        self.log(f"Assist {self.SERVICE}: {self.primary} while it answers, else {self.fallback}",
                 level="INFO")

    def _on_tick(self, kwargs):
        if self._busy:
            return
        self._busy = True
        try:
            state = self.get_state(self.primary)
        except Exception:
            state = None
        self.create_task(self._cycle(state))

    async def _cycle(self, primary_state):
        self._warned = False
        try:
            probe = await self._probe()
            before = self.verdict.healthy
            healthy = self.verdict.observe(probe.ok)
            if healthy is not before:
                if healthy:
                    self.log(f"{self.primary} is answering", level="INFO")
                else:
                    self.log(f"{self.primary} is not answering ({probe.detail})", level="WARNING")
            pipeline = await self._pipeline()
            if pipeline is None:
                return
            primary_loaded = primary_state not in (None, "unavailable")
            engine = plan(pipeline.get(self.ENGINE_FIELD), self.primary, self.fallback, healthy,
                          primary_loaded)
            if engine:
                await self._switch(pipeline, engine)
        except Exception as err:
            self._warn(f"Assist {self.SERVICE} failover cycle failed: {err!r}")
        finally:
            if not self._warned:
                self._last_warning = None
            self._busy = False

    async def _probe(self):
        return await probe_wyoming(self.host, self.port, self.probe_timeout_s, self.language)

    async def _pipeline(self):
        listing = await self._call(type="assist_pipeline/pipeline/list")
        for pipeline in (listing or {}).get("pipelines") or []:
            if pipeline.get("id") == self.pipeline_id:
                return pipeline
        if listing is not None:
            self._warn(f"Assist pipeline {self.pipeline_id} does not exist")
        return None

    async def _switch(self, pipeline, engine):
        body = {key: value for key, value in pipeline.items() if key != "id"}
        body.update(self._fields(engine))
        updated = await self._call_with_nulls(type="assist_pipeline/pipeline/update",
                                              pipeline_id=self.pipeline_id, **body)
        if updated is not None:
            self.log(f"Assist pipeline {self.SERVICE} is now {engine}", level="INFO")

    def _fields(self, engine):
        """The pipeline fields that change when the pipeline moves to `engine`."""
        return {self.ENGINE_FIELD: engine}

    def _warn(self, text):
        self._warned = True
        if text != self._last_warning:
            self._last_warning = text
            self.log(text, level="WARNING")

    async def _call(self, **request):
        """The result of one websocket command, or None (warned once) when it did not succeed."""
        plugin = self.AD.plugins.get_plugin_object(self.namespace)
        send = getattr(plugin, "websocket_send_json", None)
        if send is None:
            self._warn("The HASS plugin has no websocket_send_json; cannot reach the Assist pipeline")
            return None
        return self._result(request, await send(**request))

    async def _call_with_nulls(self, **request):
        """Like _call, over a connection of its own and with null fields kept in the request.

        The plugin's websocket_send_json drops every field whose value is null, and Home Assistant
        wants wake_word_entity and wake_word_id on a pipeline update even when they are null.
        """
        plugin = self.AD.plugins.get_plugin_object(self.namespace)
        try:
            reply = await asyncio.wait_for(self._exchange(plugin, request), WRITE_TIMEOUT_S)
        except TimeoutError:
            reply = {"success": False, "ad_status": "timeout"}
        except Exception as err:
            reply = {"success": False, "ad_status": type(err).__name__}
        return self._result(request, reply)

    @staticmethod
    async def _exchange(plugin, request):
        url = plugin.config.websocket_url
        async with plugin.create_session() as session, session.ws_connect(url) as ws:
            await ws.receive_json()
            await ws.send_json(plugin.config.auth_json)
            if (await ws.receive_json()).get("type") != "auth_ok":
                return {"success": False, "ad_status": "auth_invalid"}
            await ws.send_json({"id": 1, **request})
            return await ws.receive_json()

    def _result(self, request, reply):
        if not isinstance(reply, dict) or reply.get("success") is not True:
            self._warn(f"{request['type']} failed: {_failure(reply)}")
            return None
        return reply.get("result") or {}
