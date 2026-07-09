"""
Climate Alarm: notify when heating-related rooms drop below a set threshold.

Uses Salus thermostats; includes open window/door context (e.g. rooftop doors).
Sets input_boolean.climate_alarm_active and input_text.climate_alarm_message.
Clear button = "Recheck in 1 hour" (snooze); real completion only when all rooms
are above threshold. Cooldown applies to push notifications only; state is always updated.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
from datetime import datetime, timezone
from typing import Any, Optional


def _room_name_from_entity(entity_id: str) -> str:
    """Derive a short room name from entity_id (e.g. climate.bedroom_thermostat -> Bedroom)."""
    if "." in entity_id:
        part = entity_id.split(".", 1)[1]
        part = part.replace("_thermostat", "").replace("_", " ").strip()
        return part.title() if part else entity_id
    return entity_id


class ClimateAlarm(hass.Hass):
    def initialize(self) -> None:
        self.log("ClimateAlarm initializing...")

        raw = self.args.get("climate_entities", [])
        self.climate_entities: list = raw if isinstance(raw, list) else [raw]
        raw_wd = self.args.get("window_door_entities", [])
        self.window_door_entities: list = raw_wd if isinstance(raw_wd, list) else [raw_wd]
        self.temp_threshold: dict = self.args.get("temp_threshold", {})

        self.event_active_entity = self.args.get(
            "event_active_entity", "input_boolean.climate_alarm_active"
        )
        self.message_entity = self.args.get(
            "message_entity", "input_text.climate_alarm_message"
        )
        self.clear_button_entity = self.args.get(
            "clear_button_entity", "input_button.climate_alarm_clear"
        )

        self.notify_target = self.args.get("notify_target", "home")
        self.cooldown_min = int(self.args.get("cooldown_min", 20))
        self.check_interval_min = int(self.args.get("check_interval_min", 0))
        self.clear_recheck_sec = int(self.args.get("clear_recheck_min", 60)) * 60
        self.notify_on_resolved = bool(self.args.get("notify_on_resolved", False))

        # Config validation: every climate entity must have a threshold
        missing = [e for e in self.climate_entities if e not in self.temp_threshold]
        if missing:
            self.log(
                f"Config error: temp_threshold missing for: {missing}. Add thresholds for all climate_entities.",
                level="ERROR",
            )
            return

        self._last_notify_at: Optional[datetime] = None
        self._clear_recheck_handle: Optional[str] = None

        self.mobile_notifier = None
        try:
            self.mobile_notifier = self.get_app("MobileNotifier")
            if self.mobile_notifier:
                self.log("MobileNotifier found", level="INFO")
        except Exception as e:
            self.log(f"MobileNotifier not available: {e}", level="WARNING")

        # Triggers: listen_state on all climate and window/door entities
        for entity_id in self.climate_entities + self.window_door_entities:
            self.listen_state(self._on_entity_change, entity_id)

        # Clear button: fire when button is pressed (state becomes "pressed")
        self.listen_state(
            self._on_clear_pressed,
            self.clear_button_entity,
            new="pressed",
        )

        # Optional backup schedule
        if self.check_interval_min > 0:
            self.run_every(
                self._run_evaluate,
                "now",
                self.check_interval_min * 60,
            )
            self.log(
                f"Backup schedule: every {self.check_interval_min} min",
                level="INFO",
            )

        # One check on startup after a short delay
        self.run_in(self._run_evaluate, 2)
        self.log(
            f"Started: {len(self.climate_entities)} climate, {len(self.window_door_entities)} window/door entities",
            level="INFO",
        )

    def _on_entity_change(
        self,
        entity: str,
        attribute: Optional[str],
        old: str,
        new: str,
        kwargs: dict,
    ) -> None:
        """Any climate or window/door state change -> re-evaluate."""
        self.run_in(lambda k: self.create_task(self._evaluate()), 0)

    def _on_clear_pressed(
        self,
        entity: str,
        attribute: Optional[str],
        old: str,
        new: str,
        kwargs: dict,
    ) -> None:
        """User pressed clear: hide alarm and start recheck timer."""
        self.log("Clear button pressed: hiding alarm, recheck in 1 hour", level="INFO")
        self._cancel_clear_recheck_timer()
        self.run_in(lambda k: self.create_task(self._clear_alarm_state_and_schedule_recheck()), 0)

    def _cancel_clear_recheck_timer(self) -> None:
        if self._clear_recheck_handle is not None:
            try:
                self.cancel_timer(self._clear_recheck_handle)
            except Exception:
                pass
            self._clear_recheck_handle = None

    async def _clear_alarm_state_and_schedule_recheck(self) -> None:
        """Turn off boolean, clear message, schedule re-evaluate after clear_recheck_sec."""
        try:
            await self.call_service(
                "input_boolean/turn_off",
                entity_id=self.event_active_entity,
            )
        except Exception as e:
            self.log(f"Failed to turn off {self.event_active_entity}: {e}", level="WARNING")
        try:
            await self.call_service(
                "input_text/set_value",
                entity_id=self.message_entity,
                value="",
            )
        except Exception as e:
            self.log(f"Failed to clear {self.message_entity}: {e}", level="WARNING")

        self._clear_recheck_handle = self.run_in(
            self._run_evaluate,
            self.clear_recheck_sec,
        )
        self.log(
            f"Recheck scheduled in {self.clear_recheck_sec // 60} min",
            level="INFO",
        )

    def _run_evaluate(self, kwargs: dict) -> None:
        """Entry point for run_in/run_every: run async evaluate."""
        self.create_task(self._evaluate())

    async def _evaluate(self) -> None:
        """Check all rooms and window/door state; set alarm or clear."""
        cold_rooms: list[tuple[str, float, float]] = []  # (entity_id, current, threshold)
        open_doors: list[str] = []

        for entity_id in self.climate_entities:
            threshold = self.temp_threshold.get(entity_id)
            if threshold is None:
                continue
            try:
                state = await self.get_state(entity_id)
                if state in (None, "unknown", "unavailable"):
                    self.log(
                        f"Skip {entity_id}: state={state}",
                        level="WARNING",
                    )
                    continue
                attrs = await self.get_state(entity_id, attribute="all")
                if attrs and isinstance(attrs, dict):
                    attrs = attrs.get("attributes") or {}
                else:
                    attrs = {}
                cur = attrs.get("current_temperature")
                if cur is None:
                    self.log(
                        f"Skip {entity_id}: no current_temperature",
                        level="WARNING",
                    )
                    continue
                try:
                    temp = float(cur)
                except (TypeError, ValueError):
                    continue
                if temp < threshold:
                    cold_rooms.append((entity_id, temp, threshold))
            except Exception as e:
                self.log(f"Error reading {entity_id}: {e}", level="WARNING")

        for entity_id in self.window_door_entities:
            try:
                state = await self.get_state(entity_id)
                if state == "on":  # open
                    name = await self._friendly_name(entity_id)
                    open_doors.append(name)
            except Exception as e:
                self.log(f"Error reading window/door {entity_id}: {e}", level="WARNING")

        if cold_rooms:
            await self._set_alarm(cold_rooms, open_doors)
        else:
            await self._clear_alarm(open_doors)

    async def _friendly_name(self, entity_id: str) -> str:
        """Get friendly name for an entity, fallback to derived name."""
        try:
            name = await self.get_state(entity_id, attribute="friendly_name")
            if name and isinstance(name, str) and name.strip():
                return name.strip()
        except Exception:
            pass
        return _room_name_from_entity(entity_id)

    def _format_message(
        self,
        cold_rooms: list[tuple[str, float, float]],
        open_doors: list[str],
    ) -> str:
        """Build a short one-liner (max 255 chars for input_text)."""
        parts = []
        for entity_id, temp, thresh in cold_rooms:
            name = _room_name_from_entity(entity_id)
            parts.append(f"{name} {temp:.1f} degC (below {int(thresh)} degC)")
        msg = ". ".join(parts)
        if open_doors:
            msg += ". Open: " + ", ".join(open_doors)
        if len(msg) > 255:
            msg = msg[:252] + "..."
        return msg

    async def _set_alarm(
        self,
        cold_rooms: list[tuple[str, float, float]],
        open_doors: list[str],
    ) -> None:
        """Set alarm state and optionally notify (cooldown for notify only)."""
        message = self._format_message(cold_rooms, open_doors)
        try:
            await self.call_service(
                "input_boolean/turn_on",
                entity_id=self.event_active_entity,
            )
        except Exception as e:
            self.log(f"Failed to turn on {self.event_active_entity}: {e}", level="ERROR")
        try:
            await self.call_service(
                "input_text/set_value",
                entity_id=self.message_entity,
                value=message,
            )
        except Exception as e:
            self.log(f"Failed to set {self.message_entity}: {e}", level="ERROR")

        # Notify only if cooldown elapsed
        now = datetime.now(timezone.utc)
        should_notify = True
        if self._last_notify_at is not None:
            delta_min = (now - self._last_notify_at).total_seconds() / 60
            if delta_min < self.cooldown_min:
                should_notify = False
        if should_notify and self.mobile_notifier:
            try:
                await self.mobile_notifier.notify(
                    title="Climate alarm",
                    message=message,
                    target=self.notify_target,
                )
                self._last_notify_at = now
            except Exception as e:
                self.log(f"Notify failed: {e}", level="WARNING")

    async def _clear_alarm(self, open_doors: list[str]) -> None:
        """Clear alarm state (real completion); cancel recheck timer."""
        self._cancel_clear_recheck_timer()
        try:
            await self.call_service(
                "input_boolean/turn_off",
                entity_id=self.event_active_entity,
            )
        except Exception as e:
            self.log(f"Failed to turn off {self.event_active_entity}: {e}", level="WARNING")
        try:
            await self.call_service(
                "input_text/set_value",
                entity_id=self.message_entity,
                value="",
            )
        except Exception as e:
            self.log(f"Failed to clear {self.message_entity}: {e}", level="WARNING")

        if self.notify_on_resolved and self.mobile_notifier:
            try:
                await self.mobile_notifier.notify(
                    title="Climate alarm resolved",
                    message="All rooms are above threshold.",
                    target=self.notify_target,
                )
            except Exception as e:
                self.log(f"Notify (resolved) failed: {e}", level="WARNING")
