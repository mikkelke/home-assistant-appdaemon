"""
Apartment entry truth - publishes ``binary_sensor.apartment_entry_secure``.

Arbitration middle layer for the front door. One physical Yale lock is exposed
twice in HA: ``lock.yale_bt`` (local Bluetooth, authoritative) and ``lock.yale``
(cloud twin that can miss re-lock pushes and stick "unlocked" - it did for
1.5 h on 2026-07-12 and the user had to ask whether the door was really open).

State: on  = door closed AND the BLE lock reports locked
       off = door open, or the BLE lock reports unlocked

Attributes: lock_state / cloud_state / cloud_agrees, door_open, reason,
source_entities, computed_at (middle-layer convention).

Divergence alert: when the cloud twin disagrees with BLE continuously for
``divergence_alert_minutes``, send ONE mobile notification naming the stale
side, and re-arm once they agree again.
"""

from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass


class EntryTruth(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.lock_bt = a("lock_authoritative", "lock.yale_bt")
        self.lock_cloud = a("lock_cloud", "lock.yale")
        self.door = a("door_open_entity", "binary_sensor.apartment_door_open")
        self.publish_entity = a("publish_entity", "binary_sensor.apartment_entry_secure")
        self.alert_minutes = float(a("divergence_alert_minutes", 5))
        self.notify_target = a("notify_target", "mikkel")

        self._diverged_since = None
        self._alerted = False
        # get_app must be resolved in sync init - async context returns a Task.
        self._notifier = self.get_app("MobileNotifier")

        for ent in (self.lock_bt, self.lock_cloud, self.door):
            self.listen_state(self._on_change, ent)
        self.run_every(self._tick, "now+4", 60)

    def _on_change(self, entity, attribute, old, new, kwargs):
        self.create_task(self._eval())

    def _tick(self, kwargs):
        self.create_task(self._eval())

    async def _eval(self):
        try:
            bt = await self.get_state(self.lock_bt)
            cloud = await self.get_state(self.lock_cloud)
            door = await self.get_state(self.door)

            bt_locked = bt == "locked"
            door_open = door == "on"
            cloud_agrees = (cloud == bt) or cloud in (None, "unknown", "unavailable")

            secure = bt_locked and not door_open
            if door_open:
                reason = "door is open"
            elif not bt_locked:
                reason = f"lock reports {bt or 'unknown'} (BLE)"
            elif not cloud_agrees:
                reason = f"locked and closed (BLE authoritative; cloud twin stale: {cloud})"
            else:
                reason = "locked and closed"

            await self.set_state(self.publish_entity,
                                 state="on" if secure else "off",
                                 replace=True,
                                 attributes={
                                     "friendly_name": "Apartment entry secure",
                                     "device_class": "lock",
                                     "icon": "mdi:shield-home",
                                     "lock_state": bt,
                                     "cloud_state": cloud,
                                     "cloud_agrees": cloud_agrees,
                                     "door_open": door_open,
                                     "reason": reason,
                                     "source_entities": [self.lock_bt, self.lock_cloud, self.door],
                                     "computed_at": datetime.now().isoformat(timespec="seconds"),
                                 })

            await self._watch_divergence(bt, cloud, cloud_agrees)
        except Exception as e:
            self.log(f"entry truth eval failed: {e}", level="ERROR")

    async def _watch_divergence(self, bt, cloud, cloud_agrees):
        if cloud_agrees:
            if self._alerted:
                self.log("Yale cloud twin re-synced with BLE")
            self._diverged_since = None
            self._alerted = False
            return
        now = datetime.now()
        if self._diverged_since is None:
            self._diverged_since = now
            return
        minutes = (now - self._diverged_since).total_seconds() / 60.0
        if minutes >= self.alert_minutes and not self._alerted:
            self._alerted = True
            self.log(f"Yale divergence {minutes:.0f} min: BLE={bt} cloud={cloud} - notifying")
            try:
                await self._notifier.notify(
                    title="Front door",
                    message=(f"Yale cloud shows '{cloud}' but the lock itself (Bluetooth) "
                             f"says '{bt}'. Trust the lock; the cloud/app is stale."),
                    target=self.notify_target,
                )
            except Exception as e:
                self.log(f"divergence notify failed: {e}", level="WARNING")
