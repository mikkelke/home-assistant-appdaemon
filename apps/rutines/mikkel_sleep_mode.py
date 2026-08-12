"""
Mikkel sleep mode - Withings in-bed + phone battery + home, gated by the bedroom
actually being in sleep configuration.

Sleep mode ON: person.mikkel is home, any configured bedside in-bed sensor is on,
sensor.mikkels_ofx9p_battery_state is charging or not_charging, AND the night gate is
open: the bedroom blind covers the window OR the clock is inside 21:00-04:00.

The blind gate (2026-08-12, Mikkel's own words: "if I lay in bed and connect a charger
my phone will go to DND, that is mostly fine, but if the curtain is not up it does not
make sense"): cover.bedroom_blind is BOTTOM-UP - current_position 100 means fully
risen = COVERING the window = sleep configuration; ~38 is parked open. HA's open/
closed wording is inverted for it, so only current_position is trusted (>= 95 counts
as covering). A daytime in-bed charge with the blind parked open no longer flips
sleep mode + DND. The gate only arms ON; it never clears an already-on sleep mode
(the window ending at 04:00 must not wake the phone while he is still in bed).

Sleep mode OFF:
- While input_boolean.house_night_mode is ON (see apps/presence/house_night_mode.py):
  only three paths clear it, each explicit - out of bed sustained >= 12 min (bathroom
  trips are shorter: on 2026-08-07 he was out 04:14-04:44 and the instant clear
  cascaded into the whole house leaving night mode), battery discharging sustained
  >= 5 min, or person.mikkel explicitly away. Sensor dropouts (cloud bedsides,
  companion battery) hold state instead of clearing it.
- Otherwise (daytime): today's immediate behaviour, unchanged - discharging or any
  ON condition not met clears at once.

When sleep mode turns on/off, sends Companion command_dnd via notify.mobile_app_*
(HA Mobile App: https://companion.home-assistant.io/docs/notifications/notification-commands).

Works with wakeup_bedroom: that app turns off input_boolean.*_sleep_mode when the wake
light ramp starts. We listen for that so (1) DND is cleared on the phone, and (2) we do
not immediately turn sleep back on while still in bed + charging until Withings shows
out of bed.
"""

import json
import os
from datetime import datetime, time as dtime

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


def _in_window(t, start, end):
    """True when time-of-day t lies in [start, end), handling windows that wrap
    midnight (21:00-04:00)."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _parse_hhmm(value, fallback):
    try:
        parts = [int(p) for p in str(value).split(":")]
        return dtime(parts[0], parts[1] if len(parts) > 1 else 0)
    except (ValueError, IndexError, TypeError):
        return fallback


class MikkelSleepMode(hass.Hass):
    # Class-level defaults so bare __new__() test instances (the harness the older
    # tests in tests/test_mikkel_sleep_mode.py use) keep the legacy behaviour:
    # blind_entity/night_window None -> the ON gate is open, house_night_entity
    # None -> OFF stays immediate. initialize() sets the real values.
    blind_entity = None
    blind_covering_min = 95.0
    night_window = None
    house_night_entity = None
    out_of_bed_clear_seconds = 12 * 60
    discharging_clear_seconds = 5 * 60
    _out_of_bed_since = None
    _discharging_since = None

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

        # Night gate: ON requires the blind covering (bottom-up: position 100 =
        # covering) OR the clock inside the night window. See module docstring.
        self.blind_entity = self.args.get("blind_entity", "cover.bedroom_blind")
        self.blind_covering_min = float(self.args.get("blind_covering_min", 95))
        self.night_window = (
            _parse_hhmm(self.args.get("night_window_start", "21:00"), dtime(21, 0)),
            _parse_hhmm(self.args.get("night_window_end", "04:00"), dtime(4, 0)),
        )
        # OFF-debounce while the house is in night mode. See module docstring.
        self.house_night_entity = self.args.get(
            "house_night_entity", "input_boolean.house_night_mode"
        )
        self.out_of_bed_clear_seconds = int(
            self.args.get("out_of_bed_clear_minutes", 12)
        ) * 60
        self.discharging_clear_seconds = int(
            self.args.get("discharging_clear_minutes", 5)
        ) * 60
        self._out_of_bed_since = None
        self._discharging_since = None

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
        # The gate/debounce inputs must also trigger re-evaluation: the blind rising
        # at 20:45 or house night mode ending must not wait for a bed/battery event.
        if self.blind_entity:
            self.listen_state(
                self._on_relevant_change, self.blind_entity, attribute="current_position"
            )
        if self.house_night_entity:
            self.listen_state(self._on_relevant_change, self.house_night_entity)
        # Minutely tick: crossing 21:00 opens the gate and the night OFF-debounce
        # clocks expire with no state change to ride on (same pattern as
        # actor_attribution's reconcile tick).
        self.run_every(self._tick, "now+30", 60)

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

    def _tick(self, kwargs) -> None:
        try:
            self._apply_sleep_mode()
        except Exception as e:
            self.log(f"tick failed: {e}", level="ERROR")

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

    def _night_gate_open(self) -> bool:
        """ON-arming gate: the bedroom must be in sleep configuration - blind
        covering the window (bottom-up: current_position >= blind_covering_min) OR
        the clock inside the night window. Bare test instances with neither
        configured keep the legacy always-open gate. An unreadable blind position
        counts as not covering: only the time window can open the gate then."""
        if self.blind_entity is None and self.night_window is None:
            return True
        if self.night_window is not None and _in_window(
            self._local_time(), *self.night_window
        ):
            return True
        if self.blind_entity is not None:
            try:
                pos = self.get_state(self.blind_entity, attribute="current_position")
            except Exception:
                pos = None
            if pos is not None:
                try:
                    return float(pos) >= self.blind_covering_min
                except (TypeError, ValueError):
                    return False
        return False

    def _night_debounce_active(self) -> bool:
        if not self.house_night_entity:
            return False
        try:
            return self.get_state(self.house_night_entity) == "on"
        except Exception:
            return False

    def _apply_sleep_mode(self) -> None:
        # Before anything is decided, so a hold surviving a lost off edge self-heals on
        # the next callback instead of on the next restart.
        self._release_rearm_hold_if_out_of_bed("live re-check")

        battery = self.get_state(self.battery_entity)
        person = self.get_state(self.person_entity)
        in_bed = self._any_in_bed()

        want_on_raw = self._compute_want_on_raw()
        current = self.get_state(self.sleep_mode_entity)

        if current != "on":
            # The debounce clocks only ever run while sleep mode is on.
            self._out_of_bed_since = None
            self._discharging_since = None
            if (
                want_on_raw
                and not self._block_rearm_until_out_of_bed
                and self._night_gate_open()
            ):
                self.call_service(
                    "input_boolean/turn_on", entity_id=self.sleep_mode_entity
                )
                self.log(
                    f"Sleep mode ON (battery={battery!r}, person={person!r}, in_bed={in_bed})",
                    level="INFO",
                )
                self._send_dnd_command(self._dnd_on_command)
            return

        # Sleep mode is currently on.
        if want_on_raw:
            self._out_of_bed_since = None
            self._discharging_since = None
            return

        if not self._night_debounce_active():
            # Daytime: today's immediate behaviour, unchanged.
            self._turn_off_sleep(battery, person, in_bed, "immediate (house not in night mode)")
            return

        # House night mode: only three OFF paths, each explicit. Everything else
        # (cloud bedside dropouts, companion battery unknown/unavailable) holds.
        if person not in ("home", "unknown", "unavailable", None):
            self._turn_off_sleep(battery, person, in_bed, f"left home ({person!r})")
            return

        now = self._now_local()
        if battery == self._off_battery_state:
            if self._discharging_since is None:
                self._discharging_since = now
                self.log(
                    f"Discharging during house night mode - sleep mode clears in "
                    f">={self.discharging_clear_seconds // 60} min unless charging resumes",
                    level="INFO",
                )
        else:
            self._discharging_since = None

        if not in_bed:
            if self._out_of_bed_since is None:
                self._out_of_bed_since = now
                self.log(
                    f"Out of bed during house night mode - sleep mode clears in "
                    f">={self.out_of_bed_clear_seconds // 60} min unless he returns "
                    f"(bathroom trips are shorter - 2026-08-07 04:14)",
                    level="INFO",
                )
        else:
            self._out_of_bed_since = None

        if (
            self._discharging_since is not None
            and (now - self._discharging_since).total_seconds()
            >= self.discharging_clear_seconds
        ):
            self._turn_off_sleep(
                battery, person, in_bed,
                f"discharging sustained >={self.discharging_clear_seconds // 60} min",
            )
        elif (
            self._out_of_bed_since is not None
            and (now - self._out_of_bed_since).total_seconds()
            >= self.out_of_bed_clear_seconds
        ):
            self._turn_off_sleep(
                battery, person, in_bed,
                f"out of bed sustained >={self.out_of_bed_clear_seconds // 60} min",
            )
        # else: hold ON through the blip.

    def _turn_off_sleep(self, battery, person, in_bed, reason: str) -> None:
        self._out_of_bed_since = None
        self._discharging_since = None
        self.call_service("input_boolean/turn_off", entity_id=self.sleep_mode_entity)
        self.log(
            f"Sleep mode OFF - {reason} (battery={battery!r}, person={person!r}, "
            f"in_bed={in_bed})",
            level="INFO",
        )

    def _now_local(self):
        """Naive local datetime (repo idiom). Falls back to datetime.now() on a
        bare test instance."""
        try:
            return self.datetime()
        except Exception:
            return datetime.now()

    def _local_time(self):
        return self._now_local().time()

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
