"""
Salus health watchdog - admin-only alerts for the heating system's own
plumbing: thermostat and wiring-centre connectivity, and thermostat battery.

The Salus thermostats and the wiring centre run on zigbee2mqtt. A device that
stops answering raises no alarm of its own: zigbee2mqtt marks it offline only
after its availability timeout (60 min of silence for a thermostat, 30 min
without an answer to its ping for the wiring centre), and Home Assistant then
shows its entities as `unavailable`. Any other climate state (off/heat/auto/...)
means the thermostat is online. The wiring centre is the single point of
failure for ALL heating in the flat; its link is read from one configured
entity (controller_entity, a zone sensor that holds a thermostat address while
the wiring centre is online). A thermostat or the wiring centre that then stays
`unavailable` for offline_minutes is pushed, and so is its recovery. `unknown`
says nothing about the link, so it neither starts nor ends an outage.

This is admin-class information - nobody needs to know their thermostat's
battery is at 25% or that a Zigbee hop dropped - so every push goes to
Mikkel only, unlike the household-wide notify tiers used elsewhere.

Thermostats are discovered by naming convention (thermostat_marker), not
hardcoded per room, so adding, renaming or removing a thermostat needs no code
or yaml change here. Thermostats are the climate entities whose id contains
thermostat_marker (exclude_entities drops any that are not Salus, such as the
A/C); a thermostat's battery is the sensor with that marker whose id ends in
_battery_level.
"""

import json
import os

import appdaemon.plugins.hass.hassapi as hass

ONLINE = "online"
OFFLINE = "offline"


class SalusHealth(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.thermostat_marker = a("thermostat_marker", "thermostat")
        self.controller_entity = a("controller_entity", "sensor.wiring_centre_zone_1")
        self.offline_minutes = float(a("offline_minutes", 10))
        self.battery_warn_percent = int(a("battery_warn_percent", 40))
        self.notify_target = a("notify_target", "mikkel")
        self.state_file = a("state_file", "/conf/apps/climate/salus_health_state.json")

        exclude = a("exclude_entities", [])
        self.exclude_entities = set(exclude) if isinstance(exclude, list) else {exclude}

        self._notifier = self.get_app("MobileNotifier")

        self._offline_timer_handles = {}

        saved_state = self._load_state()
        self._offline_alerted: dict = dict(saved_state.get("offline_alerted", {}))
        self._battery_alerted_percent: dict = dict(saved_state.get("battery_alerted_percent", {}))

        thermostat_entities, battery_entities = self._discover_entities()
        controller_entities = [self.controller_entity]
        connectivity_entities = sorted(thermostat_entities + controller_entities)

        # An offline flag only means something for an entity that is watched now.
        self._offline_alerted = {
            e: v for e, v in self._offline_alerted.items() if e in connectivity_entities
        }

        for entity_id in connectivity_entities:
            self.listen_state(self._on_connectivity_change, entity_id)
            self._init_connectivity(entity_id)

        for entity_id in battery_entities:
            self.listen_state(self._on_battery_change, entity_id)
            self._check_battery(entity_id, self.get_state(entity_id))

        self.log(
            f"SalusHealth watching {len(thermostat_entities)} thermostat, "
            f"{len(controller_entities)} controller, {len(battery_entities)} battery entities",
            level="INFO",
        )

    # ---------- discovery ----------

    def _discover_entities(self):
        """Find the thermostats and their batteries by naming convention rather
        than a hardcoded per-room list - see module docstring."""
        climates = self._domain_states("climate")
        sensors = self._domain_states("sensor")

        thermostats = sorted(
            e for e in climates if e not in self.exclude_entities and self.thermostat_marker in e
        )
        battery = sorted(
            e
            for e in sensors
            if e not in self.exclude_entities
            and self.thermostat_marker in e
            and e.endswith("_battery_level")
        )
        return thermostats, battery

    def _domain_states(self, domain):
        try:
            return self.get_state(domain) or {}
        except Exception as e:
            self.log(f"{domain} discovery failed: {e}", level="WARNING")
            return {}

    def _is_controller(self, entity_id):
        return entity_id == self.controller_entity

    def _room_label(self, entity_id):
        """Derive a short room label from a discovered thermostat entity_id - same
        string-manipulation convention as climate_alarm.py's
        _room_name_from_entity."""
        part = entity_id.split(".", 1)[-1]
        part = part.removesuffix("_battery_level")
        part = part.replace("_thermostat", "")
        part = part.replace("_", " ").strip()
        return part.title() if part else entity_id

    # ---------- connectivity / offline ----------

    @staticmethod
    def _link(state):
        """ONLINE, OFFLINE, or None when the state says nothing about the link.
        zigbee2mqtt reports an offline device's entities as `unavailable`. Every
        other state is a live reading: a climate entity's HVAC mode
        (off/heat/auto/...), or the wiring centre's zone value."""
        if state in (None, "unknown"):
            return None
        return OFFLINE if state == "unavailable" else ONLINE

    def _init_connectivity(self, entity_id):
        link = self._link(self.get_state(entity_id))
        if link == OFFLINE:
            if not self._offline_alerted.get(entity_id, False):
                self._arm_offline_timer(entity_id)
            # else: already alerted before a restart and still offline - keep waiting,
            # no duplicate timer/alert needed.
        elif link == ONLINE:
            if self._offline_alerted.get(entity_id, False):
                # It recovered while AppDaemon was down/restarting - we would
                # otherwise never see the offline->online transition to report it.
                self._offline_alerted[entity_id] = False
                self._save_state()
                self.create_task(self._notify_recovery(entity_id))
        # unknown/None -> hold: neither arm nor clear anything.

    def _on_connectivity_change(self, entity, attribute, old, new, kwargs):
        link = self._link(new)
        if link == OFFLINE:
            self._arm_offline_timer(entity)
        elif link == ONLINE:
            self._disarm_offline_timer(entity)
            if self._offline_alerted.get(entity, False):
                self._offline_alerted[entity] = False
                self._save_state()
                self.create_task(self._notify_recovery(entity))
        # unknown -> hold: a blip into "unknown" must not cancel a
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
        if self._link(self.get_state(entity_id)) != OFFLINE:
            return
        self._offline_alerted[entity_id] = True
        self._save_state()
        self.create_task(self._notify_offline(entity_id))

    async def _notify_offline(self, entity_id):
        minutes = self.offline_minutes
        if self._is_controller(entity_id):
            title = "Wiring centre offline"
            message = (
                f"The wiring centre has been offline for {minutes:.0f}+ min - "
                "all room heating depends on it."
            )
        else:
            title = "Thermostat offline"
            label = self._room_label(entity_id)
            message = f"{label} thermostat has been offline for {minutes:.0f}+ min."
        await self._safe_notify(title, message)

    async def _notify_recovery(self, entity_id):
        if self._is_controller(entity_id):
            title = "Wiring centre back online"
            message = "The wiring centre reconnected - heating control is restored for all rooms."
        else:
            title = "Thermostat back online"
            label = self._room_label(entity_id)
            message = f"{label} thermostat reconnected after being offline."
        await self._safe_notify(title, message)

    # ---------- battery ----------

    def _on_battery_change(self, entity, attribute, old, new, kwargs):
        self._check_battery(entity, new)

    def _check_battery(self, entity_id, raw_value):
        """raw_value is the battery entity's PERCENT state, compared against
        battery_warn_percent. The alerted/hysteresis value is that same
        percent: a further drop alerts again, a rise above the threshold
        re-arms.
        """
        percent = self._parse_battery_level(raw_value)
        if percent is None:
            return  # unknown/unavailable/unparseable -> hold, not evidence of anything.

        if percent <= self.battery_warn_percent:
            last_alerted = self._battery_alerted_percent.get(entity_id)
            if last_alerted is None or percent < last_alerted:
                self._battery_alerted_percent[entity_id] = percent
                self._save_state()
                self.create_task(self._notify_battery(entity_id, percent))
        elif entity_id in self._battery_alerted_percent:
            # Recovered above the threshold (recharged) - re-arm for a future drop.
            del self._battery_alerted_percent[entity_id]
            self._save_state()

    @staticmethod
    def _parse_battery_level(value):
        if value in (None, "unknown", "unavailable"):
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    async def _notify_battery(self, entity_id, percent):
        label = self._room_label(entity_id)
        await self._safe_notify(
            "Thermostat battery low",
            f"{label} thermostat battery is at {percent}% - charge soon.",
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
            "battery_alerted_percent": self._battery_alerted_percent,
        }
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"state save failed: {e}", level="WARNING")
