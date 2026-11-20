"""HouseEvents - plain-English "why did the house just do that" feed for the dashboard.

Two kinds of entries (v2 - reshaped after user feedback that raw on/off logging of
things you can see/hear yourself has no value):

1. cause -> effect reports from the automation apps themselves, fired as a
   `house_events_report` AppDaemon event ("Rain on the roof sensor" -> "Close the
   terrace door" from WeatherOpeningAlert).
   Event-based on purpose: no get_app coupling, no init-order dependency - an
   emitting app just fire_event()s and never notices whether this app is running.
   An observer app reporting a plain fact with no automation behind it sends `text`
   alone instead ("Kristine started Spotify in Kitchen" - MusicAttribution).
2. A small set of observed appliance milestones that carry information you can't
   see from the machine itself (finished + energy used; emptied).
3. Observed lock activity (v3): "Apartment door unlocked" by Mikkel. The authoritative BLE
   lock entity carries no attribution, so the entry is emitted after a short delay and
   enriched from the cloud twin's `changed_by` if that attribute is fresh by then - see
   `lock_event` and the `locks`/`lock_attribution` app config. The thumb-turn from inside
   gets no row: opening the door to put the trash out is not news.
4. Who came in or went out (v6), from sensor.door_entry: a resident coming in names the
   apartment-door unlock row of that opening in `by`, so an opening stays one row; an arrival
   with no such row (a door left on the latch) gets its own "Came home" row, and a resident
   going out always gets a "Went out" row. See `entry_groups` and `apply_entry`.
5. The night (v6): one "Night mode on" row per night from the household night boolean, and
   one "Went to bed" row per resident per night, seen only by that resident (and admins),
   left out when it fell with the household row. See `night_key`.

Feed shape (attributes, replace=True on every publish):
    events: newest-first list of {"ts": <UTC ISO>, "icon": <mdi:...>, "text": <ascii>,
                                  "cause": <ascii, optional>, "effect": <ascii, optional>,
                                  "by": <person display name, optional - v4>}
State = ts of the newest event (cheap change signal for the dashboard).
`by` carries the acting HUMAN when knowable (report payloads, lock attribution, and the
resident who started or emptied an appliance run - see appliance_actor, and enrich_by for
an emptier the monitor can only name minutes after the row was written);
the dashboard renders it as its own muted "By <name>" line (HA-Activity-style), so
emitters keep cause/effect wording neutral instead of baking names into sentences.
`audience` (v5): the literal "admin" hides an entry from non-admin dashboard viewers -
plumbing-level entries (override on/off/expiry, self-heals, Mikkel's own room's
automations) that the housemates shouldn't wade through. Absent = everyone. Only the
exact value "admin" narrows; anything else stays public (hiding is the privileged
direction - a typo must never vanish an entry for the admin).
`audience_users` (v5.1): list of HA user display names (matched case-insensitively by
the dashboard) who see the entry besides admins - room-scoped entries like "Claudias
room lights switched to manual" -> ["Claudia"]. Mutually exclusive with
audience="admin" (admin-only outranks; the users list is ignored then).

Persistence (v3): entity first, state file second. The feed is re-read from the
entity on app reload (survives code deploys), and from `house_events_state.json`
next to this module when the entity is gone - AppDaemon set_state entities are
ephemeral across HA RESTARTS (see appdaemon-deploy notes), which used to wipe the
feed. The file is rewritten on every added event; deploy.sh syncs tracked files
only and never deletes, so the box's copy is safe.

EMITTER CONTRACT (user feedback 2026-07-16, after phantom lift entries): a report must
ride an ACTION ACTUALLY TAKEN on a GENUINE transition - never an init/startup/heal/
periodic-verify path re-asserting a state that already holds (track your state and skip;
see bedroom_tv_control's initial_state_check). Health/self-maintenance chatter does not
belong in this feed unless something user-visible actually happened (radio_watchdog's
restart qualifies; its silent checks do not). When the acting HUMAN is knowable, name
them: watch the raw ``state_changed`` event (listen_state drops HA's context), resolve
``context.user_id`` through person entities, and fall back to neutral wording rather
than guessing (see manual_override_timeout).
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

HOUSE_TZ = ZoneInfo("Europe/Copenhagen")
MAX_EVENTS = 40
# One source emitting the exact same text by the same person again within this window is flapping
# (e.g. washer Unemptied->Emptied->Off lands in the same second), not news.
DUP_SUPPRESS_S = 300
# An emptied row still takes the emptier's name this long after it was written (see enrich_by).
EMPTIED_ENRICH_S = 900
# Reports come from other apps but are still validated like external data - a bug
# elsewhere must not be able to bloat the feed entity past what a dashboard expects.
MAX_TEXT_LEN = 120
# An apartment-door lock row belongs to the door opening it lies within this many seconds of: it is
# written lock_report_delay_s after the lock moved, which is before or just after the door opens
# when someone unlocks to come in.
ENTRY_MATCH_BEFORE_S = 90
ENTRY_MATCH_AFTER_S = 60
# sensor.door_entry decides 30 s after the door opened; an older decision is a replay.
ENTRY_FRESH_S = 120
ENTRY_TEXTS = {"entered": "Came home", "left": "Went out"}
ENTRY_ICON = "mdi:door-open"
NIGHT_TEXT = "Night mode on"
NIGHT_ICON = "mdi:weather-night"
BED_TEXT = "Went to bed"
BED_ICON = "mdi:bed"
# A sleeper's row is written this long after their sleep mode went on, and left out when the
# household night row is within BED_WITH_NIGHT_S of that moment: the first sleeper starts the night.
BED_REPORT_DELAY_S = 60
BED_WITH_NIGHT_S = 120

APPLIANCE_ICONS = {
    "washer": "mdi:washing-machine",
    "dishwasher": "mdi:dishwasher",
    "dryer": "mdi:tumble-dryer",
}

# ---------------------------------------------------------------------------
# Pure builders/validators - module-level so the stdlib unittest gate can cover
# them without an AppDaemon runtime. Every returned text must be plain ASCII.
# ---------------------------------------------------------------------------


def appliance_event(name, old, new, attrs):
    """Event tuple (icon_key, text) for an appliance milestone worth telling humans about.

    Only finished (+ energy - information the machine's own panel doesn't show) and
    emptied. Deliberately NOT "started": whoever started it knows, and the cards
    already show live progress.
    """
    if old in (None, "unavailable", "unknown") or new == old:
        return None
    if new == "Unemptied":
        energy = attrs.get("energy_used")
        try:
            energy_txt = f" - used {float(energy):.2f} kWh" if energy not in (None, "") else ""
        except (TypeError, ValueError):
            energy_txt = ""
        return "finished", f"{name} finished{energy_txt}"
    if new == "Emptied":
        # A drum clean runs with an empty drum: opening the door airs it, nothing is emptied.
        if attrs.get("detected_programme") == "drum_clean":
            return None
        return "emptied", f"{name} emptied"
    return None


# The backfill (rows worked out afterwards from the recorder, see backfill_rows) is published whole.
MAX_BACKFILL = 300


def backfill_rows(raw):
    """The valid rows of a backfill file's list, newest first, at most MAX_BACKFILL."""
    if not isinstance(raw, list):
        return []
    rows = [e for e in raw if isinstance(e, dict) and _ts(e) is not None and isinstance(e.get("text"), str) and e["text"]]
    return sorted(rows, key=_ts, reverse=True)[:MAX_BACKFILL]


def sanitize_feed(events):
    """Validated copy of an events list from ANY persistence layer (entity attributes
    or the state file) - malformed entries dropped, newest-first order trusted (the
    writer's documented contract), hard-capped."""
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict) and e.get("ts") and e.get("text")][:MAX_EVENTS]


# Residents, by first name (lowercase). The Yale cloud reports full account names
# ("Mikkel Eskildsen") while every other feed source says "Mikkel" - _first_name_if_resident
# trims ONLY names that start with one of these, so non-person lock users ("Cleaning",
# "Auto Lock") and unknown values are never clipped to a misleading first word.
RESIDENT_FIRST_NAMES = ("mikkel", "kristine", "claudia")


def _first_name_if_resident(who):
    """'Mikkel Eskildsen' -> 'Mikkel'; anything not starting with a resident first name
    passes through untouched."""
    first = who.split()[0] if who.split() else who
    return first if first.lower() in RESIDENT_FIRST_NAMES else who


# ActorAttribution methods that name a person on evidence: the only person home
# (sole_occupant), the HA user behind a dashboard tap (dashboard_action), the one resident
# left once the others had stayed in their own rooms (room_elimination), a phone at the machine
# (ble_proximity), whoever confirmed the programme (programme_confirmed), or for the washer and
# dryer the one named on the other side of the load (same_as_starter, same_as_emptier).
RELIABLE_ACTOR_METHODS = ("sole_occupant", "dashboard_action", "room_elimination", "ble_proximity",
                          "programme_confirmed", "same_as_starter", "same_as_emptier")


def _resident_name(person):
    """'claudia' -> 'Claudia' for a resident's person key, else None."""
    key = person.strip().lower() if isinstance(person, str) else ""
    return key.capitalize() if key in RESIDENT_FIRST_NAMES else None


def appliance_actor(kind, attrs):
    """Display name of the resident who started the run (a "finished" row) or emptied it
    (an "emptied" row), or None.

    A wrong name is worse than none: only a resident named by a reliable method counts. The
    monitors publish the starter as started_by + started_by_method and the emptier as
    emptied_by + emptied_by_method; without a reliable method there is no name.
    """
    if kind == "finished" and attrs.get("started_by_method") in RELIABLE_ACTOR_METHODS:
        return _resident_name(attrs.get("started_by"))
    if kind == "emptied" and attrs.get("emptied_by_method") in RELIABLE_ACTOR_METHODS:
        return _resident_name(attrs.get("emptied_by"))
    return None


def enrich_by(events, appliance, by, now):
    """Copy of the newest-first feed `events` in which the newest "<appliance> emptied" row has `by`,
    when it has none yet and is at most EMPTIED_ENRICH_S old at `now` (an aware datetime); every
    other row, and any row it cannot date, is left as it was.

    A row is written once, at the state change, and a monitor can name the emptier minutes later
    (room elimination settles ~6.5 min after the door opened), so the name arrives after the row.
    Only the newest row of that appliance is considered: an older one belongs to an earlier emptying.
    """
    text = f"{appliance} emptied"
    out = list(events)
    for i, event in enumerate(out):
        if event.get("text") != text:
            continue
        if event.get("by"):
            break
        try:
            age_s = (now - datetime.fromisoformat(event["ts"])).total_seconds()
        except (ValueError, TypeError, KeyError):
            break
        if 0 <= age_s <= EMPTIED_ENRICH_S:
            out[i] = dict(event, by=by)
        break
    return out


# Yale's own operator words for an operation no lock user made: "<how> <what>", such as
# "One-Touch Lock", "Auto Lock", "Bluetooth Unlock" or "Manual Unlock" (yalexs activity.py).
YALE_HOW = ("Auto", "One-Touch", "Bluetooth", "Manual")
YALE_WHAT = ("Lock", "Unlock", "Unlatch")
# The integration's word for an operation made through Home Assistant.
YALE_HA_OPERATORS = ("Home Assistant", "Home Assistant user", "Home Assistant-bruger")
# An ArrivalUnlock unlock names the lock row that follows it when the lock moved within this long.
ARRIVAL_HINT_S = 30


def yale_system_operator(who):
    """True for a changed_by that names how the lock moved rather than who moved it."""
    parts = who.split()
    return who in YALE_HA_OPERATORS or (len(parts) == 2 and parts[0] in YALE_HOW and parts[1] in YALE_WHAT)


def lock_event(name, old, new, changed_by, arrival_by=None):
    """Event tuple (icon, text, by) for a lock transition worth telling humans about, or None.

    Two kinds of unlock are news:
    - a NAMED unlock: "who came in while I wasn't looking" (a resident by name,
      first-name-trimmed, or a lock user like Cleaning);
    - an unlock ArrivalUnlock made for `arrival_by`.
    Everything else is just the door being used: locking (manual or auto) is the steady state
    the lock always returns to, and an unattributed unlock tells nobody anything. The thumb-turn
    from inside is not news either (trash out, a guest let in): a resident going out gets a
    "Went out" row from sensor.door_entry instead. Yale's own
    operator words never become `by`: on an unlock a lock word is the previous operation's, left
    in changed_by because the cloud had not reported this one yet.
    Lock FAILURES are lock_health's job (phone push + its own admin feed entries), not this
    observer's.
    """
    if old in (None, "unavailable", "unknown") or new == old:
        return None
    if new != "unlocked":
        return None
    if arrival_by:
        return "mdi:lock-open-variant", f"{name} unlocked on arrival", arrival_by
    if not isinstance(changed_by, str) or not changed_by.strip():
        return None
    who = changed_by.strip()[:MAX_TEXT_LEN]
    if yale_system_operator(who):
        return None
    return "mdi:lock-open-variant", f"{name} unlocked", _first_name_if_resident(who)


def _names_text(names):
    """["Claudia"] -> "Claudia"; ["Claudia", "Kristine"] -> "Claudia and Kristine"."""
    names = [n for n in names if n]
    return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + (names[-1] if names else "")


def entry_groups(attrs):
    """[(kind, by)] for a sensor.door_entry judgement: ("entered", "Claudia") when anyone came
    in, ("left", "Mikkel and Kristine") when anyone went out, both when both happened; [] for
    an opening no resident's phone explains (state unknown)."""
    if not isinstance(attrs, dict):
        return []
    out = []
    for kind in ("entered", "left"):
        names = attrs.get(kind)
        if isinstance(names, (list, tuple)):
            names = [n.strip()[:40] for n in names if isinstance(n, str) and n.strip()]
            if names:
                out.append((kind, _names_text(names)))
    return out


def _ts(event):
    try:
        return datetime.fromisoformat(event["ts"]).timestamp()
    except (ValueError, TypeError, KeyError):
        return None


# The lock row an entry kind can name: someone who came in unlocked the door. Going out leaves no
# lock row (the thumb-turn is not news), so a departure always gets its own "Went out" row.
ENTRY_LOCK_TEXTS = {"entered": ("{} unlocked", "{} unlocked on arrival"), "left": ()}


def apply_entry(events, lock_names, kind, by, opened_at):
    """(events, matched): a copy of the newest-first feed in which the apartment-door lock row of the
    door opening at `opened_at` (epoch) carries `by` and entry=kind, and whether one was found.

    The row must read as that kind (ENTRY_LOCK_TEXTS), lie within ENTRY_MATCH_BEFORE_S before to
    ENTRY_MATCH_AFTER_S after the opening and carry no entry yet. Its text stays as written: the
    dashboard knows a row by its time and text, so a changed text would show it twice in history.
    The lock's own attribution ("One-Touch Lock", a keypad name) gives way to the phones: they say
    who walked through, the lock only who turned it."""
    texts = {text.format(name) for text in ENTRY_LOCK_TEXTS[kind] for name in lock_names}
    out = list(events)
    for i, event in enumerate(out):
        ts = _ts(event)
        if ts is None:
            continue
        if ts < opened_at - ENTRY_MATCH_BEFORE_S:
            break
        if ts > opened_at + ENTRY_MATCH_AFTER_S or event.get("entry") or event.get("text") not in texts:
            continue
        out[i] = dict(event, by=by, entry=kind)
        return out, True
    return out, False


def night_key(at):
    """The night an aware datetime belongs to, as the house date it started on: 01:30 on the
    16th is the night of the 15th. Noon is the boundary."""
    return (at.astimezone(HOUSE_TZ) - timedelta(hours=12)).date().isoformat()


def written_tonight(events, text, by, at):
    """True when the feed already has a `text` row by `by` (None = no name) in the night of `at`."""
    key = night_key(at)
    for event in events:
        if event.get("text") != text or event.get("by") != by:
            continue
        try:
            if night_key(datetime.fromisoformat(event["ts"])) == key:
                return True
        except (ValueError, TypeError, KeyError):
            continue
    return False


def night_row_near(events, at):
    """True when a NIGHT_TEXT row lies within BED_WITH_NIGHT_S of `at` (epoch)."""
    for event in events:
        if event.get("text") != NIGHT_TEXT:
            continue
        ts = _ts(event)
        if ts is not None and abs(ts - at) <= BED_WITH_NIGHT_S:
            return True
    return False


def _clean_str(value):
    return value.strip()[:MAX_TEXT_LEN] if isinstance(value, str) and value.strip() else None


def build_report_event(data):
    """Validated {icon, text, cause, effect, by} from a house_events_report payload, or None.

    An automation explains WHY it acted with non-empty `cause` and `effect`. An observer
    reporting a plain fact with no automation behind it (who started the music) sends
    `text` alone instead; cause/effect win when both are present. `icon` optional; `by`
    (v4) optional acting-person display name - the dashboard renders it as its own muted
    "By <name>" line, so emitters keep the cause/effect wording neutral instead of baking
    names into sentences. Length-capped: a misbehaving emitter must not be able to bloat
    the feed entity.
    """
    if not isinstance(data, dict):
        return None
    cause = _clean_str(data.get("cause"))
    effect = _clean_str(data.get("effect"))
    if cause and effect:
        text = f"{cause} -> {effect}"
    else:
        cause = effect = None
        text = _clean_str(data.get("text"))
        if not text:
            return None
    icon = data.get("icon")
    if not isinstance(icon, str) or not icon.startswith("mdi:"):
        icon = "mdi:auto-fix"
    by = data.get("by")
    by = by.strip()[:MAX_TEXT_LEN] if isinstance(by, str) and by.strip() else None
    # v5: only the literal "admin" narrows the audience; anything else (absent, typo'd,
    # malicious) stays visible to everyone - hiding is the privileged direction here, a
    # bad value must never make an entry vanish for the admin.
    audience = "admin" if data.get("audience") == "admin" else None
    # v5.1: audience_users = HA user display names (e.g. ["Claudia"]) who see the entry
    # besides admins. Only meaningful without audience="admin" (admin already outranks it).
    # Same fail-open stance: a malformed list degrades to public, never to hidden.
    audience_users = None
    if audience is None:
        raw_users = data.get("audience_users")
        if isinstance(raw_users, (list, tuple)):
            cleaned = []
            for user in raw_users:
                if isinstance(user, str) and user.strip():
                    name = user.strip()[:40]
                    if name not in cleaned:
                        cleaned.append(name)
                if len(cleaned) >= 5:
                    break
            audience_users = cleaned or None
    return {
        "icon": icon,
        "text": text,
        "cause": cause,
        "effect": effect,
        "by": by,
        "audience": audience,
        "audience_users": audience_users,
    }


class HouseEvents(hass.Hass):
    def initialize(self):
        self.feed_entity = self.args.get("feed_entity", "sensor.house_events")
        self._state_file = Path(self.args.get("state_file") or Path(__file__).with_name("house_events_state.json"))
        self.events = self._load_previous_feed()
        self._publish_backfill()

        self.appliances = self.args.get(
            "appliances",
            {
                "sensor.washer_state": "Washer",
                "sensor.dishwasher_state": "Dishwasher",
                "sensor.dryer_state": "Dryer",
            },
        )
        for entity in self.appliances:
            self.listen_state(self._on_appliance, entity)
            # Registered after _on_appliance so a state change that also names the emptier
            # finds its row already written; enrich_by leaves a row that has a name alone.
            self.listen_state(self._on_emptied_by, entity, attribute="emptied_by")

        # Locks: listen to the authoritative (BLE) entity, attribute from the cloud twin.
        self.locks = self.args.get("locks", {})
        self.lock_attribution = self.args.get("lock_attribution", {})
        # The cloud twin lags the BLE entity by seconds; waiting this long before emitting
        # trades feed latency for "by <name>" actually being present. Also the freshness
        # ceiling: a changed_by older than delay+fresh margin belongs to some EARLIER
        # operation and must not be pinned on this one.
        self.lock_report_delay_s = int(self.args.get("lock_report_delay_s", 20))
        self.lock_attribution_fresh_s = int(self.args.get("lock_attribution_fresh_s", 180))
        self._arrival_hint = None
        for entity in self.locks:
            self.listen_state(self._on_lock, entity)
        self.listen_event(self._on_arrival_unlock, "lock_unlocked_on_arrival")

        # Who came in or went out (sensor.door_entry), and the night.
        self.door_entry = self.args.get("door_entry")
        if self.door_entry:
            self.listen_state(self._on_door_entry, self.door_entry, attribute="decided_at")
        self.night_entity = self.args.get("night_entity")
        if self.night_entity:
            self.listen_state(self._on_night, self.night_entity, old="off", new="on")
        # entity -> resident display name; an admin sleeper's row is for admins only.
        self.sleepers = self.args.get("sleepers", {})
        self.admin_sleepers = set(self.args.get("admin_sleepers", []))
        for entity in self.sleepers:
            self.listen_state(self._on_sleep, entity, old="off", new="on")

        # Automation apps explain themselves through this event - see module docstring.
        self.listen_event(self._on_report, "house_events_report")

        # Publish immediately so the dashboard has an entity to read even before
        # the first event (and so the restored feed reappears after a reload).
        self._publish()
        self.log(f"HouseEvents initialized - {len(self.events)} restored, feed at {self.feed_entity}")

    # -- callbacks ----------------------------------------------------------

    def _on_appliance(self, entity, attribute, old, new, kwargs):
        name = self.appliances.get(entity, entity)
        state_obj = self.get_state(entity, attribute="all") or {}
        attrs = state_obj.get("attributes") or {}
        result = appliance_event(name, old, new, attrs)
        if result:
            kind, text = result
            icon = "mdi:basket-check" if kind == "emptied" else APPLIANCE_ICONS.get(name.lower(), "mdi:washing-machine")
            self._add(icon, text, by=appliance_actor(kind, attrs))

    def _on_emptied_by(self, entity, attribute, old, new, kwargs):
        """A monitor named the emptier after the emptied row was written."""
        name = self.appliances.get(entity, entity)
        attrs = (self.get_state(entity, attribute="all") or {}).get("attributes") or {}
        by = appliance_actor("emptied", attrs)
        if not by:
            return
        enriched = enrich_by(self.events, name, by, self.get_now())
        if enriched != self.events:
            self.events = enriched
            self._publish()
            self._write_state_file()
            self.log(f"Event: {name} emptied by {by}")

    def _on_lock(self, entity, attribute, old, new, kwargs):
        # Emit later, not now: attribution (cloud changed_by) usually hasn't landed yet
        # when the BLE entity flips. lock_event() re-validates old/new at emit time.
        self.run_in(
            self._emit_lock_event,
            self.lock_report_delay_s,
            lock_entity=entity,
            old=old,
            new=new,
            changed_at=self.get_now().timestamp(),
        )

    def _on_arrival_unlock(self, event_name, data, kwargs):
        by = data.get("by") if isinstance(data, dict) else None
        if isinstance(by, str) and by.strip():
            self._arrival_hint = (self.get_now().timestamp(), by.strip()[:40])

    def _arrival_by(self, changed_at):
        hint = self._arrival_hint
        if hint is not None and abs(changed_at - hint[0]) <= ARRIVAL_HINT_S:
            return hint[1]
        return None

    def _emit_lock_event(self, kwargs):
        entity = kwargs["lock_entity"]
        name = self.locks.get(entity, entity)
        changed_by = self._fresh_changed_by(self.lock_attribution.get(entity))
        result = lock_event(name, kwargs["old"], kwargs["new"], changed_by,
                            self._arrival_by(kwargs["changed_at"]))
        if result:
            icon, text, by = result
            self._add(icon, text, by=by)

    def _fresh_changed_by(self, attribution_entity):
        """The cloud twin's changed_by, or None when absent/stale (see lock_event's doc)."""
        if not attribution_entity:
            return None
        try:
            full = self.get_state(attribution_entity, attribute="all") or {}
            changed_by = (full.get("attributes") or {}).get("changed_by")
            last_updated = full.get("last_updated")
            if not changed_by or not last_updated:
                return None
            from datetime import datetime

            updated = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            age_s = (self.get_now() - updated).total_seconds()
            if 0 <= age_s <= self.lock_report_delay_s + self.lock_attribution_fresh_s:
                return changed_by
        except Exception as e:
            self.log(f"Lock attribution lookup failed for {attribution_entity}: {e}", level="DEBUG")
        return None

    def _on_door_entry(self, entity, attribute, old, new, kwargs):
        attrs = (self.get_state(entity, attribute="all") or {}).get("attributes") or {}
        try:
            opened_at = datetime.fromisoformat(attrs["door_opened_at"]).timestamp()
            decided_at = datetime.fromisoformat(attrs["decided_at"]).timestamp()
        except (ValueError, TypeError, KeyError):
            return
        if not 0 <= self.get_now().timestamp() - decided_at <= ENTRY_FRESH_S:
            return
        for kind, by in entry_groups(attrs):
            events, matched = apply_entry(self.events, list(self.locks.values()), kind, by, opened_at)
            if matched:
                self.events = events
                self._publish()
                self._write_state_file()
                self.log(f"Event: apartment door row named {by} ({kind})")
            else:
                self._add(ENTRY_ICON, ENTRY_TEXTS[kind], by=by, entry_kind=kind)

    def _on_night(self, entity, attribute, old, new, kwargs):
        if not written_tonight(self.events, NIGHT_TEXT, None, self.get_now()):
            self._add(NIGHT_ICON, NIGHT_TEXT)

    def _on_sleep(self, entity, attribute, old, new, kwargs):
        self.run_in(self._emit_bed, BED_REPORT_DELAY_S, sleep_entity=entity,
                    at=self.get_now().timestamp())

    def _emit_bed(self, kwargs):
        entity = kwargs["sleep_entity"]
        name = self.sleepers.get(entity)
        if not name or self.get_state(entity) != "on":
            return
        if night_row_near(self.events, kwargs["at"]) or written_tonight(self.events, BED_TEXT, name, self.get_now()):
            return
        if name in self.admin_sleepers:
            self._add(BED_ICON, BED_TEXT, by=name, audience="admin")
        else:
            self._add(BED_ICON, BED_TEXT, by=name, audience_users=[name])

    def _on_report(self, event_name, data, kwargs):
        event = build_report_event(data)
        if event:
            self._add(
                event["icon"],
                event["text"],
                cause=event["cause"],
                effect=event["effect"],
                by=event["by"],
                audience=event["audience"],
                audience_users=event["audience_users"],
            )

    # -- feed management ----------------------------------------------------

    def _load_previous_feed(self):
        try:
            full = self.get_state(self.feed_entity, attribute="all") or {}
            events = sanitize_feed((full.get("attributes") or {}).get("events"))
            if events:
                return events
        except Exception as e:
            self.log(f"Could not restore previous feed from entity: {e}", level="WARNING")
        # Entity empty/gone = HA restarted since the last publish (set_state entities
        # are ephemeral) - fall back to the state file written on every added event.
        try:
            events = sanitize_feed(json.loads(self._state_file.read_text()).get("events"))
            if events:
                self.log(f"Feed restored from state file ({len(events)} events)")
            return events
        except FileNotFoundError:
            return []
        except Exception as e:
            self.log(f"Could not restore previous feed from state file: {e}", level="WARNING")
            return []

    def _add(self, icon, text, cause=None, effect=None, by=None, audience=None, audience_users=None, entry_kind=None):
        now = self.get_now()
        now_iso = now.isoformat()
        # Flap guard: identical text by the same person again within the window is noise, not news.
        for event in self.events:
            if event.get("text") != text or event.get("by") != by:
                continue
            try:
                from datetime import datetime

                prev = datetime.fromisoformat(event["ts"])
                if (now - prev).total_seconds() < DUP_SUPPRESS_S:
                    return
            except (ValueError, TypeError, KeyError):
                pass
            break  # only the most recent occurrence matters
        entry = {"ts": now_iso, "icon": icon, "text": text}
        if cause and effect:
            entry["cause"] = cause
            entry["effect"] = effect
        if by:
            entry["by"] = by
        if audience == "admin":
            entry["audience"] = "admin"
        elif audience_users:
            entry["audience_users"] = list(audience_users)
        if entry_kind:
            entry["entry"] = entry_kind
        self.events.insert(0, entry)
        del self.events[MAX_EVENTS:]
        self._publish()
        self._write_state_file()
        self.log(f"Event: {text}")

    def _write_state_file(self):
        # tmp + os.replace so a crash mid-write can't leave a truncated file for the
        # next restore to choke on.
        try:
            tmp = self._state_file.with_name(self._state_file.name + ".tmp")
            tmp.write_text(json.dumps({"events": self.events}))
            os.replace(tmp, self._state_file)
        except Exception as e:
            self.log(f"State file write failed: {e}", level="WARNING")

    def _publish_backfill(self):
        """Publishes backfill_file (default house_events_backfill.json beside this module, kept on the box
        only) to backfill_entity: rows of days before a source fed this feed, which the dashboard
        merges into its history. Its copy of a row wins there, so it may also name a row written unnamed."""
        entity = self.args.get("backfill_entity")
        if not entity:
            return
        path = Path(self.args.get("backfill_file") or Path(__file__).with_name("house_events_backfill.json"))
        try:
            rows = backfill_rows(json.loads(path.read_text()))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.log(f"Backfill {path.name} unreadable: {e}", level="WARNING")
            return
        try:
            self.set_state(entity, state=str(len(rows)), replace=True,
                           attributes={"friendly_name": "House events backfill", "icon": "mdi:history", "events": rows})
        except Exception as e:
            self.log(f"Backfill publish failed: {e}", level="WARNING")

    def _publish(self):
        try:
            self.set_state(
                self.feed_entity,
                state=self.events[0]["ts"] if self.events else "empty",
                attributes={
                    "friendly_name": "House events",
                    "icon": "mdi:history",
                    "events": self.events,
                },
                replace=True,
            )
        except Exception as e:
            self.log(f"Publish failed: {e}", level="WARNING")
