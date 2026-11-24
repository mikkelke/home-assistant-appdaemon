#!/usr/bin/env python3
"""Seed the water tank's fill log with the taps made before the app kept one.

    python3 scripts/coffee_seed_fills.py            # dry run: lists what it would add
    python3 scripts/coffee_seed_fills.py --apply    # adds them to the box's coffee_state.json

The taps are the coffee_command water_filled events in HA's recorder, with the resident behind the account that
tapped (None for the wine-fridge tablet). Times are UTC, as the app writes them. A tap whose `at` is in tank.fills
already is left alone, so a second run adds nothing. Run it with CoffeeMachine stopped: the app saves its state
over this file when it stops.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HOST = "mke@10.21.0.5"
STATE = "/data/appdaemon/apps/coffee/coffee_state.json"

TAPS = (
    ("2026-10-25T11:23:09+00:00", "mikkel"),
    ("2026-11-01T12:24:26+00:00", None),
    ("2026-11-02T11:26:50+00:00", None),
    ("2026-11-02T12:20:44+00:00", None),
    ("2026-11-05T13:34:57+00:00", None),
    ("2026-11-06T08:00:18+00:00", "mikkel"),
    ("2026-11-07T07:53:38+00:00", None),
    ("2026-11-07T20:04:44+00:00", None),
    ("2026-11-07T20:17:14+00:00", None),
    ("2026-11-08T10:53:21+00:00", None),
    ("2026-11-10T11:59:25+00:00", None),
    ("2026-11-14T08:14:57+00:00", None),
    ("2026-11-17T12:31:28+00:00", "claudia"),
    ("2026-11-21T14:02:55+00:00", "mikkel"),
    ("2026-11-23T07:13:07+00:00", None),
)


def seed(state, taps=TAPS):
    """(state with the taps in tank.fills, the fills that were new)."""
    tank = state["tank"]
    fills = tank.get("fills") or []
    known = {fill["at"] for fill in fills}
    added = [{"at": at, "source": "tap", "user": user} for at, user in taps if at not in known]
    return {**state, "tank": {**tank, "fills": sorted(fills + added, key=lambda fill: fill["at"])}}, added


def ssh(command, **kwargs):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", HOST, command], text=True, check=True, timeout=60,
                          **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the merged state back")
    parser.add_argument("--state", type=Path, help="a local coffee_state.json instead of the box's")
    args = parser.parse_args()
    if args.state:
        state = json.loads(args.state.read_text())
    else:
        state = json.loads(ssh(f"sudo cat {STATE}", capture_output=True).stdout)
    if not isinstance(state.get("tank"), dict):
        sys.exit("no tank in the state file: wrong file?")
    seeded, added = seed(state)
    print(f"{len(seeded['tank']['fills'])} fills after, {len(added)} to add")
    for fill in added:
        print(f"  {fill['at']}  {fill['user'] or 'tablet'}")
    if not args.apply or not added:
        return
    text = json.dumps(seeded, indent=2, ensure_ascii=False)
    if args.state:
        tmp = args.state.with_name(args.state.name + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, args.state)
    else:
        ssh(f"sudo tee {STATE}.seed > /dev/null && sudo mv {STATE}.seed {STATE}", input=text)
    print("written")


if __name__ == "__main__":
    main()
