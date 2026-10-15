"""Row schema of ``sensor.house_upcoming`` (HouseUpcoming). Pure.

Entity shape:
    state: UTC ISO time of the last change to ``rows``
    attributes.expires: UTC ISO lease end; the dashboard drops every row once it has passed,
        so rows vanish within LEASE of the publisher stopping
    attributes.rows: list of rows, sorted by ``at`` then ``id``, untimed rows last

Row fields: ``id`` (stable), ``kind`` (rain | train | maint), ``at`` (UTC ISO, absent =
untimed), ``icon``, ``title``, ``detail``, ``chip``, ``area`` (HA area id a tap opens),
``audience`` ("admin") or ``audience_users`` (HA display names), ``next`` ("skip": the
dashboard's next chip passes the row over).

Every value is a string or a list of strings: AppDaemon 4.5.13 drops None, False and 0 from
set_state attributes at any depth and turns True into "true".
"""

import json
import re
from datetime import timedelta, timezone

MAX_ROWS = 30
TEXT_MAX = 60
CHIP_MAX = 30
NAME_MAX = 40
LEASE = timedelta(minutes=15)
KINDS = ("rain", "train", "maint")
BATTERY_ID_PREFIX = "maint:battery:"
OVERFLOW_ID = "maint:more"

_SPACE = re.compile(r"\s+")
_FIELDS = ("id", "kind", "at", "icon", "title", "detail", "chip", "area", "audience", "audience_users", "next")


def clean_text(value, limit=TEXT_MAX):
    """Printable ASCII on one line, at most `limit` characters; "" for anything but a string."""
    if not isinstance(value, str):
        return ""
    text = _SPACE.sub(" ", value.encode("ascii", "ignore").decode("ascii")).strip()
    return text[:limit].rstrip()


def utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def make_row(row_id, kind, title, detail="", *, at=None, icon="mdi:information-outline", area=None, chip=None,
             audience=None, audience_users=None, skip_next=False):
    row = {"id": row_id, "kind": kind, "icon": icon, "title": clean_text(title)}
    detail = clean_text(detail)
    if detail:
        row["detail"] = detail
    if at is not None:
        row["at"] = utc_iso(at)
    if area:
        row["area"] = area
    chip = clean_text(chip, CHIP_MAX)
    if chip:
        row["chip"] = chip
    if audience == "admin":
        row["audience"] = "admin"
    elif audience_users:
        names = [clean_text(name, NAME_MAX) for name in audience_users]
        names = [name for name in names if name]
        if names:
            row["audience_users"] = names
    if skip_next:
        row["next"] = "skip"
    return row


def _is_ascii_text(value, limit):
    return isinstance(value, str) and value == clean_text(value, limit)


def is_valid_row(row):
    """The publisher's own contract, so a builder bug is caught before it reaches a phone."""
    if not isinstance(row, dict) or set(row) - set(_FIELDS):
        return False
    if not row.get("id") or not _is_ascii_text(row["id"], 120) or row.get("kind") not in KINDS:
        return False
    if not row.get("title") or not _is_ascii_text(row["title"], TEXT_MAX):
        return False
    for key, limit in (("detail", TEXT_MAX), ("chip", CHIP_MAX), ("icon", 80), ("area", 60), ("at", 40)):
        if key in row and (not row[key] or not _is_ascii_text(row[key], limit)):
            return False
    if "audience" in row and row["audience"] != "admin":
        return False
    if "next" in row and row["next"] != "skip":
        return False
    if "audience_users" in row:
        users = row["audience_users"]
        if "audience" in row or not isinstance(users, list) or not users:
            return False
        if not all(isinstance(name, str) and name and _is_ascii_text(name, NAME_MAX) for name in users):
            return False
    if row["kind"] == "maint" and row.get("audience") != "admin":
        return False
    return True


def _sort_key(row):
    return (0, row["at"], row["id"]) if "at" in row else (1, "", row["id"])


def finalize_rows(rows):
    """Valid rows in publish order. Past MAX_ROWS the battery rows that do not fit fold into one
    "N more devices" row, so the cap never hides that maintenance exists."""
    rows = sorted((row for row in rows if is_valid_row(row)), key=_sort_key)
    if len(rows) <= MAX_ROWS:
        return rows
    batteries = [row for row in rows if row["id"].startswith(BATTERY_ID_PREFIX)]
    others = [row for row in rows if not row["id"].startswith(BATTERY_ID_PREFIX)]
    room = max(0, MAX_ROWS - len(others) - 1)
    hidden = len(batteries) - room
    overflow = make_row(OVERFLOW_ID, "maint", f"{hidden} more devices", "batteries or offline",
                        icon="mdi:battery-alert-variant-outline", audience="admin")
    return sorted(others + batteries[:room] + [overflow], key=_sort_key)


def canonical(rows):
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def lease_until(now):
    return utc_iso(now + LEASE)
