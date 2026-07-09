import appdaemon.plugins.hass.hassapi as hass

class SonosKitchenControl(hass.Hass):
    def initialize(self):
        """Initialize the app and set up listeners."""
        self.log("SonosKitchenControl Initializing")

        # Configurations
        self.device_id = self.args.get("device_id")
        self.media_player = self.args.get("media_player")
        self.master_speaker_input_select = self.args.get("master_speaker_input_select")

        if not self.device_id:
            self.error("'device_id' is missing. Z-Wave control disabled.")
        if not self.media_player:
            self.error(f"'{self.media_player}' (kitchen Sonos) is missing. App cannot control Sonos.")
            return 
        if not self.master_speaker_input_select:
            self.log("'master_speaker_input_select' is missing. Join/unjoin functionality will be disabled.", level="WARNING")

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

            # "Press and Hold" Listener for Volume Down (Join/Unjoin Request)
            if self.master_speaker_input_select:
                self.listen_event(
                    self.handle_join_unjoin_request_event,
                    "zwave_js_value_notification",
                    device_id=self.device_id,
                    command_class=self.zwave_command_class,
                    endpoint=self.zwave_endpoint,
                    property_key=self.zwave_property_key_volume_down, # Same key as volume down
                    value=self.zwave_event_value_held 
                )
                self.log(f"Listening for Z-Wave Join/Unjoin Request (Key Held Down) from {self.device_id} (Scene {self.zwave_property_key_volume_down})")
            else:
                self.log("Join/Unjoin request listener not set up: 'master_speaker_input_select' missing from config.")
        
        self.log("SonosKitchenControl Initialization complete.")

    def handle_volume_event(self, event_name, data, kwargs):
        """Handle Z-Wave volume button presses."""
        if self.volume_step is None:
            self.log("Volume event: 'volume_step' not configured. No action.", level="WARNING")
            return

        scene = data.get("property_key")
        if scene == self.zwave_property_key_volume_up:
            self.log(f"Volume Up triggered for {self.media_player} (Scene {scene})")
            self._adjust_volume(self.volume_step)
        elif scene == self.zwave_property_key_volume_down:
            self.log(f"Volume Down triggered for {self.media_player} (Scene {scene})")
            self._adjust_volume(-self.volume_step)

    def handle_join_unjoin_request_event(self, event_name, data, kwargs):
        """Handle Z-Wave button hold to fire a group join/unjoin request or action."""
        scene = data.get("property_key")
        self.log(f"Join/Unjoin Request event received via Z-Wave. Scene: {scene}, Data: {data}")

        if not self.master_speaker_input_select:
            self.log("Join/Unjoin request: 'master_speaker_input_select' not configured. Cannot proceed.", level="WARNING")
            return

        if not self.media_player:
            self.log(f"Join/Unjoin request: '{self.media_player}' (kitchen speaker) not configured. Cannot proceed.", level="WARNING")
            return

        try:
            target_master_entity_id = self.get_state(self.master_speaker_input_select, attribute="master_entity_id")

            if not target_master_entity_id:
                selected_friendly_name = self.get_state(self.master_speaker_input_select)
                self.log(f"Join/Unjoin request: Master speaker entity_id attribute is missing or None. Attribute of {self.master_speaker_input_select} (currently '{selected_friendly_name}') is '{target_master_entity_id}'. Aborting.", level="ERROR")
                return

            if target_master_entity_id == "none":
                selected_friendly_name = self.get_state(self.master_speaker_input_select)
                self.log(f"Join/Unjoin request: Master speaker entity_id attribute is the string 'none'. Attribute of {self.master_speaker_input_select} (currently '{selected_friendly_name}') is '{target_master_entity_id}'. Aborting.", level="ERROR")
                return

            if not target_master_entity_id.startswith("media_player."):
                selected_friendly_name = self.get_state(self.master_speaker_input_select)
                self.log(f"Join/Unjoin request: Resolved entity_id '{target_master_entity_id}' (from attribute of {self.master_speaker_input_select}, which is '{selected_friendly_name}') is not a media_player. Aborting.", level="ERROR")
                return
            
            slave_entity_id = self.media_player

            master_attributes = self.get_state(target_master_entity_id, attribute="all")
            is_already_grouped_with_master = False
            if master_attributes and 'attributes' in master_attributes:
                current_master_group = master_attributes['attributes'].get('sonos_group',
                                                                       master_attributes['attributes'].get('group_members', []))
                if slave_entity_id in current_master_group and target_master_entity_id in current_master_group:
                     # Ensure the master itself is also in the group if it's a check against its own attributes
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
        if not self.media_player:
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
        except Exception as e:
            self.error(f"Error getting current volume for {self.media_player}: {e}", exc_info=True)
            return 0.0

    def _adjust_volume(self, adjustment):
        """Adjust the volume of the media player."""
        if not self.media_player:
            return

        try:
            current_volume = self._get_current_volume_float()
            new_volume = round(current_volume + adjustment, 2)
            new_volume = max(0.0, min(1.0, new_volume))

            if abs(new_volume - current_volume) > 0.001:
                self.log(f"Adjusting volume for {self.media_player} from {current_volume:.2f} to {new_volume:.2f}")
                self.call_service(
                    "media_player/volume_set",
                    entity_id=self.media_player,
                    volume_level=new_volume
                )
        except Exception as e:
            self.error(f"Error adjusting volume for {self.media_player}: {e}", exc_info=True) 