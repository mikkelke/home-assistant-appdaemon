"""
KitchenCook - publishes sensor.kitchen_cook: who is cooking, read from the residents' phones on
Bluetooth (Bermuda's area sensor per phone). It names a resident only and changes nothing else;
CookAssistant asks it, through cook_for, who to send its pushes to.

SESSION. Cooking is on while any power sensor reads power_w or more, or the sous vide cooker is
heating. A session starts when cooking turns on and ends once it has stayed off for END_S.

COOK. A resident counts only while their person entity is home. Over the window from BEFORE_S
before the session started until now (never more than the last WINDOW_S), each counted resident's
share is the part of the window their phone's area sensor read the kitchen area. Any other area,
unknown, unavailable, and the time before the sensor was first recorded all count as elsewhere.
The cook is the resident whose share is at least MIN_SHARE and at least LEAD times the next
resident's, else nobody.

State is the cook's name in lower case, unknown when nobody is named, or idle without a session.
Attributes: session_start (ISO, null when idle), shares ({name: share of the window}) and reason:
leads, too_little (nobody reached MIN_SHARE), no_lead (the best was not LEAD times the next),
no_one_home or no_session.

cook_for(start) judges a start at epoch `start` by the same rule over the window from BEFORE_S
before it to AFTER_S after it, or to now when that is sooner, and returns a name or None. It needs
no session, so a caller can ask a few minutes after its own start. The phones' history is kept in
memory: after a restart it begins at each sensor's last change.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

POWER_W = 50.0
COOKER_ON = "heat"
KITCHEN = "Kitchen"
BEFORE_S = 300
AFTER_S = 300
WINDOW_S = 1200
END_S = 600
KEEP_S = 1800
MIN_SHARE = 0.60
LEAD = 2.0
TICK_S = 30


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def epoch_of(raw):
    """Epoch seconds of a Home Assistant ISO timestamp, None when raw is not one with a time zone."""
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.timestamp() if stamp.tzinfo is not None else None


def cooking(watts, cooker_state, power_w=POWER_W):
    """True when any reading in `watts` (floats or None) is power_w or more, or the cooker is heating."""
    return cooker_state == COOKER_ON or any(w is not None and w >= power_w for w in watts)


class Track:
    """The recent updates of one entity: [(epoch s, state)], the last KEEP_S, plus the one before."""

    def __init__(self):
        self.points = []

    def add(self, ts, state):
        self.points.append((ts, state))
        cutoff = ts - KEEP_S
        while len(self.points) > 1 and self.points[1][0] < cutoff:
            self.points.pop(0)

    def seconds_in(self, state, lo, hi):
        """Seconds of [lo, hi] the entity held `state`; the time before its first update counts as none."""
        total = 0.0
        for index, (ts, held) in enumerate(self.points):
            end = self.points[index + 1][0] if index + 1 < len(self.points) else hi
            if held == state:
                total += max(0.0, min(end, hi) - max(ts, lo))
        return total


class Session:
    """A cooking session: starts when cooking turns on, ends once it has stayed off for END_S."""

    def __init__(self):
        self.start = None
        self.off_since = None

    def update(self, now, on):
        """Move the session to `now`, where cooking is `on` or not; True while the session is on afterwards."""
        if on:
            if self.start is None:
                self.start = now
            self.off_since = None
        elif self.start is not None:
            if self.off_since is None:
                self.off_since = now
            elif now - self.off_since >= END_S:
                self.start = self.off_since = None
        return self.start is not None


def session_window(start, now):
    """(lo, hi) of the window a session that began at `start` is read over at `now`."""
    return max(start - BEFORE_S, now - WINDOW_S), now


def start_window(start, now):
    """(lo, hi) of the window a cook that began at `start` is read over at `now`."""
    return start - BEFORE_S, min(start + AFTER_S, now)


def shares(tracks, area, lo, hi):
    """{name: share of [lo, hi] each Track held `area`}; 0 for an empty window."""
    span = hi - lo
    return {name: track.seconds_in(area, lo, hi) / span if span > 0 else 0.0 for name, track in tracks.items()}


def pick(by_name):
    """(name or None, reason) from {name: share} by the rule in the module docstring."""
    if not by_name:
        return None, "no_one_home"
    ranked = sorted(by_name.items(), key=lambda item: item[1], reverse=True)
    name, best = ranked[0]
    if best < MIN_SHARE:
        return None, "too_little"
    if len(ranked) > 1 and best < LEAD * ranked[1][1]:
        return None, "no_lead"
    return name, "leads"


class KitchenCook(hass.Hass):
    def initialize(self):
        # Guards the tracks, readings and session: state callbacks, the tick and CookAssistant's cook_for
        # can run on different threads. _publish_lock keeps publishes in order. No AppDaemon call is made
        # while _lock is held.
        self._lock = threading.RLock()
        self._publish_lock = threading.Lock()
        a = self.args
        self.entity = a.get("entity", "sensor.kitchen_cook")
        self.kitchen = a.get("kitchen_area", KITCHEN)
        self.power_w = float(a.get("power_w", POWER_W))
        self.power_entities = list(a["power_sensors"])
        self.cooker_entity = a.get("cooker") or None
        self.residents = [(r["name"], r["person"], r["area_sensor"]) for r in a["residents"]]
        if self.power_w <= 0:
            raise ValueError("KitchenCook: power_w must be positive")
        self._tz = self.get_now().tzinfo
        now = self._now()
        self._tracks = {name: Track() for name, _, _ in self.residents}
        self._by_area = {area: name for name, _, area in self.residents}
        self._by_person = {person: name for name, person, _ in self.residents}
        self._home = {}
        self._watts = {}
        self._cooker = None
        self._session = Session()
        self._published = None
        for name, person, area in self.residents:
            self._tracks[name].add(self._since(area, now), self.get_state(area))
            self._home[name] = self.get_state(person) == "home"
            self.listen_state(self._on_area, area)
            self.listen_state(self._on_person, person)
        for entity in self.power_entities:
            self._watts[entity] = as_float(self.get_state(entity))
            self.listen_state(self._on_power, entity)
        if self.cooker_entity is not None:
            self._cooker = self.get_state(self.cooker_entity)
            self.listen_state(self._on_cooker, self.cooker_entity)
        self.listen_event(self._on_plugin_started, "plugin_started")
        self.run_every(self._on_tick, f"now+{TICK_S}", TICK_S)
        self._refresh()
        names = ", ".join(name for name, _, _ in self.residents)
        self.log(f"KitchenCook initialized: names the cook among {names} while {len(self.power_entities)} power "
                 f"sensors read {self.power_w:g} W or more or {self.cooker_entity} heats -> {self.entity}",
                 level="INFO")

    def cook_for(self, start):
        """The resident who was cooking at epoch `start`, None when the rule names nobody."""
        start = as_float(start)
        if start is None:
            return None
        lo, hi = start_window(start, self._now())
        with self._lock:
            return self._judge(lo, hi)[0]

    def _judge(self, lo, hi):
        """(name or None, {name: share}, reason) over [lo, hi] for the residents who are home."""
        home = {name: track for name, track in self._tracks.items() if self._home[name]}
        by_name = shares(home, self.kitchen, lo, hi)
        name, reason = pick(by_name)
        return name, by_name, reason

    def _since(self, entity, now):
        since = epoch_of(self.get_state(entity, attribute="last_changed"))
        return now if since is None else min(since, now)

    def _on_area(self, entity, attribute, old, new, kwargs):
        with self._lock:
            self._tracks[self._by_area[entity]].add(self._now(), new)

    def _on_person(self, entity, attribute, old, new, kwargs):
        with self._lock:
            self._home[self._by_person[entity]] = new == "home"

    def _on_power(self, entity, attribute, old, new, kwargs):
        with self._lock:
            self._watts[entity] = as_float(new)
        self._refresh()

    def _on_cooker(self, entity, attribute, old, new, kwargs):
        with self._lock:
            self._cooker = new
        self._refresh()

    def _on_tick(self, kwargs):
        self._refresh()

    def _on_plugin_started(self, event_name, data, kwargs):
        self._refresh(force=True)

    def _refresh(self, force=False):
        with self._publish_lock:
            with self._lock:
                now = self._now()
                if self._session.update(now, cooking(self._watts.values(), self._cooker, self.power_w)):
                    person, by_name, reason = self._judge(*session_window(self._session.start, now))
                    state, start = person or "unknown", self._session.start
                else:
                    state, start, by_name, reason = "idle", None, {}, "no_session"
            attributes = {"session_start": self._iso(start), "shares": {n: round(s, 2) for n, s in by_name.items()},
                          "reason": reason, "friendly_name": "Kitchen cook", "icon": "mdi:chef-hat"}
            if not force and (state, attributes) == self._published:
                return
            previous = self._published
            self.set_state(self.entity, state=state, attributes=attributes, replace=True)
            self._published = (state, attributes)
            if previous is None or previous[0] != state:
                self.log(f"KitchenCook: {state} ({reason}) shares {attributes['shares']}", level="INFO")

    def _now(self):
        return time.time()

    def _iso(self, epoch):
        return None if epoch is None else datetime.fromtimestamp(epoch, self._tz).isoformat(timespec="seconds")
