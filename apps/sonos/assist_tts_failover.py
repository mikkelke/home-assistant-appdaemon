"""AssistTtsFailover: Assist text-to-speech on the fast server while it answers, on the local one otherwise.

The failover of AssistSttFailover for the Wyoming TTS server on the GPU box. Every interval_s this app asks it
to speak a short text and expects the start and the end of the audio within probe_timeout_s, which is the whole
path a spoken answer takes. down_after failures in a row move the pipeline's tts_engine, tts_voice and
tts_language to the fallback; up_after successes in a row, with the primary entity loaded in Home Assistant,
move them back.

Announcements follow the same health, not the pipeline: SonosNotifier asks announcement_voice() for every
render, so a pipeline set to another engine by hand does not change them.
"""

from assist_stt_failover import AssistSttFailover, _event, _read_event, run_probe

PROBE_TEXT = "Ok."
NOTIFIER_APP = "SonosNotifier"


async def probe_synthesize(host, port, timeout, voice, text=PROBE_TEXT):
    """Ask a Wyoming TTS server to speak a short text and wait for the end of its audio."""

    async def exchange(reader, writer):
        writer.write(_event("synthesize", {"text": text, "voice": {"name": voice}}))
        await writer.drain()
        started = False
        while True:
            kind, data = await _read_event(reader)
            if kind == "audio-start":
                started = True
            elif kind == "audio-stop":
                return (True, "audio") if started else (False, "audio-stop without audio-start")
            elif kind == "error":
                return False, "server error " + str(data.get("code") or "")[:40]

    return await run_probe(host, port, timeout, exchange, "audio")


class AssistTtsFailover(AssistSttFailover):
    SERVICE = "text-to-speech"
    ENGINE_FIELD = "tts_engine"
    DEFAULT_PORT = 10200
    REQUIRED = AssistSttFailover.REQUIRED + ("primary_voice", "primary_language", "fallback_voice",
                                             "fallback_language")

    def announcement_voice(self):
        """The chime_tts settings of the voice an announcement should use now.

        The primary's unless the probes say it is down; unknown counts as the primary.
        """
        return self._voice("fallback" if self.verdict.healthy is False else "primary")

    def precache_voices(self):
        """The voices worth rendering ahead: the fallback always, the primary unless it is down."""
        sides = ("fallback",) if self.verdict.healthy is False else ("primary", "fallback")
        return [self._voice(side) for side in sides]

    def _voice(self, side):
        # chime_tts names an engine without its "tts." prefix, as the notifier's tts_platform is written.
        return {"tts_platform": self.args[f"{side}_entity"].removeprefix("tts."),
                "options": {"voice": self.args[f"{side}_voice"]},
                "language": self.args[f"{side}_language"]}

    def _fields(self, engine):
        voice = self._voice("primary" if engine == self.primary else "fallback")
        return {"tts_engine": engine, "tts_voice": voice["options"]["voice"],
                "tts_language": voice["language"]}

    async def _probe(self):
        return await probe_synthesize(self.host, self.port, self.probe_timeout_s,
                                      self.args["primary_voice"])

    async def _cycle(self, primary_state):
        before = self.verdict.healthy
        await super()._cycle(primary_state)
        if before is False and self.verdict.healthy:
            try:
                await self.run_in(self._precache_announcements, 0)
            except Exception as err:
                self.log(f"Could not schedule the announcement pre-render: {err!r}", level="WARNING")

    def _precache_announcements(self, kwargs):
        try:
            notifier = self.get_app(NOTIFIER_APP)
            if notifier is not None:
                notifier.precache()
        except Exception as err:
            self.log(f"Announcement pre-render after the recovery failed: {err!r}", level="WARNING")
