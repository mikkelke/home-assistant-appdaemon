"""
Apartment entry truth - publishes ``binary_sensor.apartment_entry_secure``.

Thin arbitration layer for the front door, sitting ABOVE LockHealth rather than reading
the raw lock twins directly: locks -> LockHealth (arbitrate + heal) ->
``sensor.apartment_lock`` -> EntryTruth (+ door) -> ``binary_sensor.apartment_entry_secure``.
LockHealth owns divergence/invalidity detection and healing entirely now (see its module
docstring for the two incidents - 2026-07-12 cloud stuck, 2026-07-22 BLE stuck - that
made a single hardcoded "trust BLE" rule wrong); this app no longer touches
``lock.yale_bt``/``lock.yale`` directly except as a fallback, below.

State: on  = door closed AND sensor.apartment_lock reports "locked"
       off = door open, or the arbitrated lock is not "locked"

Fallback: if sensor.apartment_lock itself is down (None/unknown/unavailable - LockHealth
not running, mid-reload, etc.), fall back to the raw BLE entity (``lock_fallback``) so a
down LockHealth doesn't take entry_secure down with it. ``lock_source_used`` records
which path was used ("arbitrated" | "fallback_ble").

Attributes: lock_state (the effective state entry_secure is judged on), bt_state /
cloud_state (read from sensor.apartment_lock's own attributes when arbitrated;
cloud_state is unknown in fallback mode - we no longer have visibility into the cloud
twin without LockHealth), door_open, reason, lock_source_used, source_entities,
computed_at (middle-layer convention).
"""

from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass


class EntryTruth(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.lock_source = a("lock_source", "sensor.apartment_lock")
        self.lock_fallback = a("lock_fallback", "lock.yale_bt")
        self.door = a("door_open_entity", "binary_sensor.apartment_door_open")
        self.publish_entity = a("publish_entity", "binary_sensor.apartment_entry_secure")

        for ent in (self.lock_source, self.door):
            self.listen_state(self._on_change, ent)
        self.run_every(self._tick, "now+4", 60)

    def _on_change(self, entity, attribute, old, new, kwargs):
        self.create_task(self._eval())

    def _tick(self, kwargs):
        self.create_task(self._eval())

    async def _eval(self):
        try:
            lock_state = await self.get_state(self.lock_source)
            door = await self.get_state(self.door)
            door_open = door == "on"

            lock_source_used = "arbitrated"
            effective_state = lock_state
            bt_state, cloud_state = None, None
            if lock_state in (None, "unknown", "unavailable"):
                lock_source_used = "fallback_ble"
                effective_state = await self.get_state(self.lock_fallback)
                bt_state = effective_state
                self.log(f"sensor.apartment_lock unavailable - falling back to {self.lock_fallback}",
                         level="DEBUG")
            else:
                full = await self.get_state(self.lock_source, attribute="all") or {}
                source_attrs = full.get("attributes") or {}
                bt_state = source_attrs.get("bt_state")
                cloud_state = source_attrs.get("cloud_state")

            locked = effective_state == "locked"
            secure = locked and not door_open

            if door_open:
                reason = "door is open"
            elif not locked:
                reason = f"lock reports {effective_state or 'unknown'} ({lock_source_used})"
            else:
                reason = f"locked and closed ({lock_source_used})"

            # door_open silently drops from published attributes whenever it's False
            # (door closed, the common case) -- AppDaemon 4.5.13 set_state bug, not
            # ours; see smart_cooling.py's _publish() for details.
            await self.set_state(self.publish_entity,
                                 state="on" if secure else "off",
                                 replace=True,
                                 attributes={
                                     "friendly_name": "Apartment entry secure",
                                     "device_class": "lock",
                                     "icon": "mdi:shield-home",
                                     "lock_state": effective_state,
                                     "bt_state": bt_state,
                                     "cloud_state": cloud_state,
                                     "door_open": door_open,
                                     "reason": reason,
                                     "lock_source_used": lock_source_used,
                                     "source_entities": [self.lock_source, self.lock_fallback, self.door],
                                     "computed_at": datetime.now().isoformat(timespec="seconds"),
                                 })
        except Exception as e:
            self.log(f"entry truth eval failed: {e}", level="ERROR")
