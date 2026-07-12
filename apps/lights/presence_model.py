"""
Presence model (shadow) - publishes ``binary_sensor.presence_<room>``.

The house already has per-room composite presence helpers
(``binary_sensor.<room>_pir_presence`` - HA group/template helpers) and every
consumer (follow_me, lights apps, darkness, dashboard) points at them. What
they lack is the middle-layer contract: WHY is presence on, WHICH sensor
fired, WHEN was it computed, and trust rules (a playing kitchen speaker can
fake FP300 micro-motion).

This app publishes an enriched twin per room in SHADOW mode:
  binary_sensor.presence_<room>   state mirrors the composite
    reason           - which member sensor(s) are currently on (groups are
                       introspected live via their entity_id attribute)
    music_suspect    - kitchen only: mmWave-only presence while the kitchen
                       speaker is playing (the 2026-07-07 ghost signature)
    source_entities / computed_at - middle-layer convention

Nothing consumes these yet - the point is a week of side-by-side data before
consumers are repointed room by room (see plans/middle-layer-2026-07-12.md).
"""

from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass


class PresenceModel(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.rooms = list(a("rooms", []))
        self.composite_pattern = a("composite_pattern", "binary_sensor.{room}_pir_presence")
        self.publish_pattern = a("publish_pattern", "binary_sensor.presence_{room}")
        self.kitchen_media = a("kitchen_media_entity", "media_player.kitchen_2")
        self.mmwave_marker = a("mmwave_marker", "presence_presence")

        self._members = {}  # room -> [member entity ids] (groups only)
        for room in self.rooms:
            composite = self.composite_pattern.format(room=room)
            self.listen_state(self._on_change, composite, room=room)
            members = self.get_state(composite, attribute="entity_id")
            if isinstance(members, (list, tuple)):
                self._members[room] = list(members)
                for m in members:
                    self.listen_state(self._on_change, m, room=room)
            else:
                self._members[room] = []
        if self.kitchen_media and "kitchen" in self.rooms:
            self.listen_state(self._on_change, self.kitchen_media, room="kitchen")

        self.run_every(self._tick, "now+6", 300)

    def _on_change(self, entity, attribute, old, new, kwargs):
        self._eval(kwargs.get("room"))

    def _tick(self, kwargs):
        for room in self.rooms:
            self._eval(room)

    def _eval(self, room):
        if not room:
            return
        try:
            composite = self.composite_pattern.format(room=room)
            state = self.get_state(composite)
            members = self._members.get(room) or []
            active = [m for m in members if self.get_state(m) == "on"]

            if state == "on":
                reason = (f"active: {', '.join(active)}" if active
                          else "composite on (members not introspectable)")
            elif state in (None, "unknown", "unavailable"):
                reason = f"composite {state}"
            else:
                reason = "no presence"

            music_suspect = False
            if room == "kitchen" and state == "on" and active:
                playing = self.get_state(self.kitchen_media) == "playing"
                only_mmwave = all(self.mmwave_marker in m for m in active)
                music_suspect = bool(playing and only_mmwave)
                if music_suspect:
                    reason += " - mmWave only while kitchen speaker plays (ghost suspect)"

            self.set_state(self.publish_pattern.format(room=room),
                           state=state if state in ("on", "off") else "off",
                           attributes={
                               "friendly_name": f"Presence {room.replace('_', ' ')}",
                               "device_class": "occupancy",
                               "reason": reason,
                               "music_suspect": music_suspect,
                               "shadow_of": composite,
                               "source_entities": [composite] + members,
                               "computed_at": datetime.now().isoformat(timespec="seconds"),
                           })
        except Exception as e:
            self.log(f"presence eval failed for {room}: {e}", level="ERROR")
