#!/usr/bin/env python3
"""Cut a replay tape of one real brew out of HA's recorder, for apps/coffee/tests/fixtures.

    python3 scripts/coffee_tape.py 2026-10-21T06:40:00Z --minutes 15 > apps/coffee/tests/fixtures/tape_20261021.json

The recorder is opened read-only inside the homeassistant container. The tape holds, in time order, every
state the coffee app listens to (plug, Connect, heartbeat, scale, water, the brew event entity, brew status,
pause, machine status, current pour) and the integration's xbloom_* and coffee_command events in the
window, as [seconds from the start, step]. A step is ["state", entity, state, attributes] or ["event",
name, data, user id]. States that are times (the event entity, the heartbeat) are kept as "@<seconds>"
so the tape replays at any clock. test_coffee_machine replays every fixtures/tape_*.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime

HOST = "mke@10.21.0.5"
ENTITIES = ("sensor.pour_over_plug_power", "switch.pour_over_plug", "switch.xbloom_studio_connect",
            "sensor.xbloom_studio_status_updated", "sensor.xbloom_studio_scale_weight",
            "sensor.xbloom_studio_water_dispensed", "event.xbloom_studio_brew_event",
            "sensor.xbloom_studio_brew_status", "binary_sensor.xbloom_studio_brew_paused",
            "sensor.xbloom_studio_machine_status", "sensor.xbloom_studio_current_pour")
TIMES = ("event.xbloom_studio_brew_event", "sensor.xbloom_studio_status_updated")

EXTRACT = r'''
import json, sqlite3, sys
start, end = float(sys.argv[1]), float(sys.argv[2])
entities = sys.argv[3].split(",")
con = sqlite3.connect("file:/config/home-assistant_v2.db?mode=ro", uri=True, timeout=10)
out = []
for entity in entities:
    row = con.execute("select metadata_id from states_meta where entity_id=?", (entity,)).fetchone()
    if not row:
        continue
    rows = con.execute("select s.last_updated_ts, s.state, a.shared_attrs from states s "
                       "left join state_attributes a on a.attributes_id = s.attributes_id "
                       "where s.metadata_id=? and s.last_updated_ts between ? and ? order by s.last_updated_ts",
                       (row[0], start, end)).fetchall()
    for ts, state, attrs in rows:
        attrs = json.loads(attrs or "{}")
        out.append({"ts": ts, "kind": "state", "entity": entity, "state": state, "attrs": attrs})
rows = con.execute("select e.time_fired_ts, t.event_type, d.shared_data, hex(e.context_user_id_bin) from events e "
                   "join event_types t on t.event_type_id = e.event_type_id "
                   "left join event_data d on d.data_id = e.data_id where e.time_fired_ts between ? and ? "
                   "and (t.event_type like 'xbloom%' or t.event_type = 'coffee_command') order by e.time_fired_ts",
                   (start, end)).fetchall()
for ts, name, data, user in rows:
    data = json.loads(data or "{}")
    out.append({"ts": ts, "kind": "event", "event": name, "data": data, "user": (user or "").lower()})
print(json.dumps(out))
'''


def epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("start", help="UTC ISO time a little before the brew")
    parser.add_argument("--minutes", type=float, default=15)
    args = parser.parse_args()
    start = epoch(args.start)
    end = start + args.minutes * 60
    command = f"sudo docker exec -i homeassistant python3 - {start} {end} {','.join(ENTITIES)}"
    raw = json.loads(subprocess.run(["ssh", "-o", "BatchMode=yes", HOST, command], input=EXTRACT, capture_output=True,
                                    text=True, check=True, timeout=300).stdout)
    tape = []
    for item in sorted(raw, key=lambda i: i["ts"]):
        offset = round(item["ts"] - start, 3)
        if item["kind"] == "event":
            tape.append([offset, ["event", item["event"], item["data"], item["user"] or None]])
            continue
        state = item["state"]
        if item["entity"] in TIMES:
            try:
                state = f"@{round(epoch(state) - start, 3)}"
            except ValueError:
                pass
        tape.append([offset, ["state", item["entity"], state, item["attrs"]]])
    print(json.dumps({"start": args.start, "minutes": args.minutes, "tape": tape}, separators=(",", ":")))


if __name__ == "__main__":
    main()
