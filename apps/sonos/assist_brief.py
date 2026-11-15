"""AssistBrief: a fresh summary of the house for the Assist agent's prompt.

Every tool round the agent makes costs the listener about two seconds, and most questions are
answered by the same few house tools. This app runs those tool scripts itself and publishes their
responses as one sensor, which the agent's prompt template reads, so a common question needs no
tool call at all. The scripts stay the only place their logic lives.

The brief is refreshed whenever a satellite starts listening (the scripts answer in well under a
second, while speech-to-text has not even finished), every refresh_every_s, and at startup. Its
state is the refresh time, so a reader can tell a stale brief; the recorder keeps identical
attributes only once. Its attributes are the script names with their responses; a script that
fails or times out is left out, so the agent falls back to calling it.
"""

import asyncio
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

SCRIPT_TIMEOUT_S = 5


def script_response(resp):
    """The script's stop response from AppDaemon's call_service(..., return_response=True) envelope."""
    node = resp
    for key in ("result", "response"):
        if isinstance(node, dict) and key in node:
            node = node[key]
    return node if isinstance(node, dict) and node else None


def build_brief(responses):
    """{script: response} for the scripts that answered, in the configured order."""
    return {name: resp for name, resp in responses if resp is not None}


class AssistBrief(hass.Hass):

    async def initialize(self):
        self.scripts = list(self.args["scripts"])
        self.sensor = self.args["sensor"]
        self._busy = False
        for sat in self.args["satellites"]:
            await self.listen_state(self._on_satellite, sat, new="listening")
        await self.run_every(self._tick, "now+5", int(self.args.get("refresh_every_s", 300)))

    async def _on_satellite(self, entity, attribute, old, new, kwargs):
        await self.refresh()

    async def _tick(self, kwargs):
        await self.refresh()

    async def _run(self, name):
        try:
            resp = await asyncio.wait_for(
                self.call_service(f"script/{name}", return_response=True), timeout=SCRIPT_TIMEOUT_S)
            return name, script_response(resp)
        except Exception as e:
            self.log(f"{name} failed: {e!r}", level="WARNING")
            return name, None

    async def refresh(self):
        if self._busy:
            return
        self._busy = True
        try:
            brief = build_brief(await asyncio.gather(*(self._run(n) for n in self.scripts)))
            await self.set_state(self.sensor, state=datetime.now().astimezone().isoformat(timespec="seconds"),
                                 attributes={"friendly_name": "Assist house brief", **brief}, replace=True)
        finally:
            self._busy = False
