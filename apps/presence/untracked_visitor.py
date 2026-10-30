"""
UntrackedVisitor - publishes sensor.untracked_visitor: is a person the house does not track
in the flat?

State is none, cleaner or guest. Attributes: who (the keypad user's name), since (when the
visit began, ISO), reason (one sentence for the current state) and end_reason (quiet, cap,
hard_cap or arrival after a visit, otherwise ""). who and since are "" at none. Consumers
read this entity, never the lock.

ON. lock.yale reports unlocked and its changed_by names a keypad person who is not a
resident: cleaner when the name is in `cleaners`, guest for any other person. Lock labels
(Auto Lock, Manual Unlock and the rest of `non_person`) are not people and never start a
visit. Names are matched word by word, ignoring case: an entry matches a keypad name that
begins with the entry's words, so residents are listed by first name only (the lock reports
full account names and this repo is public); a `non_person` label must match the whole name.
A second unlock by the same person during a visit belongs to the same visit; an unlock by
anyone else replaces it.

OFF. The first of
  * the front door is closed and every `activity` entity has been explicitly off for quiet_min
    minutes (the quiet clock restarts at every door edge and every activity change),
  * a resident in `persons` arrives home,
  * cap_hours have passed since the visit began,
  * hard_cap_hours have passed since the visit began, even with a hold.
For a cleaner visit, optional `hold` being on blocks the quiet exit and cap_hours; every
hold change restarts the quiet clock. Guests ignore the hold.

BLINDS. While the visitor is the cleaner, each blind in `blinds` is held at lowered_position
through its blind owner app (apps/blinds/bedroom_blind_owner.py) as the "visitor" source and
asked again every blind_reassert_min minutes, so the claim never lapses mid-visit. This app
never writes a cover itself. When the visit ends the claim is replaced by a short request
for the position the blind had when the visit began (restore_position when that could not be
read), then it lapses and whatever else drives the blind decides again. A blind the owner
refuses, because a hand outranks the visitor, is left alone for the rest of the visit.

Restart: the visit, its quiet clock, hold, end cause and the per-blind state are written to
state_file and read back at start; without the file the published entity is used.

Attribute values are strings only: set_state drops None, False and 0.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

NONE = "none"
CLEANER = "cleaner"
GUEST = "guest"

# Must equal blind_arbiter.SOURCE_VISITOR: the owner refuses a source it does not rank.
SOURCE = "visitor"

QUIET = "quiet"
CAP = "cap"
HARD_CAP = "hard_cap"
ARRIVAL = "arrival"

BLIND_STATES = ("pending", "lowered", "yielded")
UNLOCKED_STATES = ("unlocked", "unlocking", "open")

TICK_S = 30
REPUBLISH_S = 300


def _words(text):
    return tuple(str(text).casefold().split())


def _leads(words, entries):
    """True when the leading words of `words` are the words of any entry."""
    for entry in entries:
        lead = _words(entry)
        if lead and words[:len(lead)] == lead:
            return True
    return False


def classify(changed_by, residents, cleaners, non_person):
    """CLEANER or GUEST for a keypad person who is not a resident, else None."""
    if not isinstance(changed_by, str):
        return None
    words = _words(changed_by)
    if not words:
        return None
    if any(words == _words(label) for label in non_person):
        return None
    if _leads(words, residents):
        return None
    return CLEANER if _leads(words, cleaners) else GUEST


def is_on(state):
    return str(state).strip().lower() == "on"


def activity_state(state):
    """Tri-state activity: unknown must not become evidence that the flat is quiet."""
    normalized = str(state).strip().lower()
    return True if normalized == "on" else False if normalized == "off" else None


def end_reason(now, since, anchor, *, door_open, active, quiet_s, cap_s, hold=False, hard_cap_s=4 * 3600):
    """Why a visit that began at `since` is over, or None while it is still on. `anchor` is
    the last door edge or activity change; `active` blocks a quiet exit when activity is
    positive or unreadable. `hold` blocks quiet and the ordinary cap, never the hard cap."""
    if now - since >= hard_cap_s:
        return HARD_CAP
    if not hold and not door_open and not active and now - anchor >= quiet_s:
        return QUIET
    if not hold and now - since >= cap_s:
        return CAP
    return None


def restore_target(prior, default):
    """The position to give a blind back: where it was, else the configured default."""
    chosen = prior if prior is not None else default
    return None if chosen is None else int(chosen)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def sanitize_visit(raw):
    """A visit read back from the state file, or None when it is not one."""
    if not isinstance(raw, dict):
        return None
    kind, who, since = raw.get("kind"), raw.get("who"), _number(raw.get("since"))
    if kind not in (CLEANER, GUEST) or not isinstance(who, str) or not who.strip() or since is None:
        return None
    anchor = _number(raw.get("anchor"))
    blinds = {}
    raw_blinds = raw.get("blinds")
    for name, blind in (raw_blinds.items() if isinstance(raw_blinds, dict) else ()):
        if isinstance(blind, dict) and blind.get("state") in BLIND_STATES:
            prior = _number(blind.get("prior"))
            blinds[str(name)] = {
                "state": blind["state"],
                "prior": None if prior is None else int(prior),
                "asserted": _number(blind.get("asserted")) or 0.0,
            }
    return {"kind": kind, "who": who, "since": since,
            "anchor": since if anchor is None else anchor, "blinds": blinds}


def stamp(epoch, tz):
    return datetime.fromtimestamp(epoch, tz).isoformat(timespec="seconds")


def hhmm(epoch, tz):
    return datetime.fromtimestamp(epoch, tz).strftime("%H:%M")


def started_text(who, epoch, tz):
    return f"{who} unlocked the front door at {hhmm(epoch, tz)}"


def ended_text(why, detail, epoch, tz, quiet_min, cap_hours, hard_cap_hours=4):
    cause = {
        QUIET: f"the front door is closed and the flat has been quiet for {quiet_min:g} min",
        CAP: f"{cap_hours:g} h limit reached",
        HARD_CAP: f"{hard_cap_hours:g} h hard limit reached",
        ARRIVAL: f"{detail} arrived home",
    }[why]
    return f"Visit ended at {hhmm(epoch, tz)}: {cause}"


class UntrackedVisitor(hass.Hass):
    def initialize(self):
        a = self.args
        self.visitor_entity = a.get("entity", "sensor.untracked_visitor")
        self.lock_entity = a["lock"]
        self.door_entity = a["door"]
        self.activity = list(a["activity"])
        self.persons = list(a["persons"])
        self.residents = list(a["residents"])
        self.cleaners = list(a["cleaners"])
        self.non_person = list(a["non_person"])
        self.quiet_min = float(a.get("quiet_min", 15))
        self.cap_hours = float(a.get("cap_hours", 2))
        self.hard_cap_hours = float(a.get("hard_cap_hours", 4))
        self.hold_entity = a.get("hold")
        self.reassert_s = float(a.get("blind_reassert_min", 5)) * 60
        self.release_ttl_min = float(a.get("blind_release_ttl_min", 3))
        self.blinds = {str(name): dict(cfg) for name, cfg in (a.get("blinds") or {}).items()}
        self.state_file = Path(a.get("state_file") or Path(__file__).with_name("untracked_visitor_state.json"))

        self._lock = threading.RLock()
        self._visit = None
        self._reason = "No untracked person in the flat"
        self._end_reason = ""
        self._dirty = False
        self._published_at = 0.0
        self._warned = set()
        self._door_open = is_on(self.get_state(self.door_entity))
        self._active = {entity: activity_state(self.get_state(entity)) for entity in self.activity}
        self._hold = is_on(self.get_state(self.hold_entity)) if self.hold_entity else False

        with self._lock:
            now = self._now()
            self._load(now)
            self._step(now)

        self.listen_state(self._on_changed_by, self.lock_entity, attribute="changed_by")
        self.listen_state(self._on_door, self.door_entity)
        for entity in self.activity:
            self.listen_state(self._on_activity, entity)
        if self.hold_entity:
            self.listen_state(self._on_hold, self.hold_entity)
        for person in self.persons:
            self.listen_state(self._on_person, person, new="home")
        self.listen_event(self._on_plugin_started, "plugin_started")
        self.run_every(self._tick, f"now+{TICK_S}", TICK_S)

        current = self._visit["kind"] if self._visit else NONE
        self.log(f"UntrackedVisitor initialized: {self.visitor_entity} is {current}, "
                 f"watching {self.lock_entity}", level="INFO")

    # ---------- events ----------

    def _on_changed_by(self, entity, attribute, old, new, kwargs):
        kind = classify(new, self.residents, self.cleaners, self.non_person)
        if kind is None or str(self.get_state(self.lock_entity)) not in UNLOCKED_STATES:
            return
        with self._lock:
            self._unlocked_by(self._now(), kind, str(new).strip())

    def _on_door(self, entity, attribute, old, new, kwargs):
        with self._lock:
            self._door_open = is_on(new)
            self._touch()

    def _on_activity(self, entity, attribute, old, new, kwargs):
        with self._lock:
            self._active[entity] = activity_state(new)
            self._touch()

    def _on_hold(self, entity, attribute, old, new, kwargs):
        with self._lock:
            self._hold = is_on(new)
            self._dirty = True
            if self._visit is not None and self._visit["kind"] == CLEANER:
                self._touch()

    def _on_person(self, entity, attribute, old, new, kwargs):
        if old == "home":
            return
        with self._lock:
            if self._visit is not None:
                self._end(self._now(), ARRIVAL, entity.split(".", 1)[-1].capitalize())

    def _on_plugin_started(self, event_name, data, kwargs):
        with self._lock:
            self._publish(self._now())

    def _tick(self, kwargs):
        with self._lock:
            self._step(self._now())

    # ---------- the visit ----------

    def _unlocked_by(self, now, kind, who):
        visit = self._visit
        if visit is not None and visit["kind"] == kind and visit["who"] == who:
            visit["anchor"] = now
            self._dirty = True
            return
        blinds = {}
        if visit is not None and kind == CLEANER and visit["kind"] == CLEANER:
            blinds = visit["blinds"]
        elif visit is not None:
            self._release_blinds(visit)
        tz = self.get_now().tzinfo
        self._visit = {"kind": kind, "who": who, "since": now, "anchor": now, "blinds": blinds}
        self._reason = started_text(who, now, tz)
        self._end_reason = ""
        if kind == CLEANER:
            self._sync_blinds(now)
        self._persist()
        self._publish(now)
        self.log(f"UntrackedVisitor: {kind} - {self._reason}", level="INFO")

    def _touch(self):
        if self._visit is not None:
            self._visit["anchor"] = self._now()
            self._dirty = True

    def _step(self, now):
        visit = self._visit
        if visit is not None:
            activity_quiet = bool(self._active) and all(value is False for value in self._active.values())
            why = end_reason(now, visit["since"], visit["anchor"], door_open=self._door_open,
                             active=not activity_quiet,
                             quiet_s=self.quiet_min * 60, cap_s=self.cap_hours * 3600,
                             hold=visit["kind"] == CLEANER and self._hold,
                             hard_cap_s=self.hard_cap_hours * 3600)
            if why is not None:
                self._end(now, why)
        if self._visit is not None and self._visit["kind"] == CLEANER:
            self._sync_blinds(now)
        if self._dirty:
            self._persist()
        if now - self._published_at >= REPUBLISH_S:
            self._publish(now)

    def _end(self, now, why, detail=""):
        visit, self._visit = self._visit, None
        self._release_blinds(visit)
        tz = self.get_now().tzinfo
        self._reason = ended_text(why, detail, now, tz, self.quiet_min, self.cap_hours, self.hard_cap_hours)
        self._end_reason = why
        self._persist()
        self._publish(now)
        self.log(f"UntrackedVisitor: {visit['who']} ({visit['kind']}) - {self._reason}", level="INFO")

    # ---------- blinds ----------

    def _owner(self, cfg):
        try:
            app = self.get_app(cfg["owner_app"])
        except Exception:
            return None
        return app if app is not None and hasattr(app, "request") else None

    def _position(self, cover):
        try:
            return int(float(self.get_state(cover, attribute="current_position")))
        except (TypeError, ValueError):
            return None

    def _warn_once(self, key, message):
        if key not in self._warned:
            self._warned.add(key)
            self.log(f"UntrackedVisitor: {message}", level="WARNING")

    def _withdraw(self, owner, reason):
        try:
            owner.withdraw(SOURCE, reason=reason)
        except Exception as e:
            self.log(f"UntrackedVisitor: withdrawing the visitor claim failed: {e!r}", level="WARNING")

    def _sync_blinds(self, now):
        visit = self._visit
        for name, cfg in self.blinds.items():
            blind = visit["blinds"].setdefault(name, {"state": "pending", "prior": None, "asserted": 0.0})
            if blind["state"] == "yielded":
                continue
            if blind["state"] == "lowered" and now - blind["asserted"] < self.reassert_s:
                continue
            owner = self._owner(cfg)
            if owner is None:
                self._warn_once(("owner", name), f"no blind owner {cfg['owner_app']} for the {name} blind, "
                                "leaving it alone")
                continue
            if blind["state"] == "pending":
                blind["prior"] = self._position(cfg["cover"])
            try:
                res = owner.request(SOURCE, int(cfg["lowered_position"]), reason=f"{visit['who']} is cleaning")
            except Exception as e:
                self._warn_once(("request", name), f"asking the {name} blind owner failed: {e!r}")
                continue
            self._dirty = True
            if res and res.get("granted"):
                if blind["state"] == "pending":
                    self.log(f"UntrackedVisitor: {name} blind to {cfg['lowered_position']} "
                             f"(was {blind['prior']})", level="INFO")
                blind["state"], blind["asserted"] = "lowered", now
            else:
                blind["state"] = "yielded"
                self._withdraw(owner, "outranked, leaving the blind to its owner")
                self.log(f"UntrackedVisitor: {name} blind is held by {(res or {}).get('winner')}, "
                         "leaving it alone for this visit", level="INFO")

    def _release_blinds(self, visit):
        if visit is None or visit["kind"] != CLEANER:
            return
        for name, blind in visit["blinds"].items():
            cfg = self.blinds.get(name)
            if cfg is None or blind["state"] != "lowered":
                continue
            owner = self._owner(cfg)
            if owner is None:
                continue
            target = restore_target(blind["prior"], cfg.get("restore_position"))
            if target is None:
                self._withdraw(owner, "visit over")
                continue
            try:
                res = owner.request(SOURCE, target, reason="visit over", ttl_min=self.release_ttl_min)
            except Exception as e:
                self._warn_once(("restore", name), f"restoring the {name} blind failed: {e!r}")
                continue
            if res and res.get("granted"):
                self.log(f"UntrackedVisitor: {name} blind back to {target}", level="INFO")
            else:
                self._withdraw(owner, "outranked, leaving the blind to its owner")
                self.log(f"UntrackedVisitor: {name} blind is held by {(res or {}).get('winner')}, "
                         "not moving it back", level="INFO")

    # ---------- persistence and publishing ----------

    def _load(self, now):
        try:
            raw = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            self._visit = sanitize_visit(raw.get("visit"))
            if isinstance(raw.get("reason"), str) and raw["reason"]:
                self._reason = raw["reason"]
            if raw.get("end_reason") in (QUIET, CAP, HARD_CAP, ARRIVAL):
                self._end_reason = raw["end_reason"]
            if (self._visit is not None and self._visit["kind"] == CLEANER
                    and isinstance(raw.get("hold"), bool) and raw["hold"] != self._hold):
                self._visit["anchor"] = now
                self._dirty = True
        else:
            self._visit = self._visit_from_entity(now)
            if self._visit is None and self.get_state(self.visitor_entity) == NONE:
                reason = self.get_state(self.visitor_entity, attribute="reason")
                why = self.get_state(self.visitor_entity, attribute="end_reason")
                if isinstance(reason, str) and reason:
                    self._reason = reason
                if why in (QUIET, CAP, HARD_CAP, ARRIVAL):
                    self._end_reason = why
        if self._visit is not None:
            self._visit["since"] = min(self._visit["since"], now)
            self._visit["anchor"] = min(self._visit["anchor"], now)

    def _visit_from_entity(self, now):
        state = self.get_state(self.visitor_entity)
        if state not in (CLEANER, GUEST):
            return None
        who = self.get_state(self.visitor_entity, attribute="who")
        try:
            since = datetime.fromisoformat(str(self.get_state(self.visitor_entity, attribute="since"))).timestamp()
        except ValueError:
            return None
        if not who:
            return None
        reason = self.get_state(self.visitor_entity, attribute="reason")
        if reason:
            self._reason = str(reason)
        return {"kind": state, "who": str(who), "since": since, "anchor": now, "blinds": {}}

    def _persist(self):
        self._dirty = False
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"visit": self._visit, "reason": self._reason,
                                       "end_reason": self._end_reason, "hold": self._hold}))
            os.replace(tmp, self.state_file)
        except OSError as e:
            self.log(f"UntrackedVisitor: could not write {self.state_file}: {e!r}", level="WARNING")

    def _publish(self, now):
        visit = self._visit
        attributes = {
            "friendly_name": "Untracked visitor",
            "icon": "mdi:account-question",
            "who": visit["who"] if visit else "",
            "since": stamp(visit["since"], self.get_now().tzinfo) if visit else "",
            "reason": self._reason,
            "end_reason": self._end_reason if visit is None else "",
        }
        state = visit["kind"] if visit else NONE
        try:
            self.set_state(self.visitor_entity, state=state, attributes=attributes, replace=True)
        except Exception as e:
            self.log(f"UntrackedVisitor: publishing {self.visitor_entity} failed: {e!r}", level="WARNING")
        self._published_at = now

    def _now(self):
        return self.get_now().timestamp()
