"""
ArrivalUnlock - MEASUREMENT ONLY: would "the resident's phone is on the landing" unlock the front
door early enough, and without false triggers? A Bluetooth proxy on the landing outside the flat,
powered only while the stairwell lights are on, makes Bermuda place the resident's phone beacon in
the landing's area. This app records what an unlock on that signal would have done, so the rule can
be judged offline before anything unlocks for real. It decides nothing: it never calls a lock
service, creates no entity and sends no notification. One resident.

ARMED. `tracker` (the router's wifi tracker) is not home and has been so for away_min (its
last_changed), `lock` reads locked and `door` reads off. Any state but home counts as away.

WINDOW. Opens on a sighting while armed and no window is open: `landing_area_sensor`, Bermuda's area
sensor for the phone beacon, changing to landing_area. That moment is the would-be unlock. The area
sensor is the trigger because it changes at once, where Bermuda throttles the per-scanner distance
sensors; `landing_distance` (optional) is only logged, as its state at that moment turned into a
number, null when it is unknown, unavailable, missing or not configured. The window keeps its time,
that distance, the lock and door states then, the tracker's away_since, proxy_online, the first door
open, the tracker flipping to home and every change of the `keypad` lock's changed_by (the lock sets
it a few seconds after its state, so it arrives as a change of its own). The area changing to
landing_area again inside an open window changes nothing. An entity or area that does not exist yet
is simply waited for.

PROXY. proxy_online is when `landing_online` last left unavailable or missing: the proxy has no
power between stairwell lights, so that is it powering up. An ESPHome entity becomes available,
often as unknown, as soon as its device connects and its first value can come later, so that first
value does not move it. A window or a missed line carries it only when it lies at most 5 minutes
before the window opened or the tracker flipped, otherwise null.

CLOSE. 30 s after the door opens, or window_s after the window opened, whichever is first, or when
the app stops, one JSON line is appended to log_file:
{kind: arrival, start, end, proxy_online, tracker_away_since, would_unlock {t, distance, lock, door},
door_open, wifi_home, keypad [{t, changed_by}], outcome}. outcome is entered when the door opened
inside the window, and lead_s then holds the seconds from would_unlock to the door opening. It is
no_door when the door did not open: an unlock that would have been false. Nothing prunes log_file;
the window lives in memory only.

MISSED. When the tracker flips to home after being away for away_min and no window opened in the 5
minutes before, one line is appended: {kind: missed, t, away_since, door_open, proxy_online,
last_landing_distance_state}. door_open is the latest door opening within 3 minutes before the flip
and last_landing_distance_state the landing distance entity's state at the flip, null when it does
not exist or is not configured.

Times are ISO strings in AppDaemon's time zone. All arithmetic runs on epoch seconds, so a daylight
saving change inside a window cannot skew a duration.
"""

import itertools
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

DOOR_GRACE_S = 30
PROXY_RECENT_S = 300
WINDOW_RECENT_S = 300
DOOR_RECENT_S = 180
OFFLINE = (None, "unavailable")


def distance_of(state):
    """Metres as a float out of an entity state, None when it is not a finite number."""
    try:
        metres = float(state)
    except (TypeError, ValueError):
        return None
    return metres if math.isfinite(metres) else None


def epoch_of(raw):
    """Epoch seconds of a Home Assistant ISO timestamp, None when raw is not a timestamp with a
    time zone."""
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.timestamp() if stamp.tzinfo is not None else None


def within(stamp, now, seconds):
    """`stamp` when it lies 0 to `seconds` before `now`, else None."""
    return stamp if stamp is not None and 0 <= now - stamp <= seconds else None


def armed(tracker, changed, lock, door, now, away_s):
    """What a real unlock would have needed: the tracker not home since `changed` (epoch, None
    when unknown) for at least away_s, the lock locked and the door closed."""
    return (tracker != "home" and changed is not None and now - changed >= away_s
            and lock == "locked" and door == "off")


class ArrivalUnlock(hass.Hass):
    def initialize(self):
        # Guards the state below: callbacks, timers and terminate() can run on different threads.
        # No AppDaemon call is made while it is held, since those wait for the event loop.
        self._lock = threading.RLock()
        self._win = None
        self._ids = itertools.count(1)
        self._proxy_online = None
        self._away_since = None
        self._door_opened = None
        self._last_start = None
        a = self.args
        self.tracker_entity = a["tracker"]
        self.area_entity = a["landing_area_sensor"]
        self.landing_area = a["landing_area"]
        self.distance_entity = a.get("landing_distance") or None
        self.online_entity = a["landing_online"]
        self.lock_entity = a["lock"]
        self.door_entity = a["door"]
        self.keypad_entity = a["keypad"]
        self.away_s = float(a.get("away_min", 10)) * 60
        self.window_s = float(a.get("window_s", 180))
        self.log_file = Path(a.get("log_file") or Path(__file__).resolve().parent / "arrival_unlock_log.jsonl")
        if self.window_s <= 0:
            raise ValueError("ArrivalUnlock: window_s must be positive")
        self._tz = self.get_now().tzinfo
        if self.get_state(self.tracker_entity) != "home":
            self._away_since = epoch_of(self.get_state(self.tracker_entity, attribute="last_changed"))

        self.listen_state(self._on_area, self.area_entity, new=self.landing_area)
        self.listen_state(self._on_online, self.online_entity)
        self.listen_state(self._on_tracker, self.tracker_entity)
        self.listen_state(self._on_door, self.door_entity, new="on")
        self.listen_state(self._on_keypad, self.keypad_entity, attribute="changed_by")
        self.log(f"ArrivalUnlock initialized (measurement only, nothing is unlocked): a window of up to "
                 f"{self.window_s:g} s opens when {self.area_entity} changes to {self.landing_area!a} while "
                 f"{self.tracker_entity} has been away {self.away_s / 60:g} min and {self.lock_entity} is "
                 f"locked -> {self.log_file}", level="INFO")

    def terminate(self):
        self._close()

    def _on_area(self, entity, attribute, old, new, kwargs):
        tracker = self.get_state(self.tracker_entity)
        changed = epoch_of(self.get_state(self.tracker_entity, attribute="last_changed"))
        lock = self.get_state(self.lock_entity)
        door = self.get_state(self.door_entity)
        distance = distance_of(self._landing_distance_state())
        now = self._now()
        with self._lock:
            if self._win is not None or not armed(tracker, changed, lock, door, now, self.away_s):
                return
            wid = next(self._ids)
            self._win = {"id": wid, "start": now, "distance": distance, "lock": lock, "door": door,
                         "proxy_online": within(self._proxy_online, now, PROXY_RECENT_S),
                         "away_since": changed, "door_open": None, "wifi_home": None, "keypad": []}
            self._last_start = now
        self.run_in(self._on_expired, self.window_s, wid=wid)
        self.log(f"ArrivalUnlock: window {wid} opened, phone in {self.landing_area!a}, an unlock would happen now",
                 level="INFO")

    def _on_online(self, entity, attribute, old, new, kwargs):
        if old in OFFLINE and new not in OFFLINE:
            with self._lock:
                self._proxy_online = self._now()

    def _on_tracker(self, entity, attribute, old, new, kwargs):
        now = self._now()
        with self._lock:
            if new != "home":
                self._away_since = now
                return
            away_since, self._away_since = self._away_since, None
            win = self._win
            if win is not None and win["wifi_home"] is None:
                win["wifi_home"] = now
            caught = within(self._last_start, now, WINDOW_RECENT_S) is not None
            door_open = within(self._door_opened, now, DOOR_RECENT_S)
            proxy_online = within(self._proxy_online, now, PROXY_RECENT_S)
        if away_since is None or now - away_since < self.away_s or caught:
            return
        if self._append({"kind": "missed", "t": self._iso(now), "away_since": self._iso(away_since),
                         "door_open": self._iso(door_open), "proxy_online": self._iso(proxy_online),
                         "last_landing_distance_state": self._landing_distance_state()}, "missed record"):
            self.log(f"ArrivalUnlock: tracker home after {(now - away_since) / 60:.0f} min away and no window "
                     f"opened, missed -> {self.log_file.name}", level="INFO")

    def _on_door(self, entity, attribute, old, new, kwargs):
        now = self._now()
        with self._lock:
            self._door_opened = now
            win = self._win
            first = win is not None and win["door_open"] is None
            if first:
                win["door_open"] = now
                wid = win["id"]
        if first:
            self.run_in(self._on_expired, DOOR_GRACE_S, wid=wid)

    def _on_keypad(self, entity, attribute, old, new, kwargs):
        now = self._now()
        with self._lock:
            if self._win is not None:
                self._win["keypad"].append({"t": now, "changed_by": new})

    def _on_expired(self, kwargs):
        self._close(kwargs["wid"])

    def _close(self, wid=None):
        with self._lock:
            win = self._win
            if win is None or (wid is not None and win["id"] != wid):
                return
            self._win = None
        end = self._now()
        entered = win["door_open"] is not None
        line = {"kind": "arrival", "start": self._iso(win["start"]), "end": self._iso(end),
                "proxy_online": self._iso(win["proxy_online"]), "tracker_away_since": self._iso(win["away_since"]),
                "would_unlock": {"t": self._iso(win["start"]), "distance": win["distance"],
                                 "lock": win["lock"], "door": win["door"]},
                "door_open": self._iso(win["door_open"]), "wifi_home": self._iso(win["wifi_home"]),
                "keypad": [{"t": self._iso(k["t"]), "changed_by": k["changed_by"]} for k in win["keypad"]],
                "outcome": "entered" if entered else "no_door"}
        if entered:
            line["lead_s"] = round(win["door_open"] - win["start"], 1)
        if self._append(line, f"window {win['id']}"):
            lead = f", lead {line['lead_s']:g} s" if entered else ""
            self.log(f"ArrivalUnlock: window {win['id']} closed, {line['outcome']}{lead} -> {self.log_file.name}",
                     level="INFO")

    def _landing_distance_state(self):
        return None if self.distance_entity is None else self.get_state(self.distance_entity)

    def _append(self, line, what):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
        except OSError as e:
            self.log(f"ArrivalUnlock: {what} lost, could not write {self.log_file}: {e!a}", level="WARNING")
            return False
        return True

    def _now(self):
        return time.time()

    def _iso(self, epoch):
        return None if epoch is None else datetime.fromtimestamp(epoch, self._tz).isoformat(timespec="seconds")
