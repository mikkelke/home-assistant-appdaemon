"""Row schema of ``sensor.house_upcoming`` (HouseUpcoming). Pure.

Entity shape:
    state: UTC ISO time of the last change to ``rows``
    attributes.expires: UTC ISO lease end; the dashboard drops every row once it has passed,
        so rows vanish within LEASE of the publisher stopping
    attributes.rows: list of rows, sorted by ``at`` then ``id``, untimed rows last

Row fields: ``id`` (stable), ``kind`` (rain | train | maint), ``at`` (UTC ISO, absent =
untimed), ``icon``, ``title``, ``detail``, ``chip``, ``area`` (HA area id a tap opens),
``audience`` ("admin") or ``audience_users`` (HA display names), ``next`` ("skip": the
dashboard's next chip passes the row over). A maintenance row may carry what its sheet shows, and
a tap on such a row opens that sheet instead of its room: ``info`` (short lines), ``steps`` (what
to do, in order) with ``warn`` (the safety note), ``done`` ("tap": a Done button, which fires
house_upcoming_done with the row id), ``items`` (a plain list, one line each, for a row that
summarises several things, such as the plants) and ``plants`` (a list of records for a row that
stands in for several plants, such as the ones due: ``id`` and ``name``, and ``amount``, ``detail``,
``where``, ``aka``, ``photo`` and ``done`` when there is something to say; a ``done`` of "tap" is a Done
button that fires house_upcoming_done with the record's ``id``).

Every value is a string or a list of strings, a record of ``plants`` a dict of strings: AppDaemon
4.5.13 drops None, False and 0 from set_state attributes at any depth and turns True into "true".
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
INFO_MAX = 4
STEPS_MAX = 8
STEP_MAX = 150
ITEMS_MAX = 20
ID_MAX = 120
BATTERY_ID_PREFIX = "maint:battery:"
OVERFLOW_ID = "maint:more"

_SPACE = re.compile(r"\s+")
_FIELDS = ("id", "kind", "at", "icon", "title", "detail", "chip", "area", "audience", "audience_users", "next",
           "done", "info", "steps", "warn", "items", "plants")
_PLANT_LIMITS = {"id": ID_MAX, "name": TEXT_MAX, "amount": TEXT_MAX, "detail": TEXT_MAX, "where": TEXT_MAX,
                 "aka": TEXT_MAX, "photo": TEXT_MAX, "done": TEXT_MAX}


def clean_text(value, limit=TEXT_MAX):
    """Printable ASCII on one line, at most `limit` characters; "" for anything but a string."""
    if not isinstance(value, str):
        return ""
    text = _SPACE.sub(" ", value.encode("ascii", "ignore").decode("ascii")).strip()
    return text[:limit].rstrip()


def utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _plant_record(record):
    record = record if isinstance(record, dict) else {}
    texts = {key: clean_text(record.get(key), limit) for key, limit in _PLANT_LIMITS.items()}
    return {key: text for key, text in texts.items() if text}


def make_row(row_id, kind, title, detail="", *, at=None, icon="mdi:information-outline", area=None, chip=None,
             audience=None, audience_users=None, skip_next=False, tap_done=False, info=None, steps=None, warn=None,
             items=None, plants=None):
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
    if tap_done:
        row["done"] = "tap"
    lines = [clean_text(line) for line in info or []]
    lines = [line for line in lines if line][:INFO_MAX]
    if lines:
        row["info"] = lines
    todo = [clean_text(step, STEP_MAX) for step in steps or []]
    todo = [step for step in todo if step][:STEPS_MAX]
    if todo:
        row["steps"] = todo
    note = clean_text(warn, STEP_MAX)
    if note and todo:
        row["warn"] = note
    listed = [clean_text(line) for line in items or []]
    listed = [line for line in listed if line][:ITEMS_MAX]
    if listed:
        row["items"] = listed
    records = [_plant_record(record) for record in plants or []]
    records = [record for record in records if "id" in record and "name" in record][:ITEMS_MAX]
    if records:
        row["plants"] = records
    return row


def _is_ascii_text(value, limit):
    return isinstance(value, str) and value == clean_text(value, limit)


def _is_plant_record(record):
    if not isinstance(record, dict) or set(record) - set(_PLANT_LIMITS) or not {"id", "name"} <= set(record):
        return False
    if not all(text and _is_ascii_text(text, _PLANT_LIMITS[key]) for key, text in record.items()):
        return False
    return record.get("done", "tap") == "tap"


def is_valid_row(row):
    """The publisher's own contract, so a builder bug is caught before it reaches a phone."""
    if not isinstance(row, dict) or set(row) - set(_FIELDS):
        return False
    if not row.get("id") or not _is_ascii_text(row["id"], ID_MAX) or row.get("kind") not in KINDS:
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
    if "done" in row and (row["done"] != "tap" or row.get("kind") != "maint"):
        return False
    if "info" in row:
        lines = row["info"]
        if not isinstance(lines, list) or not 0 < len(lines) <= INFO_MAX:
            return False
        if not all(line and _is_ascii_text(line, TEXT_MAX) for line in lines):
            return False
    if "steps" in row:
        steps = row["steps"]
        if row.get("kind") != "maint" or not isinstance(steps, list) or not 0 < len(steps) <= STEPS_MAX:
            return False
        if not all(step and _is_ascii_text(step, STEP_MAX) for step in steps):
            return False
    if "warn" in row and ("steps" not in row or not row["warn"] or not _is_ascii_text(row["warn"], STEP_MAX)):
        return False
    if "items" in row:
        listed = row["items"]
        if row.get("kind") != "maint" or not isinstance(listed, list) or not 0 < len(listed) <= ITEMS_MAX:
            return False
        if not all(line and _is_ascii_text(line, TEXT_MAX) for line in listed):
            return False
    if "plants" in row:
        records = row["plants"]
        if row.get("kind") != "maint" or not isinstance(records, list) or not 0 < len(records) <= ITEMS_MAX:
            return False
        if not all(_is_plant_record(record) for record in records):
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
