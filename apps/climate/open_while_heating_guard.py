"""Open-window/door-while-heating guard - whole-apartment.

User direction (2026-07-16): the earlier Claudias-room frost feed entry focused on one
room, but the real rule is general - short airing with the heat on is normal, an opening
left open for LONG with the radiator running wastes heat and over-cools the room. This
guard watches every room that has both an outward opening contact and a heating-active
sensor, apartment-wide.

Per room: any watched opening open AND the room's heating actively running for
grace_minutes straight -> ONE public house-activity entry per episode (public because
whoever happens to be home can close it). An episode is the open-span: it re-arms only
when every watched contact in the room is closed again, so heating cycling off and on
under an open window can't double-report. Summer-silent by construction (heating_active
simply never turns on). The too-cold half of the same problem is climate_alarm's job
(mobile push); this guard covers the heat-waste half.

Claudias rooftop door 2 IS watched even though claudias_room_climate drops the setpoint
on open - that drop stops the heating within a minute, so this guard stays silent there
unless that automation breaks or gets removed (Claudia may not want it), which is
exactly when this should start speaking up.

Interior room doors are deliberately not watched - heat moving between rooms is not
waste. rooftop_door_1 is unmapped until its inner room is confirmed (see yaml).
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class OpenWhileHeatingGuard(hass.Hass):
    def initialize(self):
        self.grace_min = float(self.args.get("grace_minutes", 15))
        self.rooms = self.args.get("rooms") or {}
        self._timers = {}  # room -> pending grace run_in handle
        self._reported = set()  # rooms whose current open-episode already produced an entry
        for room, cfg in self.rooms.items():
            for contact in cfg.get("contacts", []):
                self.listen_state(self._on_change, contact, room=room)
            heating = cfg.get("heating")
            if heating:
                self.listen_state(self._on_change, heating, room=room)
        # Restart-safe: pick up windows that were already open with heat running. The
        # grace restarts from now, so a deploy can delay a report, never fabricate one.
        self.run_in(lambda _: self._recheck_all(), 10)
        self.log(f"OpenWhileHeatingGuard: {len(self.rooms)} rooms, grace {self.grace_min:.0f} min")

    def terminate(self):
        for room in list(self._timers):
            self._cancel(room)

    def _recheck_all(self):
        for room in self.rooms:
            self._evaluate(room)

    def _on_change(self, entity, attribute, old, new, kwargs):
        self._evaluate(kwargs.get("room"))

    def _open_contacts(self, room):
        cfg = self.rooms.get(room) or {}
        return [c for c in cfg.get("contacts", []) if self.get_state(c) == "on"]

    def _condition(self, room):
        cfg = self.rooms.get(room) or {}
        heating = cfg.get("heating")
        return bool(self._open_contacts(room)) and bool(heating) and self.get_state(heating) == "on"

    def _evaluate(self, room):
        if room not in self.rooms:
            return
        try:
            if self._condition(room):
                if room not in self._timers and room not in self._reported:
                    self._timers[room] = self.run_in(self._grace_elapsed, self.grace_min * 60, room=room)
            else:
                self._cancel(room)
                # Episode ends only when every contact is closed - heating cycling off
                # mid-open must not re-arm a second report for the same open window.
                if not self._open_contacts(room):
                    self._reported.discard(room)
        except Exception as e:
            self.log(f"evaluate failed for {room}: {e}", level="WARNING")

    def _grace_elapsed(self, kwargs):
        room = kwargs.get("room")
        self._timers.pop(room, None)
        try:
            if not self._condition(room):
                return
            self._reported.add(room)
            cfg = self.rooms.get(room) or {}
            label = cfg.get("label", str(room).replace("_", " ").capitalize())
            opening = cfg.get("opening", "window")
            self.fire_event(
                "house_events_report",
                cause=f"{label} {opening} open {self.grace_min:.0f}+ min with heating on",
                effect=f"Radiator is heating past an open {opening} - close it when you can",
                icon="mdi:window-open-variant",
            )
            self.log(f"{room}: open-{opening}-while-heating reported")
        except Exception as e:
            self.log(f"report failed for {room}: {e}", level="WARNING")

    def _cancel(self, room):
        handle = self._timers.pop(room, None)
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
        except Exception:
            pass
