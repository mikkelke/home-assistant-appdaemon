"""
Mikkel sleep mode - Withings in-bed + phone battery + home.

Sleep mode ON: person.mikkel is home, any configured bedside in-bed sensor is on, and
sensor.mikkels_ofx9p_battery_state is charging or not_charging.

Sleep mode OFF: battery state is discharging, or the ON conditions are not met.

When sleep mode turns on/off, sends Companion command_dnd via notify.mobile_app_*
(HA Mobile App: https://companion.home-assistant.io/docs/notifications/notification-commands).

Works with wakeup_bedroom: that app turns off input_boolean.*_sleep_mode when the wake
light ramp starts. We listen for that so (1) DND is cleared on the phone, and (2) we do
not immediately turn sleep back on while still in bed + charging until Withings shows
out of bed.
"""

import json
import os

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class MikkelSleepMode(hass.Hass):
    def initialize(self) -> None:
        self.battery_entity = self.args.get(
            "battery_entity", "sensor.mikkels_ofx9p_battery_state"
        )
        self.person_entity = self.args.get("person_entity", "person.mikkel")
        raw_in_bed = self.args.get("in_bed_entities")
        if raw_in_bed:
            self.in_bed_entities = list(raw_in_bed)
        else:
            legacy = self.args.get(
                "in_bed_entity", "binary_sensor.left_bedside"
            )
            self.in_bed_entities = [legacy] if legacy else []
        self.sleep_mode_entity = self.args.get(
            "sleep_mode_entity", "input_boolean.mikkel_sleep_mode"
        )

        notify_service = self.args.get(
            "notify_service", "notify.mobile_app_mikkels_ofx9p"
        )
        if notify_service.startswith("notify."):
            self._notify_service_path = notify_service.replace("notify.", "notify/", 1)
        else:
            self._notify_service_path = notify_service

        self._dnd_on_command = self.args.get("dnd_on_command", "priority_only")
        # Set to null in YAML to skip DND when leaving sleep mode
        self._dnd_off_command = self.args.get("dnd_off_command", "off")

        self._on_battery_states = frozenset(
            self.args.get("on_battery_states", ["charging", "not_charging"])
        )
        self._off_battery_state = self.args.get("off_battery_state", "discharging")

        # After wakeup_bedroom (or manual HA) turns sleep off while sensors still say "asleep".
        # Persisted (deploy_advisor's path convention + house_events' atomic tmp+replace
        # write - see _load_state/_save_state): an AD restart must not forget this and let
        # sleep mode silently re-arm mid-wake-up while Withings/bedside is still "in bed".
        self.state_file = self.args.get(
            "state_file", "/conf/apps/rutines/mikkel_sleep_mode_state.json"
        )
        self._state = self._load_state()
        self._block_rearm_until_out_of_bed = bool(
            self._state.get("block_rearm_until_out_of_bed", False)
        )
        # A restored hold must be reconciled against the bed right now: its clear path is
        # a live off edge, and Mikkel routinely gets out of bed while AD is down (deploy,
        # HA restart). Without this the hold outlives the night it belongs to. Only an
        # explicit "off" on every bedside releases it here - at init the cloud bedsides are
        # regularly unavailable/loading, and that must keep the hold, not clear it.
        self._release_rearm_hold_if_out_of_bed("restored at startup")

        for entity_id in (
            self.battery_entity,
            self.person_entity,
            *self.in_bed_entities,
        ):
            self.listen_state(self._on_relevant_change, entity_id)

        # Nested callback so a partial deploy cannot reference a missing class method
        def _on_sleep_boolean_change(entity, attribute, old, new, kwargs):
            if new != "off":
                return
            if self._dnd_off_command:
                self._send_dnd_command(self._dnd_off_command)
            if old == "on" and self._compute_want_on_raw():
                self._set_block_rearm_until_out_of_bed(True)
                self.log(
                    "Sleep boolean cleared while sensors still allow sleep - "
                    "holding re-arm until out of bed (e.g. wakeup routine)",
                    level="INFO",
                )

        self.listen_state(_on_sleep_boolean_change, self.sleep_mode_entity)

        self.log(
            f"MikkelSleepMode: watching {self.battery_entity}, "
            f"{self.person_entity}, {self.in_bed_entities}",
            level="INFO",
        )

    def _any_in_bed(self) -> bool:
        for ent in self.in_bed_entities:
            try:
                if self.get_state(ent) == "on":
                    return True
            except Exception:
                pass
        return False

    def _bed_reads_empty(self) -> bool:
        """Every bedside explicitly "off" - an EXPLICIT negative, never merely "not on".
        The Withings bedsides are cloud-backed: unknown/unavailable/None is what they read
        while HA restarts or the integration reloads, which is exactly when this app
        re-initializes. Treating that as "he got up" would drop the hold at the one moment
        it is needed most."""
        if not self.in_bed_entities:
            return False
        for ent in self.in_bed_entities:
            try:
                if self.get_state(ent) != "off":
                    return False
            except Exception:
                return False
        return True

    def _on_relevant_change(self, entity, attribute, old, new, kwargs) -> None:
        self._apply_sleep_mode()

    def _release_rearm_hold_if_out_of_bed(self, context: str) -> None:
        """The hold only means "do not re-arm while he is still lying there", so any read of
        an explicitly empty bed releases it. Not tied to an off EDGE on purpose: the cloud
        bedsides pass off->unknown->on, so the edge is routinely lost and a hold that waits
        for one would block sleep mode plus phone DND indefinitely. Not tied to "not in bed"
        either - see _bed_reads_empty; an unreadable bed leaves the hold exactly as it is."""
        if not self._block_rearm_until_out_of_bed or not self._bed_reads_empty():
            return
        self._set_block_rearm_until_out_of_bed(False)
        self.log(
            f"Bed reads empty - releasing the sleep-mode re-arm hold ({context})",
            level="INFO",
        )

    def _set_block_rearm_until_out_of_bed(self, value: bool) -> None:
        self._block_rearm_until_out_of_bed = value
        self._state["block_rearm_until_out_of_bed"] = value
        self._save_state()

    def _compute_want_on_raw(self) -> bool:
        battery = self.get_state(self.battery_entity)
        person = self.get_state(self.person_entity)
        in_bed = self._any_in_bed()
        if battery == self._off_battery_state:
            return False
        return (
            person == "home"
            and in_bed
            and battery in self._on_battery_states
        )

    def _apply_sleep_mode(self) -> None:
        # Before anything is decided, so a hold surviving a lost off edge self-heals on
        # the next callback instead of on the next restart.
        self._release_rearm_hold_if_out_of_bed("live re-check")

        battery = self.get_state(self.battery_entity)
        person = self.get_state(self.person_entity)
        in_bed = self._any_in_bed()

        want_on_raw = self._compute_want_on_raw()
        want_on = want_on_raw and not self._block_rearm_until_out_of_bed

        current = self.get_state(self.sleep_mode_entity)
        desired = "on" if want_on else "off"

        if current == desired:
            return

        if want_on:
            self.call_service("input_boolean/turn_on", entity_id=self.sleep_mode_entity)
            self.log(
                f"Sleep mode ON (battery={battery!r}, person={person!r}, in_bed={in_bed})",
                level="INFO",
            )
            self._send_dnd_command(self._dnd_on_command)
        else:
            self.call_service(
                "input_boolean/turn_off", entity_id=self.sleep_mode_entity
            )
            self.log(
                f"Sleep mode OFF (battery={battery!r}, person={person!r}, in_bed={in_bed})",
                level="INFO",
            )

    def _send_dnd_command(self, command: str) -> None:
        if not command:
            return
        try:
            self.call_service(
                self._notify_service_path,
                message="command_dnd",
                data={"command": command},
            )
            self.log(
                f"DND notify {self._notify_service_path!r} command_dnd -> {command!r}",
                level="INFO",
            )
        except Exception as e:
            self.log(
                f"DND notify failed ({self._notify_service_path}, command={command!r}): {e}",
                level="WARNING",
            )

    # ---------- restart survival (state file) ----------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._state, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"state save failed: {e}", level="WARNING")
