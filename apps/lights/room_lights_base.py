"""
Base class for room lighting apps with common functionality.

Provides:
- Dual-sensor (PIR + AOD) occupancy handling
- Room state parsing from darkness_calculator
- Safe state access with timeout protection
- Common logging patterns
- Adaptive Lighting integration helpers

Usage:
    from room_lights_base import RoomLightsBase
    
    class MyRoomLights(RoomLightsBase):
        def initialize(self):
            super().initialize()
            # Room-specific setup
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import time

import lighting_actions
import room_state_darkness


class RoomLightsBase(hass.Hass):
    """Base class for room lighting apps with common functionality."""

    def initialize(self):
        """Initialize common state and configuration.
        
        Subclasses should call super().initialize() first, then add room-specific setup.
        """
        # --- Common Configuration ---
        self.log_level = self.args.get("verbosity_level", "normal")
        
        # Occupancy sensors (dual-sensor pattern)
        self.occupancy_entity = self.args.get("occupancy_entity")  # AOD sensor
        self.raw_pir_sensor = self.args.get("raw_pir_sensor")  # Raw PIR for fast response
        self.occupancy_timeout_seconds = int(self.args.get("occupancy_timeout_seconds", 45))
        
        # Committed dark/bright from darkness_calculator
        self.room_state_text_entity = self.args.get("room_state_text_entity")
        self.darkness_confirmed_sensor = self.args.get("darkness_confirmed_sensor_entity")
        
        # Sleep mode entities
        self.sleep_mode_entities = self.args.get("sleep_mode_entities", [
            "input_boolean.mikkel_sleep_mode",
            "input_boolean.kristine_sleep_mode"
        ])
        
        # Home lighting mode (centralized)
        self.home_lighting_mode_entity = self.args.get(
            "home_lighting_mode_entity", 
            "input_select.home_lighting_mode"
        )
        
        # Adaptive Lighting integration
        self.adaptive_lighting_switch = self.args.get("adaptive_lighting_switch")
        
        self._last_off_is_dark = None
        self._last_room_state = None
        self._pending_pir_trigger_handle = None
        
        # --- Setup Common Listeners ---
        self._setup_occupancy_listeners()
        self._setup_room_state_listener()

    # ─────────────────────────────────────────────────────────────
    # Occupancy Handling (Dual-Sensor Pattern)
    # ─────────────────────────────────────────────────────────────

    def _setup_occupancy_listeners(self):
        """Setup listeners for dual-sensor (PIR + AOD) pattern."""
        # Raw PIR for immediate triggers
        if self.raw_pir_sensor:
            try:
                self.listen_state(
                    self._on_raw_pir_trigger, 
                    self.raw_pir_sensor, 
                    old="off", 
                    new="on"
                )
                if self.log_level == "debug":
                    self.log(f"Listening for raw PIR triggers: {self.raw_pir_sensor}", level="DEBUG")
            except Exception as e:
                self.log(f"Error registering raw PIR handler: {e}", level="ERROR")
        
        # AOD sensor for state maintenance
        if self.occupancy_entity:
            self.listen_state(self._on_occupancy_change, self.occupancy_entity)
            if self.log_level == "debug":
                self.log(f"Listening for AOD occupancy: {self.occupancy_entity}", level="DEBUG")

    def _on_raw_pir_trigger(self, entity, attribute, old, new, kwargs):
        """Handle raw PIR sensor triggers for immediate light activation.
        
        Raw PIR provides fast response. AOD sensors maintain state.
        Sets timeout to verify AOD occupancy appears.
        """
        try:
            room_name = self._get_room_name()
            self.log(f"{room_name}: Raw PIR trigger - immediate light check", level="INFO")
            
            # Check if AOD already on (no timeout needed)
            if self.occupancy_entity and self._safe_get_state(self.occupancy_entity) == "on":
                self.log("AOD occupancy already on - no timeout needed", level="DEBUG")
                self._evaluate_lights("RAW_PIR_TRIGGER")
                return
            
            # Cancel existing timeout
            if self._pending_pir_trigger_handle:
                try:
                    self.cancel_timer(self._pending_pir_trigger_handle)
                except Exception:
                    pass
            
            # Immediate evaluation
            self._evaluate_lights("RAW_PIR_TRIGGER")
            
            # Set timeout for occupancy confirmation
            self._pending_pir_trigger_handle = self.run_in(
                self._on_occupancy_timeout,
                self.occupancy_timeout_seconds
            )
            self.log(f"Set {self.occupancy_timeout_seconds}s timeout for occupancy confirmation", level="DEBUG")
            
        except Exception as e:
            self.log(f"Error in raw PIR trigger handler: {e}", level="ERROR")

    def _on_occupancy_timeout(self, kwargs):
        """Handle timeout when AOD occupancy doesn't appear after PIR trigger."""
        try:
            # Check if occupancy is now on
            if self.occupancy_entity and self._safe_get_state(self.occupancy_entity) == "on":
                self.log("Occupancy timeout - but occupancy is now on", level="DEBUG")
                self._pending_pir_trigger_handle = None
                return
            
            room_name = self._get_room_name()
            self.log(
                f"{room_name}: Occupancy timeout - AOD not detected within "
                f"{self.occupancy_timeout_seconds}s",
                level="INFO"
            )
            
            self._pending_pir_trigger_handle = None
            self._evaluate_lights("OCCUPANCY_TIMEOUT")
            
        except Exception as e:
            self.log(f"Error in occupancy timeout handler: {e}", level="ERROR")

    def _on_occupancy_change(self, entity, attribute, old, new, kwargs):
        """Handle AOD occupancy changes (for state maintenance)."""
        try:
            if self.log_level == "debug":
                self.log(f"AOD occupancy changed: {old} -> {new}", level="DEBUG")
            
            if new == "on":
                # Cancel pending timeout - occupancy confirmed
                if self._pending_pir_trigger_handle:
                    try:
                        self.cancel_timer(self._pending_pir_trigger_handle)
                        self.log("Cancelled occupancy timeout - confirmed", level="DEBUG")
                    except Exception:
                        pass
                    self._pending_pir_trigger_handle = None
                self._evaluate_lights("OCCUPANCY_ON")
            else:
                self._evaluate_lights("OCCUPANCY_OFF")
                
        except Exception as e:
            self.log(f"Error in occupancy change handler: {e}", level="ERROR")

    # ─────────────────────────────────────────────────────────────
    # Room State / Darkness Handling
    # ─────────────────────────────────────────────────────────────

    def _setup_room_state_listener(self):
        """Setup listener for centralized room state from darkness_calculator."""
        if not self.room_state_text_entity:
            return
        try:
            lighting_actions.register_room_state_push_listeners(
                self,
                self._on_room_state_push,
                room_state_entity=self.room_state_text_entity,
                darkness_sensor=self.darkness_confirmed_sensor,
            )
            if self.log_level == "debug":
                self.log(f"Listening to room state push: {self.room_state_text_entity}", level="DEBUG")
        except Exception as e:
            self.log(f"Failed to listen to room state: {e}", level="ERROR")

    def _on_room_state_push(self, entity, attribute, old, new, kwargs):
        """Handle pushes from darkness_calculator."""
        try:
            off_d = room_state_darkness.evaluate_auto_off(
                self,
                self.room_state_text_entity,
                default_dark=True,
                darkness_sensor=self.darkness_confirmed_sensor,
            )
            if self._last_off_is_dark is not None and off_d.is_dark != self._last_off_is_dark:
                pretty = "dark" if off_d.is_dark else "bright"
                room_name = self._get_room_name()
                self.log(f"{room_name}: Room is {pretty} [{off_d.rule}]", level="INFO")
            self._last_off_is_dark = off_d.is_dark
            self._evaluate_lights(attribute or "state")
        except Exception as e:
            self.log(f"Error handling room state push: {e}", level="ERROR")

    def _parse_dark_from_room_state(self, state_text: str) -> bool:
        """Parse dark/bright from room state text.
        
        Returns True if dark, False if bright.
        Safe default: preserve existing flag or assume dark.
        """
        try:
            st = state_text.lower()
            if "dark" in st:
                return True
            if "bright" in st:
                return False
        except Exception:
            pass
        # Safe default
        return True

    def _is_dark_for_auto_on(self) -> bool:
        """Fresh auto-on brightness (no cache). Defaults to dark if unknown."""
        try:
            if self.room_state_text_entity:
                return room_state_darkness.evaluate_auto_on(
                    self,
                    self.room_state_text_entity,
                    default_dark=True,
                    darkness_sensor=self.darkness_confirmed_sensor,
                ).is_dark
        except Exception:
            pass
        return True

    def _is_dark_for_auto_off(self) -> bool:
        """Fresh auto-off brightness (no cache). Defaults to dark if unknown."""
        try:
            if self.room_state_text_entity:
                return room_state_darkness.evaluate_auto_off(
                    self,
                    self.room_state_text_entity,
                    default_dark=True,
                    darkness_sensor=self.darkness_confirmed_sensor,
                ).is_dark
        except Exception:
            pass
        return True

    def _is_dark(self) -> bool:
        """Backward-compatible alias for auto-on checks."""
        return self._is_dark_for_auto_on()

    def _is_occupied(self) -> bool:
        """Check if room is currently occupied."""
        if self.occupancy_entity:
            return self._safe_get_state(self.occupancy_entity) == "on"
        return False

    # ─────────────────────────────────────────────────────────────
    # Sleep Mode Helpers
    # ─────────────────────────────────────────────────────────────

    def _is_any_sleep_mode_active(self) -> bool:
        """Check if any person's sleep mode is active."""
        for entity in self.sleep_mode_entities:
            if self._safe_get_state(entity) == "on":
                return True
        return False

    def _is_everyone_sleeping(self) -> bool:
        """Check if everyone who is home is in sleep mode."""
        # Get people who are home
        try:
            home_state = self._safe_get_state("zone.home")
            if home_state == "0":
                return False  # No one home
            
            home_attrs = self.get_state("zone.home", attribute="persons")
            if not home_attrs:
                return False
            
            # Check each person's sleep mode
            people_sleeping = 0
            for person in home_attrs:
                sleep_entity = self._get_sleep_entity_for_person(person)
                if sleep_entity and self._safe_get_state(sleep_entity) == "on":
                    people_sleeping += 1
            
            return people_sleeping == len(home_attrs) and len(home_attrs) > 0
        except Exception:
            return False

    def _get_sleep_entity_for_person(self, person: str) -> str:
        """Map person entity to their sleep mode boolean."""
        mapping = {
            "person.kristine": "input_boolean.kristine_sleep_mode",
            "person.mikkel": "input_boolean.mikkel_sleep_mode",
        }
        return mapping.get(person)

    def _get_home_lighting_mode(self) -> str:
        """Get centralized home lighting mode."""
        mode = self._safe_get_state(self.home_lighting_mode_entity, default="normal")
        return mode if mode else "normal"

    # ─────────────────────────────────────────────────────────────
    # Safe State Access
    # ─────────────────────────────────────────────────────────────

    def _safe_get_state(self, entity_id, default=None, timeout_warning=False, max_wait=2.0):
        """Safely get state with timeout protection.
        
        Returns default if call fails or times out.
        """
        start = time.time()
        try:
            state = self.get_state(entity_id)
            elapsed = time.time() - start
            if elapsed > max_wait and timeout_warning:
                self.log(f"Slow get_state for {entity_id}: {elapsed:.2f}s", level="WARNING")
            return state
        except Exception as e:
            if timeout_warning:
                self.log(f"Error getting state for {entity_id}: {e}", level="WARNING")
            return default

    # ─────────────────────────────────────────────────────────────
    # Light Control Helpers
    # ─────────────────────────────────────────────────────────────

    def turn_on_if_off(self, entity_id, **kwargs):
        """Turn on entity if currently off."""
        if self._safe_get_state(entity_id) == "off":
            self.turn_on(entity_id, **kwargs)
            self._log_action("ON", entity_id)
            return True
        return False

    def turn_off_if_on(self, entity_id):
        """Turn off entity if currently on."""
        if self._safe_get_state(entity_id) == "on":
            self.turn_off(entity_id)
            self._log_action("OFF", entity_id)
            return True
        return False

    def turn_on_with_verify(self, entity_id, verify_delay=1.0, max_retries=2, **kwargs):
        """Turn on entity with verification and retry."""
        for attempt in range(max_retries + 1):
            self.turn_on(entity_id, **kwargs)
            
            if attempt < max_retries:
                # Wait and verify
                self.run_in(
                    lambda _, eid=entity_id, att=attempt, kw=kwargs: 
                        self._verify_light_on(eid, att, max_retries, **kw),
                    verify_delay
                )
                return
        
        self._log_action("ON", entity_id)

    def _verify_light_on(self, entity_id, attempt, max_retries, **kwargs):
        """Verify light is on after turn_on command."""
        if self._safe_get_state(entity_id) != "on":
            if attempt < max_retries:
                self.log(f"Retry {attempt + 1}: {entity_id} not on, retrying", level="DEBUG")
                self.turn_on(entity_id, **kwargs)
            else:
                self.log(f"Failed to turn on {entity_id} after {max_retries + 1} attempts", level="WARNING")

    def turn_off_with_verify(self, entity_id, verify_delay=1.5, max_retries=2):
        """Turn off entity with verification and retry."""
        self.turn_off(entity_id)
        self.run_in(
            lambda _, eid=entity_id, att=0, mr=max_retries: 
                self._verify_light_off(eid, att, mr),
            verify_delay
        )

    def _verify_light_off(self, entity_id, attempt, max_retries):
        """Verify light is off after turn_off command."""
        if self._safe_get_state(entity_id) != "off":
            if attempt < max_retries:
                self.log(f"Retry {attempt + 1}: {entity_id} not off, retrying", level="DEBUG")
                self.turn_off(entity_id)
                self.run_in(
                    lambda _, eid=entity_id, att=attempt + 1, mr=max_retries: 
                        self._verify_light_off(eid, att, mr),
                    1.0
                )
            else:
                self.log(f"Failed to turn off {entity_id} after {max_retries + 1} attempts", level="WARNING")

    # ─────────────────────────────────────────────────────────────
    # Adaptive Lighting Integration
    # ─────────────────────────────────────────────────────────────

    def _apply_adaptive_lighting(self):
        """Trigger Adaptive Lighting to apply current settings."""
        if self.adaptive_lighting_switch:
            try:
                # Turn off and on to trigger re-application
                self.call_service(
                    "adaptive_lighting/apply",
                    entity_id=self.adaptive_lighting_switch.replace(
                        "switch.adaptive_lighting_", 
                        "switch.adaptive_lighting_"
                    ),
                    lights=[],  # Apply to all managed lights
                    turn_on_lights=True
                )
            except Exception as e:
                self.log(f"Error applying adaptive lighting: {e}", level="DEBUG")

    # ─────────────────────────────────────────────────────────────
    # Logging
    # ─────────────────────────────────────────────────────────────

    def _log_action(self, action: str, entity_or_reason: str = "", extra: str = ""):
        """Unified logging for light actions."""
        if self.log_level == "quiet":
            return
        
        room_name = self._get_room_name()
        friendly = self._friendly_entity_name(entity_or_reason)
        
        if extra:
            self.log(f"{room_name}: {friendly} {action} - {extra}", level="INFO")
        else:
            self.log(f"{room_name}: {friendly} {action}", level="INFO")

    def _friendly_entity_name(self, entity_id: str) -> str:
        """Convert entity_id to friendly name."""
        if not entity_id or not isinstance(entity_id, str):
            return "Lights"
        
        if entity_id.startswith("light."):
            # Extract and humanize
            name = entity_id.split(".", 1)[1]
            name = name.replace("_", " ").title()
            return name
        
        return entity_id

    def _get_room_name(self) -> str:
        """Get room name for logging. Override in subclasses."""
        # Try to extract from class name or app name
        class_name = self.__class__.__name__
        if "Lights" in class_name:
            return class_name.replace("Lights", "").replace("_", " ").strip()
        return "Room"

    # ─────────────────────────────────────────────────────────────
    # Abstract Methods (override in subclasses)
    # ─────────────────────────────────────────────────────────────

    def _evaluate_lights(self, trigger: str = "UNKNOWN"):
        """Evaluate and apply light states. Override in subclasses.
        
        Args:
            trigger: What triggered the evaluation (for logging)
        """
        raise NotImplementedError("Subclasses must implement _evaluate_lights()")

