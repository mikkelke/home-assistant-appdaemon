"""
GhostDistance - TEMPORARY DIAGNOSTIC. Remove this app (module, yaml and test) once a few
episodes are recorded.

Records what the dining room FP300 radar reports as its target distance while it holds
presence in an empty house. Read-only apart from one button press: no lights, no pushes,
nothing written to HA.

An episode is zone.home reading "0" (nobody home) while the presence sensor is on. It ends
when presence goes off or zone.home leaves "0". Once the two have held together for dwell_min
the app presses the sensor's track-target-distance button: once per episode, and for at most
max_episodes_per_day episodes a day (the FP300 runs on coin cells). The press opens a
record_min recording: the distance at the press, every change after it and a final read when
the window closes. Then one JSON line is appended to log_file:

    {"start": ISO, "presence_went_off": bool, "readings": [[ISO, metres], ...]}

metres is the sensor's number, or its raw state ("unknown", "unavailable") when it is not one.
presence_went_off is true when presence dropped inside the recording window, which runs on
after the episode itself has ended. An episode that qualifies while an earlier recording is
still open is pressed as soon as that one closes, if it still holds.

Nothing is read back from history: whatever holds at start counts from the start, and the
day's episode count lives in memory, so a restart begins it again. The press and the end of
the window are decided on a TICK_S tick, so each can come that much late.

All times are epoch seconds from the single _now() helper (never naive local datetime
arithmetic, which breaks across a DST change), written in AppDaemon's own time zone.
"""

import json
from datetime import datetime
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

TICK_S = 10


def home_empty(state):
    """zone.home counts the people in it, so "0" is an empty house. Anything else, an
    unavailable zone included, is not known to be empty."""
    return str(state).strip() == "0"


def press_due(held_s, dwell_s, *, pressed, recording, episodes_today, max_episodes):
    """True when the episode should get its one press now. held_s is how long zone.home has
    read "0" with presence on (None when the two do not hold together), pressed whether this
    episode already had its press, recording whether an earlier recording is still open."""
    return (held_s is not None and held_s >= dwell_s and not pressed and not recording
            and episodes_today < max_episodes)


def distance_value(state):
    """The sensor's metres as a number, or its raw state when it is not one."""
    try:
        return float(state)
    except (TypeError, ValueError):
        return state


class GhostDistance(hass.Hass):
    def initialize(self):
        a = self.args
        self.zone_entity = a["home_zone"]
        self.presence_entity = a["presence"]
        self.button_entity = a["track_button"]
        self.distance_entity = a["distance"]
        self.dwell_s = float(a.get("dwell_min", 3)) * 60
        self.record_s = float(a.get("record_min", 5)) * 60
        self.max_episodes = int(a.get("max_episodes_per_day", 3))
        self.record_file = Path(a.get("log_file") or Path(__file__).resolve().parent / "ghost_distance_log.jsonl")

        now = self._now()
        self._zone_empty_since = now if home_empty(self.get_state(self.zone_entity)) else None
        self._presence_on_since = now if self.get_state(self.presence_entity) == "on" else None
        self._pressed_since = None
        self._day = self._day_of(now)
        self._episodes = 0
        self._rec = None
        self._rec_end = 0.0

        self.listen_state(self._on_zone, self.zone_entity)
        self.listen_state(self._on_presence, self.presence_entity)
        self.listen_state(self._on_distance, self.distance_entity)
        self.run_every(self._tick, f"now+{TICK_S}", TICK_S)

        self.log(f"GhostDistance (temporary diagnostic) initialized: press {self.button_entity} after "
                 f"{self.dwell_s / 60:g} min of {self.presence_entity} on with {self.zone_entity} at 0, "
                 f"max {self.max_episodes} a day", level="INFO")

    def _on_zone(self, entity, attribute, old, new, kwargs):
        if not home_empty(new):
            self._zone_empty_since = None
        elif self._zone_empty_since is None:
            self._zone_empty_since = self._now()

    def _on_presence(self, entity, attribute, old, new, kwargs):
        if new == "on":
            if self._presence_on_since is None:
                self._presence_on_since = self._now()
            return
        self._presence_on_since = None
        if new == "off" and self._rec is not None:
            self._rec["presence_went_off"] = True

    def _on_distance(self, entity, attribute, old, new, kwargs):
        if self._rec is not None:
            self._rec["readings"].append([self._stamp(self._now()), distance_value(new)])

    def _tick(self, kwargs):
        self._evaluate(self._now())

    def _held_since(self):
        """When zone.home and presence started holding together: the later of their two
        edges. It identifies the episode, since a new episode always starts at a later edge."""
        if self._zone_empty_since is None or self._presence_on_since is None:
            return None
        return max(self._zone_empty_since, self._presence_on_since)

    def _evaluate(self, now):
        if self._rec is not None and now >= self._rec_end:
            self._finish(now)
        day = self._day_of(now)
        if day != self._day:
            self._day, self._episodes = day, 0
        since = self._held_since()
        if press_due(None if since is None else now - since, self.dwell_s,
                     pressed=since is not None and since == self._pressed_since,
                     recording=self._rec is not None,
                     episodes_today=self._episodes, max_episodes=self.max_episodes):
            self._press(now, since)

    def _press(self, now, since):
        self._pressed_since = since
        self._episodes += 1
        start = self._stamp(now)
        first = distance_value(self.get_state(self.distance_entity))
        try:
            self.call_service("button/press", entity_id=self.button_entity)
        except Exception as e:
            self.log(f"GhostDistance: pressing {self.button_entity} failed: {e!r}", level="WARNING")
            return
        self._rec = {"start": start, "presence_went_off": False, "readings": [[start, first]]}
        self._rec_end = now + self.record_s
        self.log(f"GhostDistance: pressed {self.button_entity}, episode {self._episodes} today, "
                 f"recording {self.record_s / 60:g} min", level="INFO")

    def _finish(self, now):
        rec, self._rec = self._rec, None
        final = distance_value(self.get_state(self.distance_entity))
        rec["readings"].append([self._stamp(now), final])
        line = json.dumps(rec)
        try:
            with open(self.record_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            self.log(f"GhostDistance: could not write {self.record_file}: {e!r}; record {line}", level="WARNING")
        self.log(f"GhostDistance: episode {rec['start']}, {len(rec['readings'])} reading(s), final distance "
                 f"{final}, presence went off: {rec['presence_went_off']}", level="INFO")

    def _now(self):
        return self.get_now().timestamp()

    def _local(self, epoch):
        return datetime.fromtimestamp(epoch, self.get_now().tzinfo)

    def _stamp(self, epoch):
        return self._local(epoch).isoformat(timespec="seconds")

    def _day_of(self, epoch):
        return self._local(epoch).date().isoformat()
