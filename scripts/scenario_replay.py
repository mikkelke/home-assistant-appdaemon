#!/usr/bin/env python3
"""Replays scenario_dump.py's dump (plus the house_events backfill rows) through scenarios_core: prints rows
and timeline items per day before and after, and writes the scenarios as a backfill file for HouseScenarios.

    python3 scripts/scenario_replay.py dump.json house_events_backfill.json house_scenarios_backfill.json
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "home_pulse"))
import scenarios_core as core  # noqa: E402

YALE_OPERATOR = re.compile(r"^(?:(?:Auto|One-Touch|Bluetooth|Manual) (?:Lock|Unlock|Unlatch)|Home Assistant(?: user|-bruger)?)$")
TV_LIFT = re.compile(r" -> (?:TV lift going (?:up|down)|TV moving to |Gave up after .* the lift needs a manual raise)")


def shown(row):
    """The row as the dashboard shows it today, or None when the dashboard hides it (houseEvents.ts)."""
    text = row.get("text") or ""
    if ("default volume and ungrouped" in text or text.startswith("Doors closed to queued rooms -> ")
            or " in a recently heated room -> The room has been heated recently" in text):
        return None
    if str(row.get("icon", "")).startswith("mdi:television") and TV_LIFT.search(text):
        return None
    row = dict(row)
    if row.get("by") and YALE_OPERATOR.match(row["by"]):
        row.pop("by")
    if not row.get("by") and text.endswith("door opened from inside"):
        return None
    if text.startswith("Front door ") and " -> " not in text:
        row["text"] = "Apartment door " + text[len("Front door "):]
    return row


def episodes(rows):
    """houseEpisodes: rows with the same cause within 60 s are one item."""
    items, open_by_cause = 0, {}
    for row in sorted(rows, key=lambda r: core.epoch(r["ts"])):
        t, cause = core.epoch(row["ts"]), row.get("cause")
        if cause and cause in open_by_cause and t - open_by_cause[cause] <= 60:
            continue
        items += 1
        if cause:
            open_by_cause[cause] = t
    return items


def main(dump_path, backfill_path, out_path):
    dump = json.loads(Path(dump_path).read_text())
    merged = {(r["ts"], r.get("text")): r for r in dump["rows"]}
    for row in json.loads(Path(backfill_path).read_text()):
        merged[(row["ts"], row.get("text"))] = row
    rows = [r for r in (shown(r) for r in merged.values()) if r]
    scenarios = core.build_scenarios(rows, dump["signals"], dump["now"])
    members = {ts for s in scenarios for ts in s["members"]}
    day = lambda ts: ts[:10]  # noqa: E731
    before = Counter(day(r["ts"]) for r in rows)
    items_before = {d: episodes([r for r in rows if day(r["ts"]) == d]) for d in before}
    singles = [r for r in rows if r["ts"] not in members]
    items_after = Counter(day(r["ts"]) for r in singles)
    for s in scenarios:
        items_after[day(s["start"])] += 1
    print("day        rows  before  after")
    for d in sorted(before):
        print(f"{d}  {before[d]:4d}  {items_before[d]:6d}  {items_after[d]:5d}")
    print("by type", Counter(s["type"] for s in scenarios), "members", len(members))
    Path(out_path).write_text(json.dumps(scenarios, ensure_ascii=True))


if __name__ == "__main__":
    main(*sys.argv[1:4])
