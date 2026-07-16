import appdaemon.plugins.hass.hassapi as hass # type: ignore

class GuestBathroomLights(hass.Hass):
    """
    Guest bathroom lighting control with presence detection, door awareness, and manual switch control.
    Uses PIR/presence sensor for presence-based light control.
    """

    def initialize(self):
        # --- Configuration ---
        self.light_entity = self.args.get("light_entity")
        self.raw_pir_sensor = self.args.get("raw_pir_sensor")
        self.door_sensor = self.args.get("door_sensor")
        self.switch_event_entity = self.args.get("switch_event_entity")
        self.adaptive_lighting_switch = self.args.get("adaptive_lighting_switch")

        self.log_level = self.args.get("verbosity_level", "normal")

        if not all([self.light_entity, self.raw_pir_sensor, self.door_sensor, self.switch_event_entity]):
            self.error("One or more critical entities are missing. App may not function correctly.")
        
        # Track if we're in bright white mode (internal state - more reliable than reading light attributes)
        self._bright_white_mode = False
        
        # Debounce button presses to prevent double-triggers
        self._last_button_press_time = 0
        self._button_debounce_ms = 300  # 300ms debounce
        
        # --- Listeners ---
        # Switch event handler (Zigbee2MQTT PTM 215Z)
        # Listen to event_type attribute changes (not state changes) for reliable event detection
        if self.switch_event_entity:
            self.listen_state(self._on_switch_event, self.switch_event_entity, attribute="event_type")
            if self.log_level == "debug":
                self.log(f"Listening for switch events (event_type attribute): {self.switch_event_entity}", level="DEBUG")
        
        if self.raw_pir_sensor:
            try:
                self.listen_state(self._on_raw_pir_trigger, self.raw_pir_sensor, old="off", new="on")
                self.listen_state(self._on_raw_pir_off, self.raw_pir_sensor, old="on", new="off")
                if self.log_level == "debug":
                    self.log(f"Listening for PIR/presence: {self.raw_pir_sensor}", level="DEBUG")
            except Exception as e:
                self.log(f"Error registering PIR/presence handler: {e}", level="ERROR")

        # Door sensor
        if self.door_sensor:
            self.listen_state(self._on_door_open, self.door_sensor, old="off", new="on")

        # Global mechanism: per-room manual override pauses ALL automatic light actions
        self.manual_override_entity = self.args.get("manual_override_boolean")
        if self.manual_override_entity:
            self.listen_state(self._on_manual_override_change, self.manual_override_entity)
            if self.log_level == "debug":
                self.log(f"Listening for door open events: {self.door_sensor}", level="DEBUG")
        
        # Initial check
        self.run_in(self._initial_check, 5)
        self.log("Guest bathroom lights app initialized", level="INFO")
    
    def _initial_check(self, kwargs=None):
        """Perform initial state check"""
        if self.log_level == "debug":
            self.log("Performing initial check of guest bathroom light state", level="DEBUG")
        self._check_lights()
    
    def _on_raw_pir_off(self, entity, attribute, old, new, kwargs):
        """Handle PIR/presence sensor OFF - re-evaluate lights."""
        try:
            self.log("Guest bathroom: Presence lost", level="INFO")
            self._check_lights()
        except Exception as e:
            self.log(f"Error in PIR off handler: {e}", level="ERROR")

    def _check_lights(self, kwargs=None):
        """Check and correct light state based on presence"""
        try:
            if self.manual_override_entity and self.get_state(self.manual_override_entity) == "on":
                return
            occupancy_state = self.get_state(self.raw_pir_sensor) if self.raw_pir_sensor else "off"
            light_state = self.get_state(self.light_entity)
            
            # If no occupancy, lights should be off
            if occupancy_state == "off":
                if light_state == "on":
                    self.turn_off(self.light_entity)
                    self._log_action("OFF", "no occupancy")
                    # Reset bright white mode when light turns off
                    self._bright_white_mode = False
                return
            
            # Occupancy is on - lights should be on (if not already)
            if light_state == "off":
                self.turn_on(self.light_entity)
                self._log_action("ON", "occupancy detected")
                # Reset bright white mode - auto-on uses adaptive lighting
                self._bright_white_mode = False
                # Trigger adaptive lighting recalculation
                self._apply_adaptive_lighting()
                
        except Exception as e:
            self.log(f"Error in _check_lights: {e}", level="ERROR")
    
    def _on_switch_event(self, entity, attribute, old, new, kwargs):
        """Handle switch events from Zigbee2MQTT PTM 215Z.
        
        SIMPLIFIED LOGIC using internal state tracking:
        - Button toggles between bright white (max cold temp) and adaptive lighting
        - Uses internal _bright_white_mode flag instead of unreliable color temp detection
        - Only responds to press_2 (ignores release_2)
        - Debounced to prevent double-triggers
        
        Now listens to event_type attribute changes for reliable detection.
        """
        try:
            # When listening to attribute changes, 'new' is the new attribute value
            event_type = new
            
            # Only respond to PRESS_2, not RELEASE_2 or other events
            if event_type != "press_2":
                if self.log_level == "debug":
                    self.log(f"Ignoring event: {event_type} (only handling press_2)", level="DEBUG")
                return
            
            # Debounce: ignore rapid successive presses
            now = self.datetime().timestamp()
            time_since_last = (now - self._last_button_press_time) * 1000  # Convert to ms
            if time_since_last < self._button_debounce_ms:
                if self.log_level == "debug":
                    self.log(f"Ignoring button press (debounce: {time_since_last:.0f}ms < {self._button_debounce_ms}ms)", level="DEBUG")
                return
            
            self._last_button_press_time = now
            
            # Log the button press for debugging
            if self.log_level == "debug":
                self.log(f"Button press_2 detected, current _bright_white_mode={self._bright_white_mode}", level="DEBUG")
            
            # Toggle between bright white and adaptive
            if self._bright_white_mode:
                # Currently bright white -> restore to adaptive
                self._bright_white_mode = False
                self._log_action("ON", "switch BL (restore to adaptive)")
                self._apply_adaptive_lighting()
            else:
                # Not bright white -> set to bright white (use light's max kelvin)
                self._bright_white_mode = True
                # Hass.turn_on -> homeassistant.turn_on rejects "kelvin"; light.turn_on uses color_temp_kelvin
                self.call_service(
                    "light/turn_on",
                    entity_id=self.light_entity,
                    color_temp_kelvin=4000,
                    brightness_pct=100,
                )
                self._log_action("ON", "switch BL (bright white)")
                    
        except Exception as e:
            self.log(f"Error in switch event handler: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")
    
    def _on_raw_pir_trigger(self, entity, attribute, old, new, kwargs):
        """Handle PIR/presence sensor ON - evaluate lights immediately."""
        try:
            self.log("Guest bathroom: Presence detected", level="INFO")
            self._check_lights()
        except Exception as e:
            self.log(f"Error in PIR trigger handler: {e}", level="ERROR")

    def _on_door_open(self, entity, attribute, old, new, kwargs):
        """Handle door open events"""
        try:
            if self.manual_override_entity and self.get_state(self.manual_override_entity) == "on":
                return
            self.log("Door opened - turning on light", level="INFO")
            
            # Turn on light if off
            if self.get_state(self.light_entity) == "off":
                self.turn_on(self.light_entity)
                self._log_action("ON", "door opened")
                # Reset bright white mode - auto-on uses adaptive lighting
                self._bright_white_mode = False
                # Trigger adaptive lighting recalculation
                self._apply_adaptive_lighting()
            else:
                if self.log_level == "debug":
                    self.log("Door opened but light already on", level="DEBUG")
                    
        except Exception as e:
            self.log(f"Error in door open handler: {e}", level="ERROR")
    
    def _apply_adaptive_lighting(self):
        """Trigger Adaptive Lighting to recalculate settings - OPTIMIZED FOR SPEED.
        
        Uses adaptive_lighting.set_manual_control service to clear manual control
        and force immediate recalculation. This is faster and more reliable than
        toggling the switch.
        """
        if not self.adaptive_lighting_switch or not self.light_entity:
            return
            
        try:
            # Ensure light is on first
            if self.get_state(self.light_entity) != "on":
                self.turn_on(self.light_entity)
            
            # Clear manual control flag and apply adaptive lighting - IMMEDIATE
            # This is the proper way to force adaptive lighting to recalculate
            self.call_service(
                "adaptive_lighting/set_manual_control",
                entity_id=self.adaptive_lighting_switch,
                lights=[self.light_entity],
                manual_control=False
            )
            
            # Apply adaptive lighting settings immediately
            self.call_service(
                "adaptive_lighting/apply",
                entity_id=self.adaptive_lighting_switch,
                lights=[self.light_entity],
                turn_on_lights=True
            )
            
            if self.log_level == "debug":
                self.log("Applied adaptive lighting (cleared manual control)", level="DEBUG")
                    
        except Exception as e:
            self.log(f"Error applying adaptive lighting: {e}", level="ERROR")
    
    def _log_action(self, action, reason=""):
        """Unified logging for light actions"""
        if self.log_level == "quiet":
            return
        reason_str = f": {reason}" if reason else ""
        self.log(f"Guest Bathroom: Light {action}{reason_str}", level="INFO")


    def _on_manual_override_change(self, entity, attribute, old, new, kwargs):
        """Global mechanism: manual override toggle - pause/resume automatic lighting.

        On clearing, only re-evaluate an EMPTY room (cleanup: lights left burning) - while
        someone is in it, an instant re-evaluate would rearrange a scene the human just
        hand-set. Occupied rooms resume automatic control on the next natural trigger
        instead. Same rule as bedroom_lights."""
        if new == "on":
            self.log("Guest bathroom: manual override ON - automatic lighting paused", level="INFO")
        elif new == "off":
            try:
                occupied = self.get_state(self.raw_pir_sensor) == "on"
            except Exception:
                occupied = True  # fail toward not touching the lights
            if occupied:
                self.log("Guest bathroom: manual override OFF - room occupied, resuming on next trigger", level="INFO")
            else:
                self.log("Guest bathroom: manual override OFF - resuming automatic lighting", level="INFO")
                self._check_lights()
