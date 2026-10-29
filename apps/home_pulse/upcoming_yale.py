"""Quiet Yale keypad/presence notes for HouseUpcoming. Only first names from configuration leave
this module; the cloud operator's account name is reduced before looking up its person.

Completed notes (including Done) persist; correlation candidates and 15-minute timers do not.
"""

import hashlib
from datetime import timedelta

from upcoming_core import make_row, utc_iso
from upcoming_maint import HOUSE_TZ, parse_iso

WAIT = timedelta(minutes=15)
ATTRIBUTION_WINDOW = timedelta(seconds=20)
ID_PREFIX = "maint:yale:"


def operators(raw):
    """Configured first name -> person, keyed case-insensitively."""
    result = {}
    for name, person in (raw or {}).items():
        if not isinstance(name, str) or not name.strip() or len(name.split()) != 1:
            raise ValueError("Yale operators must use first names only")
        if not isinstance(person, str) or not person.startswith("person."):
            raise ValueError("Yale operators must map to person entities")
        result[name.lower()] = {"name": name, "person": person}
    return result


def known_away(state):
    return isinstance(state, str) and bool(state.strip()) and state not in ("home", "unknown", "unavailable")


def operator(full, configured):
    if not isinstance(full, dict) or (full.get("attributes") or {}).get("method") != "keypad":
        return None
    name = full.get("state")
    first = name.split()[0].lower() if isinstance(name, str) and name.split() else ""
    return configured.get(first)


def row_id(person, at):
    stamp = hashlib.sha1(f"{person}:{at.isoformat()}".encode()).hexdigest()[:16]
    return ID_PREFIX + stamp


def restore(raw, configured):
    """Keep only completed notes for configured residents, including their Done marker."""
    if not isinstance(raw, dict):
        return {}
    people = {item["person"] for item in configured.values()}
    notes = {}
    for key, item in raw.items():
        if not isinstance(item, dict) or item.get("person") not in people:
            continue
        at = parse_iso(item.get("at"))
        if at is None or key != row_id(item["person"], at):
            continue
        note = {"person": item["person"], "at": at.isoformat()}
        done = parse_iso(item.get("done"))
        if done is not None:
            note["done"] = utc_iso(done)
        notes[key] = note
    return notes


def rows(notes, configured):
    people = {item["person"]: item["name"] for item in configured.values()}
    result = []
    for key, note in notes.items():
        if note.get("done") or note["person"] not in people:
            continue
        at = parse_iso(note["at"])
        clock = at.astimezone(HOUSE_TZ).strftime("%H:%M")
        result.append(make_row(key, "maint", f"{people[note['person']]}'s code used, not home",
                               f"front door {clock}; phone not home 15 min later", icon="mdi:door-open",
                               audience="admin", tap_done=True, skip_next=True))
    return result
