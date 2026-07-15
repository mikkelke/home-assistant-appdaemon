"""HouseEvents - plain-English "what did the house just do" feed for the dashboard.

Observer only: listens to a handful of apartment-level entities (appliances, AC,
bedroom blind, front door lock, vacuum) and publishes a rolling feed to
`sensor.house_events` so non-technical housemates can see WHY the home changed
("AC started - precool", "Washer finished - 0.7 kWh") instead of experiencing it
as haunted. It never controls anything, and it deliberately does NOT require the
emitting apps to cooperate - everything is derived from the entities they already
publish, so new sources are one listen_state + one text builder away.

Feed shape (attributes, replace=True on every publish):
    events: newest-first list of {"ts": <UTC ISO>, "icon": <mdi:...>, "text": <ascii>}
State = ts of the newest event (gives the dashboard a cheap change signal).

Persistence: on app reload the previous feed is re-read from the entity itself, so
a code deploy does not wipe the list. An HA RESTART does wipe it - AppDaemon
set_state entities are ephemeral (see appdaemon-deploy notes); the feed simply
rebuilds from new events. Accepted for v1 rather than adding file persistence.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

MAX_EVENTS = 40
# One source emitting the exact same text again within this window is flapping
# (e.g. washer Unemptied->Emptied->Off lands in the same second), not news.
DUP_SUPPRESS_S = 300

APPLIANCE_ICONS = {
    "washer": "mdi:washing-machine",
    "dishwasher": "mdi:dishwasher",
    "dryer": "mdi:tumble-dryer",
}

# ---------------------------------------------------------------------------
# Pure text builders - module-level so the stdlib unittest gate can cover them
# without an AppDaemon runtime. Every returned text must be plain ASCII.
# ---------------------------------------------------------------------------


def appliance_event(name, old, new, attrs):
    """Event tuple (icon_key, text) for a semantic appliance-state transition, or None."""
    if old in (None, "unavailable", "unknown") or new == old:
        return None
    if new == "Running":
        return "started", f"{name} started"
    if new == "Unemptied":
        energy = attrs.get("energy_used")
        try:
            energy_txt = f" - used {float(energy):.2f} kWh" if energy not in (None, "") else ""
        except (TypeError, ValueError):
            energy_txt = ""
        return "finished", f"{name} finished{energy_txt}"
    if new == "Emptied":
        return "emptied", f"{name} emptied"
    return None


def ac_event(old, new, smart_status):
    """Event text for the AC thermostat flipping between off and an active hvac mode."""
    if old in (None, "unavailable", "unknown") or new in (None, "unavailable", "unknown") or new == old:
        return None
    was_on = old != "off"
    is_on = new != "off"
    if was_on == is_on:
        return None  # mode-to-mode change (cool->dry etc.) is detail, not a house decision
    if is_on:
        reason = f" - {smart_status}" if smart_status and smart_status not in ("idle", "off", "unknown", "unavailable") else ""
        return f"AC started{reason}"
    return "AC stopped"


def blind_event(old, new):
    if old in (None, "unavailable", "unknown") or new == old:
        return None
    if new == "closed":
        return "Bedroom blind closed"
    if new == "open" and old in ("closed", "opening", "closing"):
        return "Bedroom blind opened"
    return None


def lock_event(old, new):
    if old in (None, "unavailable", "unknown") or new == old:
        return None
    if new == "locked":
        return "Front door locked"
    if new == "unlocked":
        return "Front door unlocked"
    return None


def vacuum_event(old, new):
    if old in (None, "unavailable", "unknown") or new == old:
        return None
    if new == "cleaning":
        return "Rober2 started cleaning"
    if new == "docked" and old in ("returning", "cleaning", "paused"):
        return "Rober2 docked"
    if new == "error":
        return "Rober2 needs attention"
    return None


class HouseEvents(hass.Hass):
    def initialize(self):
        self.feed_entity = self.args.get("feed_entity", "sensor.house_events")
        self.events = self._load_previous_feed()

        self.appliances = self.args.get(
            "appliances",
            {
                "sensor.washer_state": "Washer",
                "sensor.dishwasher_state": "Dishwasher",
                "sensor.dryer_state": "Dryer",
            },
        )
        for entity in self.appliances:
            self.listen_state(self._on_appliance, entity)

        self.ac_entity = self.args.get("ac_entity", "climate.air_conditioner_thermostat")
        self.smart_cooling_status = self.args.get("smart_cooling_status", "sensor.smart_cooling_status")
        self.listen_state(self._on_ac, self.ac_entity)

        self.blind_entity = self.args.get("blind_entity", "cover.bedroom_blind")
        self.listen_state(self._on_blind, self.blind_entity)

        self.lock_entity = self.args.get("lock_entity", "lock.yale_bt")
        self.listen_state(self._on_lock, self.lock_entity)

        self.vacuum_entity = self.args.get("vacuum_entity", "vacuum.rober2")
        self.listen_state(self._on_vacuum, self.vacuum_entity)

        # Publish immediately so the dashboard has an entity to read even before
        # the first event (and so the restored feed reappears after a reload).
        self._publish()
        self.log(f"HouseEvents initialized - {len(self.events)} restored, feed at {self.feed_entity}")

    # -- callbacks ----------------------------------------------------------

    def _on_appliance(self, entity, attribute, old, new, kwargs):
        name = self.appliances.get(entity, entity)
        state_obj = self.get_state(entity, attribute="all") or {}
        result = appliance_event(name, old, new, state_obj.get("attributes") or {})
        if result:
            kind, text = result
            icon = APPLIANCE_ICONS.get(name.lower(), "mdi:washing-machine")
            if kind == "emptied":
                icon = "mdi:basket-check"
            self._add(icon, text)

    def _on_ac(self, entity, attribute, old, new, kwargs):
        status = self.get_state(self.smart_cooling_status)
        text = ac_event(old, new, status)
        if text:
            self._add("mdi:snowflake", text)

    def _on_blind(self, entity, attribute, old, new, kwargs):
        text = blind_event(old, new)
        if text:
            self._add("mdi:blinds", text)

    def _on_lock(self, entity, attribute, old, new, kwargs):
        text = lock_event(old, new)
        if text:
            self._add("mdi:lock" if new == "locked" else "mdi:lock-open-variant", text)

    def _on_vacuum(self, entity, attribute, old, new, kwargs):
        text = vacuum_event(old, new)
        if text:
            self._add("mdi:robot-vacuum", text)

    # -- feed management ----------------------------------------------------

    def _load_previous_feed(self):
        try:
            full = self.get_state(self.feed_entity, attribute="all") or {}
            events = (full.get("attributes") or {}).get("events")
            if isinstance(events, list):
                return [e for e in events if isinstance(e, dict) and e.get("ts") and e.get("text")][:MAX_EVENTS]
        except Exception as e:
            self.log(f"Could not restore previous feed: {e}", level="WARNING")
        return []

    def _add(self, icon, text):
        now = self.get_now()
        now_iso = now.isoformat()
        # Flap guard: identical text again within the window is noise, not news.
        for event in self.events:
            if event.get("text") != text:
                continue
            try:
                from datetime import datetime

                prev = datetime.fromisoformat(event["ts"])
                if (now - prev).total_seconds() < DUP_SUPPRESS_S:
                    return
            except (ValueError, TypeError, KeyError):
                pass
            break  # only the most recent occurrence matters
        self.events.insert(0, {"ts": now_iso, "icon": icon, "text": text})
        del self.events[MAX_EVENTS:]
        self._publish()
        self.log(f"Event: {text}")

    def _publish(self):
        try:
            self.set_state(
                self.feed_entity,
                state=self.events[0]["ts"] if self.events else "empty",
                attributes={
                    "friendly_name": "House events",
                    "icon": "mdi:history",
                    "events": self.events,
                },
                replace=True,
            )
        except Exception as e:
            self.log(f"Publish failed: {e}", level="WARNING")
