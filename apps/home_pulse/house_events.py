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

Feed shape (attributes, replace=True on every publish):
    events: newest-first list of {"ts": <UTC ISO>, "icon": <mdi:...>, "text": <ascii>,
                                  "cause": <ascii, optional>, "effect": <ascii, optional>}
State = ts of the newest event (cheap change signal for the dashboard).

Persistence: the previous feed is re-read from the entity on app reload, so a code
deploy does not wipe it. An HA RESTART does wipe it - AppDaemon set_state entities
are ephemeral (see appdaemon-deploy notes); the feed simply rebuilds. Accepted for v2.
"""

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

    def _on_report(self, event_name, data, kwargs):
        event = build_report_event(data)
        if event:
            self._add(event["icon"], event["text"], cause=event["cause"], effect=event["effect"])

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
