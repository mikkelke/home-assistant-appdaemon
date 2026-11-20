#!/usr/bin/env python3
"""Dumps what HouseScenarios reads, from the recorder, as JSON on stdout: every sensor.house_events row since
SINCE (newest copy of each) and the signal histories scenarios_core takes. Read-only.

    ssh mke@10.21.0.5 'sudo docker exec -i homeassistant python3 - 2026-11-05' < scripts/scenario_dump.py > dump.json

Then scripts/scenario_replay.py turns the dump plus the house_events backfill into scenarios.
"""

import json
import sqlite3
import sys
from datetime import datetime

DB = "file:/config/home-assistant_v2.db?mode=ro"
FEED = "sensor.house_events"
NIGHT = "input_boolean.house_night_mode"
MACHINES = {"washer": "sensor.washer_state", "dryer": "sensor.dryer_state", "dish": "sensor.dishwasher_state"}
POWER = {"hob": "sensor.hotplate_power_monitor_total_active_power", "oven": "sensor.oven_plug_switch_0_power"}
AREAS = {
    "Mikkel": "sensor.bermuda_d6462f88944242db9e31bdff598edc25_100_40004_area",
    "Claudia": "sensor.claudias_iphone_ble_area",
    "Kristine": "sensor.kristines_iphone_ble_area",
}
POWER_THRESHOLDS = {"hob": 300.0, "oven": 200.0}


def main():
    since = datetime.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-11-05").timestamp()
    con = sqlite3.connect(DB, uri=True)

    def metadata(entity):
        row = con.execute("SELECT metadata_id FROM states_meta WHERE entity_id = ?", (entity,)).fetchone()
        return row[0] if row else None

    def points(entity, start=since):
        meta = metadata(entity)
        if meta is None:
            return []
        rows = con.execute(
            "SELECT state, last_updated_ts FROM states WHERE metadata_id = ? AND last_updated_ts >= ? ORDER BY last_updated_ts",
            (meta, start - 86400),
        ).fetchall()
        return [(t, s) for s, t in rows if s not in ("unknown", "unavailable", None)]

    def edges(entity, threshold):
        """Only the readings that cross the threshold, plus the last one: all power_intervals needs."""
        out, above = [], None
        for t, s in points(entity):
            try:
                now_above = float(s) > threshold
            except ValueError:
                continue
            if now_above != above:
                out.append((t, float(s)))
                above = now_above
        return out

    meta = metadata(FEED)
    rows = {}
    for (attrs,) in con.execute(
        "SELECT a.shared_attrs FROM states s JOIN state_attributes a ON s.attributes_id = a.attributes_id "
        "WHERE s.metadata_id = ? AND s.last_updated_ts >= ? ORDER BY s.last_updated_ts",
        (meta, since),
    ):
        try:
            events = json.loads(attrs).get("events") or []
        except ValueError:
            continue
        for row in events:
            if isinstance(row, dict) and isinstance(row.get("ts"), str):
                rows[(row["ts"], row.get("text"))] = row
    json.dump(
        {
            "rows": list(rows.values()),
            "signals": {
                "night": points(NIGHT),
                "machines": {kind: points(entity) for kind, entity in MACHINES.items()},
                "power": {kind: edges(entity, POWER_THRESHOLDS[kind]) for kind, entity in POWER.items()},
                "areas": {name: points(entity) for name, entity in AREAS.items()},
            },
            "now": datetime.now().timestamp(),
        },
        sys.stdout,
    )


main()
