"""ResidentRoomLightsOff - a resident's room lights go off once the room has stood empty for
the vacancy hold while its owner is away.

Per room, two conditions must hold together, continuously, for vacancy_hold_min:
  - the owner is away: any person state other than home/unknown/unavailable;
  - the room reads empty: binary_sensor.<zone>_active is "off" with every witness readable
    (stale "false").
The hold restarts whenever either condition starts holding, so it runs from the later of the
departure and the room emptying, and any break cancels it. One elapsed hold per away-and-empty
stretch: a light switched on later in the same stretch is left alone. Nothing happens while the
owner is home or while the room reads active or unreadable.

The hold follows the callbacks' own values, never a later store read, because AppDaemon 4.5.13
can drop a store update while still firing the callback. When the hold elapses a fresh read must
agree as well (owner away, room_active_read says empty, witnesses readable), otherwise the hold
re-arms. initialize, which also re-runs after every HA restart, only ever starts a hold.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import room_active_read  # apps/presence - AppDaemon puts every app dir on sys.path

NOT_AWAY = (None, "home", "unknown", "unavailable")


def is_away(state):
    return state not in NOT_AWAY


def reads_empty(full_state):
    """True when a full room-active state object reads empty with every witness readable."""
    if not isinstance(full_state, dict):
        return False
    attributes = full_state.get("attributes") or {}
    return full_state.get("state") == "off" and attributes.get("stale") == "false"


class ResidentRoomLightsOff(hass.Hass):
    def initialize(self):
        self.hold_s = int(float(self.args.get("vacancy_hold_min", 60)) * 60)
        self.rooms = {}
        for zone, cfg in (self.args.get("rooms") or {}).items():
            room = {
                "zone": zone,
                "active_entity": f"binary_sensor.{zone}_active",
                "owner": cfg["owner"],
                "name": cfg["name"],
                "room_label": cfg.get("room_label") or "their room",
                "lights": list(cfg["lights"]),
                "away": False,
                "empty": False,
                "timer": None,
                "done": False,
            }
            self.rooms[zone] = room
            room["away"] = is_away(self.get_state(room["owner"]))
            room["empty"] = reads_empty(self.get_state(room["active_entity"], attribute="all"))
            self._evaluate(room, restart=True)
            self.listen_state(self._on_owner, room["owner"], zone=zone)
            self.listen_state(self._on_room, room["active_entity"], attribute="all", zone=zone)
        self.log(
            f"ResidentRoomLightsOff: {sorted(self.rooms)}, vacancy hold {self.hold_s // 60} min",
            level="INFO",
        )

    # ---------- callbacks ----------

    def _on_owner(self, entity, attribute, old, new, kwargs):
        try:
            room = self.rooms[kwargs["zone"]]
            room["away"] = is_away(new)
            self._evaluate(room, restart=room["away"] and not is_away(old))
        except Exception as e:
            self.log(f"owner change handling failed for {entity}: {e}", level="ERROR")

    def _on_room(self, entity, attribute, old, new, kwargs):
        try:
            room = self.rooms[kwargs["zone"]]
            room["empty"] = reads_empty(new)
            self._evaluate(room, restart=room["empty"] and not reads_empty(old))
        except Exception as e:
            self.log(f"room change handling failed for {entity}: {e}", level="ERROR")

    def _on_hold_elapsed(self, kwargs):
        try:
            room = self.rooms[kwargs["zone"]]
            room["timer"] = None
            if not (room["away"] and room["empty"]):
                return
            zone, name = room["zone"], room["name"]
            owner = self.get_state(room["owner"])
            active = room_active_read.active(self, zone)
            stale = self.get_state(room["active_entity"], attribute="stale")
            if not is_away(owner) or active is not False or stale != "false":
                self.log(
                    f"{zone}: hold elapsed but a fresh read disagrees "
                    f"(owner {owner}, active {active}, stale {stale}) - holding again",
                    level="WARNING",
                )
                self._start_hold(room)
                return
            room["done"] = True
            lit = [light for light in room["lights"] if self.get_state(light) == "on"]
            minutes = self.hold_s // 60
            if not lit:
                self.log(f"{zone}: {name} away and the room empty for {minutes} min - no light on", level="INFO")
                return
            for light in lit:
                self.turn_off(light)
            self.log(
                f"{zone}: {name} away and the room empty for {minutes} min - turned off {', '.join(lit)}",
                level="INFO",
            )
            self._report(room, lit)
        except Exception as e:
            self.log(f"hold handling failed: {e}", level="ERROR")

    # ---------- hold ----------

    def _evaluate(self, room, restart=False):
        if not (room["away"] and room["empty"]):
            room["done"] = False
            if room["timer"] is not None:
                self._cancel(room)
                blocker = "room active or unreadable" if room["away"] else f"{room['name']} home"
                self.log(f"{room['zone']}: hold cancelled - {blocker}", level="INFO")
            return
        if restart:
            room["done"] = False
            self._cancel(room)
        if room["timer"] is None and not room["done"]:
            self._start_hold(room)

    def _start_hold(self, room):
        room["timer"] = self.run_in(self._on_hold_elapsed, self.hold_s, zone=room["zone"])
        self.log(
            f"{room['zone']}: {room['name']} away and the room empty - any light still on goes off "
            f"in {self.hold_s // 60} min unless that changes",
            level="INFO",
        )

    def _cancel(self, room):
        handle, room["timer"] = room["timer"], None
        if handle is None:
            return
        try:
            self.cancel_timer(handle)
        except Exception:
            pass

    def _report(self, room, lit):
        one = len(lit) == 1
        try:
            self.fire_event(
                "house_events_report",
                cause=f"{room['name']} left - {room['room_label']} {'light was' if one else 'lights were'} on",
                effect="Turned it off" if one else "Turned them off",
                icon="mdi:lightbulb-off",
                audience_users=[room["name"]],
            )
        except Exception as e:
            self.log(f"house_events_report failed: {e}", level="DEBUG")
