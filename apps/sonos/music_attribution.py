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
2. Spotify Connect: every resident whose Spotify account entity (``spotify_accounts``) is playing on
   the group's Spotify Connect device ("Kitchen", or "Kitchen + 3" for a group), polled for up to
   ``spotify_wait_seconds``; several (a Jam) are all named. "A guest" only when every resident's
   account is configured and available, none matched or plays the same track, and Spotify still
   plays at the deadline; otherwise "Someone". A resident whose account turns up playing on the
   session's lead speaker after the row was written joined it (a Jam): the same row is re-sent
   naming them too, "Guest and Mikkel started Spotify in Kitchen". Names are never removed.
3. AirPlay, Music Assistant's own app, line-in and the Sonos app expose no identity: "Someone".
4. TV audio is not music: no row.

Rows are text-only, public ``house_events_report`` events naming the rooms coordinator first:
"Bathroom", "Bathroom and Bedroom", "Bathroom, Bedroom and Kitchen", or "the whole house" when every
speaker plays. Each row carries a ``key`` and is re-sent in place when it grows: a speaker that
joins the group led by the session's lead speaker is added to its rooms (and gets the session's
start in ``sessions_sensor``), whoever grouped it; rooms are never removed. A session is followed
until a new session starts on one of its speakers, for ``SESSION_TRACK_S``, or until AppDaemon
restarts.

Who stopped it: a speaker that leaves playing for idle, paused or off and is still not playing
``settle_seconds`` later stopped. A stopping service call (media_stop, media_pause, unjoin, ...)
on it or its group within ``call_window_seconds`` before names a resident or the house, as for
starts; without one the stop came from outside Home Assistant (the Sonos or Music Assistant app,
a speaker button, or the music ending). Stops get no feed row.

A Spotify stop from outside Home Assistant names the residents whose account was playing on the
stopped group's device just before it and whose first poll from ``STOP_DECIDE_FROM_S`` after the
stop (or a change from ``STOP_LOOKBACK_S`` before it) shows it paused, idle or playing on a device
that is not a house speaker; an account that was at the end of its track (``TRACK_END_S``) let
the music end. Several are all named; accounts still undecided after ``spotify_stop_watch_seconds``
are not.

The account entities poll Spotify every 30 s. While a Spotify start waits for its account and
right after a Spotify stop with candidates, the app asks Home Assistant to refresh them
(homeassistant.update_entity), at most once per ``spotify_refresh_min_seconds`` per account, which
is also the cooldown Home Assistant's own refresh debouncer applies.

``sessions_sensor`` holds, per Music Assistant speaker entity, when its current or last session
started and who started it, and when and by whom it stopped, so the house status can answer who
started or stopped the music.
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
STOP_SERVICES = frozenset(
    {
        ("media_player", "media_stop"),
        ("media_player", "media_pause"),
        ("media_player", "media_play_pause"),
        ("media_player", "turn_off"),
        ("media_player", "clear_playlist"),
        ("media_player", "unjoin"),
    }
)
CALL_DOMAINS = ("media_player", "music_assistant", "sonos")
STOPPED_STATES = ("idle", "paused", "off")

HOUSE_LABEL = "the house"
HA_SOMEONE_LABEL = "someone in Home Assistant"
OUTSIDE_LABEL = "outside Home Assistant: an app, a speaker button, or the music ended"

WHAT = {"spotify": "Spotify", "airplay": "AirPlay"}
GUEST = "Guest"
# A session's row grows with joining rooms and residents for at most this long after it started.
SESSION_TRACK_S = 12 * 3600
# Stop matching: an account change this long before the stop still belongs to it; an unchanged
# poll decides only from this long after it; an account this close to its track's end let it end.
STOP_LOOKBACK_S = 5.0
STOP_DECIDE_FROM_S = 3.0
TRACK_END_S = 3.0
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


def stop_call_targets(domain, service, service_data):
    """Entity ids targeted by a service call that stops or pauses playback, or None."""
    if (domain, service) not in STOP_SERVICES:
        return None
    data = service_data if isinstance(service_data, dict) else {}
    return _as_list(data.get("entity_id"))


def origin_label(origin):
    """Display text for who started or stopped a session; None (no call) is outside HA."""
    if origin is None:
        return OUTSIDE_LABEL
    if origin == HOUSE:
        return HOUSE_LABEL
    if origin == SOMEONE:
        return HA_SOMEONE_LABEL
    return origin[1]


def iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


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


def where_label(labels, total=None):
    """Coordinator-first room labels -> 'Kitchen', 'Kitchen and Bedroom', 'Kitchen, Bedroom and
    Bathroom', or 'the whole house' once they are all `total` speakers."""
    if not labels or (total and len(labels) >= total):
        return "the whole house"
    return people_text(list(labels))


def session_text(who, kind, where):
    text = f"{who} started {WHAT.get(kind, 'music')} in {where}"
    return text.encode("ascii", "ignore").decode("ascii")


def icon_for(kind):
    return ICONS.get(kind, "mdi:music")


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


def account_view(state):
    """(state, device key) of a Spotify account state dict - what a change means."""
    state = state if isinstance(state, dict) else {}
    return state.get("state"), device_key((state.get("attributes") or {}).get("source"))


def track_ended(state, at_ts):
    """True when the account's playing state had at most TRACK_END_S of its track left at at_ts."""
    attrs = (state or {}).get("attributes") or {}
    position, duration = attrs.get("media_position"), attrs.get("media_duration")
    updated = parse_ts(attrs.get("media_position_updated_at"))
    if not isinstance(position, (int, float)) or not isinstance(duration, (int, float)) or updated is None:
        return False
    return position + (at_ts - updated) >= duration - TRACK_END_S


def stop_verdict(before, after, stop_ts, house_keys):
    """Did this account stop the speaker? `before` is its playing state on the speaker just before
    the stop, `after` a later poll. True, False, or None while it says nothing (unavailable)."""
    state, key = account_view(after)
    if state in UNKNOWN_STATES:
        return None
    if state == "playing":
        return key not in house_keys
    return not track_ended(before, stop_ts)


def people_text(names):
    """'Mikkel', 'Mikkel and Kristine', 'Mikkel, Kristine and Claudia'."""
    return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1]


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


class MusicSession:
    """One music session with a feed row: what plays (`kind`), who started it (`starter`: GUEST,
    "Someone" or None when the starters are in `names`), the residents named, the speakers its row
    names (`members`, fixed once written; `joined` holds speakers that joined before that) and the
    row's key. `accounts` marks a Spotify session whose row grows with residents' accounts."""

    def __init__(self, speaker, group, run_ts, kind="spotify"):
        self.speaker = speaker
        self.group = list(group)
        self.rooms = set(group) | {speaker}
        self.run_ts = run_ts
        self.kind = kind
        self.lead_speaker = self.group[0] if self.group else speaker
        self.key = f"music:{speaker}:{int(run_ts)}"
        self.starter = None
        self.names = []
        self.members = None
        self.joined = []
        self.accounts = False
        self.written = False


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

    def settled(self, speaker):
        """Playing in a run whose settle check has run, so its session is known."""
        return speaker in self._confirmed

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

    def add_call(self, ts, speakers, origin, kind="start"):
        if not speakers:
            return
        self._calls.append((ts, frozenset(speakers), origin, kind))
        horizon = ts - max(self.call_window_s, 60.0)
        self._calls = [call for call in self._calls if call[0] >= horizon]

    def origin_for(self, speakers, run_ts, kind="start"):
        """Origin of the newest `kind` call ('start' or 'stop') on any of `speakers` within the
        call window before run_ts, or None."""
        wanted = set(speakers)
        for ts, targets, origin, call_kind in reversed(self._calls):
            if call_kind == kind and run_ts - self.call_window_s <= ts <= run_ts + 1.0 and targets & wanted:
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
        self.stop_watch_s = float(self.args.get("spotify_stop_watch_seconds", 40))
        self.refresh_min_s = float(self.args.get("spotify_refresh_min_seconds", 10))
        self._sessions = []
        self._stop_watch = {}
        self._account_change = {}
        self._refreshed = {}
        self.ledger = PlaybackLedger(
            idle_s=float(self.args.get("session_idle_minutes", 20)) * 60.0,
            call_window_s=float(self.args.get("call_window_seconds", 30)),
        )
        self._person_by_user_id = {}
        self.sessions_sensor = self.args.get("sessions_sensor", "sensor.music_sessions")
        kept = self._attr(self.sessions_sensor, "rooms")
        self.sessions = {k: dict(v) for k, v in kept.items() if isinstance(v, dict)} if isinstance(kept, dict) else {}

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
        for entity in self.spotify_accounts.values():
            self.listen_state(self._on_account, entity, attribute="all")
        self.log(
            f"MusicAttribution: {len(self.raw_to_base)} speakers, "
            f"{len(self.spotify_accounts)}/{len(self.residents)} Spotify accounts linked"
        )
        self.run_every(self._republish, "now+10", 600)

    def _now(self):
        try:
            return self.get_now().timestamp()
        except Exception:
            return time.time()

    # -- inputs -------------------------------------------------------------

    def _on_speaker(self, entity, attribute, old, new, kwargs):
        ts = self._now()
        if new == "playing":
            self._stop_watch.pop(entity, None)
        if self.ledger.transition(entity, old, new, ts):
            self.run_in(self._settle, self.settle_s, speaker=entity, run_ts=ts)
        elif old == "playing" and new in STOPPED_STATES:
            group = self._group(entity)
            self._watch_stop(entity, group, ts)
            self.run_in(self._settle_stop, self.settle_s, speaker=entity, stop_ts=ts, group=group)

    def _on_call(self, event_name, data, kwargs):
        try:
            domain, service, service_data = data.get("domain"), data.get("service"), data.get("service_data")
            calls = (
                ("start", start_call_targets(domain, service, service_data)),
                ("stop", stop_call_targets(domain, service, service_data)),
            )
            context = (data.get("metadata") or {}).get("context") or {}
            for kind, targets in calls:
                speakers = self._speakers_for(targets or [])
                if not speakers:
                    continue
                origin = call_origin(
                    context.get("user_id"), context.get("parent_id"), self.house_user_ids, self._resident_for
                )
                self.ledger.add_call(self._now(), speakers, origin, kind)
        except Exception as e:
            self.log(f"call_service handling failed: {e}", level="WARNING")

    # -- session decisions --------------------------------------------------

    def _settle(self, kwargs):
        speaker = kwargs["speaker"]
        run_ts = kwargs["run_ts"]
        group = self._group(speaker)
        verdict = self.ledger.settle(speaker, run_ts, group)
        if verdict == "join":
            self._record_join(speaker, group[0])
            self._grow_rooms(speaker, group[0])
            return
        if verdict == "resume":
            self._record_resume(speaker)
            return
        if verdict != "new":
            return
        rooms = set(group) | {speaker}
        self._sessions = [
            s for s in self._sessions if not (s.rooms & rooms) and run_ts - s.run_ts < SESSION_TRACK_S
        ]
        kind = content_kind(self._content(speaker), self._attr(speaker, "source"))
        if kind == "tv":
            return
        origin = self.ledger.origin_for(set(group) | {speaker}, run_ts)
        if origin == HOUSE:
            self.log(f"Session on {speaker} started by the house - no row", level="DEBUG")
            self._record_start(group, HOUSE_LABEL, run_ts)
            return
        session = MusicSession(speaker, group, run_ts, kind)
        self._sessions.append(session)
        if origin is not None:
            if origin[1]:
                session.names = [origin[1]]
            else:
                session.starter = "Someone"
            self._report_session(session)
            return
        if kind == "spotify" and self.spotify_accounts:
            session.accounts = True
            self._refresh_accounts(self.spotify_accounts.values())
            self.run_in(self._spotify_poll, self.spotify_poll_s, session=session)
            return
        session.starter = "Someone"
        self._report_session(session)

    def _spotify_poll(self, kwargs):
        session = kwargs["session"]
        if session not in self._sessions:
            return
        speaker = session.speaker
        states = {entity: self._full_state(entity) for entity in self.spotify_accounts.values()}
        hits = spotify_matches(self.spotify_accounts, states, self._device_keys(session.group))
        if hits:
            session.names = list(hits)
            self._report_session(session)
            return
        if self._now() < session.run_ts + self.spotify_wait_s:
            self._refresh_accounts(self.spotify_accounts.values())
            self.run_in(self._spotify_poll, self.spotify_poll_s, session=session)
            return
        still_spotify = self.ledger.playing(speaker) and content_kind(self._content(speaker)) == "spotify"
        track = track_key(self._full_state(speaker).get("attributes"))
        guest = spotify_is_guest(self.residents, self.spotify_accounts, states, still_spotify, track)
        session.starter = GUEST if guest else "Someone"
        self._report_session(session)

    def _on_account(self, entity, attribute, old, new, kwargs):
        """A resident whose account turns up playing on a session's speaker after its row is written
        joined it: the row grows to name them. Every poll also feeds the Spotify stop watch."""
        state = new if isinstance(new, dict) else {}
        person = next((p for p, e in self.spotify_accounts.items() if e == entity), None)
        if person is None:
            return
        if account_view(old) != account_view(state):
            self._account_change[entity] = (self._now(), old if isinstance(old, dict) else {})
        self._stop_poll(person, state)
        if state.get("state") != "playing":
            return
        source = device_key((state.get("attributes") or {}).get("source"))
        for session in list(self._sessions):
            if not session.accounts or not session.written or person in session.names:
                continue
            if self._now() - session.run_ts >= SESSION_TRACK_S:
                self._sessions.remove(session)
                continue
            lead = session.lead_speaker
            if not self.ledger.settled(lead) or content_kind(self._content(lead)) != "spotify":
                continue
            if source in self._device_keys([lead]):
                session.names.append(person)
                self.log(f"{person} joined the Spotify session on {lead}")
                self._report_session(session)

    def _grow_rooms(self, speaker, coordinator):
        """`speaker` joined the group `coordinator` leads: the session led from there gains it."""
        now = self._now()
        for session in list(self._sessions):
            if session.lead_speaker != coordinator:
                continue
            if now - session.run_ts >= SESSION_TRACK_S:
                self._sessions.remove(session)
                continue
            session.rooms.add(speaker)
            if session.written:
                if speaker not in session.members:
                    session.members.append(speaker)
                    self.log(f"{speaker} joined the music session on {coordinator}")
                    self._report_session(session)
            elif speaker not in session.joined:
                session.joined.append(speaker)

    def _report_session(self, session):
        if session.members is None:
            current = self._group(session.speaker) if self.ledger.playing(session.speaker) else []
            members = current if len(current) >= len(session.group) else list(session.group)
            session.members = members + [s for s in session.joined if s not in members]
        session.written = True
        names = list(session.names)
        starter = session.starter
        if starter and names:
            who = people_text([starter] + names)
        elif starter:
            who = "A guest" if starter == GUEST else starter
        else:
            who = people_text(names)
        by = names[0] if len(names) == 1 and not starter else None
        self._report(who, session.kind, session.members, by, session.run_ts, session.key)

    def _settle_stop(self, kwargs):
        speaker = kwargs["speaker"]
        if self.ledger.playing(speaker):
            return
        if content_kind(self._content(speaker), self._attr(speaker, "source")) == "tv":
            return
        group = set(kwargs["group"]) | {speaker}
        origin = self.ledger.origin_for(group, kwargs["stop_ts"], kind="stop")
        rec = self.sessions.setdefault(self.raw_to_base[speaker], {})
        rec.update(stopped_at=iso(kwargs["stop_ts"]), stopped_by=origin_label(origin))
        self.log(f"Music stopped on {speaker}: {rec['stopped_by']}")
        self._publish()
        watch = self._stop_watch.get(speaker)
        if watch is None or watch["stop_ts"] != kwargs["stop_ts"]:
            return
        if origin is not None:
            del self._stop_watch[speaker]
            return
        watch["settled"] = True
        if not self._stop_done(speaker):
            self._refresh_accounts(self._undecided(watch))
            self.run_in(self._stop_refresh, 10, speaker=speaker, stop_ts=watch["stop_ts"])
            self.run_in(self._stop_deadline, max(0.0, watch["stop_ts"] + self.stop_watch_s - self._now()),
                        speaker=speaker, stop_ts=watch["stop_ts"])

    # -- who stopped Spotify ------------------------------------------------

    def _watch_stop(self, speaker, group, stop_ts):
        """Spotify stop on `speaker`: note the accounts that were playing on its group's device
        just before, and any that changed in the last STOP_LOOKBACK_S."""
        if not self.spotify_accounts or content_kind(self._content(speaker), self._attr(speaker, "source")) != "spotify":
            return
        keys = self._device_keys(group)
        candidates, decided = {}, {}
        for person, entity in self.spotify_accounts.items():
            current = self._full_state(entity)
            change = self._account_change.get(entity)
            early = change is not None and stop_ts - change[0] <= STOP_LOOKBACK_S
            before = change[1] if early else current
            view = account_view(before)
            if view[0] != "playing" or view[1] not in keys:
                continue
            candidates[person] = before
            if early:
                verdict = stop_verdict(before, current, stop_ts, self._house_keys())
                if verdict is not None:
                    decided[person] = verdict
        if candidates:
            self._stop_watch[speaker] = {"stop_ts": stop_ts, "candidates": candidates, "decided": decided,
                                         "settled": False}

    def _undecided(self, watch):
        return [self.spotify_accounts[p] for p in watch["candidates"] if p not in watch["decided"]]

    def _stop_poll(self, person, state):
        for speaker, watch in list(self._stop_watch.items()):
            if person not in watch["candidates"] or person in watch["decided"]:
                continue
            before = watch["candidates"][person]
            if self._now() < watch["stop_ts"] + STOP_DECIDE_FROM_S and account_view(state) == account_view(before):
                continue
            verdict = stop_verdict(before, state, watch["stop_ts"], self._house_keys())
            if verdict is not None:
                watch["decided"][person] = verdict
                self._stop_done(speaker)

    def _stop_refresh(self, kwargs):
        watch = self._stop_watch.get(kwargs["speaker"])
        if watch is not None and watch["stop_ts"] == kwargs["stop_ts"]:
            self._refresh_accounts(self._undecided(watch))

    def _stop_deadline(self, kwargs):
        watch = self._stop_watch.get(kwargs["speaker"])
        if watch is not None and watch["stop_ts"] == kwargs["stop_ts"]:
            self._stop_done(kwargs["speaker"], final=True)

    def _stop_done(self, speaker, final=False):
        """Writes the named stoppers once the stop is settled and every candidate is decided (or at
        the deadline). True when the watch is over."""
        watch = self._stop_watch.get(speaker)
        if watch is None:
            return True
        if not watch["settled"] or (not final and self._undecided(watch)):
            return False
        del self._stop_watch[speaker]
        names = [p for p in self.spotify_accounts if watch["decided"].get(p)]
        rec = self.sessions.get(self.raw_to_base[speaker]) or {}
        if names and rec.get("stopped_at") == iso(watch["stop_ts"]) and not self.ledger.playing(speaker):
            rec["stopped_by"] = people_text(names)
            self.log(f"Spotify stopped on {speaker} by {rec['stopped_by']}")
            self._publish()
        return True

    def _refresh_accounts(self, entities):
        """Ask HA to poll these Spotify accounts now, each at most once per refresh_min_s."""
        now = self._now()
        due = [e for e in entities if now - self._refreshed.get(e, float("-inf")) >= self.refresh_min_s]
        if not due:
            return
        for entity in due:
            self._refreshed[entity] = now
        self.log(f"Spotify refresh: {', '.join(sorted(due))}")
        try:
            self.call_service("homeassistant/update_entity", entity_id=due)
        except Exception as e:
            self.log(f"Spotify refresh failed: {e}", level="WARNING")

    # -- session records ----------------------------------------------------

    def _record_start(self, members, who, run_ts):
        """Writes the session's start on its speakers. A speaker already holding this start keeps
        its stop; one holding a newer session is left alone."""
        started_at = iso(run_ts)
        for raw in members:
            rec = self.sessions.get(self.raw_to_base[raw])
            if rec and rec.get("started_at") == started_at:
                rec["started_by"] = who
            elif not rec or (parse_ts(rec.get("started_at")) or 0) < run_ts:
                self.sessions[self.raw_to_base[raw]] = {"started_at": started_at, "started_by": who}
        self._publish()

    def _record_join(self, speaker, coordinator):
        lead = self.sessions.get(self.raw_to_base.get(coordinator))
        if lead and "started_at" in lead:
            self.sessions[self.raw_to_base[speaker]] = {k: lead[k] for k in ("started_at", "started_by")}
            self._publish()

    def _record_resume(self, speaker):
        rec = self.sessions.get(self.raw_to_base[speaker])
        if rec and rec.pop("stopped_at", None):
            rec.pop("stopped_by", None)
            self._publish()

    def _republish(self, kwargs):
        if self.sessions:
            self._publish()

    def _publish(self):
        try:
            self.set_state(
                self.sessions_sensor,
                state=iso(self._now()),
                attributes={"friendly_name": "Music sessions", "rooms": self.sessions},
                replace=True,
            )
        except Exception as e:
            self.log(f"publishing {self.sessions_sensor} failed: {e}", level="WARNING")

    # -- reporting ----------------------------------------------------------

    def _report(self, who, kind, members, by, run_ts, key):
        """Fires the public feed row naming `members` and records the start on them."""
        labels = [room_label(self.raw_to_base[m]) for m in members]
        text = session_text(who, kind, where_label(labels, len(self.raw_to_base)))
        payload = {"text": text, "icon": icon_for(kind), "key": key}
        if by:
            payload["by"] = by
        self.log(f"Music session: {text}")
        self._record_start(members, who, run_ts)
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

    def _house_keys(self):
        return self._device_keys(self.raw_to_base)

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
