"""HouseEvents - plain-English "why did the house just do that" feed for the dashboard.

Two kinds of entries (v2 - reshaped after user feedback that raw on/off logging of
things you can see/hear yourself has no value):

1. cause -> effect reports from the automation apps themselves, fired as a
   `house_events_report` AppDaemon event ("Living room TV button held" ->
   "TV moving to kitchen"; "Bedroom TV turned on" -> "TV lift going down").
   Event-based on purpose: no get_app coupling, no init-order dependency - an
   emitting app just fire_event()s and never notices whether this app is running.
2. A small set of observed appliance milestones that carry information you can't
   see from the machine itself (finished + energy used; emptied).
3. Observed lock activity (v3): "Front door unlocked by Mikkel" / "locked
   automatically". The authoritative BLE lock entity carries no attribution, so the
   entry is emitted after a short delay and enriched from the cloud twin's
   `changed_by` if that attribute is fresh by then - see `lock_event` and the
   `locks`/`lock_attribution` app config.

Feed shape (attributes, replace=True on every publish):
    events: newest-first list of {"ts": <UTC ISO>, "icon": <mdi:...>, "text": <ascii>,
                                  "cause": <ascii, optional>, "effect": <ascii, optional>}
State = ts of the newest event (cheap change signal for the dashboard).

Persistence (v3): entity first, state file second. The feed is re-read from the
entity on app reload (survives code deploys), and from `house_events_state.json`
next to this module when the entity is gone - AppDaemon set_state entities are
ephemeral across HA RESTARTS (see appdaemon-deploy notes), which used to wipe the
feed. The file is rewritten on every added event; deploy.sh syncs tracked files
only and never deletes, so the box's copy is safe.
"""

import json
import os
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

MAX_EVENTS = 40
# One source emitting the exact same text again within this window is flapping
# (e.g. washer Unemptied->Emptied->Off lands in the same second), not news.
DUP_SUPPRESS_S = 300
# Reports come from other apps but are still validated like external data - a bug
# elsewhere must not be able to bloat the feed entity past what a dashboard expects.
MAX_TEXT_LEN = 120

APPLIANCE_ICONS = {
    "washer": "mdi:washing-machine",
    "dishwasher": "mdi:dishwasher",
    "dryer": "mdi:tumble-dryer",
}

# ---------------------------------------------------------------------------
# Pure builders/validators - module-level so the stdlib unittest gate can cover
# them without an AppDaemon runtime. Every returned text must be plain ASCII.
# ---------------------------------------------------------------------------


def appliance_event(name, old, new, attrs):
    """Event tuple (icon_key, text) for an appliance milestone worth telling humans about.

    Only finished (+ energy - information the machine's own panel doesn't show) and
    emptied. Deliberately NOT "started": whoever started it knows, and the cards
    already show live progress.
    """
    if old in (None, "unavailable", "unknown") or new == old:
        return None
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


def sanitize_feed(events):
    """Validated copy of an events list from ANY persistence layer (entity attributes
    or the state file) - malformed entries dropped, newest-first order trusted (the
    writer's documented contract), hard-capped."""
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict) and e.get("ts") and e.get("text")][:MAX_EVENTS]


def lock_event(name, old, new, changed_by):
    """Event tuple (icon, text) for a lock transition, or None.

    `changed_by` is best-effort attribution (the cloud twin's attribute, already
    freshness-checked by the caller): a person name reads "by <name>", the
    integration's literal "Auto Lock" reads "automatically", and no/stale
    attribution just states the fact - a wrong name is worse than no name.
    """
    if old in (None, "unavailable", "unknown") or new == old:
        return None
    if new == "unlocked":
        verb = "unlocked"
        icon = "mdi:lock-open-variant"
    elif new == "locked":
        verb = "locked"
        icon = "mdi:lock"
    else:
        return None  # jammed/opening/unavailable - not a human-meaningful milestone
    text = f"{name} {verb}"
    if isinstance(changed_by, str) and changed_by.strip():
        by = changed_by.strip()[:MAX_TEXT_LEN]
        text = f"{text} automatically" if by == "Auto Lock" else f"{text} by {by}"
    return icon, text


def build_report_event(data):
    """Validated {icon, text, cause, effect} from a house_events_report payload, or None.

    `cause` and `effect` are required non-empty strings (that's the entire point of a
    report - explaining WHY); `icon` optional. Length-capped: a misbehaving emitter
    must not be able to bloat the feed entity.
    """
    if not isinstance(data, dict):
        return None
    cause = data.get("cause")
    effect = data.get("effect")
    if not isinstance(cause, str) or not cause.strip():
        return None
    if not isinstance(effect, str) or not effect.strip():
        return None
    cause = cause.strip()[:MAX_TEXT_LEN]
    effect = effect.strip()[:MAX_TEXT_LEN]
    icon = data.get("icon")
    if not isinstance(icon, str) or not icon.startswith("mdi:"):
        icon = "mdi:auto-fix"
    return {
        "icon": icon,
        "text": f"{cause} -> {effect}",
        "cause": cause,
        "effect": effect,
    }


class HouseEvents(hass.Hass):
    def initialize(self):
        self.feed_entity = self.args.get("feed_entity", "sensor.house_events")
        self._state_file = Path(self.args.get("state_file") or Path(__file__).with_name("house_events_state.json"))
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

        # Locks: listen to the authoritative (BLE) entity, attribute from the cloud twin.
        self.locks = self.args.get("locks", {})
        self.lock_attribution = self.args.get("lock_attribution", {})
        # The cloud twin lags the BLE entity by seconds; waiting this long before emitting
        # trades feed latency for "by <name>" actually being present. Also the freshness
        # ceiling: a changed_by older than delay+fresh margin belongs to some EARLIER
        # operation and must not be pinned on this one.
        self.lock_report_delay_s = int(self.args.get("lock_report_delay_s", 20))
        self.lock_attribution_fresh_s = int(self.args.get("lock_attribution_fresh_s", 180))
        for entity in self.locks:
            self.listen_state(self._on_lock, entity)

        # Automation apps explain themselves through this event - see module docstring.
        self.listen_event(self._on_report, "house_events_report")

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
            icon = "mdi:basket-check" if kind == "emptied" else APPLIANCE_ICONS.get(name.lower(), "mdi:washing-machine")
            self._add(icon, text)

    def _on_lock(self, entity, attribute, old, new, kwargs):
        # Emit later, not now: attribution (cloud changed_by) usually hasn't landed yet
        # when the BLE entity flips. lock_event() re-validates old/new at emit time.
        self.run_in(self._emit_lock_event, self.lock_report_delay_s, lock_entity=entity, old=old, new=new)

    def _emit_lock_event(self, kwargs):
        entity = kwargs["lock_entity"]
        name = self.locks.get(entity, entity)
        changed_by = self._fresh_changed_by(self.lock_attribution.get(entity))
        result = lock_event(name, kwargs["old"], kwargs["new"], changed_by)
        if result:
            icon, text = result
            self._add(icon, text)

    def _fresh_changed_by(self, attribution_entity):
        """The cloud twin's changed_by, or None when absent/stale (see lock_event's doc)."""
        if not attribution_entity:
            return None
        try:
            full = self.get_state(attribution_entity, attribute="all") or {}
            changed_by = (full.get("attributes") or {}).get("changed_by")
            last_updated = full.get("last_updated")
            if not changed_by or not last_updated:
                return None
            from datetime import datetime

            updated = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            age_s = (self.get_now() - updated).total_seconds()
            if 0 <= age_s <= self.lock_report_delay_s + self.lock_attribution_fresh_s:
                return changed_by
        except Exception as e:
            self.log(f"Lock attribution lookup failed for {attribution_entity}: {e}", level="DEBUG")
        return None

    def _on_report(self, event_name, data, kwargs):
        event = build_report_event(data)
        if event:
            self._add(event["icon"], event["text"], cause=event["cause"], effect=event["effect"])

    # -- feed management ----------------------------------------------------

    def _load_previous_feed(self):
        try:
            full = self.get_state(self.feed_entity, attribute="all") or {}
            events = sanitize_feed((full.get("attributes") or {}).get("events"))
            if events:
                return events
        except Exception as e:
            self.log(f"Could not restore previous feed from entity: {e}", level="WARNING")
        # Entity empty/gone = HA restarted since the last publish (set_state entities
        # are ephemeral) - fall back to the state file written on every added event.
        try:
            events = sanitize_feed(json.loads(self._state_file.read_text()).get("events"))
            if events:
                self.log(f"Feed restored from state file ({len(events)} events)")
            return events
        except FileNotFoundError:
            return []
        except Exception as e:
            self.log(f"Could not restore previous feed from state file: {e}", level="WARNING")
            return []

    def _add(self, icon, text, cause=None, effect=None):
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
        entry = {"ts": now_iso, "icon": icon, "text": text}
        if cause and effect:
            entry["cause"] = cause
            entry["effect"] = effect
        self.events.insert(0, entry)
        del self.events[MAX_EVENTS:]
        self._publish()
        self._write_state_file()
        self.log(f"Event: {text}")

    def _write_state_file(self):
        # tmp + os.replace so a crash mid-write can't leave a truncated file for the
        # next restore to choke on.
        try:
            tmp = self._state_file.with_name(self._state_file.name + ".tmp")
            tmp.write_text(json.dumps({"events": self.events}))
            os.replace(tmp, self._state_file)
        except Exception as e:
            self.log(f"State file write failed: {e}", level="WARNING")

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
