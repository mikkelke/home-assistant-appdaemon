"""
RoomActive (Wave 1, shadow-only) - publishes binary_sensor.<zone>_active per configured
zone, unioning a small set of tiered "witness" entities (room mmWave, spot sensors, bed
mats/strip, PIR/motion channels) into one occupancy signal per zone.

This is the first of two Wave-1 pieces in the room-active presence-unification plan: this
app publishes, room_active_read.py reads (with its own live-recompute fallback). Nothing in
the existing codebase consumes binary_sensor.<zone>_active yet - zero repoints, purely
additive - so this can run side by side with every existing composite (binary_sensor.
<zone>_pir_presence groups/templates) and presence_model.py's binary_sensor.presence_<room>
shadow for as long as needed before any consumer is repointed.

self.zones (dict[str, list[dict]], zone -> list of {"entity", "tier", "on_states"}) is the
single source of truth for zone membership - room_active_read.py's fallback path introspects
it directly via self.get_app("RoomActive").zones rather than duplicating the witness list.

State rule: a zone is "on" if ANY witness currently reads a state in its own on_states
(default ["on"]). A witness reading unavailable/unknown/None is INERT - it never asserts and
never denies. Tier priority when multiple witnesses assert at once (used to pick the single
witness named in `reason`/`tier`): bed > spot > room > channel - a mat/strip witness is
trusted over a room-level mmWave read, which in turn outranks a raw PIR/motion channel.

Timing: off->on is immediate (0s) - the instant any witness starts asserting where none did
before, republish right away. on->off requires the union to read "nothing asserting" for a
full settle_off_sec (default 15s) continuously before the entity's state actually flips to
"off" - a witness reasserting at any point during that window cancels the pending timer and
the state never touches "off". The `settling` attribute flips to "true" the moment the union
first reads nothing-asserting (published immediately, even though the state string itself
stays "on" until the timer completes), so a consumer/dashboard can see a zone is about to
clear before the hard state transition happens.

Publish contract, absolute: AppDaemon 4.5.13's set_state silently drops any attribute value
that is exactly None/False/0 (and empty containers are one bad accident away from becoming
falsy-adjacent bugs too, so this app avoids them on principle) - see bed_presence_compare.py's
module docstring or apps/lights/presence_model.py's history for the reference incident. Every
attribute here is therefore a non-empty string or a non-empty list of strings:
active_witnesses is ["<none>"] rather than [] when nothing asserts, witness_states is a list
of "<entity_id>=<state>" strings rather than a dict, and settling/stale/auto_on_ok are the
literal strings "true"/"false" rather than Python bools.

auto_on_ok is hardcoded "true" for every zone for now - wiring presence_trust.presence_suspect()
in for the zones it covers (kitchen/bedroom/bathroom ghost-interference rules) is a later phase.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

# Priority order for picking the single witness named in `reason`/`tier` when more than one
# is asserting at once - lower number wins. Any tier not listed here (config typo) sorts last.
_TIER_PRIORITY = {"bed": 0, "spot": 1, "room": 2, "channel": 3}
_HIGH_CONFIDENCE_TIERS = ("room", "spot", "bed")


class RoomActive(hass.Hass):
    # Class-level defaults so bare __new__() test instances are well-defined - same pattern
    # as HouseNightMode/ActorAttribution/BedPresenceCompare in this directory.
    zones: dict = {}
    settle_off_sec = 15
    heartbeat_sec = 60
    republish_after_sec = 120

    def initialize(self):
        a = self.args.get
        self.settle_off_sec = int(a("settle_off_sec", 15))
        self.heartbeat_sec = int(a("heartbeat_sec", 60))
        self.republish_after_sec = int(a("republish_after_sec", 120))
        self.zones = self._parse_zones(a("zones", {}))
        self._zone_state = {zone: self._fresh_zone_state() for zone in self.zones}

        for zone, witnesses in self.zones.items():
            for w in witnesses:
                self.listen_state(self._on_witness_change, w["entity"], zone=zone)

        self.listen_event(self._on_plugin_started, "plugin_started")
        # "now+N", not "now" - run_every(cb, "now", N) does NOT fire immediately (first call
        # is now+N, see appdaemon-deploy memory); moot here either way since the loop below
        # already does an immediate first publish, but this avoids the trap for the future.
        self.run_every(self._heartbeat, f"now+{self.heartbeat_sec}", self.heartbeat_sec)

        # Immediate first computation+publish at startup (in addition to the plugin_started
        # listener - plugin_started won't fire again on a normal app reload, but entities
        # must still be correct right away, and AD set_state entities vanish on every HA
        # restart so a fresh publish is never redundant).
        for zone in self.zones:
            self._evaluate(zone, force=True)

        self.log(
            f"RoomActive: publishing {len(self.zones)} zone(s): {sorted(self.zones)}",
            level="INFO",
        )

    # ---------- config parsing ----------

    @staticmethod
    def _parse_zones(raw):
        zones = {}
        for zone, cfg in (raw or {}).items():
            witnesses = []
            for w in (cfg or {}).get("witnesses", []) or []:
                witnesses.append(
                    {
                        "entity": w["entity"],
                        "tier": w["tier"],
                        "on_states": list(w["on_states"]) if w.get("on_states") else ["on"],
                    }
                )
            zones[str(zone)] = witnesses
        return zones

    @staticmethod
    def _fresh_zone_state():
        return {"state": None, "settle_timer": None, "last_cmp": None, "last_publish_time": None}

    # ---------- callbacks ----------

    def _on_witness_change(self, entity, attribute, old, new, kwargs):
        zone = kwargs.get("zone") if isinstance(kwargs, dict) else None
        if not zone:
            return
        try:
            self._evaluate(zone)
        except Exception as e:
            self.log(f"ROOMACTIVE recompute failed for {zone} ({entity}): {e}", level="ERROR")

    def _settle_fire(self, kwargs):
        zone = kwargs.get("zone") if isinstance(kwargs, dict) else None
        if not zone:
            return
        zstate = self._zone_state.get(zone)
        if zstate is not None:
            zstate["settle_timer"] = None
        try:
            self._evaluate(zone, timer_fired=True)
        except Exception as e:
            self.log(f"ROOMACTIVE settle-fire failed for {zone}: {e}", level="ERROR")

    def _heartbeat(self, kwargs):
        now = time.time()
        for zone in self.zones:
            zstate = self._zone_state.setdefault(zone, self._fresh_zone_state())
            last = zstate.get("last_publish_time")
            # Force a republish even with an unchanged payload once republish_after_sec has
            # elapsed - keeps computed_at fresh so the reader's staleness check keeps working.
            force = last is None or (now - last) >= self.republish_after_sec
            try:
                self._evaluate(zone, force=force)
            except Exception as e:
                self.log(f"ROOMACTIVE heartbeat failed for {zone}: {e}", level="ERROR")

    def _on_plugin_started(self, event_name, data, kwargs):
        self.log("RoomActive: plugin_started - republishing all zones", level="INFO")
        for zone in self.zones:
            try:
                self._evaluate(zone, force=True)
            except Exception as e:
                self.log(f"ROOMACTIVE plugin_started recompute failed for {zone}: {e}", level="ERROR")

    # ---------- evaluation ----------

    def _compute_live(self, zone):
        """Live union read - no timer/state mutation. Unreadable witnesses are inert:
        counted for `stale`, never for `active`."""
        witnesses = self.zones.get(zone) or []
        source_entities = [w["entity"] for w in witnesses]
        witness_states = []
        active = []  # [(tier, entity), ...] currently asserting, config order
        stale = False
        for w in witnesses:
            entity = w["entity"]
            on_states = w["on_states"]
            try:
                state = self.get_state(entity)
            except Exception:
                state = None
            witness_states.append(f"{entity}={state}")
            if state in (None, "unavailable", "unknown"):
                stale = True
                continue
            if state in on_states:
                active.append((w["tier"], entity))
        return {
            "source_entities": source_entities,
            "witness_states": witness_states,
            "active": active,
            "stale": stale,
        }

    def _attributes(self, live, settling):
        active = live["active"]
        if active:
            tier, entity = min(active, key=lambda pair: _TIER_PRIORITY.get(pair[0], 99))
            reason = f"{tier}:{entity}"
            confidence = "high" if tier in _HIGH_CONFIDENCE_TIERS else "medium"
        else:
            tier = "none"
            reason = "all_clear"
            confidence = "low"
        active_witnesses = [entity for _, entity in active] or ["<none>"]
        return {
            "reason": reason,
            "tier": tier,
            "confidence": confidence,
            # Phase 2 will wire in presence_trust.presence_suspect() for the zones it
            # covers (kitchen/bedroom/bathroom ghost-interference rules); every zone is
            # trusted unconditionally until then.
            "auto_on_ok": "true",
            "active_witnesses": active_witnesses,
            "source_entities": live["source_entities"],
            "witness_states": live["witness_states"],
            "settling": "true" if settling else "false",
            "stale": "true" if live["stale"] else "false",
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _evaluate(self, zone, timer_fired=False, force=False):
        """Recompute one zone and publish iff the payload (state or any attribute other
        than computed_at) actually changed, or `force` is set. Returns whether it published."""
        live = self._compute_live(zone)
        zstate = self._zone_state.setdefault(zone, self._fresh_zone_state())
        is_first = zstate["state"] is None

        if live["active"]:
            # Something asserts right now: off->on (or staying on) is always immediate,
            # and cancels any pending settle-off countdown.
            if zstate["settle_timer"] is not None:
                self.cancel_timer(zstate["settle_timer"])
                zstate["settle_timer"] = None
            new_state = "on"
            settling = False
        elif is_first:
            # Nothing to transition FROM yet (cold start) - publish "off" immediately,
            # no settle window for a state that was never "on".
            new_state = "off"
            settling = False
        elif zstate["state"] == "on":
            if timer_fired:
                # Full settle_off_sec elapsed with nothing asserting the whole time
                # (re-verified live just above) - now flip to off.
                new_state = "off"
                settling = False
            else:
                # Union just went (or still is) from asserting to not-asserting. Arm the
                # settle timer only once per settle window.
                if zstate["settle_timer"] is None:
                    zstate["settle_timer"] = self.run_in(
                        self._settle_fire, self.settle_off_sec, zone=zone
                    )
                new_state = "on"
                settling = True
        else:
            # Already published "off", steady state, nothing asserting - no-op.
            new_state = "off"
            settling = False

        attributes = self._attributes(live, settling)
        cmp_payload = {
            "state": new_state,
            **{k: v for k, v in attributes.items() if k != "computed_at"},
        }
        changed = is_first or cmp_payload != zstate["last_cmp"]

        if changed or force:
            self._publish(zone, new_state, attributes)
            zstate["last_cmp"] = cmp_payload
            zstate["last_publish_time"] = time.time()

        zstate["state"] = new_state
        return changed or force

    def _publish(self, zone, state, attributes):
        entity_id = f"binary_sensor.{zone}_active"
        try:
            self.set_state(entity_id, state=state, replace=True, attributes=dict(attributes))
        except Exception as e:
            self.log(f"ROOMACTIVE publish failed for {zone}: {e}", level="WARNING")
