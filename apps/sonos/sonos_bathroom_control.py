import appdaemon.plugins.hass.hassapi as hass
import time
# import datetime # No longer needed

class SonosBathroomControl(hass.Hass):
    def initialize(self):
        """Initialize the app and set up listeners."""
        self.log("SonosBathroomControl Initializing")

        # Critical configurations
        self.device_id = self.args.get("device_id")
        self.media_player = self.args.get("media_player") # Bathroom Sonos
        self.master_speaker_input_select = self.args.get("master_speaker_input_select")

        # NEW: Load the friendly name map from YAML
        self.entity_friendly_name_map = self.args.get("entity_friendly_name_map", {})
        # NEW: Create the inverse map for friendly_name to entity_id
        self._friendly_to_entity_map = {v: k for k, v in self.entity_friendly_name_map.items()}

        if not self.device_id:
            self.error("'device_id' is missing. Z-Wave control disabled.")
        if not self.media_player:
            self.error("'media_player' (bathroom Sonos) is missing. App cannot control Sonos.")
            return 
        if not self.master_speaker_input_select:
            self.log("'master_speaker_input_select' is missing. Join functionality will be disabled.", level="WARNING")

        # Volume step
        raw_volume_step = self.args.get("volume_step")
        if raw_volume_step is None:
            self.error("'volume_step' is missing. Button volume control disabled.")
            self.volume_step = None
        else:
            try:
                self.volume_step = float(raw_volume_step)
            except ValueError:
                self.error(f"'volume_step' ('{raw_volume_step}') is invalid. Button volume control disabled.")
                self.volume_step = None

        # Z-Wave listener configurations
        self.zwave_command_class = self.args.get("zwave_command_class", 91)
        self.zwave_endpoint = self.args.get("zwave_endpoint", 0)
        self.zwave_property_key_volume_up = self.args.get("zwave_property_key_volume_up", "004")
        self.zwave_property_key_volume_down = self.args.get("zwave_property_key_volume_down", "003")
        self.zwave_event_value_pressed = self.args.get("zwave_event_value_pressed", "KeyPressed")
        self.zwave_event_value_held = self.args.get("zwave_event_value_held", "KeyHeldDown")

        # Bath presence configurations
        self.bath_presence_sensor = self.args.get("bath_presence_sensor")
        self._logically_present = None
        self.bath_presence_volume_boost = None
        self.presence_on_max_volume = None
        # Throttling for presence changes to prevent excessive callbacks
        self._last_presence_change_time = 0.0
        self._presence_throttle_seconds = 0.5  # Minimum seconds between presence change processing

        if self.bath_presence_sensor:
            raw_boost = self.args.get("bath_presence_volume_boost")
            if raw_boost is None:
                self.error(f"... 'bath_presence_volume_boost' is missing ... Presence-based volume boost disabled.")
            else:
                try:
                    self.bath_presence_volume_boost = float(raw_boost)
                except ValueError:
                    self.error(f"... 'bath_presence_volume_boost' ('{raw_boost}') invalid ... Presence-based volume boost disabled.")
            
            raw_max_volume = self.args.get("presence_on_max_volume")
            if raw_max_volume is None:
                self.error(f"... 'presence_on_max_volume' is missing ... Presence 'on' volume capping disabled.")
            else:
                try:
                    self.presence_on_max_volume = float(raw_max_volume)
                except ValueError:
                    self.error(f"... 'presence_on_max_volume' ('{raw_max_volume}') invalid ... Presence 'on' volume capping disabled.")

            try:
                current_presence_state = self.get_state(self.bath_presence_sensor)
                self._logically_present = current_presence_state == "on"
                self.log(f"Initial logical presence: {self._logically_present} (from {self.bath_presence_sensor}: {current_presence_state})")
            except Exception as e:
                self.error(f"Error getting initial state for {self.bath_presence_sensor}: {e}. Assuming not present.")
                self._logically_present = False
        else:
            self.log("'bath_presence_sensor' not configured. Presence-based volume control disabled.")
        
        # Setup Z-Wave Listeners
        if self.device_id:
            if self.volume_step is not None:
                # Volume Up Listener
                self.listen_event(
                    self.handle_volume_event,
                    "zwave_js_value_notification",
                    device_id=self.device_id,
                    command_class=self.zwave_command_class,
                    endpoint=self.zwave_endpoint,
                    property_key=self.zwave_property_key_volume_up,
                    value=self.zwave_event_value_pressed
                )
                self.log(f"Listening for Z-Wave volume up (Key Pressed) from {self.device_id} (Scene {self.zwave_property_key_volume_up})")

                # Volume Down Listener
                self.listen_event(
                    self.handle_volume_event,
                    "zwave_js_value_notification",
                    device_id=self.device_id,
                    command_class=self.zwave_command_class,
                    endpoint=self.zwave_endpoint,
                    property_key=self.zwave_property_key_volume_down,
                    value=self.zwave_event_value_pressed
                )
                self.log(f"Listening for Z-Wave volume down (Key Pressed) from {self.device_id} (Scene {self.zwave_property_key_volume_down})")

            # "Press and Hold" Listener for Volume Down (Join Request)
            if self.master_speaker_input_select:
                self.listen_event(
                    self.handle_join_request_event,
                    "zwave_js_value_notification",
                    device_id=self.device_id,
                    command_class=self.zwave_command_class,
                    endpoint=self.zwave_endpoint,
                    property_key=self.zwave_property_key_volume_down, # Same key as volume down
                    value=self.zwave_event_value_held 
                )
                self.log(f"Listening for Z-Wave Join Request (Key Held Down) from {self.device_id} (Scene {self.zwave_property_key_volume_down})")
            else:
                self.log("Join request listener not set up: 'master_speaker_input_select' missing from config.")

        # Presence sensor listener - only listen to meaningful state changes (on/off)
        if self.bath_presence_sensor and self.bath_presence_volume_boost is not None and self.presence_on_max_volume is not None:
            # Only listen to on/off transitions, not all state changes (reduces CPU usage)
            self.listen_state(self._handle_presence_change, self.bath_presence_sensor, new="on")
            self.listen_state(self._handle_presence_change, self.bath_presence_sensor, new="off")
            self.log(f"Listening for on/off state changes on {self.bath_presence_sensor} for volume control.")
        else:
            if self.bath_presence_sensor:
                 self.log("Presence-based volume control is not fully configured (boost or max_volume missing/invalid). State changes on presence sensor will not affect volume.")
        
        self.log("SonosBathroomControl Initialization complete.")

    def _handle_presence_change(self, entity, attribute, old_state, new_state, kwargs):
        """Handle state changes of the presence sensor and update logical presence."""
        if self.bath_presence_volume_boost is None or self.presence_on_max_volume is None:
            return  # Config missing, silently skip

        # Throttle: prevent excessive callbacks
        now = time.time()
        if now - self._last_presence_change_time < self._presence_throttle_seconds:
            return  # Too soon since last change, skip
        self._last_presence_change_time = now

        # Only process meaningful state changes (on/off)
        if new_state == "on":
            if not self._logically_present:
                self._logically_present = True
                current_volume = self._get_current_volume_float()
                if current_volume < self.presence_on_max_volume:
                    potential_target = current_volume + self.bath_presence_volume_boost
                    capped_target = min(potential_target, self.presence_on_max_volume)
                    adj = capped_target - current_volume
                    if adj > 0.001:
                        self.log(f"Presence ON: Boosting {self.media_player} by {adj:.2f} to {capped_target:.2f}")
                        self._adjust_volume(adj)
                    # Removed verbose logging for no-op cases
                # Removed verbose logging for already at max volume
        elif new_state == "off":
            if self._logically_present:
                self._logically_present = False
                self.log(f"Presence OFF: Reducing vol for {self.media_player} by {self.bath_presence_volume_boost:.2f}")
                self._adjust_volume(-self.bath_presence_volume_boost)
        # Removed handling of unavailable/unknown - we only listen to on/off now

    def handle_volume_event(self, event_name, data, kwargs):
        """Handle Z-Wave volume button presses."""
        if self.volume_step is None:
            self.log("Volume event: 'volume_step' not configured. No action.", level="WARNING")
            return

        scene = data.get("property_key")
        # self.log(f"Volume event. Scene: {scene}, Data: {data}") # Can be verbose

        if scene == self.zwave_property_key_volume_up:
            self.log(f"Volume Up triggered for {self.media_player} (Scene {scene})")
            self._adjust_volume(self.volume_step)
        elif scene == self.zwave_property_key_volume_down:
            self.log(f"Volume Down triggered for {self.media_player} (Scene {scene})")
            self._adjust_volume(-self.volume_step)

    def handle_join_request_event(self, event_name, data, kwargs):
        """Handle Z-Wave button hold (property_key_volume_down) to fire a group join request event."""
        scene = data.get("property_key")
        self.log(f"Join Request event received via Z-Wave. Scene: {scene}, Data: {data}")

        if not self.master_speaker_input_select:
            self.log("Join request: 'master_speaker_input_select' not configured. Cannot fire event.", level="WARNING")
            return

        if not self.media_player:
            self.log("Join request: 'media_player' (bathroom speaker) not configured. Cannot fire event.", level="WARNING")
            return

        try:
            # Get the master entity ID directly from the attribute of the input_select
            target_master_entity_id = self.get_state(self.master_speaker_input_select, attribute="master_entity_id")

            # Check if the attribute value is Python None or an empty string
            if not target_master_entity_id:
                selected_friendly_name = self.get_state(self.master_speaker_input_select) # For logging
                # Clarified log: attribute itself is None (or empty), not the string "none"
                self.log(f"Join/Unjoin request: Master speaker entity_id attribute is missing or None. Attribute of {self.master_speaker_input_select} (currently '{selected_friendly_name}') is '{target_master_entity_id}'. Aborting.", level="ERROR")
                return

            if target_master_entity_id == "none": # Check for the string "none" as well for robustness
                selected_friendly_name = self.get_state(self.master_speaker_input_select) # For logging
                self.log(f"Join/Unjoin request: Master speaker entity_id attribute is the string 'none'. Attribute of {self.master_speaker_input_select} (currently '{selected_friendly_name}') is '{target_master_entity_id}'. Aborting.", level="ERROR")
                return

            if not target_master_entity_id.startswith("media_player."):
                selected_friendly_name = self.get_state(self.master_speaker_input_select) # For logging
                self.log(f"Join/Unjoin request: Resolved entity_id '{target_master_entity_id}' (from attribute of {self.master_speaker_input_select}, which is '{selected_friendly_name}') is not a media_player. Aborting.", level="ERROR")
                return
            
            slave_entity_id = self.media_player

            # Check if the slave is currently part of the target master's group
            master_attributes = self.get_state(target_master_entity_id, attribute="all")
            is_already_grouped_with_master = False
            if master_attributes and 'attributes' in master_attributes:
                current_master_group = master_attributes['attributes'].get('sonos_group',
                                                                       master_attributes['attributes'].get('group_members', []))
                if slave_entity_id in current_master_group:
                    is_already_grouped_with_master = True
            
            if is_already_grouped_with_master:
                # Slave is already in the master's group, so unjoin it
                # Route through GroupManager queue with high priority (user-initiated action)
                self.log(f"Slave '{slave_entity_id}' is already in master '{target_master_entity_id}'s group. Requesting unjoin via queue.")
                self.fire_event(
                    "request_sonos_group_unjoin",
                    entity_id=slave_entity_id
                )
                self.log(f"Unjoin request event fired for '{slave_entity_id}'.")
            else:
                # Slave is not in the master's group, so fire the join request
                self.log(f"Firing 'request_sonos_group_join' event. Master: {target_master_entity_id}, Slave: {slave_entity_id}")
                self.fire_event(
                    "request_sonos_group_join",
                    master_entity_id=target_master_entity_id,
                    slave_entity_id=slave_entity_id
                )
                self.log(f"Event 'request_sonos_group_join' fired for master '{target_master_entity_id}' and slave '{slave_entity_id}'.")

        except Exception as e:
            self.error(f"Error processing join/unjoin request event trigger: {e}", exc_info=True)

    def _get_current_volume_float(self):
        """Safely get the current volume as a float, defaulting to 0.0 on error or None."""
        if not self.media_player: # Should have already been checked in init
            # self.error("Cannot get current volume, media_player not configured.") # Repetitive
            return 0.0
        try:
            volume_level = self.get_state(self.media_player, attribute="volume_level")
            if volume_level is None:
                self.log(f"Volume for {self.media_player} is None, defaulting to 0.0", level="WARNING")
                return 0.0
            return float(volume_level)
        except ValueError:
            self.log(f"Volume for {self.media_player} ('{volume_level}') is not a float. Defaulting to 0.0", level="WARNING")
            return 0.0
        except Exception as e: # Catch other potential errors
            self.error(f"Error getting current volume for {self.media_player}: {e}", exc_info=True)
            return 0.0

    def _adjust_volume(self, adjustment):
        """Adjust the volume of the media player."""
        if not self.media_player: # Should have been checked
            return

        try:
            current_volume = self._get_current_volume_float()
            new_volume = round(current_volume + adjustment, 2) # Round to 2 decimal places
            new_volume = max(0.0, min(1.0, new_volume))

            if abs(new_volume - current_volume) > 0.001: # Only call if changed significantly
                self.log(f"Adjusting volume for {self.media_player} from {current_volume:.2f} to {new_volume:.2f}")
                self.call_service(
                    "media_player/volume_set",
                    entity_id=self.media_player,
                    volume_level=new_volume
                )
            # else: self.log(f"Volume for {self.media_player} already at target {new_volume:.2f}, no change needed.")
        except Exception as e:
            self.error(f"Error adjusting volume for {self.media_player}: {e}", exc_info=True) 