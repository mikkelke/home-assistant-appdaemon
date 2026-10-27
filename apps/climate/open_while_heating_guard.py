"""Open-window/door-while-heating guard - whole-apartment.

An outward opening left open for grace_minutes produces one public house-activity
entry and one mobile push when the room's thermostat is in heat mode and the heating
season is enabled. The timer measures the open episode, not the thermostat's current
heat demand: ordinary floor-heating cycles must never restart the airing allowance.

An episode ends only when all watched contacts in the room are closed. If heating is
enabled after the allowance has elapsed, warn immediately while the opening is still
open. Recipients come from the room's notify list; unmapped phones are skipped by
MobileNotifier. The too-cold half of the problem remains climate_alarm's job.

Interior room doors are deliberately not watched - heat moving between rooms is not
waste. The living-room window and terrace door share one open episode.
"""

from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class OpenWhileHeatingGuard(hass.Hass):
    def initialize(self):
        self.grace_min = float(self.args.get("grace_minutes", 12))
        self.rooms = self.args.get("rooms") or {}
        self.season_entity = self.args.get("season_entity")
        self._timers = {}  # room -> pending grace run_in handle
        self._opened_at = {}  # room -> beginning of the current open episode
        self._reported = set()  # rooms whose current open-episode already produced an entry
        for room, cfg in self.rooms.items():
            for contact in cfg.get("contacts", []):
                self.listen_state(self._on_change, contact, room=room)
            thermostat = cfg.get("thermostat")
            if thermostat:
                self.listen_state(self._on_change, thermostat, room=room)
        if self.season_entity:
            self.listen_state(self._on_change, self.season_entity)
        # Recover the remaining grace from the contacts' last_changed timestamps.
        self.run_in(lambda _: self._recheck_all(), 10)
        self.log(f"OpenWhileHeatingGuard: {len(self.rooms)} rooms, grace {self.grace_min:.0f} min")

    def terminate(self):
        for room in list(self._timers):
            self._cancel(room)

    def _recheck_all(self):
        for room in self.rooms:
            self._evaluate(room)

    def _on_change(self, entity, attribute, old, new, kwargs):
        room = kwargs.get("room")
        if room is None:
            self._recheck_all()
        else:
            self._evaluate(room)

    def _open_contacts(self, room):
        cfg = self.rooms.get(room) or {}
        return [c for c in cfg.get("contacts", []) if self.get_state(c) == "on"]

    def _heating_enabled(self, room):
        cfg = self.rooms.get(room) or {}
        thermostat = cfg.get("thermostat")
        return (
            bool(self.season_entity)
            and self.get_state(self.season_entity) == "on"
            and bool(thermostat)
            and self.get_state(thermostat) == "heat"
        )

    def _episode_start(self, contacts, now):
        """Use the oldest currently open contact after a reload; missing dates start now."""
        opened = []
        for contact in contacts:
            full = self.get_state(contact, attribute="all") or {}
            try:
                changed = datetime.fromisoformat(str(full.get("last_changed")))
                # HA supplies aware timestamps. A missing offset cannot be trusted.
                if changed.tzinfo is not None:
                    opened.append(min(now, changed.timestamp()))
            except (TypeError, ValueError):
                pass
        return min(opened, default=now)

    def _evaluate(self, room):
        if room not in self.rooms:
            return
        try:
            contacts = self._open_contacts(room)
            if not contacts:
                self._cancel(room)
                # Unknown contacts prove neither an open nor a closed episode. Keep
                # its age and report marker until every contact is explicitly closed.
                if all(self.get_state(c) == "off" for c in self.rooms[room].get("contacts", [])):
                    self._opened_at.pop(room, None)
                    self._reported.discard(room)
                return
            if room in self._reported:
                return
            now = self.get_now().timestamp()
            if room not in self._opened_at:
                self._opened_at[room] = self._episode_start(contacts, now)
            remaining = self.grace_min * 60 - (now - self._opened_at[room])
            if remaining > 0:
                if room not in self._timers:
                    self._timers[room] = self.run_in(self._grace_elapsed, remaining, room=room)
            elif self._heating_enabled(room):
                self._cancel(room)
                self._report(room)
        except Exception as e:
            self.log(f"evaluate failed for {room}: {e}", level="WARNING")

    def _grace_elapsed(self, kwargs):
        room = kwargs.get("room")
        self._timers.pop(room, None)
        self._evaluate(room)

    def _report(self, room):
        try:
            self._reported.add(room)
            cfg = self.rooms.get(room) or {}
            label = cfg.get("label", str(room).replace("_", " ").capitalize())
            labels = cfg.get("opening_labels") or {}
            opening = " and ".join(dict.fromkeys(
                labels.get(contact, cfg.get("opening", "window"))
                for contact in self._open_contacts(room)
            ))
            self.fire_event(
                "house_events_report",
                cause=f"{label} {opening} open {self.grace_min:.0f}+ min with heating on",
                # Reader-neutral wording (user 2026-07-30): the log is read by anybody, so no
                # "you" - the phone push keeps its personal phrasing, this entry does not.
                effect=f"Heating is enabled with an open {opening} - worth closing it soon",
                icon="mdi:window-open-variant",
            )
            self._push(room, cfg, label, opening)
            self.log(f"{room}: open-{opening}-while-heating reported")
        except Exception as e:
            self.log(f"report failed for {room}: {e}", level="WARNING")

    def _push(self, room, cfg, label, opening):
        """One mobile push per episode, to the room's own audience (yaml `notify` list).
        Fail-soft on every step - a missing MobileNotifier or an unmapped recipient must
        never take the feed entry (already sent) down with it."""
        recipients = cfg.get("notify") or []
        if not recipients:
            return
        try:
            notifier = self.get_app("MobileNotifier")
            if notifier is None:
                self.log(f"{room}: MobileNotifier not available, push skipped", level="WARNING")
                return
            self.create_task(
                notifier.notify(
                    title="Open " + opening + " with heating on",
                    message=f"{label}: the {opening} has been open {self.grace_min:.0f}+ min with the "
                            f"heating on - close it when you can.",
                    target=list(recipients),
                )
            )
        except Exception as e:
            self.log(f"push failed for {room}: {e}", level="WARNING")

    def _cancel(self, room):
        handle = self._timers.pop(room, None)
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
        except Exception:
            pass
