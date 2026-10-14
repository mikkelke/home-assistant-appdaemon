# /conf/apps/sonos/family_room_volume_sync.py
import time
from datetime import datetime, timezone

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

# Sonos volumes are integer percents, so every real move is a multiple of 0.01.
STEP = 0.01    # smallest net change in a window that counts as a deliberate move
SAME = 0.015   # movers whose deltas differ by more than this are not one user action
FLOOR = 0.01   # followers are never written to 0
CEIL = 1.0
NOISE = 0.005  # volume_level changes below this are rounding, not a move
EPS = 1e-6     # float slack: one 0.01 step computes as 0.00999..., a timer can fire a hair early
PARTY_CHECK_S = 900
BREAKER_SYNCS = 4         # syncs one group may make within BREAKER_WINDOW_S
BREAKER_WINDOW_S = 60
BREAKER_PAUSE_S = 600     # how long the app stops when a group goes past that


class FamilyRoomVolumeSync(hass.Hass):
    """
    Relative volume sync for the family-room Sonos speakers (the Music Assistant entities
    listed in `speakers`).

    When one or more speakers of a Sonos group holding at least two family speakers change
    volume by the same step, every other active member of that group moves by that step, so
    the offsets between the rooms stay put. Any speaker can lead. Changes are collected in a
    short window so a dragged slider or a double tap propagates once with its net amount.

    Followers are written through their native twin (<entity>_2): the write lands exactly on
    the target and leaves the mute state alone (volume_set on the Music Assistant entity
    un-mutes). Mute is never touched, volume 0 is never written, and a muted follower is not
    written at all, so it keeps its level. A follower moves from the volume it reports now.
    Home Assistant answers a service call to an unavailable twin with success and does
    nothing, so those followers are skipped.

    After a speaker is written, the next volume change it reports is taken as the answer to
    that write and never leads, whatever its value and however late it comes; a user change
    that lands in that gap is lost on purpose. Only the latest write to a speaker waits.

    A change is measured from the last state this app saw for the speaker, not from the old
    state AppDaemon passes in. The states read at startup are not trusted: a speaker neither
    leads nor follows until it has reported live once.

    Active = playing, volume above 0 and, for tv_speaker, not on the TV source. Inactive,
    solo and unavailable speakers neither lead nor follow. Any change in a speaker's group
    members or coordinator drops the moves collected for that group, so nothing carries over
    a join or a leave. Bursts where the group moves by clearly different amounts (Sonos group
    volume, announcements) are left alone. A speaker whose volume moves while it is inactive
    (to or from 0, paused, on the TV) gives up its lead in the window and cannot lead for
    max_window_seconds after: that is a fade or a restore ramping it, not someone turning it.

    A speaker whose own volume changed in the current window, at most max_window_seconds
    before it closes, is never written as a follower of it: it already took part in the move
    (a group step lifts a speaker from 0 while the others step too), and that holds even when
    its changes net to nothing. Those changes are forgotten when the window closes.

    No group is synced more than BREAKER_SYNCS times within BREAKER_WINDOW_S: past that the
    app stops for BREAKER_PAUSE_S and logs a warning.

    While the party-mode helper is on, or cannot be read, the app does nothing, and switching
    it on drops the moves collected so far. A periodic check switches the helper off again
    once it has been on for more than party_mode_max_hours; a Home Assistant restart restarts
    that clock.
    """

    def initialize(self):
        a = self.args.get
        self.speakers = list(self.args["speakers"])
        self.tv_speaker = a("tv_speaker", "media_player.living_room")
        self.party_entity = a("party_mode_entity", "input_boolean.family_volume_sync_party")
        self.settle = float(a("settle_seconds", 0.8))
        self.max_window = float(a("max_window_seconds", 5))
        self.party_max_hours = float(a("party_mode_max_hours", 12))

        self._speaker_set = frozenset(self.speakers)
        self._snap = {}
        self._live = set()
        self._pending = {}
        self._who = {}
        self._touched = {}
        self._inactive_moves = {}
        self._outstanding = {}
        self._syncs = {}
        self._stopped_until = float("-inf")
        self._timer = None
        self._window_open = 0.0
        self._last_move = 0.0
        self._party_on = False
        self._party_since = None
        self._who_map = self._person_names()

        # attribute="all": callbacks get the full state dicts, which carry context.user_id.
        for m in self.speakers:
            self._snap[m] = self.get_state(m, attribute="all")
            self.listen_state(self._on_change, m, attribute="all")

        # AppDaemon 4.5.13 does not learn entities created while it runs, so get_state alone
        # would miss a helper created after startup; the listener sees it from its first event.
        if self.party_entity:
            self._set_party(self.get_state(self.party_entity, attribute="all"))
            self.listen_state(self._on_party, self.party_entity, attribute="all")
            self.run_every(self._check_party_expiry, "now+60", PARTY_CHECK_S)

        self.log("Scenario: FamilyRoomVolumeSync_loaded", level="INFO")

    def _now(self):
        return time.monotonic()

    def _wall_now(self):
        return datetime.now(timezone.utc)

    def _person_names(self):
        names = {}
        try:
            for entity_id, state in (self.get_state("person") or {}).items():
                attrs = (state or {}).get("attributes") or {}
                user_id = attrs.get("user_id")
                if user_id:
                    names[user_id] = attrs.get("friendly_name") or entity_id.split(".", 1)[-1]
        except Exception as e:
            self.log(f"Scenario: family_volume_sync_person_names_failed -> {e}", level="DEBUG")
        return names

    @staticmethod
    def _short(entity):
        return entity.split(".", 1)[-1]

    @staticmethod
    def _vol(s):
        if not isinstance(s, dict):
            return None
        v = (s.get("attributes") or {}).get("volume_level")
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    def _active(self, m, s):
        if not isinstance(s, dict) or s.get("state") != "playing":
            return False
        v = self._vol(s)
        if v is None or v <= 0:
            return False
        if m == self.tv_speaker and (s.get("attributes") or {}).get("source") == "TV":
            return False
        return True

    @staticmethod
    def _members(s):
        members = ((s or {}).get("attributes") or {}).get("group_members")
        return list(members) if isinstance(members, (list, tuple)) else []

    def _key(self, s):
        return frozenset(self._members(s)) & self._speaker_set

    def _label(self, s):
        user_id = ((s or {}).get("context") or {}).get("user_id")
        if not user_id:
            return "device"
        return self._who_map.get(user_id) or f"user {str(user_id)[:8]}"

    def _touched_recently(self, m, touched, now):
        at = touched.get(m)
        return at is not None and now - at <= self.max_window + EPS

    def _call(self, service, **data):
        try:
            result = self.call_service(service, **data)
        except Exception as e:
            self.log(f"Scenario: family_volume_sync_call_failed -> {service} {data.get('entity_id')}: {e}", level="WARNING")
            return False
        # AppDaemon hands back Home Assistant's whole websocket response; a rejected call is
        # a result dict, not an exception.
        if isinstance(result, dict) and result.get("success") is False:
            err = result.get("error") or {}
            self.log(
                f"Scenario: family_volume_sync_call_failed -> {service} {data.get('entity_id')}: "
                f"{err.get('code')} {err.get('message')}",
                level="WARNING",
            )
            return False
        return True

    @staticmethod
    def _parse_time(value):
        try:
            since = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return since if since.tzinfo else since.replace(tzinfo=timezone.utc)

    def _set_party(self, st):
        state = st.get("state") if isinstance(st, dict) else None
        on = state != "off"
        self._party_since = self._parse_time(st.get("last_changed")) if state == "on" else None
        if on == self._party_on:
            return
        self._party_on = on
        if not on:
            self.log("Scenario: family_volume_sync_party -> off, sync resumed", level="INFO")
            return
        self._pending.clear()
        if state == "on":
            self.log("Scenario: family_volume_sync_party -> on, sync paused", level="INFO")
        else:
            self.log(
                f"Scenario: family_volume_sync_party -> {self.party_entity} is {state or 'missing'}, "
                f"sync paused until it reports",
                level="INFO",
            )

    def _on_party(self, entity, attribute, old, new, kwargs):
        self._set_party(new)

    def _check_party_expiry(self, kwargs=None):
        if not self._party_on or self._party_since is None:
            return
        if (self._wall_now() - self._party_since).total_seconds() <= self.party_max_hours * 3600:
            return
        self.log(
            f"Scenario: family_volume_sync_party_expired -> {self.party_entity} has been on for more than "
            f"{self.party_max_hours:g} h, turning it off",
            level="INFO",
        )
        self._call("input_boolean/turn_off", entity_id=self.party_entity)

    def _on_change(self, entity, attribute, old, new, kwargs):
        prev = self._snap.get(entity)
        self._snap[entity] = new
        if entity not in self._live:
            self._live.add(entity)
            return
        if self._members(prev) != self._members(new):
            for m in self._key(prev) | self._key(new) | {entity}:
                self._pending.pop(m, None)
        ov, nv = self._vol(prev), self._vol(new)
        if ov is None or nv is None or abs(nv - ov) < NOISE:
            return
        now = self._now()
        if entity in self._outstanding:
            target = self._outstanding.pop(entity)
            self.log(
                f"Scenario: family_volume_sync_answer -> {self._short(entity)} {ov:.2f}->{nv:.2f} "
                f"answers our write of {target:.2f}, not a lead",
                level="DEBUG",
            )
            return
        if self._party_on or now < self._stopped_until:
            return
        self._touched[entity] = now
        if not (self._active(entity, prev) and self._active(entity, new)):
            self._pending.pop(entity, None)
            self._inactive_moves[entity] = now
            return
        if len(self._key(new)) < 2 or now - self._inactive_moves.get(entity, float("-inf")) <= self.max_window + EPS:
            return
        self._pending[entity] = self._pending.get(entity, 0.0) + nv - ov
        self._who[entity] = self._label(new)
        self._last_move = now
        if self._timer is None:
            self._window_open = now
            self._timer = self.run_in(self._on_timer, self.settle)

    def _on_timer(self, kwargs=None):
        self._timer = None
        now = self._now()
        quiet = now - self._last_move
        age = now - self._window_open
        if quiet < self.settle - EPS and age < self.max_window - EPS:
            self._timer = self.run_in(self._on_timer, min(self.settle - quiet, self.max_window - age))
            return
        pending, who, touched = self._pending, self._who, self._touched
        self._pending, self._who, self._touched = {}, {}, {}
        if self._party_on or now < self._stopped_until:
            return
        self._apply(now, pending, who, touched)

    def _apply(self, now, pending, who, touched):
        moved = {m: d for m, d in pending.items() if abs(d) >= STEP - EPS}
        groups = {}
        for m in self.speakers:
            s = self._snap.get(m)
            if self._active(m, s):
                key = self._key(s)
                if len(key) >= 2:
                    groups.setdefault(key, []).append(m)

        for key, members in groups.items():
            if len(members) < 2:
                continue
            movers = [m for m in members if m in moved]
            if not movers:
                continue
            group = ",".join(sorted(self._short(m) for m in members))
            led = ", ".join(f"{self._short(m)} ({who.get(m, 'device')})" for m in movers)
            deltas = [moved[m] for m in movers]
            if max(deltas) - min(deltas) > SAME:
                moves = ", ".join(f"{self._short(m)} {moved[m]:+.2f}" for m in movers)
                self.log(
                    f"Scenario: family_volume_sync_skip -> {moves} differ by more than {SAME} "
                    f"(group change), not synced | group {group}",
                    level="INFO",
                )
                continue
            d = round(sum(deltas) / len(deltas), 2)
            alone = {}
            for m in members:
                if m in movers:
                    continue
                if self._touched_recently(m, touched, now):
                    alone[m] = "touched"
                elif m not in self._live:
                    alone[m] = "not seen live yet"
                elif (self._snap[m].get("attributes") or {}).get("is_volume_muted") is True:
                    alone[m] = "muted"
            followers = [m for m in members if m not in movers and m not in alone]
            note = ""
            if alone:
                note = " | left alone: " + ", ".join(f"{self._short(m)} ({why})" for m, why in alone.items())
            if not followers:
                self.log(
                    f"Scenario: family_volume_sync_together -> {d:+.2f} by {led}, nothing to follow{note} | group {group}",
                    level="INFO",
                )
                continue

            recent = [t for t in self._syncs.get(key, []) if now - t < BREAKER_WINDOW_S]
            self._syncs[key] = recent
            if len(recent) >= BREAKER_SYNCS:
                self._stopped_until = now + BREAKER_PAUSE_S
                self.log(
                    f"Scenario: family_volume_sync_stopped -> {BREAKER_SYNCS} syncs within {BREAKER_WINDOW_S} s "
                    f"in group {group}, stopping for {BREAKER_PAUSE_S // 60} min",
                    level="WARNING",
                )
                return

            changes = []
            for j in followers:
                cur = self._vol(self._snap[j])
                want = round(cur + d, 2)
                target = min(CEIL, max(FLOOR, want))
                if abs(target - cur) < NOISE:
                    continue
                twin = j + "_2"
                if self.get_state(twin) in (None, "unavailable", "unknown"):
                    self.log(
                        f"Scenario: family_volume_sync_twin_unavailable -> {twin} is not available, "
                        f"{self._short(j)} not written",
                        level="INFO",
                    )
                    continue
                if not self._call("media_player/volume_set", entity_id=twin, volume_level=target):
                    continue
                self._outstanding[j] = target
                changes.append(f"{self._short(j)} {cur:.2f}->{target:.2f}{' clamped' if target != want else ''}")

            if changes:
                recent.append(now)
                self.log(
                    f"Scenario: family_volume_sync_apply -> {d:+.2f} led by {led} | "
                    f"{', '.join(changes)}{note} | group {group}",
                    level="INFO",
                )
