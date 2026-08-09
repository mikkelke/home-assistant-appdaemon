"""
Forecast log: hourly snapshots of what the forecast SAID would happen, so the dashboard can
later score it against what the rooftop station actually MEASURED.

Home Assistant keeps no history of past forecasts - `weather.get_forecasts` only ever returns
the future, and the recorder stores the weather entity's *current* attributes, not the
predictions it made yesterday. So "the 24 h forecast is typically off by 1.2 C" is
unanswerable unless something writes the predictions down as they are issued. That is all this
app does; it draws no conclusions and drives nothing.

Output goes to HA's www folder so the dashboard can fetch it at /local/<dir>/<file> exactly
like the vacuum map index does. Writes are atomic (tmp + os.replace) because the dashboard may
fetch mid-write, and a half-written JSON would surface as a parse error on the wall tablet.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


def parse_forecast_envelope(resp: Any, entity_id: str) -> list[dict]:
    """Unwrap weather.get_forecasts' response into a plain list of forecast entries.

    HA has shipped this payload in several shapes over the years, and AppDaemon adds its own
    wrapper depending on version: {"result": {"response": {<entity>: {"forecast": [...]}}}},
    {"response": {...}}, {<entity>: {"forecast": [...]}}, or the bare list. Tolerate all of
    them and return [] for anything unrecognised - callers treat empty exactly like a failed
    fetch (see smart_cooling.py, which learned this the hard way: the service intermittently
    returns an empty payload with no error at all).
    """
    node: Any = resp
    for key in ("result", "response"):
        if isinstance(node, dict) and key in node:
            node = node[key]
    if isinstance(node, dict) and entity_id in node:
        node = node[entity_id]
    if isinstance(node, dict) and "forecast" in node:
        node = node["forecast"]
    return node if isinstance(node, list) else []


def build_snapshot(issued_iso: str, entries: list[dict], horizon_hours: float) -> Optional[dict]:
    """One snapshot: when it was issued + the entries falling inside the horizon.

    Only the fields worth scoring are kept (temperature, wind, precipitation, condition) -
    this file is appended to every hour and read by a phone, so it stays lean. An entry whose
    datetime or temperature will not parse is dropped rather than stored as null: a gap is
    honest, a null would score as a miss.
    """
    try:
        issued = datetime.fromisoformat(issued_iso.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    horizon_end = issued + timedelta(hours=horizon_hours)

    kept: list[dict] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        try:
            at = datetime.fromisoformat(str(item["datetime"]).replace("Z", "+00:00"))
            temp = float(item["temperature"])
        except (KeyError, TypeError, ValueError):
            continue
        if at < issued or at > horizon_end:
            continue

        row: dict[str, Any] = {"at": at.astimezone(timezone.utc).isoformat(), "temp": round(temp, 2)}
        for src, dst in (("wind_speed", "wind"), ("precipitation", "precip"), ("precipitation_probability", "precip_prob")):
            val = item.get(src)
            if val is not None:
                try:
                    row[dst] = round(float(val), 2)
                except (TypeError, ValueError):
                    pass
        cond = item.get("condition")
        if isinstance(cond, str) and cond:
            row["condition"] = cond
        kept.append(row)

    if not kept:
        return None
    return {"issued": issued.astimezone(timezone.utc).isoformat(), "entries": kept}


def prune_snapshots(snapshots: list[dict], now: datetime, retain_days: float) -> list[dict]:
    """Drop snapshots issued longer than retain_days ago; keep chronological order.

    Bounded on purpose: this file is fetched by the dashboard, so it must not grow without
    limit. A snapshot with an unparseable `issued` is dropped too - it can never be scored.
    """
    cutoff = now - timedelta(days=retain_days)
    out: list[dict] = []
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        try:
            issued = datetime.fromisoformat(str(snap.get("issued", "")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if issued >= cutoff:
            out.append(snap)
    out.sort(key=lambda s: str(s.get("issued", "")))
    return out


class ForecastLog(hass.Hass):
    def initialize(self) -> None:
        self.weather_entity = self.args.get("weather_entity", "weather.forecast_home")
        # /www inside the AppDaemon container == /data/homeassistant/www on the box, served
        # by HA at /local/ - the same route the vacuum map index takes to the dashboard.
        self.out_dir = self.args.get("output_dir", "/www/forecast_log")
        self.out_file = self.args.get("output_file", "forecast_log.json")
        self.horizon_hours = float(self.args.get("horizon_hours", 24))
        self.retain_days = float(self.args.get("retain_days", 14))
        self.interval_min = float(self.args.get("interval_minutes", 60))
        self.fetch_timeout_s = float(self.args.get("fetch_timeout_seconds", 12))

        self.path = os.path.join(self.out_dir, self.out_file)

        # "now" fires at now+interval per AppDaemon's docs, not immediately - "immediate" is
        # the real keyword (same trap that left WeatherOpeningAlert blind for 60 s per reload,
        # see commit 8666460).
        self.run_every(self._run_snapshot, "immediate", self.interval_min * 60)
        self.log(
            f"Started: logging {self.weather_entity} every {self.interval_min:.0f} min "
            f"({self.horizon_hours:.0f} h horizon, {self.retain_days:.0f} d retained) -> {self.path}",
            level="INFO",
        )

    def _run_snapshot(self, kwargs: Optional[dict] = None) -> None:
        self.create_task(self._snapshot())

    async def _snapshot(self) -> None:
        try:
            resp = await asyncio.wait_for(
                self.call_service(
                    "weather/get_forecasts",
                    entity_id=self.weather_entity,
                    type="hourly",
                    return_response=True,
                ),
                timeout=self.fetch_timeout_s,
            )
        except Exception as e:
            self.log(f"Forecast fetch failed ({e}) - skipping this snapshot", level="WARNING")
            return

        entries = parse_forecast_envelope(resp, self.weather_entity)
        if not entries:
            # Empty-with-no-error is a known HA behaviour, not an exception path.
            self.log("Forecast fetch returned nothing - skipping this snapshot", level="WARNING")
            return

        now = datetime.now(timezone.utc)
        snapshot = build_snapshot(now.isoformat(), entries, self.horizon_hours)
        if snapshot is None:
            self.log("Forecast had no usable entries in horizon - skipping this snapshot", level="WARNING")
            return

        try:
            await self.run_in_executor(self._append_snapshot, snapshot, now)
        except Exception as e:
            self.log(f"Could not write forecast log: {e}", level="ERROR")

    def _append_snapshot(self, snapshot: dict, now: datetime) -> None:
        """Read-modify-write on the executor (blocking file IO must stay off the event loop)."""
        data: dict[str, Any] = {"version": 1, "snapshots": []}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and isinstance(loaded.get("snapshots"), list):
                data = loaded
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as e:
            # A corrupt file must not wedge logging forever - start a fresh one and say so.
            self.log(f"Forecast log unreadable ({e}) - starting a new one", level="WARNING")

        snapshots = prune_snapshots(list(data.get("snapshots", [])) + [snapshot], now, self.retain_days)
        payload = {"version": 1, "updated": now.isoformat(), "snapshots": snapshots}

        os.makedirs(self.out_dir, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, self.path)
        self.log(f"Logged forecast: {len(snapshot['entries'])} entries, {len(snapshots)} snapshots retained", level="DEBUG")
