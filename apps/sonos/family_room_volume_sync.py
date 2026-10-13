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
    un-mutes). Mute is never touched and volume 0 is never written. A follower always moves
    from the volume it reports now. Home Assistant answers a service call to an unavailable
    twin with success and does nothing, so those followers are skipped.

    Our own writes are recognised by value, not by time: every target we write is remembered
    for echo_window_seconds and the first change that lands on it is its echo and is ignored.
    Any other change, including one on a follower right after we wrote it, is a user change.

    Active = playing, volume above 0 and, for tv_speaker, not on the TV source. Inactive,
    solo and unavailable speakers neither lead nor follow, and a speaker only counts changes
    made while it was in the group (no rebalance on join). A change remembers the group it was
    made in, so a speaker that joined before the window closed is not written. Bursts where
    the group moves by clearly different amounts (Sonos group volume, announcements) are left
    alone. A speaker whose volume moves while it is inactive (to or from 0, paused, on the TV)
    gives up its lead in the window and cannot lead for max_window_seconds after: that is a
    fade or a restore ramping it, not someone turning it.

    A speaker whose own volume changed within max_window_seconds of the sync is never written
    as a follower of it: it already took part in the move (a group step lifts a speaker from 0
    while the others step too), and that holds even when its changes net to nothing.

    While the party-mode helper is on the app does nothing. A periodic check switches the
    helper off again once it has been on for more than party_mode_max_hours; a Home Assistant
    restart restarts that clock.
    """

    def initialize(self):
        a = self.args.get
        self.speakers = list(self.args["speakers"])
        self.tv_speaker = a("tv_speaker", "media_player.living_room")
        self.party_entity = a("party_mode_entity", "input_boolean.family_volume_sync_party")
        self.settle = float(a("settle_seconds", 0.8))
        self.max_window = float(a("max_window_seconds", 5))
        self.echo_window = float(a("echo_window_seconds", 10))
        self.party_max_hours = float(a("party_mode_max_hours", 12))

        self._speaker_set = frozenset(self.speakers)
        self._snap = {}
        self._pending = {}
        self._who = {}
        self._touched = {}
        self._inactive_moves = {}
        self._expected = {}
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

    def _key(self, s):
        members = ((s or {}).get("attributes") or {}).get("group_members")
        if not isinstance(members, (list, tuple)):
            return frozenset()
        return frozenset(members) & self._speaker_set

    def _label(self, s):
        user_id = ((s or {}).get("context") or {}).get("user_id")
        if not user_id:
            return "device"
        return self._who_map.get(user_id) or f"user {str(user_id)[:8]}"

    def _live_expected(self, m, now):
        live = [(until, target) for until, target in self._expected.get(m, []) if now < until]
        self._expected[m] = live
        return live

    def _own_echo(self, m, v, now):
        live = self._live_expected(m, now)
        for i, (_, target) in enumerate(live):
            if abs(target - v) < NOISE:
                del live[i]
                return True
        return False

    def _expect(self, m, target):
        now = self._now()
        self._live_expected(m, now).append((now + self.echo_window, target))

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
        on = isinstance(st, dict) and st.get("state") == "on"
        self._party_since = self._parse_time(st.get("last_changed")) if on else None
        if on != self._party_on:
            self._party_on = on
            self.log(
                "Scenario: family_volume_sync_party -> on, sync paused" if on
                else "Scenario: family_volume_sync_party -> off, sync resumed",
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
        self._snap[entity] = new
        ov, nv = self._vol(old), self._vol(new)
        if ov is None or nv is None or abs(nv - ov) < NOISE:
            return
        now = self._now()
        if self._own_echo(entity, nv, now):
            self.log(
                f"Scenario: family_volume_sync_echo -> {self._short(entity)} {ov:.2f}->{nv:.2f} "
                f"is our own write, ignored",
                level="DEBUG",
            )
            return
        if self._party_on:
            return
        self._touched[entity] = now
        if not (self._active(entity, old) and self._active(entity, new)):
            self._pending.pop(entity, None)
            self._inactive_moves[entity] = now
            return
        key = self._key(new)
        if len(key) < 2 or now - self._inactive_moves.get(entity, float("-inf")) <= self.max_window + EPS:
            return
        self._pending.setdefault(entity, [key, 0.0])[1] += nv - ov
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
        if self._party_on:
            return
        self._apply(now, pending, who, touched)

    def _apply(self, now, pending, who, touched):
        moved = {m: rec[1] for m, rec in pending.items() if abs(rec[1]) >= STEP - EPS}
        groups = {}
        for m in self.speakers:
            s = self._snap.get(m)
            if self._active(m, s):
                key = self._key(s)
                if len(key) >= 2:
                    groups.setdefault(key, []).append(m)

        for members in groups.values():
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
            eligible = frozenset.intersection(*(pending[m][0] for m in movers))
            others = [m for m in members if m not in movers]
            regrouped = [m for m in others if m not in eligible]
            touched_now = [m for m in others if m in eligible and self._touched_recently(m, touched, now)]
            followers = [m for m in others if m in eligible and m not in touched_now]
            note = ""
            if touched_now:
                note += f" | touched, left alone: {','.join(self._short(m) for m in touched_now)}"
            if regrouped:
                note += f" | not in the group when it moved, left alone: {','.join(self._short(m) for m in regrouped)}"
            if not followers:
                self.log(
                    f"Scenario: family_volume_sync_together -> {d:+.2f} by {led}, nothing to follow{note} | group {group}",
                    level="INFO",
                )
                continue

            changes = []
            for j in followers:
                snap = self._snap[j]
                cur = self._vol(snap)
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
                self._expect(j, target)
                flags = ""
                if (snap.get("attributes") or {}).get("is_volume_muted") is True:
                    flags += " muted"
                if target != want:
                    flags += " clamped"
                changes.append(f"{self._short(j)} {cur:.2f}->{target:.2f}{flags}")

            if changes:
                self.log(
                    f"Scenario: family_volume_sync_apply -> {d:+.2f} led by {led} | "
                    f"{', '.join(changes)}{note} | group {group}",
                    level="INFO",
                )
