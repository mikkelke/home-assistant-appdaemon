"""MusicAttribution - one house-feed row per music session on the Sonos speakers, naming who
started it: a resident, "A guest" or "Someone".

A session starts with the first transition to playing after ``session_idle_minutes`` without
playback on that speaker or on its group coordinator (the idle time after which SonosStateReset
ungroups a group). A speaker joining a playing group belongs to that session; playing again inside
the idle window continues it. Each transition is judged ``settle_seconds`` after it happened, by
when Sonos has published the new group membership; a run that already stopped again by then never
counts.

Who started it:
1. A Home Assistant service call that starts playback (play_media, media_play, select_source, ...)
   on the group within ``call_window_seconds`` before playback began. ``context.user_id`` resolves
   through the person entities to a resident. ``house_user_ids`` (AppDaemon's own account) and
   automations (a ``context.parent_id`` without a user) are the house itself: no row, the apps and
   automations that do it report their own actions. Announcements and joins are not starts. Any
   other account is "Someone".
2. Spotify Connect: the resident whose Spotify account entity (``spotify_accounts``) is playing on
   the group's Spotify Connect device ("Kitchen", or "Kitchen + 3" for a group), polled for up to
   ``spotify_wait_seconds``. "A guest" only when every resident's account is configured and
   available, none matched or plays the same track, and Spotify still plays at the deadline;
   otherwise "Someone".
3. AirPlay, Music Assistant's own app, line-in and the Sonos app expose no identity: "Someone".
4. TV audio is not music: no row.

Rows are text-only ``house_events_report`` events, scoped like manual_override_timeout's
ROOM_AUDIENCE: a session touching a shared room is public, otherwise its rooms' occupants and the
admins see it.
"""

import time
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

UNKNOWN_STATES = (None, "unavailable", "unknown")

HOUSE = ("house", None)
SOMEONE = ("someone", None)

START_SERVICES = frozenset(
    {
        ("media_player", "play_media"),
        ("media_player", "media_play"),
        ("media_player", "media_play_pause"),
        ("media_player", "select_source"),
        ("music_assistant", "play_media"),
        ("music_assistant", "transfer_queue"),
        ("sonos", "play_queue"),
    }
)
CALL_DOMAINS = ("media_player", "music_assistant", "sonos")

ROOM_AUDIENCE = {
    "bedroom": {"audience": "admin"},
    "bathroom": {"audience": "admin"},
    "claudias_room": {"audience_users": ["Claudia"]},
    "kristines_room": {"audience_users": ["Kristine"]},
}

WHAT = {"spotify": "Spotify", "airplay": "AirPlay"}
ICONS = {"spotify": "mdi:spotify", "airplay": "mdi:airplay", "line_in": "mdi:cast-audio"}


# ---------------------------------------------------------------------------
# Pure helpers - module-level so the stdlib unittest gate covers them without
# an AppDaemon runtime.
# ---------------------------------------------------------------------------


def normalize_user_id(user_id):
    """HA account id in context.user_id's own form: undashed lowercase hex."""
    return str(user_id or "").replace("-", "").strip().lower()


def _truthy(value):
    return value is True or str(value).strip().lower() in ("true", "1", "yes", "on")


def _as_list(value):
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v]
    return []


def start_call_targets(domain, service, service_data):
    """Entity ids targeted by a service call that starts playback, or None for any other call.
    An announcement is not a start. Area/device targets are not resolved."""
    if (domain, service) not in START_SERVICES:
        return None
    data = service_data if isinstance(service_data, dict) else {}
    if _truthy(data.get("announce")):
        return None
    return _as_list(data.get("entity_id"))


def call_origin(user_id, parent_id, house_user_ids, resident_for):
    """Who is behind a service call: HOUSE, ('person', name) or SOMEONE.

    `resident_for(user_id)` returns the resident's display name or None.
    """
    if user_id:
        uid = normalize_user_id(user_id)
        if uid in house_user_ids:
            return HOUSE
        name = resident_for(uid)
        return ("person", name) if name else SOMEONE
    if parent_id:
        return HOUSE
    return SOMEONE


def content_kind(media_content_id, source=None):
    """What a Sonos player is playing, from the HA Sonos entity's media_content_id (source as a
    fallback while the URI is still empty): spotify, airplay, tv, line_in, other, or None."""
    cid = str(media_content_id or "")
    if cid.startswith("x-sonos-vli:"):
        if ",spotify:" in cid:
            return "spotify"
        if ",airplay:" in cid:
            return "airplay"
        return "other"
    if cid.startswith("x-sonos-htastream:"):
        return "tv"
    if cid.startswith("x-rincon-stream:"):
        return "line_in"
    if cid:
        return "other"
    return {"Spotify Connect": "spotify", "AirPlay": "airplay", "TV": "tv", "Line-in": "line_in"}.get(source)


def room_key(entity):
    """media_player.claudias_room -> 'claudias_room'."""
    return str(entity).split(".", 1)[-1]


def room_label(entity):
    """media_player.claudias_room -> 'Claudias room'."""
    return room_key(entity).replace("_", " ").strip().capitalize()


def where_label(labels):
    """Coordinator-first room labels -> 'Kitchen' or 'Kitchen + 3 more'."""
    if not labels:
        return "the house"
    if len(labels) == 1:
        return labels[0]
    return f"{labels[0]} + {len(labels) - 1} more"


def session_text(who, kind, where):
    text = f"{who} started {WHAT.get(kind, 'music')} in {where}"
    return text.encode("ascii", "ignore").decode("ascii")


def icon_for(kind):
    return ICONS.get(kind, "mdi:music")


def audience_for(room_keys):
    """Feed audience for a session over these rooms: {} (public) as soon as one room is shared,
    else the union of the rooms' occupants, or admin-only for admin-only rooms."""
    users = []
    for key in room_keys:
        rule = ROOM_AUDIENCE.get(key)
        if rule is None:
            return {}
        for user in rule.get("audience_users", []):
            if user not in users:
                users.append(user)
    if users:
        return {"audience_users": users}
    return {"audience": "admin"} if room_keys else {}


def device_key(name):
    """Comparable form of a Spotify Connect device or room name: "Kitchen + 3" -> 'kitchen',
    "Claudia's Room" -> 'claudiasroom'. Spotify names a Sonos group after its coordinator plus a
    member count."""
    if not isinstance(name, str):
        return ""
    base = name.split("+", 1)[0]
    return "".join(ch for ch in base.lower() if ch.isalnum())


def track_key(attributes):
    """(title, artist) lowercased, or None without a title."""
    attrs = attributes if isinstance(attributes, dict) else {}
    title = str(attrs.get("media_title") or "").strip().lower()
    if not title:
        return None
    return title, str(attrs.get("media_artist") or "").strip().lower()


def spotify_matches(accounts, states, device_keys):
    """Residents whose Spotify account entity is playing on one of `device_keys`.

    `accounts` maps resident -> entity id, `states` maps entity id -> HA state dict."""
    hits = []
    for person, entity in accounts.items():
        state = states.get(entity) or {}
        if state.get("state") != "playing":
            continue
        if device_key((state.get("attributes") or {}).get("source")) in device_keys:
            hits.append(person)
    return hits


def spotify_is_guest(residents, accounts, states, still_spotify, track):
    """True when an unmatched Spotify start can only be a guest's: every resident's account is
    configured and available, none plays the speaker's current track, and Spotify still plays on
    the speaker (so every account had the whole wait to show up)."""
    if not still_spotify or not residents:
        return False
    for person in residents:
        entity = accounts.get(person)
        if not entity:
            return False
        state = states.get(entity) or {}
        if state.get("state") in UNKNOWN_STATES:
            return False
        if track is not None and state.get("state") == "playing" and track_key(state.get("attributes")) == track:
            return False
    return True


def parse_ts(value):
    """Epoch seconds from an HA ISO timestamp, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class PlaybackLedger:
    """Play-run bookkeeping per speaker: decides which transition to playing opens a session and
    which recent service call explains it. Pure - fed by the app and by recorder replays alike."""

    def __init__(self, idle_s=1200.0, call_window_s=30.0):
        self.idle_s = float(idle_s)
        self.call_window_s = float(call_window_s)
        self._run_start = {}
        self._confirmed = set()
        self._heard = {}
        self._calls = []

    def seed(self, speaker, state, since_ts):
        """Current state at app start. A speaker not playing counts as heard when it entered its
        current state - its last run may have ended right then."""
        if state in UNKNOWN_STATES or since_ts is None:
            return
        if state == "playing":
            self._run_start[speaker] = since_ts
            self._confirmed.add(speaker)
        self._heard[speaker] = since_ts

    def transition(self, speaker, old, new, ts):
        """Feed a state change; True when a settle check must run for the run starting now."""
        if new == "playing":
            if old == "playing":
                return False
            self._run_start[speaker] = ts
            self._confirmed.discard(speaker)
            if old in UNKNOWN_STATES:
                self._confirmed.add(speaker)
                self._heard[speaker] = ts
                return False
            return True
        if speaker in self._run_start:
            if speaker in self._confirmed:
                self._heard[speaker] = ts
            del self._run_start[speaker]
            self._confirmed.discard(speaker)
        return False

    def playing(self, speaker):
        return speaker in self._run_start

    def settle(self, speaker, run_ts, group):
        """Verdict for the run that began at run_ts, judged now: 'stale' (that run is over),
        'join', 'resume' or 'new'. `group` is the speaker's current group, coordinator first."""
        if self._run_start.get(speaker) != run_ts:
            return "stale"
        coordinator = group[0] if group else speaker
        verdict = "new"
        if coordinator != speaker and self._run_start.get(coordinator, run_ts) < run_ts:
            verdict = "join"
        else:
            for member in {speaker, coordinator}:
                heard = self._heard.get(member)
                if heard is not None and heard >= run_ts - self.idle_s:
                    verdict = "resume"
                    break
        self._confirmed.add(speaker)
        self._heard[speaker] = max(self._heard.get(speaker) or run_ts, run_ts)
        if verdict == "new":
            for member in group:
                self._heard[member] = max(self._heard.get(member) or run_ts, run_ts)
        return verdict

    def add_call(self, ts, speakers, origin):
        if not speakers:
            return
        self._calls.append((ts, frozenset(speakers), origin))
        horizon = ts - max(self.call_window_s, 60.0)
        self._calls = [call for call in self._calls if call[0] >= horizon]

    def origin_for(self, speakers, run_ts):
        """Origin of the newest start call on any of `speakers` within the call window before
        run_ts, or None."""
        wanted = set(speakers)
        for ts, targets, origin in reversed(self._calls):
            if run_ts - self.call_window_s <= ts <= run_ts + 1.0 and targets & wanted:
                return origin
        return None


class MusicAttribution(hass.Hass):
    def initialize(self):
        self.raw_to_base = dict(self.args["raw_to_base_map"])
        self.base_to_raw = {base: raw for raw, base in self.raw_to_base.items()}
        self.residents = [str(name) for name in (self.args.get("residents") or [])]
        self.house_user_ids = {normalize_user_id(uid) for uid in (self.args.get("house_user_ids") or [])}
        self.spotify_accounts = {
            str(person): entity
            for person, entity in (self.args.get("spotify_accounts") or {}).items()
            if isinstance(entity, str) and entity.startswith("media_player.")
        }
        self.settle_s = float(self.args.get("settle_seconds", 5))
        self.spotify_wait_s = float(self.args.get("spotify_wait_seconds", 60))
        self.spotify_poll_s = float(self.args.get("spotify_poll_seconds", 5))
        self.ledger = PlaybackLedger(
            idle_s=float(self.args.get("session_idle_minutes", 20)) * 60.0,
            call_window_s=float(self.args.get("call_window_seconds", 30)),
        )
        self._person_by_user_id = {}

        now = self._now()
        for raw in self.raw_to_base:
            try:
                full = self.get_state(raw, attribute="all") or {}
            except Exception:
                full = {}
            self.ledger.seed(raw, full.get("state"), parse_ts(full.get("last_changed")) or now)
            self.listen_state(self._on_speaker, raw)
        for domain in CALL_DOMAINS:
            self.listen_event(self._on_call, "call_service", domain=domain)
        self.log(
            f"MusicAttribution: {len(self.raw_to_base)} speakers, "
            f"{len(self.spotify_accounts)}/{len(self.residents)} Spotify accounts linked"
        )

    def _now(self):
        try:
            return self.get_now().timestamp()
        except Exception:
            return time.time()

    # -- inputs -------------------------------------------------------------

    def _on_speaker(self, entity, attribute, old, new, kwargs):
        ts = self._now()
        if self.ledger.transition(entity, old, new, ts):
            self.run_in(self._settle, self.settle_s, speaker=entity, run_ts=ts)

    def _on_call(self, event_name, data, kwargs):
        try:
            targets = start_call_targets(data.get("domain"), data.get("service"), data.get("service_data"))
            speakers = self._speakers_for(targets or [])
            if not speakers:
                return
            context = (data.get("metadata") or {}).get("context") or {}
            origin = call_origin(
                context.get("user_id"), context.get("parent_id"), self.house_user_ids, self._resident_for
            )
            self.ledger.add_call(self._now(), speakers, origin)
        except Exception as e:
            self.log(f"call_service handling failed: {e}", level="WARNING")

    # -- session decisions --------------------------------------------------

    def _settle(self, kwargs):
        speaker = kwargs["speaker"]
        run_ts = kwargs["run_ts"]
        group = self._group(speaker)
        verdict = self.ledger.settle(speaker, run_ts, group)
        if verdict != "new":
            return
        kind = content_kind(self._content(speaker), self._attr(speaker, "source"))
        if kind == "tv":
            return
        origin = self.ledger.origin_for(set(group) | {speaker}, run_ts)
        if origin is not None:
            if origin == HOUSE:
                self.log(f"Session on {speaker} started by the house - no row", level="DEBUG")
                return
            self._report(origin[1] or "Someone", kind, speaker, group, by=origin[1])
            return
        if kind == "spotify" and self.spotify_accounts:
            deadline = run_ts + self.spotify_wait_s
            self.run_in(self._spotify_poll, self.spotify_poll_s, speaker=speaker, group=group, deadline=deadline)
            return
        self._report("Someone", kind, speaker, group)

    def _spotify_poll(self, kwargs):
        speaker = kwargs["speaker"]
        group = kwargs["group"]
        deadline = kwargs["deadline"]
        states = {entity: self._full_state(entity) for entity in self.spotify_accounts.values()}
        hits = spotify_matches(self.spotify_accounts, states, self._device_keys(group))
        if len(hits) == 1:
            self._report(hits[0], "spotify", speaker, group, by=hits[0])
            return
        if self._now() < deadline:
            self.run_in(self._spotify_poll, self.spotify_poll_s, speaker=speaker, group=group, deadline=deadline)
            return
        still_spotify = self.ledger.playing(speaker) and content_kind(self._content(speaker)) == "spotify"
        track = track_key(self._full_state(speaker).get("attributes"))
        guest = not hits and spotify_is_guest(self.residents, self.spotify_accounts, states, still_spotify, track)
        self._report("A guest" if guest else "Someone", "spotify", speaker, group)

    # -- reporting ----------------------------------------------------------

    def _report(self, who, kind, speaker, group, by=None):
        current = self._group(speaker) if self.ledger.playing(speaker) else []
        members = current if len(current) >= len(group) else group
        text = session_text(who, kind, where_label([room_label(self.raw_to_base[m]) for m in members]))
        payload = {"text": text, "icon": icon_for(kind)}
        payload.update(audience_for([room_key(self.raw_to_base[m]) for m in members]))
        if by:
            payload["by"] = by
        self.log(f"Music session: {text}")
        try:
            self.fire_event("house_events_report", **payload)
        except Exception:
            pass

    # -- HA state helpers ---------------------------------------------------

    def _full_state(self, entity):
        try:
            return self.get_state(entity, attribute="all") or {}
        except Exception:
            return {}

    def _attr(self, entity, attribute):
        try:
            return self.get_state(entity, attribute=attribute)
        except Exception:
            return None

    def _content(self, speaker):
        return self._attr(speaker, "media_content_id")

    def _group(self, speaker):
        """The speaker's group as known speakers, coordinator first (HA Sonos lists it first)."""
        members = self._attr(speaker, "group_members")
        group = [m for m in members if m in self.raw_to_base] if isinstance(members, list) else []
        return group if speaker in group else [speaker]

    def _speakers_for(self, entity_ids, depth=0):
        """Sonos speakers (raw entities) behind service-call targets: a speaker's Sonos or Music
        Assistant entity, or a media_player group of them (HA group or Music Assistant group
        player)."""
        speakers = set()
        for entity in entity_ids:
            if entity in self.raw_to_base:
                speakers.add(entity)
            elif entity in self.base_to_raw:
                speakers.add(self.base_to_raw[entity])
            elif entity == "all":
                speakers.update(self.raw_to_base)
            elif depth == 0 and entity.startswith("media_player."):
                members = self._attr(entity, "entity_id") or self._attr(entity, "group_members")
                if isinstance(members, list):
                    speakers |= self._speakers_for([m for m in members if isinstance(m, str)], depth + 1)
        return speakers

    def _device_keys(self, group):
        keys = set()
        for raw in group:
            base = self.raw_to_base[raw]
            for name in (room_label(base), self._attr(raw, "friendly_name"), self._attr(base, "friendly_name")):
                key = device_key(name)
                if key:
                    keys.add(key)
        return keys

    def _resident_for(self, user_id):
        """Resident display name for an HA account id via the person entities, or None. The map
        refreshes lazily on an unseen id."""
        if user_id not in self._person_by_user_id:
            self._refresh_person_map()
        name = self._person_by_user_id.get(user_id)
        if not name:
            return None
        for resident in self.residents:
            if resident.lower() == str(name).strip().lower():
                return resident
        return None

    def _refresh_person_map(self):
        try:
            for entity in self.get_state("person") or {}:
                attrs = self._full_state(entity).get("attributes") or {}
                uid = attrs.get("user_id")
                if uid:
                    self._person_by_user_id[normalize_user_id(uid)] = (
                        attrs.get("friendly_name") or entity.split(".", 1)[-1].capitalize()
                    )
        except Exception as e:
            self.log(f"person map refresh failed: {e}", level="WARNING")
