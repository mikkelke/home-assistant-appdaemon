#!/usr/bin/env python3
"""
Historical baseline for lighting rollout (all darkness_calculator zones).

Uses Home Assistant recorder when HA_URL + HA_TOKEN are set. In normal mode,
missing recorder credentials is a hard failure (exit 1). Use --replay-only for
incident-profile checks without HA access.

Metrics per zone:
  - auto-on dark rate (committed + pending from calculator)
  - hysteresis band samples (between dark_threshold and bright_threshold)

Run:
  export HA_URL=http://homeassistant:8123 HA_TOKEN=...
  python3 appdaemon/apps/lights/tools/lighting_historical_baseline.py --days 7
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow import from apps/lights
_LIGHTS = Path(__file__).resolve().parents[1]
if str(_LIGHTS) not in sys.path:
    sys.path.insert(0, str(_LIGHTS))

import room_state_darkness  # noqa: E402

ZONES = [
    "sensor.room_state_family_room",
    "sensor.room_state_bedroom_bathroom",
    "sensor.room_state_claudias_room",
    "sensor.room_state_hallway",
    "sensor.room_state_kristines_room",
    "sensor.room_state_guest_bathroom",
]

OUTDOOR_ENTITY = "sensor.gw2000a_solar_lux"


class _FakeHass:
    """Minimal hass shim matching tests.FakeHass (supports attribute=all)."""

    def __init__(self, state: str, attrs: dict | None = None, pending=None):
        self._state = state
        self._attrs = dict(attrs or {})
        self._pending = pending

    def get_state(self, entity_id, attribute=None, default=None):
        if attribute == "pending_target":
            return self._pending
        if attribute == "all":
            return {"state": self._state, "attributes": self._attrs}
        if attribute is None:
            return self._state
        return self._attrs.get(attribute, default)


def _ha_history(entity_ids: list[str], start: datetime, end: datetime) -> dict:
    base = os.environ.get("HA_URL", "").rstrip("/")
    token = os.environ.get("HA_TOKEN", "")
    if not base or not token:
        raise RuntimeError("Set HA_URL and HA_TOKEN for recorder access")

    start_s = start.replace(tzinfo=timezone.utc).isoformat()
    end_s = end.replace(tzinfo=timezone.utc).isoformat()
    out: dict[str, list] = {}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    for eid in entity_ids:
        url = (
            f"{base}/api/history/period/{urllib.parse.quote(start_s)}"
            f"?filter_entity_id={urllib.parse.quote(eid)}"
            f"&end_time={urllib.parse.quote(end_s)}"
            "&minimal_response"
        )
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        out[eid] = data[0] if data else []
    return out


def _attrs_from_hist(row: dict) -> dict:
    attrs = dict(row.get("attributes") or {})
    for k in ("indoor_lux", "outdoor_lux", "bright_threshold", "dark_threshold"):
        if k in attrs:
            try:
                attrs[k] = float(attrs[k])
            except (TypeError, ValueError):
                pass
    return attrs


def analyze_zone(
    entity_id: str,
    rows: list[dict],
    outdoor_by_ts: dict[str, float] | None,
) -> dict:
    """Summarize policy deltas on historical room_state samples."""
    n = 0
    auto_on_dark = 0
    band_samples = 0

    for row in rows:
        state = row.get("state")
        if not state or state in ("unknown", "unavailable"):
            continue
        attrs = _attrs_from_hist(row)
        ts = row.get("last_changed") or row.get("last_updated")
        if outdoor_by_ts and ts and attrs.get("outdoor_lux") is None:
            # Nearest outdoor not implemented - attrs usually include outdoor_lux
            pass

        pending = attrs.get("pending_target")
        fake = _FakeHass(state, attrs, pending=pending)
        ctx = room_state_darkness.read_room_lighting_context(fake, entity_id)
        on_d = room_state_darkness.evaluate_auto_on(fake, entity_id, default_dark=True)

        n += 1
        if on_d.is_dark:
            auto_on_dark += 1
        if (
            ctx.indoor_lux is not None
            and ctx.bright_threshold is not None
            and ctx.dark_threshold is not None
            and ctx.dark_threshold <= ctx.indoor_lux < ctx.bright_threshold
        ):
            band_samples += 1

    return {
        "entity_id": entity_id,
        "samples": n,
        "auto_on_dark_pct": round(100 * auto_on_dark / n, 1) if n else 0,
        "hysteresis_band_samples": band_samples,
    }


def replay_incident_profiles() -> None:
    """Static replay for 19 May triage windows (no HA required)."""
    cases = [
        (
            "18:42 bedroom committed dark (auto-on)",
            {
                "state": "Occupied (Dark)",
                "attributes": {
                    "indoor_lux": 172,
                    "outdoor_lux": 9800,
                    "bright_threshold": 250,
                    "dark_threshold": 160,
                    "pending_target": None,
                },
            },
            True,
        ),
        (
            "08:42 bright shutoff (pending bright)",
            {
                "state": "Occupied (Dark)",
                "attributes": {
                    "indoor_lux": 280,
                    "outdoor_lux": 500,
                    "bright_threshold": 250,
                    "dark_threshold": 160,
                    "pending_target": "bright",
                },
            },
            False,
        ),
        (
            "21:33 evening dark",
            {
                "state": "Occupied (Dark)",
                "attributes": {
                    "indoor_lux": 2,
                    "outdoor_lux": 50,
                    "bright_threshold": 250,
                    "dark_threshold": 160,
                },
            },
            True,
        ),
    ]
    eid = "sensor.room_state_bedroom_bathroom"
    print("\n=== Incident profile replay (Layer 2) ===")
    for name, ent, expect_dark_on in cases:
        fake = _FakeHass(ent["state"], ent.get("attributes"), pending=ent["attributes"].get("pending_target"))
        on_d = room_state_darkness.evaluate_auto_on(fake, eid, default_dark=True)
        off_d = room_state_darkness.evaluate_auto_off(fake, eid, default_dark=True)
        ok = on_d.is_dark == expect_dark_on
        print(
            f"  {name}: auto_on={'dark' if on_d.is_dark else 'bright'} "
            f"[{on_d.rule}] auto_off={'dark' if off_d.is_dark else 'bright'} "
            f"[{off_d.rule}] {'OK' if ok else 'FAIL'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Lighting historical baseline")
    parser.add_argument("--days", type=int, default=7, help="History window (requires HA)")
    parser.add_argument("--replay-only", action="store_true", help="Skip HA; replay incident profiles")
    args = parser.parse_args()

    replay_incident_profiles()

    if args.replay_only:
        return 0

    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=args.days)
        hist = _ha_history(ZONES + [OUTDOOR_ENTITY], start, end)
    except RuntimeError as e:
        print(f"\nHA recorder skipped: {e}")
        print("Set HA_URL and HA_TOKEN, or use --replay-only.")
        return 1

    print(f"\n=== Zone baseline ({args.days}d, UTC) ===")
    for zid in ZONES:
        rows = hist.get(zid, [])
        summary = analyze_zone(zid, rows, None)
        print(
            f"  {zid}: n={summary['samples']} "
            f"auto_on_dark={summary['auto_on_dark_pct']}% "
            f"hyst_band={summary['hysteresis_band_samples']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
