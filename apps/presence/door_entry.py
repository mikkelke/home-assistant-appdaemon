"""
DoorEntry - publishes sensor.door_entry: who came in or went out each time the apartment door
opens, read from the residents' phones on Bluetooth (Bermuda's area sensor per phone, with the
landing proxy outside the door) and their wifi trackers. It names residents only; it decides
nothing and changes nothing else.

State is entered, left, unknown, or none until the first opening. Attributes: entered and left
(resident names, in config order), door_opened_at and decided_at (ISO), reason (one sentence) and
landing_proxy (on or off: whether the landing proxy, which only has power while the stairwell light
is on, was up at any moment of the window; null without landing_online).

WINDOW. An opening of `door` at T is judged at T + post_s from what was recorded between
T - pre_s and T + post_s. A phone is heard when its area sensor holds any area, unheard when it is
unknown, unavailable or missing.

ENTERED. A resident came in when they were away at T - pre_s and their phone is heard inside the
window. Away means their wifi tracker was not home then, or their phone had been unheard for the
whole away_s before then (that covers a quick return the wifi tracker has not noticed yet).

LEFT. A resident who did not come in went out when their phone was heard in a room of the flat
(any area but landing_area) between T - pre_s and T, and is heard in landing_area between T and
T + post_s. Landing_area turns up only around the door opening, so this needs no wait for the phone
to drop out of range.

The state is entered when anyone came in, else left when anyone went out, else unknown: the door
opened and no resident's phone explains it (a guest, a cleaner, a resident without their phone,
or a phone that is not tracked over Bluetooth). Each opening is judged on its own; a second opening
before the first is judged simply publishes after it. Every judgement also appends one JSON line
to log_file with each resident's evidence, for checking the rule against what really happened.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

UNHEARD = (None, "unknown", "unavailable")
OFFLINE = (None, "unavailable")


def state_at(history, t):
    """The state a [(epoch, state), ...] history (sorted, oldest first) held at t, None before it
    starts."""
    held = None
    for when, state in history:
        if when > t:
            break
        held = state
    return held


def held_during(history, lo, hi):
    """Every state the history held at some instant of [lo, hi]."""
    states = [state_at(history, lo)]
    states += [state for when, state in history if lo < when <= hi]
    return states


def heard_since(history, lo, hi):
    """When the phone's first heard spell that reaches into [lo, hi] began (it may begin before lo),
    None when it is unheard throughout."""
    start = None
    for when, state in history:
        if when > hi:
            break
        if state in UNHEARD:
            if start is not None and when > lo:
                return start
            start = None
        else:
            if start is None:
                start = when
            if when >= lo:
                return start
    return start


def judge(area, tracker, t_open, pre_s, post_s, away_s, landing):
    """(verdict, evidence) for one resident and one door opening at t_open. verdict is "entered",
    "left" or None; evidence is a dict of what decided it, times in seconds after t_open."""
    lo, hi = t_open - pre_s, t_open + post_s
    wifi_away = state_at(tracker, lo) not in (None, "home")
    ble_away = all(state in UNHEARD for state in held_during(area, lo - away_s, lo))
    heard = [state for state in held_during(area, lo, hi) if state not in UNHEARD]
    first_heard = heard_since(area, lo, hi)
    first_heard = None if first_heard is None else first_heard - t_open
    landing_after = next((when - t_open for when, state in area if t_open <= when <= hi and state == landing), None)
    if landing_after is None and state_at(area, t_open) == landing:
        landing_after = 0.0
    inside_before = any(state not in UNHEARD and state != landing for state in held_during(area, lo, t_open))
    evidence = {"wifi_away": wifi_away, "ble_away": ble_away, "first_heard_s": first_heard,
                "landing_after_s": landing_after, "inside_before": inside_before}
    if (wifi_away or ble_away) and heard:
        return "entered", evidence
    if inside_before and landing_after is not None:
        return "left", evidence
    return None, evidence


def describe(entered, left, evidence):
    """The reason sentence for a judged opening."""
    def seconds(name, key):
        s = evidence[name][key]
        return "" if s is None else f" {abs(s):.0f} s {'before' if s < 0 else 'after'} the door opened"

    parts = [f"{name} came in (phone first heard{seconds(name, 'first_heard_s')})" for name in entered]
    parts += [f"{name} went out (phone on the landing{seconds(name, 'landing_after_s')})" for name in left]
    if parts:
        return "; ".join(parts) + "."
    return "No resident's phone explains the door opening."


class DoorEntry(hass.Hass):
    def initialize(self):
        # Guards the histories: state callbacks and the judging timer can run on different threads.
        # No AppDaemon call is made while it is held.
        self._lock = threading.RLock()
        a = self.args
        self.entity = a.get("entity", "sensor.door_entry")
        self.door_entity = a["door"]
        self.landing = a["landing_area"]
        self.online_entity = a.get("landing_online") or None
        self.pre_s = float(a.get("pre_s", 120))
        self.post_s = float(a.get("post_s", 30))
        self.away_s = float(a.get("away_s", 300))
        self.keep_s = self.pre_s + self.post_s + self.away_s + 60
        self.log_file = Path(a.get("log_file") or Path(__file__).resolve().parent / "door_entry_log.jsonl")
        self.residents = [(r["name"], r["area_sensor"], r["tracker"]) for r in a["residents"]]
        if self.pre_s < 0 or self.post_s <= 0 or self.away_s < 0:
            raise ValueError("DoorEntry: pre_s and away_s must not be negative and post_s must be positive")
        self._tz = self.get_now().tzinfo
        self._history = {}
        for name, area, tracker in self.residents:
            for entity in (area, tracker):
                self._history[entity] = [self._seed(entity)]
                self.listen_state(self._on_change, entity)
        if self.online_entity is not None:
            self._history[self.online_entity] = [self._seed(self.online_entity)]
            self.listen_state(self._on_change, self.online_entity)
        self.listen_state(self._on_door, self.door_entity, new="on")
        if self.get_state(self.entity) is None:
            self._publish("none", [], [], None, None, "No door opening judged since AppDaemon started.", None)
        names = ", ".join(name for name, _, _ in self.residents)
        self.log(f"DoorEntry initialized: judges each opening of {self.door_entity} {self.post_s:g} s later "
                 f"for {names} -> {self.entity}", level="INFO")

    def _seed(self, entity):
        state = self.get_state(entity)
        since = epoch_of(self.get_state(entity, attribute="last_changed"))
        return (since if since is not None else self._now(), state)

    def _on_change(self, entity, attribute, old, new, kwargs):
        now = self._now()
        with self._lock:
            history = self._history[entity]
            history.append((now, new))
            while len(history) > 1 and history[1][0] < now - self.keep_s:
                history.pop(0)

    def _on_door(self, entity, attribute, old, new, kwargs):
        self.run_in(self._on_judge, self.post_s, t_open=self._now())

    def _on_judge(self, kwargs):
        t_open = kwargs["t_open"]
        with self._lock:
            snapshot = {e: list(h) for e, h in self._history.items()}
        online = None
        if self.online_entity is not None:
            window = held_during(snapshot[self.online_entity], t_open - self.pre_s, t_open + self.post_s)
            online = any(state not in OFFLINE for state in window)
        entered, left, evidence = [], [], {}
        for name, area, tracker in self.residents:
            verdict, evidence[name] = judge(snapshot[area], snapshot[tracker], t_open, self.pre_s, self.post_s,
                                            self.away_s, self.landing)
            if verdict == "entered":
                entered.append(name)
            elif verdict == "left":
                left.append(name)
        state = "entered" if entered else "left" if left else "unknown"
        reason = describe(entered, left, evidence)
        if online is False:
            reason += " The landing proxy had no power around the opening."
        now = self._now()
        self._publish(state, entered, left, t_open, now, reason, online)
        self._append({"door_opened_at": self._iso(t_open), "decided_at": self._iso(now), "state": state,
                      "entered": entered, "left": left, "landing_proxy": online, "evidence": evidence})
        self.log(f"DoorEntry: door opened {self._iso(t_open)}: {state}. {reason}", level="INFO")

    def _publish(self, state, entered, left, t_open, decided, reason, online):
        attributes = {"entered": entered, "left": left, "door_opened_at": self._iso(t_open),
                      "decided_at": self._iso(decided), "reason": reason,
                      "landing_proxy": None if online is None else ("on" if online else "off"),
                      "friendly_name": "Door entry", "icon": "mdi:door-open"}
        self.set_state(self.entity, state=state, attributes=attributes, replace=True)

    def _append(self, line):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
        except OSError as e:
            self.log(f"DoorEntry: judgement not logged, could not write {self.log_file}: {e!a}", level="WARNING")

    def _now(self):
        return time.time()

    def _iso(self, epoch):
        return None if epoch is None else datetime.fromtimestamp(epoch, self._tz).isoformat(timespec="seconds")


def epoch_of(raw):
    """Epoch seconds of a Home Assistant ISO timestamp, None when raw is not one with a time zone."""
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.timestamp() if stamp.tzinfo is not None else None
