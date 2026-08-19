"""
Salus gateway diagnostics -- publishes battery/RSSI/LQI/connectivity/problem entities
by talking to the iT600 gateway DIRECTLY, independent of whichever HA integration is
installed.

WHY: our HA integration fork (~/repositories/homeassistant_salus) exposes battery,
RSSI, LQI, connectivity and problem entities that the maintained upstream we plan to
migrate to does not produce (see homeassistant_salus's MIGRATION_PLAN.md - upstream
derives battery from a register none of our six SQ610RFNH thermostats expose). Rather
than block the migration on upstream accepting patches, this app is the "thin local
overlay" that plan proposes: it reads the gateway itself and republishes the same
class of diagnostics under its own entity ids, so they survive any integration swap
underneath (including no integration installed at all). salus_health.py (the watchdog
that alerts on these) already understands both this app's entities and the
integration's, picking either/both via its entity_markers knob.

READ-ONLY, always: this app only ever sends "read" commands to the gateway - there is
no write code path in this file or in salus_gateway_protocol.py to misuse. Never write
to the gateway from here.

Polling discipline (read this before changing poll_minutes): the gateway serves ONE
request at a time. Home Assistant's own salus integration already polls it roughly
every 30s, and concurrent reads have demonstrably caused HA-side timeouts and, once,
left every thermostat unavailable. So: poll INFREQUENTLY (default every 5 min, a yaml
knob), never in a tight loop, with a generous timeout, and treat every error - timeout,
connection refused, bad decrypt, malformed JSON, whatever - as "skip this cycle, keep
whatever was last published, try again next tick". Nothing here ever retries within a
cycle or raises out of the poll.

Entity shape (per device, slug from the device's own gateway name, e.g. "bedroom_thermostat"
or "control_centre" - see salus_gateway_protocol.slugify):
  sensor.salus_<slug>_battery        percent (0/20/40/60/80/100), device_class battery,
                                      raw_level attribute carries the underlying 0-5.
  sensor.salus_<slug>_rssi           dBm.
  sensor.salus_<slug>_lqi            raw link-quality index, no standard unit.
  sensor.salus_<slug>_connectivity   "on"/"off" - AppDaemon's set_state cannot create a
                                      real binary_sensor (no platform/registry entry), so
                                      this is a plain sensor.* entity. Its STATE VALUE is
                                      still the literal string "on"/"off" (exactly what a
                                      real binary_sensor's state would be) rather than some
                                      other vocabulary like "online"/"offline" - on purpose,
                                      so salus_health.py's existing on/off comparisons work
                                      unchanged against either source.
  sensor.salus_<slug>_problem        same "on"/"off" convention; state is "on" whenever any
                                      Error* register is active, with an `errors` attribute
                                      listing which ones (register names), matching the
                                      integration's own `errors` attribute shape.
Battery is thermostat-only (the wiring centre has no sIT600TH section, hence no battery
register). RSSI/LQI are documented as intermittently absent even on a healthy device
(sIT600I is sometimes missing from an otherwise-normal read) - a missing field this
cycle simply isn't republished, so the entity holds its last known value rather than
flapping to unknown. Connectivity/problem are republished every cycle a device appears
in at all (their source registers are effectively always present).

Every numeric state/attribute is passed through str() before set_state(): AppDaemon
4.5.13's set_state runs every kwarg through clean_http_kwargs -> remove_literals(val,
(None, False)), which silently drops any (possibly nested) value equal to 0 or False -
0 == False in Python, so a battery reading of 0% or an RSSI of exactly 0 would vanish
from the very state/attribute meant to report it. Same bug, same str()-everywhere fix
as bed_presence_compare.py's _publish (see its docstring) and the replace=True already
used by smart_cooling.py/bedroom_solar_shade.py's _publish methods.

Credentials: `host` (the gateway's LAN address) and `euid` (its 16-character token) are
yaml knobs, never hardcoded and never logged - see salus_gateway_diagnostics.yaml for
where the real values live (HA's own salus config entry) and how to wire them in. With
either knob blank (the committed default) this app logs once and does not poll, rather
than hammering a placeholder host every cycle.
"""

from __future__ import annotations

import json

import aiohttp
import appdaemon.plugins.hass.hassapi as hass

import salus_gateway_protocol as proto


class SalusGatewayDiagnostics(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.host = str(a("host", "") or "").strip()
        self.port = int(a("port", 80))
        self.euid = str(a("euid", "") or "").strip()
        self.poll_minutes = float(a("poll_minutes", 5))
        self.request_timeout_s = float(a("request_timeout", 20))

        if not self.host or not self.euid:
            self.log(
                "SalusGatewayDiagnostics: host/euid not configured (see "
                "salus_gateway_diagnostics.yaml for where the real values come from) - "
                "not polling.",
                level="WARNING",
            )
            return

        self._cipher = proto.GatewayCipher(self.euid)
        interval_s = max(60, int(self.poll_minutes * 60))
        self.run_every(self._on_poll_tick, "now+30", interval_s)
        self.log(f"SalusGatewayDiagnostics polling every {self.poll_minutes:.0f} min", level="INFO")

    # ---------- scheduling ----------

    def _on_poll_tick(self, kwargs) -> None:
        self.create_task(self._async_poll())

    async def _async_poll(self) -> None:
        try:
            records = await self._fetch_raw_devices()
        except Exception as e:
            self.log(f"Salus gateway poll failed - keeping last published values: {e}", level="WARNING")
            return

        if not records:
            self.log("Salus gateway poll returned no thermostat/control-centre devices", level="WARNING")
            return

        for record in records:
            try:
                diag = proto.extract_diagnostics(record)
            except Exception as e:
                self.log(f"Salus gateway: could not parse one device, skipping it: {e}", level="WARNING")
                continue
            await self._publish(diag)

    # ---------- gateway I/O ----------

    async def _fetch_raw_devices(self) -> list:
        """readall (minimal, for classification) -> ONE batched deviceid request for
        every thermostat + the wiring centre together. Two gateway requests per poll,
        regardless of how many thermostats exist - see the module docstring on why
        request count is minimized."""
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            readall = await self._post_read(session, proto.readall_body())
            candidates = [r for r in readall.get("id", []) if proto.is_relevant_record(r)]
            if not candidates:
                return []
            detail = await self._post_read(session, proto.deviceid_body(candidates))
            return detail.get("id", [])

    async def _post_read(self, session: aiohttp.ClientSession, body: dict) -> dict:
        url = f"http://{self.host}:{self.port}/deviceid/read"
        payload = self._cipher.encrypt(json.dumps(body))
        async with session.post(url, data=payload, headers={"content-type": "application/json"}) as resp:
            raw = await resp.read()
        decrypted = self._cipher.decrypt(raw)
        parsed = json.loads(decrypted)
        return proto.unwrap_response(parsed)

    # ---------- publish ----------

    async def _publish(self, diag: dict) -> None:
        """One device's diagnostics -> its sensor.salus_<slug>_* entities. Missing
        (None) fields are simply not republished this cycle (hold last known value -
        see module docstring); connectivity/problem always publish when the device
        appears in a poll at all. Every numeric value goes through str() - see module
        docstring for the AppDaemon 4.5.13 set_state 0/False-dropping bug this avoids.
        """
        try:
            slug = diag["slug"]
            label = slug.replace("_", " ").title()

            if diag["battery_level"] is not None:
                await self.set_state(
                    f"sensor.salus_{slug}_battery",
                    state=str(diag["battery_level"] * 20),
                    replace=True,
                    attributes={
                        "friendly_name": f"{label} battery",
                        "unit_of_measurement": "%",
                        "device_class": "battery",
                        "state_class": "measurement",
                        "raw_level": str(diag["battery_level"]),
                    },
                )

            if diag["rssi"] is not None:
                await self.set_state(
                    f"sensor.salus_{slug}_rssi",
                    state=str(diag["rssi"]),
                    replace=True,
                    attributes={
                        "friendly_name": f"{label} RSSI",
                        "unit_of_measurement": "dBm",
                        "device_class": "signal_strength",
                        "state_class": "measurement",
                    },
                )

            if diag["lqi"] is not None:
                await self.set_state(
                    f"sensor.salus_{slug}_lqi",
                    state=str(diag["lqi"]),
                    replace=True,
                    attributes={
                        "friendly_name": f"{label} LQI",
                        "state_class": "measurement",
                        "icon": "mdi:signal",
                    },
                )

            if diag["online"] is not None:
                await self.set_state(
                    f"sensor.salus_{slug}_connectivity",
                    state="on" if diag["online"] else "off",
                    replace=True,
                    attributes={
                        "friendly_name": f"{label} connectivity",
                        "icon": "mdi:lan-connect" if diag["online"] else "mdi:lan-disconnect",
                    },
                )

            errors = diag["errors"]
            await self.set_state(
                f"sensor.salus_{slug}_problem",
                state="on" if errors else "off",
                replace=True,
                attributes={
                    "friendly_name": f"{label} problem",
                    "errors": list(errors),
                    "icon": "mdi:alert-circle" if errors else "mdi:check-circle",
                },
            )
        except Exception as e:
            self.log(f"Salus gateway: publish failed for {diag.get('slug')}: {e}", level="WARNING")
