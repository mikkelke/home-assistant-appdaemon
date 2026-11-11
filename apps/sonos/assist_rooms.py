"""Room-local music controls for Assist satellites.

satellites maps each Assist satellite entity to its HA area id; speakers maps areas
to Music Assistant players. A satellite entering listening mutes its room's speaker
only while it is playing and unmuted, and never while one of answer_sensors says a
satellite's answer is still playing on a speaker: Music Assistant lifts a mute for its
announcement and restores the mute it saw, so a mute made then would come back after
the release. Only mutes made here are released, either on
leaving listening or after duck_failsafe_s. Release checks the current mute state,
even if playback has stopped, and always forgets ownership. Responding needs no end
signal. Volume levels never change, and ducking never pauses a group. FollowMe may
change mute independently; this app never retries a mute or restores somebody else's.

Intent events carry area_id. assist_music_here joins a different playing master,
otherwise resumes a paused room speaker, otherwise starts radio. The master comes
from master_select's master_entity_id attribute. assist_music_leave requests unjoin
when grouped, otherwise pauses a playing speaker. assist_radio requests unjoin when
grouped and starts radio one second later; a solo speaker starts immediately.
Group requests use GroupManager's custom events. Unknown areas warn and do nothing;
satellites without a configured room speaker are ignored.

assist_music_group carries mode (join, move or ungroup), source_area and target_areas
(area ids as a list or a comma separated string; a multi-room phrase such as the family
room is several ids). Join takes the first playing room among the named sources and then
the targets as the music source, so "group A with B" works whichever of them plays, and
every other named room joins the group that source plays in, through its leader. Move
only accepts a named source: the targets join first, then the source rooms leave once all
targets show in their group, which keeps the music going on the targets. Ungroup unjoins
every speaker that follows a leader, so the music keeps playing on the leaders. Nothing
playing means nothing happens. Only rooms listed in speakers take part, which keeps
rooms that voice never groups out of it.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

MOVE_POLL_S = 1
MOVE_GIVE_UP_S = 15


def _attributes(state):
    attrs = (state or {}).get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _is_muted(state):
    value = _attributes(state).get("is_volume_muted")
    return value is True or str(value).lower() in ("true", "on", "yes", "1")


def duck_actions(speaker, state, listening, owned):
    """Decide a listening-edge mute or an owned release from a player snapshot."""
    if not speaker:
        return []
    if listening:
        if owned or (state or {}).get("state") != "playing" or _is_muted(state):
            return []
    elif not owned or not _is_muted(state):
        return []
    return [{"service": "media_player/volume_mute",
             "data": {"entity_id": speaker, "is_volume_muted": listening}}]


def intent_actions(event_name, speaker, state, master, master_state, radio):
    """Decide services and group requests without changing the supplied states."""
    if not speaker:
        return []
    playback = (state or {}).get("state")
    members = _attributes(state).get("group_members") or []
    grouped = len(members) > 1
    unjoin = {"event": "request_sonos_group_unjoin", "data": {"entity_id": speaker}}
    play_radio = {"service": "media_player/play_media",
                  "data": {"entity_id": speaker, "media_content_id": radio,
                           "media_content_type": "music"}}
    if event_name == "assist_music_here":
        if master and master != speaker and (master_state or {}).get("state") == "playing":
            return [{"event": "request_sonos_group_join",
                     "data": {"master_entity_id": master, "slave_entity_id": speaker}}]
        if playback == "paused":
            return [{"service": "media_player/media_play", "data": {"entity_id": speaker}}]
        return [play_radio]
    if event_name == "assist_music_leave":
        if grouped:
            return [unjoin]
        if playback == "playing":
            return [{"service": "media_player/media_pause", "data": {"entity_id": speaker}}]
    if event_name == "assist_radio":
        if grouped:
            return [unjoin, dict(play_radio, delay=1)]
        return [play_radio]
    return []


def _playing(state):
    return (state or {}).get("state") == "playing"


def _group_of(speaker, state):
    """Leader and members of the group a speaker is in; a solo speaker leads itself."""
    raw = _attributes(state).get("group_members")
    members = [m for m in raw if isinstance(m, str)] if isinstance(raw, list) else []
    return (members[0], members) if len(members) > 1 else (speaker, [speaker])


def _join(master, speaker):
    return {"event": "request_sonos_group_join",
            "data": {"master_entity_id": master, "slave_entity_id": speaker}}


def _unjoin(speaker):
    return {"event": "request_sonos_group_unjoin", "data": {"entity_id": speaker}}


def group_actions(mode, sources, targets, states):
    """Decide the group requests for one grouping voice request from player snapshots.

    Join: the first playing speaker among the named sources, then the targets, is the
    music source; every other named speaker not already in its group joins the group's
    leader. Move: only a named source can be the music source and only the targets join
    (the source leaves later, see move_leave_actions). Ungroup: every speaker that
    follows a leader leaves, so the leaders keep playing. Nothing playing gives nothing.
    """
    if mode == "ungroup":
        return [_unjoin(speaker) for speaker, state in states.items()
                if _group_of(speaker, state)[0] != speaker]
    if mode not in ("join", "move"):
        return []
    named = list(dict.fromkeys(sources if mode == "move" else sources + targets))
    source = next((s for s in named if _playing(states.get(s))), None)
    if source is None:
        return []
    master, members = _group_of(source, states[source])
    joiners = named if mode == "join" else [t for t in dict.fromkeys(targets) if t not in sources]
    return [_join(master, s) for s in joiners if s != source and s not in members]


def move_leave_actions(sources, targets, states):
    """Unjoin the moved-from rooms once every target shows in their group.

    Returns None while a target is still missing, so the caller can look again later;
    the leader goes last so the group keeps its music while leadership passes on.
    """
    source = next((s for s in dict.fromkeys(sources) if _playing(states.get(s))), None)
    moved = [t for t in dict.fromkeys(targets) if t not in sources]
    if source is None or not moved:
        return []
    leader, members = _group_of(source, states[source])
    if any(t not in members for t in moved):
        return None
    leavers = [s for s in dict.fromkeys(sources) if s in members]
    return [_unjoin(s) for s in sorted(leavers, key=lambda s: s == leader)]


class AssistRooms(hass.Hass):
    def initialize(self):
        self.satellites = self.args.get("satellites", {})
        self.speakers = self.args.get("speakers", {})
        self.master_select = self.args.get("master_select", "input_select.sonos_master_speaker")
        self.radio = self.args.get("radio", "dr_podcasts://radio/p3")
        self.duck_failsafe_s = float(self.args.get("duck_failsafe_s", 20))
        self.answer_sensors = self.args.get("answer_sensors", [])
        self._ducked = {}
        self._duck_generation = 0

        for satellite in self.satellites:
            self.listen_state(self._on_satellite_state, satellite)
        for event in ("assist_music_here", "assist_music_leave", "assist_radio"):
            self.listen_event(self._on_intent, event)
        self.listen_event(self._on_group_intent, "assist_music_group")

    def _read(self, entity):
        state = self.get_state(entity, attribute="all")
        return state if isinstance(state, dict) else {}

    def _on_satellite_state(self, entity, attribute, old, new, kwargs):
        area = self.satellites.get(entity)
        speaker = self.speakers.get(area)
        if not speaker or old == new:
            return
        if new == "listening" and not self._answer_playing():
            actions = duck_actions(speaker, self._read(speaker), True, area in self._ducked)
            if actions:
                self._run_actions(area, actions)
                self._duck_generation += 1
                self._ducked[area] = self._duck_generation
                self.run_in(self._duck_failsafe, self.duck_failsafe_s,
                            area=area, generation=self._duck_generation)
        elif old == "listening":
            self._release_duck(area)

    def _answer_playing(self):
        return any(self.get_state(sensor) == "on" for sensor in self.answer_sensors)

    def _release_duck(self, area):
        owned = self._ducked.pop(area, None) is not None
        if owned:
            speaker = self.speakers.get(area)
            self._run_actions(area, duck_actions(speaker, self._read(speaker), False, owned))

    def _duck_failsafe(self, kwargs):
        area = kwargs["area"]
        if self._ducked.get(area) == kwargs["generation"]:
            self._release_duck(area)

    def _on_intent(self, event_name, data, kwargs):
        area = data.get("area_id")
        speaker = self.speakers.get(area) if isinstance(area, str) else None
        if not speaker:
            self.log(f"{event_name}: no speaker configured for area {area!r}", level="WARNING")
            return
        master = self.get_state(self.master_select, attribute="master_entity_id")
        master_state = self._read(master) if isinstance(master, str) and master.startswith("media_player.") else {}
        self._run_actions(area, intent_actions(event_name, speaker, self._read(speaker),
                                              master, master_state, self.radio))

    def _group_speakers(self, areas):
        if isinstance(areas, str):
            areas = areas.split(",")
        speakers = []
        for area in areas if isinstance(areas, list) else []:
            area = area.strip() if isinstance(area, str) else area
            if not area:
                continue
            speaker = self.speakers.get(area)
            if speaker:
                speakers.append(speaker)
            else:
                self.log(f"assist_music_group: no speaker configured for area {area!r}",
                         level="WARNING")
        return list(dict.fromkeys(speakers))

    def _on_group_intent(self, event_name, data, kwargs):
        mode = data.get("mode")
        if mode not in ("join", "move", "ungroup"):
            self.log(f"{event_name}: unknown mode {mode!r}", level="WARNING")
            return
        sources = self._group_speakers(data.get("source_area"))
        targets = self._group_speakers(data.get("target_areas"))
        involved = list(self.speakers.values()) if mode == "ungroup" else sources + targets
        states = {entity: self._read(entity) for entity in dict.fromkeys(involved)}
        actions = group_actions(mode, sources, targets, states)
        if not actions and mode != "move":
            self.log(f"{event_name} {mode}: nothing to do", level="INFO")
        self._run_actions("group", actions)
        if mode == "move":
            self._finish_move({"sources": sources, "targets": targets, "waited": 0})

    def _finish_move(self, kwargs):
        sources, targets, waited = kwargs["sources"], kwargs["targets"], kwargs["waited"]
        actions = move_leave_actions(sources, targets,
                                     {entity: self._read(entity) for entity in sources})
        if actions is not None:
            if not actions:
                self.log("assist_music_group move: nothing to do", level="INFO")
            self._run_actions("group", actions)
        elif waited < MOVE_GIVE_UP_S:
            self.run_in(self._finish_move, MOVE_POLL_S, sources=sources, targets=targets,
                        waited=waited + MOVE_POLL_S)
        else:
            self.log("assist_music_group move: targets never joined, source keeps playing",
                     level="WARNING")

    def _run_actions(self, area, actions):
        for action in actions:
            if action.get("delay"):
                delayed = {key: value for key, value in action.items() if key != "delay"}
                self.run_in(self._run_delayed_action, action["delay"], area=area, action=delayed)
                continue
            data = action["data"]
            if "event" in action:
                self.fire_event(action["event"], **data)
                if action["event"] == "request_sonos_group_join":
                    message = f"Joining {data['slave_entity_id']} to {data['master_entity_id']}"
                else:
                    message = f"Leaving group for {data['entity_id']}"
            else:
                service = action["service"]
                self.call_service(service, **data)
                speaker = data["entity_id"]
                if service == "media_player/volume_mute":
                    verb = "Muted" if data["is_volume_muted"] else "Unmuted"
                    message = f"{verb} {speaker}"
                elif service == "media_player/play_media":
                    message = f"Started radio on {speaker}"
                elif service == "media_player/media_play":
                    message = f"Resumed {speaker}"
                else:
                    message = f"Paused {speaker}"
            self.log(f"{message} for assist ({area})", level="INFO")

    def _run_delayed_action(self, kwargs):
        self._run_actions(kwargs["area"], [kwargs["action"]])
