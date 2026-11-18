"""Room-local music controls for Assist satellites.

satellites maps each Assist satellite entity to its HA area id; speakers maps areas
to Music Assistant players; duck_hosts maps areas to the Sonos local API host of the
room speaker. A satellite is busy while it is listening or processing; answer_sensors
(on while a satellite's answer plays on a speaker) name no room, so one that is on
makes every room in duck_hosts busy. Each state change while a room is busy ducks its
speaker through Sonos's own duck if Music Assistant says it is playing, which also
catches music that starts mid-conversation, and renews a duck already held; Sonos lifts
a duck itself after DUCK_MS. A held duck is lifted once its room has been quiet for
QUIET_S, which bridges the gap between an answer and a follow-up turn. Nothing is muted,
no volume is written and ducking never pauses a group. Only ducks made here are lifted;
a request that fails is logged and owns nothing.

Intent events carry area_id. assist_music_here joins a different playing master,
otherwise resumes a paused room speaker, otherwise starts radio. The master comes
from master_select's master_entity_id attribute. assist_music_leave requests unjoin
when grouped, otherwise pauses a playing speaker. assist_radio requests unjoin when
grouped and starts radio one second later; a solo speaker starts immediately. Its area_id
may name several rooms (a list or a comma separated string): the first room starts the
radio that way and the others join it RADIO_JOIN_S later, so they play as one group.
Group requests use GroupManager's custom events. Unknown areas warn and do nothing;
satellites without a configured room speaker are ignored.

assist_music_group carries mode (join, move, only or ungroup), source_area and target_areas
(area ids as a list or a comma separated string; a multi-room phrase such as the family
room is several ids). Join takes the first playing room among the named sources and then
the targets as the music source, so "group A with B" works whichever of them plays, and
every other named room joins the group that source plays in, through its leader. Move
only accepts a named source: the targets join first, then the source rooms leave once all
targets show in their group, which keeps the music going on the targets. Only leaves the
music in exactly the target rooms: the group of the first playing target, else of the first
playing room, gains the missing targets and then loses its other rooms the way a move does;
other groups keep playing. Ungroup unjoins every speaker that follows a leader, so the
music keeps playing on the leaders. Nothing playing means nothing happens. Only rooms listed in speakers take part, which keeps
rooms that voice never groups out of it.
"""

import json
import ssl
import urllib.request

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

MOVE_POLL_S = 1
MOVE_GIVE_UP_S = 15
# The public key aiosonos and Home Assistant send to the speaker's local API.
SONOS_LOCAL_KEY = "123e4567-e89b-12d3-a456-426655440000"
QUIET_S = 2
RADIO_JOIN_S = 2
# Sonos lifts a duck itself after this; every busy edge renews it.
DUCK_MS = 60000


def _attributes(state):
    attrs = (state or {}).get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def sonos_duck(host, duck):
    """Duck a speaker through its local API, or lift the duck.

    Blocking, with a 2 s socket timeout. The speaker's certificate is self-signed, so it
    is not verified.
    """
    body = {"durationMillis": DUCK_MS} if duck else {}
    request = urllib.request.Request(
        f"https://{host}:1443/api/v1/players/local/playerVolume/{'duck' if duck else 'unduck'}",
        data=json.dumps(body).encode(), method="POST",
        headers={"X-Sonos-Api-Key": SONOS_LOCAL_KEY, "Content-Type": "application/json"})
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, context=context, timeout=2):
        pass


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


def radio_actions(speakers, states, radio):
    """Radio in the first speaker, the others joining it once it plays."""
    leader = speakers[0]
    actions = intent_actions("assist_radio", leader, states.get(leader), None, {}, radio)
    start = max((action.get("delay", 0) for action in actions), default=0) + RADIO_JOIN_S
    return actions + [dict(_join(leader, speaker), delay=start) for speaker in speakers[1:]]


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


def only_plan(targets, states):
    """Turn "music only in the targets" into a join or a move.

    Returns (mode, sources): the rooms of the chosen group outside the targets move away,
    or, when there are none, the playing room is the source the targets join. Nothing
    playing gives (None, []).
    """
    playing = [s for s in dict.fromkeys(targets) if _playing(states.get(s))]
    playing += [s for s in states if _playing(states.get(s))]
    if not playing:
        return None, []
    _, members = _group_of(playing[0], states[playing[0]])
    outside = [m for m in members if m in states and m not in targets]
    return ("move", outside) if outside else ("join", [playing[0]])


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
        self.duck_hosts = self.args.get("duck_hosts", {})
        self.master_select = self.args.get("master_select", "input_select.sonos_master_speaker")
        self.radio = self.args.get("radio", "dr_podcasts://radio/p3")
        self.answer_sensors = self.args.get("answer_sensors", [])
        self._ducked = set()
        self._gen = {}

        watched = [*self.satellites, *self.answer_sensors]
        for entity in watched:
            self.listen_state(self._on_state, entity)
        self._seen = {entity: self.get_state(entity) for entity in watched}
        for event in ("assist_music_here", "assist_music_leave", "assist_radio"):
            self.listen_event(self._on_intent, event)
        self.listen_event(self._on_group_intent, "assist_music_group")

    def _read(self, entity):
        state = self.get_state(entity, attribute="all")
        return state if isinstance(state, dict) else {}

    def _on_state(self, entity, attribute, old, new, kwargs):
        if old == new:
            return
        self._seen[entity] = new
        areas = [self.satellites[entity]] if entity in self.satellites else list(self.duck_hosts)
        for area in areas:
            speaker = self.speakers.get(area)
            if not speaker:
                continue
            self._gen[area] = self._gen.get(area, 0) + 1
            if self._busy(area):
                if self.get_state(speaker) == "playing" and self._send(area, True):
                    if area not in self._ducked:
                        self._ducked.add(area)
                        self.log(f"Ducked {speaker} for assist ({area})", level="INFO")
            elif area in self._ducked:
                self.run_in(self._on_quiet, QUIET_S, area=area, generation=self._gen[area])

    def _busy(self, area):
        satellites = [entity for entity, room in self.satellites.items() if room == area]
        return (any(self._seen.get(entity) in ("listening", "processing") for entity in satellites)
                or any(self._seen.get(sensor) == "on" for sensor in self.answer_sensors))

    def _on_quiet(self, kwargs):
        area = kwargs["area"]
        if kwargs["generation"] != self._gen.get(area) or area not in self._ducked:
            return
        self._ducked.discard(area)
        if self._send(area, False):
            self.log(f"Unducked {self.speakers[area]} ({area})", level="INFO")

    def _send(self, area, duck):
        host = self.duck_hosts.get(area)
        if not host:
            return False
        try:
            sonos_duck(host, duck)
        except Exception as error:
            self.log(f"Sonos {'duck' if duck else 'unduck'} of {self.speakers[area]} failed "
                     f"({area}): {type(error).__name__}: {error}", level="WARNING")
            return False
        return True

    def _on_intent(self, event_name, data, kwargs):
        area = data.get("area_id")
        if event_name == "assist_radio":
            speakers = self._group_speakers(area, event_name)
            if len(speakers) > 1:
                self._run_actions("radio", radio_actions(
                    speakers, {entity: self._read(entity) for entity in speakers}, self.radio))
                return
            area = next((room for room, entity in self.speakers.items() if [entity] == speakers), None)
        speaker = self.speakers.get(area) if isinstance(area, str) else None
        if not speaker:
            self.log(f"{event_name}: no speaker configured for area {area!r}", level="WARNING")
            return
        master = self.get_state(self.master_select, attribute="master_entity_id")
        master_state = self._read(master) if isinstance(master, str) and master.startswith("media_player.") else {}
        self._run_actions(area, intent_actions(event_name, speaker, self._read(speaker),
                                              master, master_state, self.radio))

    def _group_speakers(self, areas, event_name="assist_music_group"):
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
                self.log(f"{event_name}: no speaker configured for area {area!r}",
                         level="WARNING")
        return list(dict.fromkeys(speakers))

    def _on_group_intent(self, event_name, data, kwargs):
        mode = data.get("mode")
        if mode not in ("join", "move", "only", "ungroup"):
            self.log(f"{event_name}: unknown mode {mode!r}", level="WARNING")
            return
        sources = self._group_speakers(data.get("source_area"))
        targets = self._group_speakers(data.get("target_areas"))
        involved = list(self.speakers.values()) if mode in ("only", "ungroup") else sources + targets
        states = {entity: self._read(entity) for entity in dict.fromkeys(involved)}
        if mode == "only":
            mode, sources = only_plan(targets, states)
            if mode is None:
                self.log(f"{event_name} only: nothing is playing", level="INFO")
                return
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
                if service == "media_player/play_media":
                    message = f"Started radio on {speaker}"
                elif service == "media_player/media_play":
                    message = f"Resumed {speaker}"
                else:
                    message = f"Paused {speaker}"
            self.log(f"{message} for assist ({area})", level="INFO")

    def _run_delayed_action(self, kwargs):
        self._run_actions(kwargs["area"], [kwargs["action"]])
