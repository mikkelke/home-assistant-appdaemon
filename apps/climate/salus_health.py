"""
Salus health watchdog - admin-only alerts for the heating system's own
plumbing: thermostat/controller connectivity, fault registers, and battery.

The Salus iT600 gateway silently drops a thermostat's climate entity to
`unavailable` on a dropout and never logs it (see homeassistant_salus's
ENHANCEMENT_PLAN.md - Claudia's dropouts ran 16 min -> 55 min -> hours on
2026-08-17 before anyone noticed). The wiring centre ("Control Centre") is
worse: it is the single point of failure for ALL heating in the flat, yet HA
exposed nothing for it until the connectivity/problem binary_sensor pair this
alert relies on was added for it.

This is admin-class information - nobody needs to know their thermostat's
battery reads 2/5 or that a Zigbee hop dropped - so every push goes to
Mikkel only, unlike the household-wide notify tiers used elsewhere.

Entities are discovered by naming convention (thermostat_marker /
controller_marker), not hardcoded per room, so adding, renaming or removing
a thermostat needs no code or yaml change here.
"""

import json
import os

import appdaemon.plugins.hass.hassapi as hass


class SalusHealth(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.thermostat_marker = a("thermostat_marker", "thermostat")
        self.controller_marker = a("controller_marker", "control_centre")
        self.offline_minutes = float(a("offline_minutes", 10))
        self.battery_warn_level = int(a("battery_warn_level", 2))
        self.notify_target = a("notify_target", "mikkel")
        self.state_file = a("state_file", "/conf/apps/climate/salus_health_state.json")

        exclude = a("exclude_entities", [])
        self.exclude_entities = set(exclude) if isinstance(exclude, list) else {exclude}

        self._notifier = self.get_app("MobileNotifier")

        self._offline_timer_handles = {}

        saved_state = self._load_state()
        self._offline_alerted: dict = dict(saved_state.get("offline_alerted", {}))
        self._problem_alerted: dict = dict(saved_state.get("problem_alerted", {}))
        self._battery_alerted_level: dict = dict(saved_state.get("battery_alerted_level", {}))

        connectivity_entities, problem_entities, battery_entities = self._discover_entities()

        for entity_id in connectivity_entities:
            self.listen_state(self._on_connectivity_change, entity_id)
            self._init_connectivity(entity_id)

        for entity_id in problem_entities:
            self.listen_state(self._on_problem_change, entity_id)
            self._init_problem(entity_id)

        for entity_id in battery_entities:
            self.listen_state(self._on_battery_change, entity_id)
            self._check_battery(entity_id, self.get_state(entity_id))

        self.log(
            f"SalusHealth watching {len(connectivity_entities)} connectivity, "
            f"{len(problem_entities)} problem, {len(battery_entities)} battery entities",
            level="INFO",
        )

    # ---------- discovery ----------

    def _discover_entities(self):
        """Find Salus diagnostic entities by naming convention rather than a
        hardcoded per-room list - see module docstring."""

        def matches(entity_id, suffix):
            if entity_id in self.exclude_entities:
                return False
            if not entity_id.endswith(suffix):
                return False
            return self.thermostat_marker in entity_id or self.controller_marker in entity_id

        try:
            binary_sensors = self.get_state("binary_sensor") or {}
        except Exception as e:
            self.log(f"binary_sensor discovery failed: {e}", level="WARNING")
            binary_sensors = {}

        try:
            sensors = self.get_state("sensor") or {}
        except Exception as e:
            self.log(f"sensor discovery failed: {e}", level="WARNING")
            sensors = {}

        connectivity = sorted(e for e in binary_sensors if matches(e, "_connectivity"))
        problem = sorted(e for e in binary_sensors if matches(e, "_problem"))
        battery = sorted(e for e in sensors if matches(e, "_battery_level"))
        return connectivity, problem, battery

    def _is_controller(self, entity_id):
        return self.controller_marker in entity_id

    def _room_label(self, entity_id):
        """Derive a short room/device label from a discovered entity_id - same
        string-manipulation convention as climate_alarm.py's
        _room_name_from_entity. Naturally yields "Control Centre" for the
        controller's own entities without any special-casing here."""
        part = entity_id.split(".", 1)[-1]
        for suffix in ("_connectivity", "_problem", "_battery_level"):
            if part.endswith(suffix):
                part = part[: -len(suffix)]
                break
        part = part.replace("_thermostat", "")
        part = part.replace("_", " ").strip()
        return part.title() if part else entity_id

    # ---------- connectivity / offline ----------

    def _init_connectivity(self, entity_id):
        current = self.get_state(entity_id)
        if current == "off":
            if not self._offline_alerted.get(entity_id, False):
                self._arm_offline_timer(entity_id)
            # else: already alerted before a restart and still off - keep waiting,
            # no duplicate timer/alert needed.
        elif current == "on":
            if self._offline_alerted.get(entity_id, False):
                # It recovered while AppDaemon was down/restarting - we would
                # otherwise never see the off->on transition to report it.
                self._offline_alerted[entity_id] = False
                self._save_state()
                self.create_task(self._notify_recovery(entity_id))
        # unknown/unavailable/None -> hold: neither arm nor clear anything.

    def _on_connectivity_change(self, entity, attribute, old, new, kwargs):
        if new == "off":
            self._arm_offline_timer(entity)
        elif new == "on":
            self._disarm_offline_timer(entity)
            if self._offline_alerted.get(entity, False):
                self._offline_alerted[entity] = False
                self._save_state()
                self.create_task(self._notify_recovery(entity))
        # unknown/unavailable -> hold: a blip into "unknown" must not cancel a
        # pending timer nor clear an already-alerted flag.

    def _arm_offline_timer(self, entity_id):
        self._disarm_offline_timer(entity_id)
        delay_s = int(self.offline_minutes * 60)
        self._offline_timer_handles[entity_id] = self.run_in(
            self._maybe_alert_offline, delay_s, entity_id=entity_id
        )

    def _disarm_offline_timer(self, entity_id):
        handle = self._offline_timer_handles.pop(entity_id, None)
        if handle:
            try:
                if self.timer_running(handle):
                    self.cancel_timer(handle)
            except Exception:
                pass

    def _maybe_alert_offline(self, kwargs):
        entity_id = kwargs.get("entity_id")
        if entity_id is None:
            return
        self._offline_timer_handles.pop(entity_id, None)
        if self._offline_alerted.get(entity_id, False):
            return
        if self.get_state(entity_id) != "off":
            return
        self._offline_alerted[entity_id] = True
        self._save_state()
        self.create_task(self._notify_offline(entity_id))

    async def _notify_offline(self, entity_id):
        label = self._room_label(entity_id)
        minutes = self.offline_minutes
        if self._is_controller(entity_id):
            title = "Heating controller offline"
            message = (
                f"{label} has been offline for {minutes:.0f}+ min - this affects "
                "ALL room heating, not just one room."
            )
        else:
            title = "Thermostat offline"
            message = f"{label} thermostat has been offline for {minutes:.0f}+ min."
        await self._safe_notify(title, message)

    async def _notify_recovery(self, entity_id):
        label = self._room_label(entity_id)
        if self._is_controller(entity_id):
            title = "Heating controller back online"
            message = f"{label} reconnected - heating control is restored for all rooms."
        else:
            title = "Thermostat back online"
            message = f"{label} thermostat reconnected after being offline."
        await self._safe_notify(title, message)

    # ---------- problem (fault registers) ----------

    def _init_problem(self, entity_id):
        current = self.get_state(entity_id)
        if current == "on":
            if not self._problem_alerted.get(entity_id, False):
                self._problem_alerted[entity_id] = True
                self._save_state()
                self.create_task(self._notify_problem(entity_id))
        elif current == "off":
            if self._problem_alerted.get(entity_id, False):
                self._problem_alerted[entity_id] = False
                self._save_state()
        # unknown/unavailable/None -> hold.

    def _on_problem_change(self, entity, attribute, old, new, kwargs):
        if new == "on":
            if not self._problem_alerted.get(entity, False):
                self._problem_alerted[entity] = True
                self._save_state()
                self.create_task(self._notify_problem(entity))
        elif new == "off":
            if self._problem_alerted.get(entity, False):
                self._problem_alerted[entity] = False
                self._save_state()
        # unknown/unavailable -> hold.

    async def _read_errors(self, entity_id):
        try:
            info = await self.get_state(entity_id, attribute="all") or {}
        except Exception as e:
            self.log(f"Could not read attributes for {entity_id}: {e}", level="DEBUG")
            return []
        attrs = info.get("attributes") if isinstance(info, dict) else None
        raw_errors = (attrs or {}).get("errors")
        return [str(x) for x in raw_errors] if isinstance(raw_errors, list) else []

    async def _notify_problem(self, entity_id):
        errors = await self._read_errors(entity_id)
        detail = f" ({', '.join(errors)})" if errors else ""
        label = self._room_label(entity_id)
        if self._is_controller(entity_id):
            title = "Heating controller fault"
            message = f"{label} reports a fault{detail} - this affects ALL room heating."
        else:
            title = "Thermostat fault"
            message = f"{label} thermostat reports a fault{detail}."
        await self._safe_notify(title, message)

    # ---------- battery ----------

    def _on_battery_change(self, entity, attribute, old, new, kwargs):
        self._check_battery(entity, new)

    def _check_battery(self, entity_id, raw_value):
        level = self._parse_battery_level(raw_value)
        if level is None:
            return  # unknown/unavailable/unparseable -> hold, not evidence of anything.

        if level <= self.battery_warn_level:
            last_alerted = self._battery_alerted_level.get(entity_id)
            if last_alerted is None or level < last_alerted:
                self._battery_alerted_level[entity_id] = level
                self._save_state()
                self.create_task(self._notify_battery(entity_id, level))
        elif entity_id in self._battery_alerted_level:
            # Recovered above the threshold (replaced) - re-arm for a future drop.
            del self._battery_alerted_level[entity_id]
            self._save_state()

    @staticmethod
    def _parse_battery_level(value):
        if value in (None, "unknown", "unavailable"):
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    async def _notify_battery(self, entity_id, level):
        label = self._room_label(entity_id)
        await self._safe_notify(
            "Thermostat battery low",
            f"{label} thermostat battery is at {level}/5 - replace soon.",
        )

    # ---------- notify ----------

    async def _safe_notify(self, title, message):
        try:
            await self._notifier.notify(title=title, message=message, target=self.notify_target)
            self.log(f"Alerted: {title} - {message}")
        except Exception as e:
            self.log(f"notify failed: {e}", level="WARNING")

    # ---------- restart survival (state file) ----------

    def _load_state(self) -> dict:
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self) -> None:
        data = {
            "offline_alerted": self._offline_alerted,
            "problem_alerted": self._problem_alerted,
            "battery_alerted_level": self._battery_alerted_level,
        }
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"state save failed: {e}", level="WARNING")
