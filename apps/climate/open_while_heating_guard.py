"""Open-window/door-while-heating guard - whole-apartment.

An outward opening left open for grace_minutes produces one public house-activity
entry and one mobile push when the room's thermostat is in heat mode, the heating
season is enabled, and actual heat ran within heating_lookback_hours. The timer measures
the open episode, not current heat demand: ordinary floor-heating cycles must never
restart the airing allowance. A confirmed off-to-on heat call while an opening is
already open warns immediately instead of waiting for the allowance.
Warm rooms delay the allowance until air reaches setpoint + warm_margin_c; once
started it keeps running. Heat-start warnings are suppressed while air is warm.

An episode ends only when all watched contacts in the room are closed. If heating is
enabled after the allowance has elapsed, warn immediately while the opening is still
open. Recipients come from the room's notify list; unmapped phones are skipped by
MobileNotifier. The too-cold half of the problem remains climate_alarm's job.

Interior room doors are deliberately not watched - heat moving between rooms is not
waste. The living-room window and terrace door share one open episode.
"""

from datetime import datetime, timezone
from math import isfinite

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class OpenWhileHeatingGuard(hass.Hass):
    def initialize(self):
        self.grace_min = float(self.args.get("grace_minutes", 12))
        self.warm_margin_c = float(self.args.get("warm_margin_c", 0.5))
        self.rooms = self.args.get("rooms") or {}
        self.season_entity = self.args.get("season_entity")
        self.heating_lookback_s = float(self.args.get("heating_lookback_hours", 24)) * 3600
        self._last_heated = {}  # room -> latest confirmed heat observation or on-to-off edge
        self._timers = {}  # room -> pending grace run_in handle
        self._opened_at = {}  # room -> beginning of the current open episode
        self._grace_started_at = {}  # room -> start of the airing allowance, after any warm spell
        self._reported = set()  # rooms whose current open-episode already produced an entry
        for room, cfg in self.rooms.items():
            for contact in cfg.get("contacts", []):
                self.listen_state(self._on_change, contact, room=room)
            for signal in (cfg.get("thermostat"), cfg.get("heating"), cfg.get("air")):
                if signal:
                    self.listen_state(self._on_change, signal, room=room)
            if cfg.get("thermostat"):
                self.listen_state(self._on_change, cfg["thermostat"], attribute="temperature", room=room)
        if self.season_entity:
            self.listen_state(self._on_change, self.season_entity)
        # Recorder I/O runs after initialize; recover recent heat and remaining open time.
        self.run_in(self._startup_recheck, 10)
        self.log(f"OpenWhileHeatingGuard: {len(self.rooms)} rooms, grace {self.grace_min:.0f} min")

    def terminate(self):
        for room in list(self._timers):
            self._cancel(room)

    def _startup_recheck(self, kwargs):
        now = self.get_now().timestamp()
        for room, cfg in self.rooms.items():
            heating = cfg.get("heating")
            if not heating:
                continue
            try:
                raw = self.get_history(
                    entity_id=heating,
                    start_time=datetime.fromtimestamp(now - self.heating_lookback_s, timezone.utc),
                    end_time=datetime.fromtimestamp(now, timezone.utc),
                    minimal_response=True,
                    no_attributes=True,
                )
                last_heated = self._history_last_heated(raw, now)
                if last_heated is not None:
                    self._remember_heat(room, last_heated)
            except Exception as e:
                self.log(f"{room}: recent heating history unavailable: {e}", level="WARNING")
        recent = ", ".join(room for room in self.rooms if self._recent_heat(room)) or "none"
        self.log(f"OpenWhileHeatingGuard: heating within {self.heating_lookback_s / 3600:g}h: {recent}")
        self._recheck_all()

    @staticmethod
    def _history_last_heated(raw, now):
        """A closing off edge proves an on interval; an initial off row proves nothing."""
        points = []
        for group in raw or []:
            rows = [group] if isinstance(group, dict) else group
            for row in rows or []:
                try:
                    changed = datetime.fromisoformat(str(row.get("last_changed") or row.get("last_updated")))
                    if changed.tzinfo is not None and changed.timestamp() <= now:
                        points.append((changed.timestamp(), row.get("state")))
                except (AttributeError, TypeError, ValueError):
                    continue
        last_heated = None
        was_on = False
        for ts, state in sorted(points, key=lambda item: item[0]):
            if state == "on" or (state == "off" and was_on):
                last_heated = ts
            # An unavailable gap cannot be filled with assumed heating.
            was_on = state == "on"
        return last_heated

    def _remember_heat(self, room, timestamp):
        # Recorder queries may return after a newer live heating observation.
        self._last_heated[room] = max(timestamp, self._last_heated.get(room, timestamp))

    def _recheck_all(self):
        for room in self.rooms:
            self._evaluate(room)

    def _on_change(self, entity, attribute, old, new, kwargs):
        room = kwargs.get("room")
        if room in self.rooms and entity == self.rooms[room].get("heating"):
            if new == "on" or (old == "on" and new == "off"):
                self._remember_heat(room, self.get_now().timestamp())
            if (old == "off" and new == "on" and room not in self._reported
                    and self._open_contacts(room) and self._heating_enabled(room)
                    and not self._too_warm(room)):
                self._cancel(room)
                self._report(room, heating_started=True)
        if room is None:
            self._recheck_all()
        else:
            self._evaluate(room)

    def _open_contacts(self, room):
        cfg = self.rooms.get(room) or {}
        return [c for c in cfg.get("contacts", []) if self.get_state(c) == "on"]

    def _too_warm(self, room):
        cfg = self.rooms[room]
        if not cfg.get("air") or not cfg.get("thermostat"):
            return None
        try:
            air = float(self.get_state(cfg["air"]))
            target = float(self.get_state(cfg["thermostat"], attribute="temperature"))
            if isfinite(air) and isfinite(target):
                return air > target + self.warm_margin_c
        except (TypeError, ValueError):
            pass
        return None

    def _heating_enabled(self, room):
        cfg = self.rooms.get(room) or {}
        thermostat = cfg.get("thermostat")
        return (
            bool(self.season_entity)
            and self.get_state(self.season_entity) == "on"
            and bool(thermostat)
            and self.get_state(thermostat) == "heat"
            and self._recent_heat(room)
        )

    def _recent_heat(self, room):
        heating = self.rooms[room].get("heating")
        now = self.get_now().timestamp()
        if heating and self.get_state(heating) == "on":
            self._remember_heat(room, now)
            return True
        last_heated = self._last_heated.get(room)
        return last_heated is not None and 0 <= now - last_heated <= self.heating_lookback_s

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
                    self._grace_started_at.pop(room, None)
                    self._reported.discard(room)
                return
            if room in self._reported:
                return
            now = self.get_now().timestamp()
            new_episode = room not in self._opened_at
            if new_episode:
                self._opened_at[room] = self._episode_start(contacts, now)
            if room not in self._grace_started_at:
                too_warm = self._too_warm(room)
                if too_warm:
                    return
                self._grace_started_at[room] = (
                    self._opened_at[room] if new_episode or too_warm is None else now
                )
            remaining = self.grace_min * 60 - (now - self._grace_started_at[room])
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

    def _report(self, room, heating_started=False):
        try:
            self._reported.add(room)
            cfg = self.rooms.get(room) or {}
            label = cfg.get("label", str(room).replace("_", " ").capitalize())
            labels = cfg.get("opening_labels") or {}
            opening = " and ".join(dict.fromkeys(
                labels.get(contact, cfg.get("opening", "window"))
                for contact in self._open_contacts(room)
            ))
            cause = (
                f"{label} heating started while the {opening} was open" if heating_started else
                f"{label} {opening} open {self.grace_min:.0f}+ min in a recently heated room"
            )
            effect = (
                f"Heating started with an open {opening} - worth closing it soon" if heating_started else
                f"The room has been heated recently - worth closing the {opening} soon"
            )
            self.fire_event(
                "house_events_report",
                cause=cause,
                # Reader-neutral wording (user 2026-07-30): the log is read by anybody, so no
                # "you" - the phone push keeps its personal phrasing, this entry does not.
                effect=effect,
                icon="mdi:window-open-variant",
            )
            self._push(room, cfg, label, opening, heating_started=heating_started)
            self.log(f"{room}: open-{opening}-while-heating reported")
        except Exception as e:
            self.log(f"report failed for {room}: {e}", level="WARNING")

    def _push(self, room, cfg, label, opening, heating_started=False):
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
            title = ("Heating started with an open " + opening if heating_started else
                     "Open " + opening + " in a heated room")
            message = (
                f"{label}: heating has started while the {opening} is open - close it when you can."
                if heating_started else
                f"{label}: the {opening} has been open {self.grace_min:.0f}+ min. "
                f"The room has been heated recently - close it when you can."
            )
            self.create_task(
                notifier.notify(
                    title=title,
                    message=message,
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
