"""AssistMyMusic - the voice answers for "play my music" and "what is playing".

Both intents (intent_script.yaml on the box) fire an event with the satellite's area id (or the room
asked about) and wait a few seconds for `assist_my_music_result` carrying {request, speech}; the
intent speaks that sentence. The app answers from the state it reads at the moment of the event.

WHO IS SPEAKING. The speaker in a satellite's area is, in order:
    1. the person an identity sensor names for that area (`identity`: the bathroom display reads
       mikkel once his phone was near or rose during the visit),
    2. the only resident whose Bluetooth area (`ble_areas`, Bermuda's area name) is that area,
    3. the area's owner (`owners`), the person whose room it is.
The house never asks who it is; with none of the three the answer says it cannot tell.

PLAY MY MUSIC. From the speaker's Spotify account entity (`accounts`, HA's Spotify integration,
polled about every 30 s):
    - playing on a house Sonos (its Spotify Connect source is a speaker's name, "Kitchen + 2" for a
      group): the room joins that speaker, already there means nothing to do;
    - playing or paused elsewhere (a phone): the room's speaker plays the account's current track
      through Music Assistant with radio mode, so similar songs follow, and a playing phone is
      paused, which moves the music;
    - idle: the account's last track, remembered by this app from every playing or paused state
      (persisted in state_file, seeded from the recorder for `recent_days` at startup), the same way;
    - nothing within recent_days: it says so.
Spotify's API gives no way to control a Sonos and HA exposes no context URI (playlist or album), so
a track plus radio mode is the closest playable form of "their music". Music Assistant's single
Spotify provider plays any public track whoever's account it came from. A room speaker that follows
another speaker leaves its group first.

WHAT IS PLAYING. For the room (the satellite's area unless one is named): the playing speaker's
title and artist, else the first playing room elsewhere. Who: residents whose account plays on that
speaker's group ("Kristine's Jam with Mikkel" for several, the host being the one sensor.music_sessions
names first), else the session's started_by when it names a person.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

RESULT_EVENT = "assist_my_music_result"
NOT_PEOPLE = ("someone", "outside home assistant")


def device_key(name):
    """'Kitchen + 3' -> 'kitchen', "Claudia's Room" -> 'claudiasroom'."""
    if not isinstance(name, str):
        return ""
    return "".join(ch for ch in name.split("+", 1)[0].lower() if ch.isalnum())


def area_key(name):
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def people_text(names):
    return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1]


def song_text(title, artist):
    return f"{title} by {artist}" if artist else str(title)


def resolve_speaker(area, identity, ble_states, owner):
    """The person speaking in `area`, or None.

    identity: the person an identity sensor names there, or None. ble_states: {person: Bermuda area
    name}. owner: the area's owner, or None."""
    if identity:
        return identity
    here = [person for person, name in ble_states.items() if area_key(name) == area_key(area)]
    if len(here) == 1:
        return here[0]
    return owner


def item_from_state(state):
    """{uri, title, artist, source, state} from a Spotify account state dict, or None."""
    state = state if isinstance(state, dict) else {}
    attrs = state.get("attributes") or {}
    uri = attrs.get("media_content_id")
    if state.get("state") not in ("playing", "paused") or not isinstance(uri, str) or not uri.startswith("spotify:"):
        return None
    return {"uri": uri, "title": attrs.get("media_title") or "", "artist": attrs.get("media_artist") or "",
            "source": attrs.get("source") or "", "state": state.get("state")}


def my_music_plan(person, speaker, speaker_state, account, account_state, last_item, sonos_keys, now, recent_days):
    """(speech, actions) for "play my music".

    account: the person's Spotify account entity. sonos_keys: {device_key: MA speaker entity} for
    every house Sonos. last_item: the remembered {uri, title, artist, at (ISO)} or None. Actions are
    AssistRooms-style dicts."""
    if not person:
        return "I can't tell whose music to play.", []
    if not speaker:
        return f"{person}, there's no speaker I can use here.", []
    current = item_from_state(account_state)
    if current and current["state"] == "playing":
        sonos = sonos_keys.get(device_key(current["source"]))
        if sonos:
            if sonos == speaker or sonos in _members(speaker_state):
                return f"{person}, your Spotify is already playing here.", []
            return (f"{person}, your Spotify joins from the {area_label(sonos)}.",
                    [{"event": "request_sonos_group_join",
                      "data": {"master_entity_id": sonos, "slave_entity_id": speaker}}])
    item = current
    if not item and last_item:
        at = _parse(last_item.get("at"))
        if at and now - at <= timedelta(days=recent_days):
            item = last_item
    if not item:
        return f"{person}, you haven't played anything on Spotify lately.", []
    actions = []
    if len(_members(speaker_state)) > 1:
        actions.append({"event": "request_sonos_group_unjoin", "data": {"entity_id": speaker}})
    play = {"service": "music_assistant/play_media",
            "data": {"entity_id": speaker, "media_id": item["uri"], "radio_mode": True}}
    actions.append(dict(play, delay=1) if actions else play)
    song = song_text(item["title"], item["artist"])
    if item is current and current["state"] == "playing":
        actions.append({"service": "media_player/media_pause", "data": {"entity_id": account}})
        return f"{person}, your Spotify moves here: {song}.", actions
    return f"{person}, your last song, {song}, then more like it.", actions


def area_label(entity):
    return entity.split(".", 1)[-1].replace("_", " ")


def _members(state):
    raw = ((state or {}).get("attributes") or {}).get("group_members")
    return [m for m in raw if isinstance(m, str)] if isinstance(raw, list) else []


def _parse(value):
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def spotify_players(speaker, speaker_state, names, accounts, account_states):
    """Residents whose Spotify account plays on the speaker's group, in account order."""
    keys = {device_key(names.get(m, area_label(m))) for m in (_members(speaker_state) or [speaker])}
    return [person for person, entity in accounts.items()
            if (account_states.get(entity) or {}).get("state") == "playing"
            and device_key(((account_states.get(entity) or {}).get("attributes") or {}).get("source")) in keys]


def who_plays(players, session):
    """"Kristine's Jam with Mikkel", "Claudia's Spotify", 'put on by Mikkel' or ''.

    players: spotify_players(). session: the speaker's row in sensor.music_sessions, or {}."""
    session = session or {}
    started = str(session.get("started_by") or "")
    began, ended = _parse(session.get("started_at")), _parse(session.get("stopped_at"))
    current = bool(started) and began is not None and (ended is None or ended < began)
    if players:
        order = sorted(players, key=lambda p: (started.find(p) if current and p in started else len(started) + 1))
        if len(order) > 1:
            return f"{order[0]}'s Jam with {people_text(order[1:])}"
        return f"{order[0]}'s Spotify"
    if current and not started.lower().startswith(NOT_PEOPLE):
        return "put on by " + (started[0].lower() + started[1:] if started.startswith("A ") else started)
    return ""


def what_playing_text(here, speaker, states, names, accounts, account_states, sessions):
    """The answer to "what is playing" in `speaker`'s room; here: the room is the satellite's own.

    states: {MA speaker entity: state dict} for every house speaker, in a stable order."""
    def describe(entity):
        state = states.get(entity) or {}
        attrs = state.get("attributes") or {}
        players = spotify_players(entity, state, names, accounts, account_states)
        title, artist = attrs.get("media_title"), attrs.get("media_artist")
        if not title and players:
            spotify = account_states[accounts[players[0]]].get("attributes") or {}
            title, artist = spotify.get("media_title"), spotify.get("media_artist")
        if not title:
            return None
        song = song_text(title, artist)
        who = who_plays(players, sessions.get(entity))
        if who.startswith("put on by"):
            return f"{song}, {who}"
        return f"{who}: {song}" if who else song

    room = names.get(speaker, area_label(speaker)).lower() if speaker else ""
    where = "here" if here else f"in the {room}"
    if speaker and (states.get(speaker) or {}).get("state") == "playing":
        text = describe(speaker)
        if text:
            return f"{text}." if here else f"In the {room}: {text}."
    for entity, state in states.items():
        members = _members(state)
        if entity == speaker or state.get("state") != "playing" or (members and members[0] != entity):
            continue
        text = describe(entity)
        if text:
            other = names.get(entity, area_label(entity)).lower()
            return f"Nothing {where}. The {other} plays {text}." if speaker else f"The {other} plays {text}."
    return f"Nothing is playing {where}." if speaker else "Nothing is playing."


class AssistMyMusic(hass.Hass):

    def initialize(self):
        self.satellites = self.args.get("satellites", {})
        self.speakers = self.args.get("speakers", {})
        self.sonos = list(self.args.get("sonos", []))
        self.accounts = dict(self.args.get("accounts", {}))
        self.ble_areas = dict(self.args.get("ble_areas", {}))
        self.identity = dict(self.args.get("identity", {}))
        self.owners = dict(self.args.get("owners", {}))
        self.sessions_sensor = self.args.get("sessions_sensor", "sensor.music_sessions")
        self.recent_days = float(self.args.get("recent_days", 7))
        self.state_file = self.args.get("state_file") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "assist_my_music_state.json")
        self.last = self._load()
        for person, entity in self.accounts.items():
            if person not in self.last:
                self._seed(person, entity)
            self.listen_state(self._on_account, entity, attribute="all", person=person)
        self.listen_event(self._on_my_music, "assist_my_music")
        self.listen_event(self._on_what_playing, "assist_what_playing")
        self.log(f"Remembered last Spotify songs for {', '.join(sorted(self.last)) or 'nobody'}", level="INFO")

    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.last, fh, ensure_ascii=False)
        os.replace(tmp, self.state_file)

    def _seed(self, person, entity):
        start = datetime.now(timezone.utc) - timedelta(days=self.recent_days)
        try:
            raw = self.get_history(entity_id=entity, start_time=start)
        except Exception as err:  # noqa: BLE001 - the recorder is optional for this
            self.log(f"History of {entity} unavailable: {err}", level="WARNING")
            return
        rows = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], list) else raw or []
        for row in reversed(rows if isinstance(rows, list) else []):
            item = item_from_state(row)
            if item:
                item["at"] = row.get("last_updated") or row.get("last_changed")
                self.last[person] = item
                self._save()
                return

    def _on_account(self, entity, attribute, old, new, kwargs):
        item = item_from_state(new)
        if not item:
            return
        person = kwargs["person"]
        item["at"] = datetime.now(timezone.utc).isoformat()
        known = self.last.get(person) or {}
        self.last[person] = item
        if known.get("uri") != item["uri"] or known.get("state") != item["state"]:
            self._save()

    def _read(self, entity):
        state = self.get_state(entity, attribute="all") if entity else None
        return state if isinstance(state, dict) else {}

    def _satellite_area(self, data):
        area = data.get("area_id")
        if area:
            return area
        areas = list(dict.fromkeys(self.satellites.values()))
        return areas[0] if len(areas) == 1 else None

    def _speaker_in(self, area):
        spec = self.identity.get(area) or {}
        if spec and self.get_state(spec.get("entity")) == spec.get("state"):
            identity = spec.get("person")
        else:
            identity = None
        ble = {person: self.get_state(entity) for person, entity in self.ble_areas.items()}
        return resolve_speaker(area, identity, ble, self.owners.get(area))

    def _reply(self, data, speech):
        self.fire_event(RESULT_EVENT, request=data.get("request", ""), speech=speech)

    def _on_my_music(self, event_name, data, kwargs):
        area = self._satellite_area(data)
        person = self._speaker_in(area) if area else None
        speaker = self.speakers.get(area)
        entity = self.accounts.get(person)
        account = self._read(entity)
        sonos_keys = {device_key(self._read(s).get("attributes", {}).get("friendly_name") or area_label(s)): s
                      for s in self.sonos}
        speech, actions = my_music_plan(person, speaker, self._read(speaker), entity, account,
                                        self.last.get(person), sonos_keys, datetime.now(timezone.utc),
                                        self.recent_days)
        self._reply(data, speech)
        self.log(f"My music ({area}, {person or 'nobody'}): {speech}", level="INFO")
        for action in actions:
            if action.get("delay"):
                self.run_in(self._run_delayed, action["delay"],
                            action={k: v for k, v in action.items() if k != "delay"})
            else:
                self._run(action)

    def _run_delayed(self, kwargs):
        self._run(kwargs["action"])

    def _run(self, action):
        if "event" in action:
            self.fire_event(action["event"], **action["data"])
        else:
            self.call_service(action["service"], **action["data"])

    def _on_what_playing(self, event_name, data, kwargs):
        asked = [a for a in str(data.get("rooms") or "").split(",") if a]
        satellite_area = self._satellite_area({})
        area = asked[0] if asked else (data.get("area_id") or satellite_area)
        here = not asked and area == satellite_area
        speaker = self.speakers.get(area)
        states = {s: self._read(s) for s in self.sonos}
        names = {s: (st.get("attributes") or {}).get("friendly_name") or area_label(s) for s, st in states.items()}
        account_states = {e: self._read(e) for e in self.accounts.values()}
        sessions = (self._read(self.sessions_sensor).get("attributes") or {}).get("rooms") or {}
        speech = what_playing_text(here, speaker, states, names, self.accounts, account_states, sessions)
        self._reply(data, speech)
        self.log(f"What is playing ({area}): {speech}", level="INFO")
