import appdaemon.plugins.hass.hassapi as hass # type: ignore

import lighting_actions
import room_state_darkness

class BathroomLights(hass.Hass):
    """
    Simple bathroom lighting with presence-based control.
    
    Main Logic:
      - No presence -> all lights OFF
      - Presence + dark + sleep mode -> no auto-on (use manual switch)
      - Presence + dark -> all lights ON
      - Presence + bright -> turn off main lights (sun is sufficient; bath spot still handles dark corner)
    
    Key principle: Dark = auto-on, Bright = auto-off (matches family room / claudias room).
    Bath spot remains active in bright mode for the darker shower corner.
    
    Bath Sub-Logic (for the darker shower corner):
      - Bath presence + room is bright -> bath spot ON (corner is darker than rest)
      - Bath presence lost + room is bright -> bath spot OFF
      - Room is dark -> bath spot follows main lights (part of all_spots_group)
    
    Switch is independent toggle.
    Uses centralized darkness calculator for consistency.
    """

    def initialize(self):
        # --- Configuration ---
        self.all_spots_group = self.args.get("all_spots_group")
        self.main_5_spots_group = self.args.get("main_5_spots_group")
        self.bath_spot_light = self.args.get("bath_spot_light")

        self.raw_pir_sensor = self.args.get("raw_pir_sensor")
        self.bathroom_door_sensor = self.args.get("bathroom_door_sensor")
        self.specific_bath_presence_entity = self.args.get("specific_bath_presence_entity")

        self.switch_device_id = self.args.get("switch_device_id") 
        # Centralized, human-readable room state published by darkness_calculator
        self.room_state_text_entity = self.args.get("room_state_text_entity", "sensor.room_state_bathroom")
        self.darkness_confirmed_sensor = self.args.get("darkness_confirmed_sensor_entity")
        self.mikkel_sleep_entity = self.args.get("mikkel_sleep_entity")
        self.bedroom_blind_entity = self.args.get("bedroom_blind_entity")
        # Global mechanism: per-room manual override pauses ALL automatic light actions
        self.manual_override_entity = self.args.get("manual_override_boolean")

        if not all([self.all_spots_group, self.main_5_spots_group, self.bath_spot_light,
                    self.raw_pir_sensor, self.specific_bath_presence_entity,
                    self.switch_device_id,
                    self.mikkel_sleep_entity]):
            self.error("One or more critical entities are missing. App may not function correctly.")

        # Logging configuration
        self.log_level = self.args.get("verbosity_level", "normal")

        self._last_off_is_dark = None

        # --- Listeners ---
        if self.switch_device_id:
            self.listen_event(self._on_switch_event,
                              "zwave_js_value_notification",
                              device_id=self.switch_device_id,
                              command_class=91,
                              property_key="001",
                              value="KeyPressed")

        if self.specific_bath_presence_entity:
            self.listen_state(self._on_bath_presence_change, self.specific_bath_presence_entity)

        if self.raw_pir_sensor:
            self.listen_state(
                self._on_raw_pir_on,
                self.raw_pir_sensor,
                old="off",
                new="on",
            )

        if self.bathroom_door_sensor:
            self.listen_state(
                self._on_bathroom_door_open,
                self.bathroom_door_sensor,
                old="off",
                new="on",
            )

        lighting_actions.register_room_state_push_listeners(
            self,
            self._on_room_state_push,
            room_state_entity=self.room_state_text_entity,
            darkness_sensor=self.darkness_confirmed_sensor,
        )

        if self.manual_override_entity:
            self.listen_state(self._on_manual_override_change, self.manual_override_entity)

        self.run_in(lambda _: self._on_room_state_push(None, None, None, None, {}), 5)
        self.log("Bathroom: App initialized", level="INFO")

    def _initial_check(self, kwargs=None):
        """Initial state check on startup."""
        self._evaluate_main_lights("INITIAL_CHECK")
        self._evaluate_bath_spot("INITIAL_CHECK")

    def _lighting_decisions(self, trigger: str = ""):
        sensor = self.darkness_confirmed_sensor
        on_d = room_state_darkness.evaluate_auto_on(
            self,
            self.room_state_text_entity,
            default_dark=True,
            darkness_sensor=sensor,
        )
        off_d = room_state_darkness.evaluate_auto_off(
            self,
            self.room_state_text_entity,
            default_dark=True,
            darkness_sensor=sensor,
        )
        if trigger == "PIR_PRESENCE_ON" and not on_d.is_dark:
            self.log(
                f"Bathroom: {room_state_darkness.format_decision_log(on_d)} - skip auto-on",
                level="INFO",
            )
        return on_d, off_d

    def _is_local_occupied(self) -> bool:
        """Bathroom occupancy based only on bathroom sensors (no shared-zone occupancy)."""
        try:
            if self.raw_pir_sensor and self.get_state(self.raw_pir_sensor) == "on":
                return True
            if (
                self.specific_bath_presence_entity
                and self.get_state(self.specific_bath_presence_entity) == "on"
            ):
                return True
        except Exception:
            pass
        return False

    def _is_bright_fast(self, trigger: str, off_decision) -> bool:
        """
        Fast bright decision for bath-spot-only logic.

        We keep strict confirmed-bright for global main-light auto-off, but allow
        pending_target=bright as a quick hint for motion/door triggers so bath
        corner light can react without waiting for full confirmation.
        """
        if not off_decision.is_dark:
            return True
        if trigger not in ("PIR_ON", "DOOR_OPEN", "BATH_PRESENCE_CHANGE"):
            return False
        try:
            pending = self.get_state(self.room_state_text_entity, attribute="pending_target")
            if isinstance(pending, str) and pending.lower() == "bright":
                if self.log_level == "debug":
                    self.log(
                        "Bathroom: Using pending_target=bright for fast bath-spot decision",
                        level="DEBUG",
                    )
                return True
        except Exception:
            pass
        return False

    def _on_switch_event(self, event_name, data, kwargs):
        """Handle manual switch press - independent toggle."""
        self.log("Bathroom: Switch pressed - toggling all lights", level="INFO")
        self.toggle(self.all_spots_group)

    def _on_bath_presence_change(self, entity, attribute, old, new, kwargs):
        """Handle bath area presence - SUB-LOGIC only for bath spot."""
        self._evaluate_bath_spot("BATH_PRESENCE_CHANGE")

    def _on_raw_pir_on(self, entity, attribute, old, new, kwargs):
        """Fast path: PIR on before ``room_state`` push (calculator may lag). Dark/bright still from push."""
        self._evaluate_main_lights("PIR_ON")
        self._evaluate_bath_spot("PIR_ON")

    def _on_bathroom_door_open(self, entity, attribute, old, new, kwargs):
        """Entering from bedroom - lights should not wait on ``room_state`` alone."""
        self._evaluate_main_lights("DOOR_OPEN")
        self._evaluate_bath_spot("DOOR_OPEN")

    def _on_room_state_push(self, entity, attribute, old, new, kwargs):
        try:
            _, off_d = self._lighting_decisions(attribute or "state")
            if self._last_off_is_dark is not None and off_d.is_dark != self._last_off_is_dark:
                self.log(
                    f"Bathroom: Room is {'dark' if off_d.is_dark else 'bright'} [{off_d.rule}]",
                    level="INFO",
                )
            self._last_off_is_dark = off_d.is_dark
            self._evaluate_main_lights(attribute or "state")
            self._evaluate_bath_spot(attribute or "state")
        except Exception as e:
            self.log(f"Bathroom: room_state push: {e}", level="ERROR")

    # ─────────────────────────────────────────────────────────────
    # MAIN LOGIC - Simple decision tree (like family room)
    # ─────────────────────────────────────────────────────────────
    
    def _evaluate_main_lights(self, trigger="UNKNOWN"):
        """
        Main lighting decision tree - simple and stable.

        Decision:
          - No presence -> lights OFF
          - Presence + dark + sleep mode -> no auto-on of mains (manual switch if needed)
          - Presence + dark -> all lights ON
          - Presence + bright -> mains OFF when on (natural light sufficient); bath spot
            may still turn on separately for the shower corner via _evaluate_bath_spot

        Bright/dark from darkness_calculator (``sensor.room_state_*`` / ``sensor.darkness_*``).

        We do not auto-on mains when already bright. When the room becomes bright while
        someone is present and mains were on, mains go off; corner lighting is handled
        by bath-spot logic, not by keeping all spots on.
        """
        try:
            if lighting_actions.manual_override_active(self, self.manual_override_entity):
                return
            sleep_mode = self._is_sleep_mode_active()
            lights_on = self._are_any_lights_on()
            occupied = self._is_local_occupied()
            if trigger in ("PIR_ON", "DOOR_OPEN"):
                occupied = True
            lighting_actions.apply_global_lighting(
                self,
                room_state_entity=self.room_state_text_entity,
                darkness_sensor=self.darkness_confirmed_sensor,
                default_dark=True,
                lights_on=lights_on,
                turn_on=self._turn_on_all,
                turn_off=self._turn_off_all,
                occupied=occupied,
                block_auto_on=sleep_mode,
                log_fn=lambda m: self.log(f"Bathroom: {m}", level="INFO"),
                # Bright shutoff targets mains only: the bath spot is the bath
                # sub-logic's to manage, and killing it here strands the shower
                # corner dark (all-group off + stale-state read skips the re-on).
                bright_lights_on=self.get_state(self.main_5_spots_group) == "on",
                turn_off_bright=self._turn_off_mains,
            )
        except Exception as e:
            self.log(f"Error in main light evaluation: {e}", level="ERROR")

    # ─────────────────────────────────────────────────────────────
    # BATH SUB-LOGIC - For the darker shower corner when room is bright
    # ─────────────────────────────────────────────────────────────
    
    def _evaluate_bath_spot(self, trigger="UNKNOWN"):
        # Global mechanism: manual override pauses the bath-spot sub-logic too
        if lighting_actions.manual_override_active(self, self.manual_override_entity):
            return
        """
        Bath spot sub-logic - for the darker shower corner.
        
        Purpose: The shower corner is darker than the rest of the bathroom.
        When the room is bright (sunny) and main lights are off, someone in
        the shower still needs the bath spot for adequate light.
        
        When room is dark: bath spot is part of all_spots_group, follows main lights.
        When room is bright: bath spot provides light for the darker corner.
        """
        try:
            # Check if someone is in the bath/shower area
            bath_presence = self.get_state(self.specific_bath_presence_entity) == "on"
            
            _, off_d = self._lighting_decisions(trigger)
            is_bright = self._is_bright_fast(trigger, off_d)
            
            # Check if main lights are on
            main_lights_on = self.get_state(self.main_5_spots_group) == "on"
            
            # Check current bath spot state
            bath_spot_on = self.get_state(self.bath_spot_light) == "on"
            
            # Only manage bath spot independently when room is bright
            # When dark, bath spot follows main lights (part of all_spots_group)
            if is_bright and not main_lights_on:
                # Room is bright, main lights are off
                if bath_presence:
                    # Someone in shower corner -> need bath spot (corner is darker)
                    if not bath_spot_on:
                        self.log("Bathroom: Bath spot ON (bright room, dark corner)", level="INFO")
                        self.turn_on(self.bath_spot_light)
                        # audience=admin (user 2026-07-16): this is Mikkel's own bathroom - the
                        # housemates use the guest bathroom, so its hidden-logic moments are his.
                        try:
                            self.fire_event(
                                "house_events_report",
                                cause="Bright room, darker shower corner",
                                effect="Bath spot turned on",
                                icon="mdi:shower-head",
                                audience="admin",
                            )
                        except Exception:
                            pass
                else:
                    # No one in shower corner -> turn off bath spot
                    if bath_spot_on:
                        self.log("Bathroom: Bath spot OFF (no bath presence)", level="INFO")
                        self.turn_off(self.bath_spot_light)
            # When dark or main lights on: bath spot follows main lights (group handles it)
            
        except Exception as e:
            self.log(f"Error in bath spot evaluation: {e}", level="ERROR")

    # ─────────────────────────────────────────────────────────────
    # HELPER METHODS
    # ─────────────────────────────────────────────────────────────
    
    def _turn_on_all(self):
        """Turn on all bathroom lights."""
        try:
            self.turn_on(self.all_spots_group)
        except Exception as e:
            self.log(f"Error turning on lights: {e}", level="ERROR")

    def _turn_off_all(self):
        """Turn off all bathroom lights."""
        try:
            self.turn_off(self.all_spots_group)
        except Exception as e:
            self.log(f"Error turning off lights: {e}", level="ERROR")

    def _turn_off_mains(self):
        """Turn off the 5 main spots only — bath spot stays with the bath sub-logic.

        Only reached from the confirmed-bright auto-off path (``turn_off_bright``), so a
        successful off here IS the "daylight is enough" decision - reported to the Home
        activity feed because lights going off while you're in the room is exactly the
        kind of house action that looks like a bug until it's explained. audience=admin
        (user 2026-07-16): this is Mikkel's own bathroom, same scoping as its overrides."""
        try:
            was_on = self.get_state(self.main_5_spots_group) == "on"
            self.turn_off(self.main_5_spots_group)
            if was_on:
                try:
                    self.fire_event(
                        "house_events_report",
                        cause="Bathroom is bright from daylight",
                        effect="Ceiling spots turned off",
                        icon="mdi:white-balance-sunny",
                        audience="admin",
                    )
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Error turning off main lights: {e}", level="ERROR")

    def _are_any_lights_on(self) -> bool:
        """Check if any bathroom lights are on."""
        try:
            if self.get_state(self.main_5_spots_group) == "on":
                return True
            if self.get_state(self.bath_spot_light) == "on":
                return True
        except Exception:
            pass
        return False

    def _is_sleep_mode_active(self) -> bool:
        """Check if sleep mode is active (prevents auto-on)."""
        try:
            if self.get_state(self.mikkel_sleep_entity) == "on":
                return True
            # Bedroom blind fully closed = sleep proxy
            if self.bedroom_blind_entity:
                try:
                    blind_pos = int(self.get_state(self.bedroom_blind_entity, attribute="current_position") or 0)
                    if blind_pos >= 100:
                        return True
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass
        return False


    def _on_manual_override_change(self, entity, attribute, old, new, kwargs):
        """Global mechanism: manual override toggle - pause/resume automatic lighting.

        On clearing, only re-evaluate an EMPTY room (cleanup: lights left burning) - while
        someone is in it, an instant re-evaluate would rearrange a scene the human just
        hand-set (the 12 h timeout can expire mid-shower). Occupied rooms resume automatic
        control on the next natural trigger instead. Same rule as bedroom_lights."""
        if new == "on":
            self.log("Bathroom: manual override ON - automatic lighting paused", level="INFO")
        elif new == "off":
            try:
                occupied = self._is_local_occupied()
            except Exception:
                occupied = True  # fail toward not touching the lights
            if occupied:
                self.log("Bathroom: manual override OFF - room occupied, resuming on next trigger", level="INFO")
            else:
                self.log("Bathroom: manual override OFF - resuming automatic lighting", level="INFO")
                self._evaluate_main_lights("OVERRIDE_CLEARED")
                self._evaluate_bath_spot("OVERRIDE_CLEARED")
