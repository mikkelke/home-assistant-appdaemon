"""Room-local music controls for Assist satellites.

speakers maps HA areas to Music Assistant players. The room speaker is never muted
while a satellite listens: the person talking is in that room.

Intent events carry area_id. assist_music_here joins a different playing master,
otherwise resumes a paused room speaker, otherwise starts radio. The master comes
from master_select's master_entity_id attribute. assist_music_leave requests unjoin
when grouped, otherwise pauses a playing speaker. assist_radio requests unjoin when
grouped and starts radio one second later; a solo speaker starts immediately.
Group requests use GroupManager's custom events. Areas without a speaker warn and do
nothing.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


def _attributes(state):
    attrs = (state or {}).get("attributes")
    return attrs if isinstance(attrs, dict) else {}


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


class AssistRooms(hass.Hass):
    def initialize(self):
        self.speakers = self.args.get("speakers", {})
        self.master_select = self.args.get("master_select", "input_select.sonos_master_speaker")
        self.radio = self.args.get("radio", "dr_podcasts://radio/p3")

        for event in ("assist_music_here", "assist_music_leave", "assist_radio"):
            self.listen_event(self._on_intent, event)

    def _read(self, entity):
        state = self.get_state(entity, attribute="all")
        return state if isinstance(state, dict) else {}

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
