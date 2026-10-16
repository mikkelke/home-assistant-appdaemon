#!/usr/bin/env python3
"""Who uses which dashboard control, counted from the recorder.

The dashboard calls script.dashboard_ui_tap for every Quick Access control and the home screen
controls that duplicate them. The script fires one dashboard_ui_tap event in the caller's own
context, so the recorder keeps who tapped (events.context_user_id_bin) and what (the event's ui and
detail). The ui `app.open` is the dashboard opening, at most once per device per five minutes.

    sudo docker exec -i homeassistant python3 - [DAYS] [--detail] [--all] < scripts/ui_usage_report.py
    ssh mke@10.21.0.5 'sudo docker exec -i homeassistant python3 - 30' < scripts/ui_usage_report.py

Prints taps per control and person over the last DAYS days (default 30), then app.open per person
per day. --detail splits each control by its detail, such as the room of a room-card tap. Taps
whose ui starts with test. are left out, and counted in a footnote, unless --all.

Read-only: the database is opened read-only, and only the user names are read from .storage/auth.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Copenhagen")
EVENT_TYPE = "dashboard_ui_tap"
APP_OPEN = "app.open"
TEST_PREFIX = "test."
NO_USER = "(none)"
DAY_S = 86400


def load_users(auth_path):
    """{user id: name} from .storage/auth. Nothing else in the file is kept."""
    with open(auth_path, encoding="utf-8") as handle:
        users = json.load(handle)["data"]["users"]
    return {user["id"]: user.get("name") or user["id"] for user in users}


def read_taps(con, since):
    """[(epoch, user id or None, ui, detail)] for every tap since `since`, oldest first."""
    row = con.execute("SELECT event_type_id FROM event_types WHERE event_type = ?", (EVENT_TYPE,)).fetchone()
    if row is None:
        return []
    rows = con.execute(
        "SELECT e.time_fired_ts, e.context_user_id_bin, d.shared_data"
        " FROM events e LEFT JOIN event_data d ON d.data_id = e.data_id"
        " WHERE e.event_type_id = ? AND e.time_fired_ts >= ? ORDER BY e.time_fired_ts",
        (row[0], since),
    )
    taps = []
    for epoch, user, shared in rows:
        try:
            data = json.loads(shared) if shared else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        taps.append((epoch, user.hex() if user else None, str(data.get("ui", "")) or "(no ui)", str(data.get("detail", ""))))
    return taps


def person(user_id, users):
    if user_id is None:
        return NO_USER
    return users.get(user_id) or "?" + user_id[:8]


def table(first, columns, rows, total_label="Total"):
    """Text table: `first` heads the row labels, `columns` the people, `rows` is [(label, {person: n})].
    A total column and a total row are added; a zero shows as a dash."""
    rows = [(label, {name: counts.get(name, 0) for name in columns}) for label, counts in rows]
    sums = {name: sum(counts[name] for _, counts in rows) for name in columns}
    head = [first, *columns, total_label]
    body = [[label, *[counts[name] for name in columns], sum(counts.values())] for label, counts in rows]
    body.append([total_label, *[sums[name] for name in columns], sum(sums.values())])
    cells = [head] + [[row[0], *["-" if n == 0 else str(n) for n in row[1:]]] for row in body]
    widths = [max(len(row[i]) for row in cells) for i in range(len(head))]
    lines = []
    for row in cells:
        lines.append("  ".join(row[0].ljust(widths[0]) if i == 0 else row[i].rjust(widths[i]) for i in range(len(row))).rstrip())
    lines.insert(len(lines) - 1, "")
    return "\n".join(lines)


def local_day(epoch):
    return datetime.fromtimestamp(epoch, TZ).date()


def dd_mm(day):
    return day.strftime("%d.%m")


def controls_section(taps, users, by_detail, since_day, today):
    counts = defaultdict(Counter)
    for _, user, ui, detail in taps:
        label = f"{ui} [{detail}]" if by_detail and detail else ui
        counts[label][person(user, users)] += 1
    title = f"Taps per control and person, {dd_mm(since_day)} to {dd_mm(today)}"
    if not counts:
        return f"{title}\n\nNo taps yet."
    columns = sorted({name for per in counts.values() for name in per})
    rows = sorted(counts.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    return f"{title}\n\n{table('control', columns, rows)}"


def opens_section(taps, users, today):
    counts = defaultdict(Counter)
    for epoch, user, _, _ in taps:
        counts[local_day(epoch)][person(user, users)] += 1
    title = "Dashboard opens (app.open) per person per day"
    if not counts:
        return f"{title}\n\nNo opens yet."
    columns = sorted({name for per in counts.values() for name in per})
    first = min(counts)
    days = [today - timedelta(days=offset) for offset in range((today - first).days + 1)]
    return f"{title}\n\n{table('day', columns, [(dd_mm(day), counts[day]) for day in days])}"


def report(taps, users, days, now, by_detail=False, include_test=False):
    today = local_day(now)
    since_day = local_day(now - days * DAY_S)
    left_out = 0 if include_test else sum(tap[2].startswith(TEST_PREFIX) for tap in taps)
    shown = taps if include_test else [tap for tap in taps if not tap[2].startswith(TEST_PREFIX)]
    controls = [tap for tap in shown if tap[2] != APP_OPEN]
    opens = [tap for tap in shown if tap[2] == APP_OPEN]
    parts = [controls_section(controls, users, by_detail, since_day, today), opens_section(opens, users, today)]
    if left_out:
        parts.append(f"Left out: {left_out} test rows (ui starting {TEST_PREFIX}). --all includes them.")
    return "\n\n".join(parts)


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description="Dashboard usage per person from the HA recorder.")
    parser.add_argument("days", nargs="?", type=int, default=30, help="how many days back (default 30)")
    parser.add_argument("--detail", action="store_true", help="split each control by its detail")
    parser.add_argument("--all", action="store_true", help=f"include taps whose ui starts with {TEST_PREFIX}")
    parser.add_argument("--db", default="/config/home-assistant_v2.db")
    parser.add_argument("--auth", default="/config/.storage/auth")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("days must be at least 1")
    now = time.time() if now is None else now
    try:
        users = load_users(args.auth)
        with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)) as con:
            con.execute("PRAGMA query_only = ON")
            taps = read_taps(con, now - args.days * DAY_S)
    except (OSError, KeyError, ValueError, sqlite3.Error) as error:
        print(f"Cannot read the recorder or the users: {error}", file=sys.stderr)
        return 1
    print(report(taps, users, args.days, now, args.detail, args.all))
    return 0


if __name__ == "__main__":
    sys.exit(main())
