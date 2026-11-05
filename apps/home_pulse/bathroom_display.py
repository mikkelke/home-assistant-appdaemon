"""BathroomDisplay - what the round display in the main bathroom (ESPHome node bathroom-display) shows.

Publishes sensor.bathroom_display, the one entity the display reads:
    state       "off" (the bathroom is not active), "someone" (active, Mikkel not identified yet) or
                "mikkel" (active, and his phone has been near during this visit)
    attributes  every value a string, since AppDaemon 4.5.13's set_state drops None, False and 0:
        reason      why, in RoomActive's style: "inactive:binary_sensor.bathroom_active"; "unknown:..."
                    for a first evaluation room_active_read has no opinion on; while active
                    "active:binary_sensor.bathroom_active phone:near", the word being the phone's status:
                    near, weak (under mikkel_rssi_min), stale (older than rssi_fresh_s) or none (no
                    reading), or held for a mikkel whose phone is not near right now
        person      "mikkel" for state mikkel, else ""
        greeting    "Hej Mikkel" for state mikkel, else ""
        line1..3    the title of each line, at most LINE_MAX characters; "" when unused
        icon1..3    the line's mdi icon, the row's own or mdi:train for a transit line; "" when unused
        due1..3     when the line is due: "overdue", "today", "tomorrow" or "6 d"; "" when it has none
        friendly_name, icon
    The lines are filled only for state mikkel. The phone's dBm is not published (mikkel_rssi_min is
    calibrated from the history of its sensor), so the payload changes only with the state, the lines and
    the phone's status word.

The state is next_state(). Occupancy is room_active_read.active(), never a raw presence sensor, and False is
off at once. In an active room Mikkel is identified when the latest reading of his phone - the iBeacon the
display hears, sensor.bathroom_display_mikkel_phone_rssi, which reads unknown (NaN on the device) once the
phone is out of range - is a number at or above mikkel_rssi_min dBm and was updated at most rssi_fresh_s
seconds ago. From then on he stays identified for as long as the room stays active, whatever the phone does.
No opinion on the room (None) changes nothing, the state, reason and lines staying as they were; before
there is a state it is off.

The lines for Mikkel, at most three:
    transit  one line for each of his routes (the `transit` list) whose status is Delayed or Disrupted
             and whose data is at most transit_max_age_min old: the first upcoming cancelled departure
             ("S-tog: cancelled 07:42"), else the first late one ("S-tog: 07:42 +12 min"), else the status.
    rows     from sensor.house_upcoming while its lease (`expires`) holds, never a row that asks the next
             chip to pass it over or names other people. First what is due by 24 hours from now, earliest
             first: a row timed in the past or inside 24 hours, a one-off task that lost its time on its
             day ("Due dd.mm" info) and a calendar plant that is due ("due today", "due since dd.mm"
             detail). Then the rows of the zone's own area (the zone doubles as the HA area id) timed
             within 14 days. A line is the row's title, cut back to a whole word and ended in "..." when
             it is longer than LINE_MAX; its due chip counts house days. A row with no day (an offline
             battery, a dry sensor plant) is never a line.

State acquisition follows HouseUpcoming: one snapshot of each source at initialize, then
listen_state(attribute="all"), and a value from a callback is never replaced by a later read, because
AppDaemon 4.5.13's state store can lag behind HA. The freshness of a reading is its own last_updated, so a
late callback or a stale snapshot does not look new. Every TICK_S seconds everything is evaluated again, which
is how a reading that stops coming, a lease that runs out, an old transit report and a due chip that rolls
over reach the display; the result is published only when it differs from the last one published, and always
on plugin_started, since HA forgets a set_state entity when it restarts.
"""

import re
from datetime import date, datetime, timedelta
from typing import NamedTuple

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import room_active_read  # apps/presence - AppDaemon puts every app dir on sys.path
import upcoming_core as core
import upcoming_maint as maint
import upcoming_train as train

TICK_S = 10
LINE_MAX = 32
LINES = 3
RESIDENT = "Mikkel"
ZONE = "bathroom"
RSSI_MIN = -75.0
RSSI_FRESH_S = 20.0
TRANSIT_MAX_AGE_MIN = 45.0
LATE_MIN = 10
SOON = timedelta(hours=24)
AHEAD = timedelta(days=14)
BAD_STATUS = ("Delayed", "Disrupted")
ICON = "mdi:monitor-dashboard"
TRANSIT_ICON = "mdi:train"
ICON_MAX = 80

_DUE_INFO = re.compile(r"Due (\d\d)\.(\d\d)(?:\.(\d\d))?")
_DUE_DETAIL = re.compile(r"due (?:today|since (\d\d)\.(\d\d)(?:\.(\d\d))?)")


class Line(NamedTuple):
    text: str
    icon: str = ""
    due: str = ""


# -- the phone ----------------------------------------------------------------


def phone_reading(full):
    """(dBm, moment of the update) of the RSSI sensor's state dict; None for a part it does not have. A
    state that is not a finite number (unknown, unavailable) has no dBm."""
    if not isinstance(full, dict):
        return None, None
    return maint.reading(full.get("state")), maint.parse_iso(full.get("last_updated"))


def phone_status(value, at, now, rssi_min, fresh):
    """"none" without a dBm, "stale" once it is older than `fresh` or undated, "weak" under `rssi_min`, else
    "near"."""
    if value is None:
        return "none"
    if at is None or now - at > fresh:
        return "stale"
    return "near" if value >= rssi_min else "weak"


def next_state(prev, active, status):
    """The state after an evaluation: `prev` is the one before it (None before the first), `active` what
    room_active_read says (True, False or None) and `status` the phone's. An inactive room is off. In an
    active one a near phone identifies Mikkel, who then stays identified, whatever the phone does, for as
    long as the room stays active; until then it is someone. No opinion on the room keeps `prev`, which is
    off before there is one."""
    if active is None:
        return prev or "off"
    if not active:
        return "off"
    return "mikkel" if prev == "mikkel" or status == "near" else "someone"


def reason_text(active_entity, active, state, status):
    """Why `state`, in RoomActive's style. While active it names the phone's status; a mikkel whose phone is
    not near right now is "held"."""
    if active is None:
        return f"unknown:{active_entity}"
    if not active:
        return f"inactive:{active_entity}"
    word = "held" if state == "mikkel" and status != "near" else status
    return f"active:{active_entity} phone:{word}"


# -- lines --------------------------------------------------------------------


def fit(title, limit=LINE_MAX):
    """`title` on one line of at most `limit` characters: one that is longer is cut back to a whole word where
    it can be and ends in "..."."""
    if len(title) <= limit:
        return title
    head = title[:limit - 3]
    if " " in head and head[-1] != " " and title[limit - 3] != " ":
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,:;-+") + "..."


def transit_line(label, full, now, max_age):
    """The title for a route whose status is Delayed or Disrupted and whose data is at most `max_age` old, else
    "". With a cancelled departure ahead (the first one, whatever else is wrong, when Disrupted) it names
    that, otherwise the first one late by delay_threshold_min, otherwise the status. `full` is the state
    dict of the sensor TransitStatus writes; departures need the `ts` it adds with expose_departure_ts."""
    state = full.get("state") if isinstance(full, dict) else None
    if state not in BAD_STATUS:
        return ""
    attrs = full.get("attributes") if isinstance(full.get("attributes"), dict) else {}
    as_of = maint.parse_iso(attrs.get("data_as_of"))
    if as_of is None or now - as_of > max_age:
        return ""
    try:
        threshold = float(attrs.get("delay_threshold_min") or LATE_MIN)
    except (TypeError, ValueError):
        threshold = LATE_MIN
    problems = [dep for dep in train.departures(attrs.get("departures"), now) if dep[3] or dep[2] >= threshold]
    if state == "Disrupted":
        problems = [dep for dep in problems if dep[3]] or problems
    if not problems:
        return fit(f"{label}: {state.lower()}")
    _, scheduled, delay, cancelled = problems[0]
    return fit(f"{label}: " + (f"cancelled {scheduled}" if cancelled else f"{scheduled} +{delay} min"))


def transit_lines(routes, now, max_age):
    """The lines of [(label, state dict)] that have one, in order."""
    texts = (transit_line(label, full, now, max_age) for label, full in routes)
    return [Line(text, TRANSIT_ICON) for text in texts if text]


def live_rows(full, now):
    """The rows of the sensor.house_upcoming state dict while its lease holds, else none: an `expires` that is
    missing, unreadable or past means the publisher has stopped."""
    attrs = full.get("attributes") if isinstance(full, dict) else None
    attrs = attrs if isinstance(attrs, dict) else {}
    expires, rows = maint.parse_iso(attrs.get("expires")), attrs.get("rows")
    return rows if expires is not None and expires > now and isinstance(rows, list) else []


def _day(match, today):
    """The house day a "dd.mm" or "dd.mm.yy" match names: `today` for a match with no date in it, None for a
    date that does not exist."""
    day, month, year = match.groups()
    if day is None:
        return today
    try:
        return date(2000 + int(year) if year else today.year, int(month), int(day))
    except ValueError:
        return None


def due_at(row, now):
    """When `row` is due: its `at`; else, for a Done task that has lost its time on the day it is due, that day
    at maint.DUE_TIME - a one-off task's "Due dd.mm" info line (upcoming_once) or a calendar plant's "due
    today" / "due since dd.mm" detail (upcoming_plants). None for a row with no day."""
    at = maint.parse_iso(row.get("at"))
    if at is not None:
        return at
    if row.get("done") != "tap":
        return None
    info = row.get("info") if isinstance(row.get("info"), list) else []
    for text, pattern in [(line, _DUE_INFO) for line in info] + [(row.get("detail"), _DUE_DETAIL)]:
        match = pattern.fullmatch(text) if isinstance(text, str) else None
        if match:
            day = _day(match, maint.house_date(now))
            return datetime.combine(day, maint.DUE_TIME, maint.HOUSE_TZ) if day else None
    return None


def due_chip(due, now):
    """overdue, today or tomorrow by house day, else the days ahead as "6 d"."""
    days = (maint.house_date(due) - maint.house_date(now)).days
    if days < 0:
        return "overdue"
    return "today" if days == 0 else "tomorrow" if days == 1 else f"{days} d"


def _wanted(row, resident):
    """Whether `row` may be a line for `resident`: it has a title, does not ask the next chip to pass it, and
    names nobody or names him."""
    if not isinstance(row, dict) or row.get("next") == "skip" or not core.clean_text(row.get("title")):
        return False
    users = row.get("audience_users")
    if users is None:
        return True
    return isinstance(users, list) and resident.lower() in {str(user).strip().lower() for user in users}


def _icon(row):
    icon = row.get("icon")
    return core.clean_text(icon, ICON_MAX) if isinstance(icon, str) and icon.startswith("mdi:") else ""


def task_lines(rows, now, resident=RESIDENT, area=ZONE, limit=LINES):
    """At most `limit` lines from the house_upcoming rows: what is due by SOON from now (including anything
    overdue), earliest first, then the rows of `area` due within AHEAD, earliest first."""
    soon, later = [], []
    for row in rows if isinstance(rows, list) else []:
        if not _wanted(row, resident):
            continue
        due = due_at(row, now)
        if due is None:
            continue
        if due <= now + SOON:
            soon.append((due, str(row.get("id")), row))
        elif row.get("area") == area and due <= now + AHEAD:
            later.append((due, str(row.get("id")), row))
    picked = sorted(soon, key=lambda item: item[:2]) + sorted(later, key=lambda item: item[:2])
    return [Line(fit(core.clean_text(row.get("title"))), _icon(row), due_chip(due, now))
            for due, _, row in picked[:max(0, limit)]]


def payload(state, reason, resident, lines):
    """The attributes of sensor.bathroom_display: strings only, the lines padded with empty ones."""
    mikkel = state == "mikkel"
    attributes = {"friendly_name": "Bathroom display", "icon": ICON, "reason": reason,
                  "person": resident.lower() if mikkel else "", "greeting": f"Hej {resident}" if mikkel else ""}
    for number, line in enumerate((list(lines) + [Line("")] * LINES)[:LINES], 1):
        attributes.update({f"line{number}": line.text, f"icon{number}": line.icon, f"due{number}": line.due})
    return attributes


class BathroomDisplay(hass.Hass):
    def initialize(self):
        a = self.args
        self.entity = a.get("entity", "sensor.bathroom_display")
        self.zone = a.get("zone", ZONE)
        self.active_entity = f"binary_sensor.{self.zone}_active"
        self.resident = str(a.get("resident", RESIDENT))
        self.rssi_entity = a.get("rssi_entity", "sensor.bathroom_display_mikkel_phone_rssi")
        self.rssi_min = float(a.get("mikkel_rssi_min", RSSI_MIN))
        self.rssi_fresh = timedelta(seconds=float(a.get("rssi_fresh_s", RSSI_FRESH_S)))
        self.upcoming_entity = a.get("upcoming_entity", "sensor.house_upcoming")
        self.routes = [(str(route["label"]), route["entity"]) for route in a.get("transit") or []]
        self.transit_max_age = timedelta(minutes=float(a.get("transit_max_age_min", TRANSIT_MAX_AGE_MIN)))

        self._cache = {}
        self._current = None  # (state, attributes) as last worked out
        self._published = None  # (state, attributes) as last handed to HA

        for entity in self._sources():
            self._cache[entity] = self._read(entity)
            self.listen_state(self._on_source, entity, attribute="all")
        self.listen_state(self._on_active, self.active_entity)
        self.listen_event(self._on_plugin_started, "plugin_started")
        self.run_every(self._on_tick, f"now+{TICK_S}", TICK_S)

        self._evaluate(publish=True)
        self.log(f"BathroomDisplay initialized - {self.entity} from {self.active_entity}, "
                 f"phone >= {self.rssi_min:g} dBm within {self.rssi_fresh.total_seconds():g} s, "
                 f"{len(self.routes)} transit routes")

    # -- sources --------------------------------------------------------------

    def _sources(self):
        return list(dict.fromkeys([self.rssi_entity, self.upcoming_entity] + [entity for _, entity in self.routes]))

    def _read(self, entity):
        try:
            full = self.get_state(entity, attribute="all")
        except Exception as e:
            self.log(f"Read of {entity} failed: {e!r}", level="WARNING")
            return None
        return full if isinstance(full, dict) else None

    # -- callbacks ------------------------------------------------------------

    def _on_source(self, entity, attribute, old, new, kwargs):
        self._cache[entity] = new if isinstance(new, dict) else None
        self._evaluate()

    def _on_active(self, entity, attribute, old, new, kwargs):
        self._evaluate()

    def _on_plugin_started(self, event_name, data, kwargs):
        self._evaluate(publish=True)

    def _on_tick(self, kwargs):
        for entity, cached in list(self._cache.items()):
            if cached is None:
                self._cache[entity] = self._read(entity)
        self._evaluate()

    # -- evaluation -----------------------------------------------------------

    def _evaluate(self, publish=False):
        active = room_active_read.active(self, self.zone)
        prev = self._current[0] if self._current else None
        if active is not None or prev is None:
            self._current = self._compute(prev, active)
            if self._current[0] != prev:
                self.log(f"{self._current[0]} ({self._current[1]['reason']})")
        if publish or self._current != self._published:
            self._publish(*self._current)

    def _compute(self, prev, active):
        now = self.get_now()
        value, at = phone_reading(self._cache.get(self.rssi_entity))
        status = phone_status(value, at, now, self.rssi_min, self.rssi_fresh)
        state = next_state(prev, active, status)
        reason = reason_text(self.active_entity, active, state, status)
        return state, payload(state, reason, self.resident, self._lines(now) if state == "mikkel" else [])

    def _lines(self, now):
        routes = [(label, self._cache.get(entity)) for label, entity in self.routes]
        lines = transit_lines(routes, now, self.transit_max_age)
        rows = live_rows(self._cache.get(self.upcoming_entity), now)
        return (lines + task_lines(rows, now, self.resident, self.zone, LINES - len(lines)))[:LINES]

    def _publish(self, state, attributes):
        try:
            self.set_state(self.entity, state=state, attributes=attributes, replace=True)
        except Exception as e:
            self.log(f"Publish failed: {e!r}", level="WARNING")
            return
        self._published = (state, attributes)
