"""
PresenceStuckWatch - pushes the owner when a room's presence sensor looks stuck ON.

Notify only - never controls a device. One instance per room (see presence_stuck_watch.yaml,
flat args); a second room is a second yaml instance of this same module, not a `rooms:` dict.

no_motion rule, evaluated on the presence on-edge and a 60s run_every tick: while
`presence` reads "on", no transition into small/large motion for >= no_motion_min minutes,
falling back to the presence session's own start when no motion has been seen at all this
session.

Exactly one push per stuck episode (presence turning on to it turning off again) via
MobileNotifier, target="user" - never a repeat nag while the same episode stays stuck.

Restart seeding: presence_on_since/last_motion_at are seeded from the presence/motion
entities' own last_changed (HA only records changes) rather than from "now", so a restart
mid-episode alerts on the very next tick if the thresholds were already exceeded before the
restart - this app only ever sends a push, so being eager here costs nothing.

All timestamps are epoch seconds via the single _now() helper (never naive local datetime
arithmetic, which breaks across a DST change), so tests can fake the clock directly.
"""

from __future__ import annotations

import time
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

_MOTION_ACTIVE = ("small", "large")


def _parse_iso_epoch(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


class PresenceStuckWatch(hass.Hass):
    def initialize(self):
        a = self.args
        self.presence = a["presence"]
        self.motion = a["motion"]
        self.no_motion_min = float(a.get("no_motion_min", 30))
        self.room_name = a.get("room") or "Room"

        self._notifier = self.get_app("MobileNotifier")
        self._stuck = False
        self._presence_on_since = None
        self._last_motion_at = None

        now = self._now()
        if self.get_state(self.presence) == "on":
            self._presence_on_since = self._seed(self.presence, now)
        if self.get_state(self.motion) in _MOTION_ACTIVE:
            self._last_motion_at = self._seed(self.motion, now)

        self.listen_state(self._on_presence_change, self.presence)
        self.listen_state(self._on_motion_change, self.motion)
        self.run_every(self._tick, "now+60", 60)

        self.log(f"PresenceStuckWatch initialized for {self.room_name}", level="INFO")

    def _seed(self, entity, now):
        epoch = _parse_iso_epoch(self.get_state(entity, attribute="last_changed"))
        return now if epoch is None else epoch

    # ---------- listeners ----------

    def _on_presence_change(self, entity, attribute, old, new, kwargs):
        now = self._now()
        if new == "on" and old != "on":
            self._presence_on_since = now
            self._stuck = False
            self._evaluate(now)
        elif new == "off" and old != "off":
            self._close_episode(now)

    def _on_motion_change(self, entity, attribute, old, new, kwargs):
        # Transitions only: HA fires on value change, and a held small/large is not proof of
        # fresh motion (the device can report "small" in the same message that drops presence).
        if new in _MOTION_ACTIVE:
            self._last_motion_at = self._now()

    def _tick(self, kwargs):
        self._evaluate(self._now())

    # ---------- evaluation ----------

    def _evaluate(self, now):
        # No get_state(presence) re-poll here: presence_on_since is only ever set/cleared by
        # the presence listener's own edges, so trusting it avoids AD's stale get_state cache
        # inventing a session that never happened.
        if self._presence_on_since is None or self._stuck:
            return
        # Motion from a previous session must not count: the presence on-edge arrives before
        # its own motion event.
        last_motion = max(self._last_motion_at or 0.0, self._presence_on_since)
        no_motion_min = (now - last_motion) / 60.0
        if no_motion_min >= self.no_motion_min:
            hold_min = (now - self._presence_on_since) / 60.0
            self._alert(f"Presence on for {hold_min:.0f} min, no motion for {no_motion_min:.0f} min")

    def _alert(self, message):
        self._stuck = True
        self.log(f"PresenceStuckWatch: {self.room_name} presence stuck - {message}", level="WARNING")
        self.create_task(self._push(f"{self.room_name} presence stuck?", message))

    async def _push(self, title, message):
        try:
            sent = await self._notifier.notify(title=title, message=message, target="user")
        except Exception as e:
            self.log(f"PresenceStuckWatch: {self.room_name} notify failed: {e}", level="WARNING")
            self._stuck = False
            return
        if not sent:
            self.log(f"PresenceStuckWatch: {self.room_name} notify reached no recipients, will retry", level="WARNING")
            self._stuck = False

    def _close_episode(self, now):
        if self._stuck and self._presence_on_since is not None:
            minutes = (now - self._presence_on_since) / 60.0
            self.log(f"PresenceStuckWatch: {self.room_name} episode cleared after {minutes:.0f} min", level="INFO")
        self._presence_on_since = None
        self._stuck = False

    # ---------- misc ----------

    def _now(self):
        """Epoch seconds via AppDaemon's own clock (so a scheduler time-travel plugin is
        honoured); falls back to the wall clock on a bare test instance."""
        try:
            return self.get_now().timestamp()
        except Exception:
            return time.time()
