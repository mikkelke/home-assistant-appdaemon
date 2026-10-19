#!/usr/bin/env python3
"""Backfill the coffee ledger with the brews the pour-over plug saw before the coffee app ran.

    python3 scripts/coffee_backfill.py            # dry run: lists what it would add
    python3 scripts/coffee_backfill.py --apply    # adds them to the box's coffee_feedback.json

The plug's power history comes from HA's recorder, opened read-only inside the homeassistant container
(nothing in the database is written). The brews are found by apps/coffee/coffee_power.py, the detector
the app runs live: a grind followed by heat, complete with three heater pulses or more. Each becomes a
record with times only (outcome unobserved, or stopped when it ended early; no dose, curve or who), so
the brew counts per week and the maintenance pace start from real history. A brew that overlaps a record
already in the ledger is left out. The merged ledger replaces the old one in one step; the app reads its
ledger again before every write, so a running app keeps the added records.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "coffee"))
import coffee_power  # noqa: E402

HOST = "mke@10.21.0.5"
LEDGER = "/data/appdaemon/apps/coffee/coffee_feedback.json"
ENTITY = "sensor.pour_over_plug_power"
OVERLAP_S = 300

EXTRACT = r'''
import sqlite3, sys, time
days, entity = float(sys.argv[1]), sys.argv[2]
con = sqlite3.connect("file:/config/home-assistant_v2.db?mode=ro", uri=True, timeout=10)
row = con.execute("select metadata_id from states_meta where entity_id=?", (entity,)).fetchone()
if row:
    since = time.time() - days * 86400
    for ts, state in con.execute("select last_updated_ts, state from states where metadata_id=? and last_updated_ts>? "
                                 "order by last_updated_ts", (row[0], since)):
        print(f"{ts:.3f}\t{state}")
'''


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def epoch(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def power_rows(days):
    command = f"sudo docker exec -i homeassistant python3 - {days} {ENTITY}"
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", HOST, command], input=EXTRACT, capture_output=True,
                         text=True, check=True, timeout=600).stdout
    rows = []
    for line in out.splitlines():
        ts, _, state = line.partition("\t")
        rows.append((float(ts), state))
    return rows


def record(start, end, pulses, complete, now):
    return {"id": f"c{int(start)}", "ts": iso(end), "duration_min": round((end - start) / 60, 2),
            "completion_class": "complete" if complete else "interrupted",
            "outcome": "unobserved" if complete else "stopped", "source": "power", "started_at": iso(start),
            "variant": "unknown", "recipe_match": "unknown", "who": "none", "notify": "none", "push": "none",
            "faults": [], "power_pulses": pulses, "backfilled_at": iso(now)}


def overlaps(candidate, records):
    start, end = epoch(candidate["started_at"]), epoch(candidate["ts"])
    for r in records:
        r_start, r_end = epoch(r.get("started_at") or r.get("ts")), epoch(r.get("ts"))
        if r_start is None or r_end is None:
            continue
        if start <= r_end + OVERLAP_S and r_start <= end + OVERLAP_S:
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=float, default=90)
    parser.add_argument("--apply", action="store_true", help="write the merged ledger to the box")
    args = parser.parse_args()
    now = datetime.now(timezone.utc).timestamp()
    rows = power_rows(args.days)
    found = coffee_power.detect(rows)
    ledger = json.loads(subprocess.run(["ssh", "-o", "BatchMode=yes", HOST, f"sudo cat {LEDGER}"], capture_output=True,
                                       text=True, check=True, timeout=60).stdout)
    cycles = ledger.get("cycles") or []
    added = [r for r in (record(*brew, now) for brew in found) if not overlaps(r, cycles)]
    complete = sum(1 for r in added if r["completion_class"] == "complete")
    print(f"{len(rows)} plug readings over {args.days:g} days: {len(found)} brews found, {len(cycles)} records in the "
          f"ledger, {len(added)} to add ({complete} complete)")
    for r in added:
        print(f"  {r['id']}  {r['started_at']}  {r['duration_min']:5.1f} min  {r['outcome']}")
    if not args.apply or not added:
        return
    merged = {**ledger, "version": ledger.get("version", 1),
              "cycles": sorted(cycles + added, key=lambda r: r.get("started_at") or r.get("ts") or "")}
    text = json.dumps(merged, indent=2)
    subprocess.run(["ssh", "-o", "BatchMode=yes", HOST,
                    f"sudo tee {LEDGER}.backfill > /dev/null && sudo mv {LEDGER}.backfill {LEDGER}"],
                   input=text, text=True, check=True, timeout=60)
    print(f"written: {len(merged['cycles'])} records")


if __name__ == "__main__":
    main()
