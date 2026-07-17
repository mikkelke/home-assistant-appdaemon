import appdaemon.plugins.hass.hassapi as hass  # type: ignore

class LivingRoomTvControl(hass.Hass):
    def _safe_cancel_timer(self, handle):
        """Cancel a timer only if still running (avoids invalid-handle warnings)."""
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
                return True
        except Exception:
            pass
        return False


    def _report_house_event(self, cause, effect):
        """Explain an automated TV/lift move to the dashboard's Home activity feed.
        Fire-and-forget: HouseEvents (apps/home_pulse) listens for this event; if it
        is not running the event just evaporates. Must never break TV control."""
        try:
            self.fire_event("house_events_report", cause=cause, effect=effect, icon="mdi:television")
        except Exception:
            pass

    def initialize(self):
        self.log("LivingRoomTvControl Initializing")

        # Configuration
        self.tv_entity = self.args.get("tv_entity")
        self.lift_select_entity = self.args.get("lift_select_entity")
        self.zwave_device_id = self.args.get("zwave_device_id")
        
        self.button_3_property_key = self.args.get("button_3_property_key")
        self.button_4_property_key = self.args.get("button_4_property_key")

        self.zwave_command_class = self.args.get("zwave_command_class")
        self.zwave_endpoint = self.args.get("zwave_endpoint")
        self.zwave_event_value_held = self.args.get("zwave_event_value_held")

        # Optional WOL helper
        self.wol_button_entity = self.args.get("wol_button_entity")

        # Announcement config
        self.announce_on_lift_move = self.args.get("announce_on_lift_move", True)
        self.announce_speakers_living = self.args.get("announce_speakers_living", [])

        # Zone isolation config
        self.family_zone_speakers = self.args.get("family_zone_speakers", 
                                                ["media_player.living_room", 
                                                 "media_player.kitchen", 
                                                 "media_player.dining_room"])
        
        # Rooftop configuration
        self.rooftop_entity = self.args.get("rooftop_entity", "media_player.rooftop")
        self.rooftop_charging_sensor = self.args.get("rooftop_charging_sensor", "binary_sensor.rooftop_charging")
        
        # TV entity configuration (for power-on checks)
        self.hisense_tv_entity = self.args.get("hisense_tv_entity", "media_player.living_room_hisense_tv_television")
        self.apple_tv_entity = self.args.get("apple_tv_entity", "media_player.living_room_apple_tv")
        self.living_room_speaker_entity = self.args.get("living_room_speaker_entity", "media_player.living_room")
        
        # Lift verification
        self.lift_verify_enabled = self.args.get("lift_verify_enabled", True)
        self.lift_verify_timeout_s = float(self.args.get("lift_verify_timeout_s", 10.0))
        self._expected_lift_position = None
        self._lift_verify_handle = None
        
        # TV state change debouncing
        self.tv_off_debounce_seconds = float(self.args.get("tv_off_debounce_seconds", 2.0))
        self._tv_off_debounce_handle = None
        
        # Min operation time (configurable)
        self.min_operation_time_s = float(self.args.get("min_operation_time_s", 3.0))

        # TV off to wall delay
        self.off_to_wall_delay_seconds = self.args.get("off_to_wall_delay_seconds", 600)
        # Minimum delay before moving to wall (prevents false off triggers during power-on)
        self.min_wall_move_delay_seconds = self.args.get("min_wall_move_delay_seconds", 10)
        # Smart delay: if both Apple TV and Hisense are confirmed off, use shorter delay
        self.smart_wall_move_delay_seconds = self.args.get("smart_wall_move_delay_seconds", 5)
        # Apple TV state correction delay (seconds)
        try:
            self.apple_tv_reset_delay_seconds = int(self.args.get("apple_tv_reset_delay_seconds", 30))
        except Exception:
            self.apple_tv_reset_delay_seconds = 30
        # Enable/disable Apple TV remote reset (workaround for stuck Apple TV integration in older HA versions)
        self.apple_tv_reset_enabled = self.args.get("apple_tv_reset_enabled", True)
        self.tv_operation_in_progress = False  # Flag to prevent overlapping operations
        self._tv_operation_start_time = None  # Track when operation started for stuck flag detection
        # Make timeout configurable (default 45s to allow for WoL + multiple TV turn-ons)
        self._tv_operation_timeout_s = int(self.args.get("tv_operation_timeout_s", 45))
        # Runtime handles/state
        self._delayed_wall_handle = None
        self._session_end_wall_handle = None
        self._last_button_press_ts = None
        # Timer for delayed Apple TV remote reset (off/on) after TV turns on
        self._apple_tv_reset_handle = None
        # Verification handle to ensure Apple TV remote ends ON
        self._apple_tv_remote_verify_handle = None

        # Power-aware wall move (optional; default off until YAML opts in)
        self.power_aware_wall_move_enabled = bool(self.args.get("power_aware_wall_move_enabled", False))
        self.tv_power_entity = self.args.get("tv_power_entity")
        if isinstance(self.tv_power_entity, str):
            self.tv_power_entity = self.tv_power_entity.strip() or None
        # Idle ~27W vs TV-on ~60W+: keep threshold in the gap (see YAML comments).
        self.power_off_threshold_w = float(self.args.get("power_off_threshold_w", 52))
        self.power_off_confirm_seconds = float(self.args.get("power_off_confirm_seconds", 2))
        self.power_recheck_interval_seconds = float(self.args.get("power_recheck_interval_seconds", 2))
        self.power_max_wait_seconds = float(self.args.get("power_max_wait_seconds", 45))
        # When relay <= power_off_threshold_w, ignore Apple TV for wall-cancel (flaky paused/idle)
        self.ignore_apple_tv_when_relay_standby = bool(
            self.args.get("ignore_apple_tv_when_relay_standby", True)
        )
        # After universal TV + Hisense stay "session over", wait this long then schedule wall (ignores Apple TV noise).
        # 0 = disabled: use legacy power-verify path only.
        self.session_end_grace_seconds = float(self.args.get("session_end_grace_seconds", 22.0))
        # At session-end fire: if relay is still above this, skip wall (full stack still drawing like viewing).
        self.power_definitely_on_w = float(self.args.get("power_definitely_on_w", 115.0))
        # Symmetric to wall-off: before Sonos reset / Apple TV remote cycle, confirm real TV-on via
        # relay power (> threshold) and/or Apple & Hisense indicators (when power-aware mode is on).
        # Cap for polling when relay/indicators lag; default matches wall power wait, not TV WoL timeout.
        self.power_on_confirm_max_wait_seconds = float(
            self.args.get("power_on_confirm_max_wait_seconds", 12)
        )
        self.power_on_confirm_recheck_seconds = float(
            self.args.get("power_on_confirm_recheck_seconds", 1.0)
        )
        self._power_wall_verify_handle = None
        self._power_on_confirm_handle = None
        self._power_on_confirm_wait_start = None
        self._power_verify_wait_start = None
        self._power_verify_below_since = None
        self._power_verify_apple_state = None
        self._power_verify_hisense_state = None
        self._power_verify_schedule_mode = "debounced"
        # When relay confirms standby while universal TV still reports on/paused, begin wall path early
        self.power_standby_wall_enabled = bool(self.args.get("power_standby_wall_enabled", True))
        self._power_standby_wall_handle = None
        self._power_standby_below_since = None
        if self.power_aware_wall_move_enabled and not self.tv_power_entity:
            self.log(
                "power_aware_wall_move_enabled is true but tv_power_entity is missing or empty; "
                "bypassing the power gate (wall scheduling matches pre-power-aware behavior).",
                level="WARNING",
            )

        # Validations
        if not self.tv_entity:
            self.error("'tv_entity' (e.g., media_player.living_room_tv) is missing. App cannot control TV.")
            return
        if not self.lift_select_entity:
            self.error("'lift_select_entity' (e.g., select.living_room_tv_lift_position) is missing. App cannot control TV lift.")
            return
        if not self.zwave_device_id:
            self.error("'zwave_device_id' is missing. Z-Wave control disabled.")
            # We can still proceed if only Z-Wave is missing, for the TV off -> lift to wall functionality
        else: # Only validate Z-Wave specific keys if zwave_device_id is present
            if not self.button_3_property_key:
                self.log("'button_3_property_key' is missing. Control for button 3 disabled.", level="WARNING")
            if not self.button_4_property_key:
                self.log("'button_4_property_key' is missing. Control for button 4 disabled.", level="WARNING")
            if not self.zwave_command_class:
                self.error("'zwave_command_class' is missing in YAML. This is now a required Z-Wave parameter.")
                # Potentially return here if Z-Wave control is critical and cannot proceed
            if self.zwave_endpoint is None: # Endpoint can be 0, so check for None
                self.error("'zwave_endpoint' is missing in YAML. This is now a required Z-Wave parameter.")
            if not self.zwave_event_value_held:
                self.error("'zwave_event_value_held' is missing in YAML (e.g., 'KeyHeldDown'). This is now a required Z-Wave parameter.")

        # Listen for Z-Wave events if configured
        if self.zwave_device_id and self.button_3_property_key and self.zwave_command_class and self.zwave_endpoint is not None and self.zwave_event_value_held:
            self.listen_event(
                self.handle_zwave_event,
                "zwave_js_value_notification",
                device_id=self.zwave_device_id,
                command_class=self.zwave_command_class,
                endpoint=self.zwave_endpoint,
                property_key=self.button_3_property_key,
                button_id="3"
            )
            self.log(f"Listening for Z-Wave Button 3 Hold (Key Held Down) from {self.zwave_device_id} (Property Key {self.button_3_property_key})")

        if self.zwave_device_id and self.button_4_property_key and self.zwave_command_class and self.zwave_endpoint is not None and self.zwave_event_value_held:
            self.listen_event(
                self.handle_zwave_event,
                "zwave_js_value_notification",
                device_id=self.zwave_device_id,
                command_class=self.zwave_command_class,
                endpoint=self.zwave_endpoint,
                property_key=self.button_4_property_key,
                button_id="4"
            )
            self.log(f"Listening for Z-Wave Button 4 Hold (Key Held Down) from {self.zwave_device_id} (Property Key {self.button_4_property_key})")

        # Listen for TV state changes (to 'off')
        if self.tv_entity and self.lift_select_entity:
            # Listen to all state changes, then debounce in handler
            self.listen_state(self.handle_tv_state_change, self.tv_entity)
            # Real power-on only: off -> on (avoids playing/on flips from Apple TV remote toggle)
            self.listen_state(self.handle_tv_power_on, self.tv_entity, old="off", new="on")
            self.log(f"Listening for state changes on {self.tv_entity} (to 'off'/'on') to control {self.lift_select_entity} and reset speakers")
            # Listen for activity on Apple TV and Hisense to cancel delayed wall moves if viewing continues
            try:
                self.listen_state(self._on_tv_indicator_active, self.apple_tv_entity)
                self.listen_state(self._on_tv_indicator_active, self.hisense_tv_entity)
            except Exception:
                pass
            # Listen for lift position changes for verification
            if self.lift_verify_enabled:
                self.listen_state(self._lift_position_verification_handler, self.lift_select_entity)
            
            # Listen for lift position changes to cancel wall moves when manually moved away
            self.listen_state(self._on_lift_position_changed, self.lift_select_entity)

            if (
                self.power_aware_wall_move_enabled
                and self.tv_power_entity
                and self.power_standby_wall_enabled
            ):
                self.listen_state(self._on_relay_power_changed, self.tv_power_entity)
                self.log(
                    f"Listening for standby power on {self.tv_power_entity} "
                    f"(<= {self.power_off_threshold_w} W) to trigger wall when HA TV state lags",
                )
        
        # Check on startup: if TV is off and lift is not at Wall, schedule wall move
        # This handles the case where AppDaemon restarts while TV is off
        self.run_in(self._check_tv_state_on_init, 5)  # Delay slightly for HA to be fully ready
        
        self.log("LivingRoomTvControl Initialization complete.")

    def handle_zwave_event(self, event_name, data, kwargs):
        button_id = kwargs.get("button_id")
        # Demote raw event to DEBUG to avoid noisy logs
        self.log(f"Z-Wave raw event for button {button_id}: {data}", level="DEBUG")

        # Validate that this is a held event
        try:
            value = data.get("value")
            value_raw = data.get("value_raw")
            # Accept common held indicators: 'KeyHeldDown', 2, '2'
            is_held = (
                value == self.zwave_event_value_held or
                value in ["KeyHeldDown", "held", "2"] or
                value_raw in [2, "2"]
            )
            if not is_held:
                self.log(f"Ignoring non-held event for button {button_id}: value={value} value_raw={value_raw}", level="DEBUG")
                return
            else:
                # Concise info when we actually act on a held event
                self.log(f"Z-Wave HELD detected for button {button_id}")
        except Exception:
            self.log("Could not validate held status; proceeding cautiously.", level="WARNING")

        # Prevent overlapping operations, but detect and reset stuck flags
        if self.tv_operation_in_progress:
            # Check if flag is stuck (operation started too long ago)
            if self._tv_operation_start_time is not None:
                try:
                    elapsed = (self.datetime() - self._tv_operation_start_time).total_seconds()
                    if elapsed > self._tv_operation_timeout_s:
                        self.log(f"Resetting stuck tv_operation_in_progress flag (was set {elapsed:.1f}s ago)", level="WARNING")
                        self.tv_operation_in_progress = False
                        self._tv_operation_start_time = None
                    else:
                        self.log(f"TV operation in progress for {elapsed:.1f}s, ignoring button {button_id} press", level="WARNING")
                        return
                except Exception:
                    pass
            else:
                self.log(f"TV operation already in progress, ignoring button {button_id} press", level="WARNING")
                return

        try:
            self.tv_operation_in_progress = True
            self._tv_operation_start_time = self.datetime()
            # Record when the button was pressed for off->wall delay logic
            try:
                self._last_button_press_ts = self.datetime()
            except Exception:
                self._last_button_press_ts = None

            # Cancel any pending wall scheduling (session grace, delayed move, power verify, power standby)
            self._cancel_delayed_wall_move("Z-Wave viewing")

            if self._power_wall_verify_handle:
                self._cancel_power_wall_verify()

            # Determine target position from button
            # SIMPLIFIED: Buttons always go to their target position, never to Wall
            # Wall position is exclusively controlled by TV turning off
            if button_id == "3":
                target_pos = "Living room"
                announce_phrase = "living room"
            elif button_id == "4":
                target_pos = "Kitchen"
                announce_phrase = "kitchen"
            else:
                self.log(f"Unknown button ID '{button_id}' in Z-Wave event handler.", level="WARNING")
                self.tv_operation_in_progress = False
                self._tv_operation_start_time = None
                return

            # Check if we're already at the target position
            current_pos = None
            try:
                current_pos = self.get_state(self.lift_select_entity)
            except Exception as e:
                self.log(f"Could not read current lift position: {e}", level="WARNING")

            # If already at target, just ensure TV is on (no position change needed)
            if current_pos == target_pos:
                self.log(f"Lift already at '{target_pos}', ensuring TV is powered on")
            
            # Always try to power on TV when button is pressed (user wants to watch)
            should_power_on = True

            # Move lift if not already at target position
            needs_move = (current_pos != target_pos)
            
            if needs_move:
                # Announce immediately so user knows it registered
                try:
                    if self.announce_on_lift_move:
                        notifier = self.get_app("SonosNotifier")
                        if notifier:
                            notifier.notify(
                                message=f"TV moving to {announce_phrase}",
                                target_speakers=self.announce_speakers_living
                            )
                        else:
                            self.log("SonosNotifier app not found; skipping audible confirmation.", level="WARNING")
                except Exception as e:
                    self.log(f"Failed to send audible confirmation for button {button_id}: {e}", level="WARNING")

                # Move lift immediately
                try:
                    # Set expected position for verification
                    if self.lift_verify_enabled:
                        self._expected_lift_position = target_pos
                        # Set timeout for verification
                        try:
                            self._lift_verify_handle = self.run_in(self._lift_verify_timeout, self.lift_verify_timeout_s)
                        except Exception:
                            pass
                    self.call_service("select/select_option", entity_id=self.lift_select_entity, option=target_pos)
                    self.log(f"Lift moved to '{target_pos}' (immediate)")
                    self._report_house_event(f"Living room TV button {button_id} held", f"TV moving to {announce_phrase}")
                except Exception as e:
                    self.error(f"Error moving lift to '{target_pos}': {e}")
                    # Clear verification on error
                    if self._lift_verify_handle:
                        self._safe_cancel_timer(self._lift_verify_handle)
                        self._lift_verify_handle = None
                    self._expected_lift_position = None
            else:
                self.log(f"Lift already at '{target_pos}', skipping move")

            # Fire Wake-on-LAN and power-on only if moving to a viewing position and TV is not already on
            if should_power_on:
                self._power_on_tv_if_needed(reason=f"Z-Wave button {button_id} pressed")

            # Operation finished - but don't reset flag immediately
            # TV power-on is async and may take a few seconds. Keep flag True for a minimum
            # period to prevent race conditions where another button press is accepted before TV is on.
            # Reset after a short delay to allow TV state to settle.
            try:
                elapsed = (self.datetime() - self._tv_operation_start_time).total_seconds()
                if elapsed < self.min_operation_time_s:
                    remaining = self.min_operation_time_s - elapsed
                    self.run_in(self._reset_tv_operation_flag, remaining)
                else:
                    self._reset_tv_operation_flag()
            except Exception:
                # Fallback: reset immediately if time calculation fails
                self._reset_tv_operation_flag()

        except Exception as e:
            self.error(f"Error handling Z-Wave event for button {button_id}: {str(e)}")
            self.error("Exception details:", exc_info=True)
            # On error, reset immediately (error handling shouldn't block)
            self._reset_tv_operation_flag()
    
    def _reset_tv_operation_flag(self, kwargs=None):
        """Reset the TV operation flag - called after minimum delay"""
        self.tv_operation_in_progress = False
        self._tv_operation_start_time = None

    def _stop_living_room_media_and_reset(self, reason=""):
        # Stop living room media if it looks stuck playing from TV
        try:
            state = None
            try:
                state = self.get_state(self.living_room_speaker_entity)
            except Exception:
                state = None
            # Only stop when actually playing to avoid unnecessary calls
            if state == "playing":
                msg = f"Stopping {self.living_room_speaker_entity} because TV turned off" + (f" ({reason})" if reason else "")
                self.log(msg)
                try:
                    self.call_service("media_player/media_stop", entity_id=self.living_room_speaker_entity)
                except Exception as e:
                    # Fallback to turn_off if media_stop fails for some integrations
                    self.log(f"media_stop failed for {self.living_room_speaker_entity}: {e} - attempting turn_off", level="WARNING")
                    try:
                        self.call_service("media_player/turn_off", entity_id=self.living_room_speaker_entity)
                    except Exception:
                        pass
            # After stopping, request a targeted reset so volumes/mute are normalized
            try:
                self.fire_event(
                    "sonos_reset_speakers",
                    targets=[self.living_room_speaker_entity],
                    source="Living room TV turned off",
                )
            except Exception:
                pass
        except Exception as e:
            self.log(f"Failed to stop and reset living room media: {e}", level="WARNING")


    def _cancel_session_end_wall_timer(self, reason=""):
        """Cancel pending session-end grace timer (TV must stay 'off' for the full grace period)."""
        if not self._session_end_wall_handle:
            return
        self._safe_cancel_timer(self._session_end_wall_handle)
        self._session_end_wall_handle = None
        if reason:
            self.log(f"Cancelled session-end wall timer ({reason})", level="INFO")

    def _cancel_delayed_wall_move(self, reason=""):
        # Session timer is also a pending wall path - cancel together
        self._cancel_session_end_wall_timer(reason)
        self._cancel_power_standby_monitor(reason)
        # Cancel any scheduled delayed move to Wall
        if self._delayed_wall_handle:
            self._safe_cancel_timer(self._delayed_wall_handle)
            self._delayed_wall_handle = None
            try:
                reason_str = f" ({reason})" if reason else ""
                self.log(f"Cancelled scheduled delayed move to 'Wall'{reason_str}")
            except Exception:
                pass

    def _read_tv_power_w(self):
        """Return current TV relay power in watts, or None if unreadable."""
        if not self.tv_power_entity:
            return None
        try:
            raw = self.get_state(self.tv_power_entity)
        except Exception:
            return None
        if raw in (None, "unavailable", "unknown", ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _relay_suggests_tv_standby(self):
        """True when relay is at/below off threshold (standby vs TV-on draw)."""
        if not self.tv_power_entity:
            return False
        p = self._read_tv_power_w()
        if p is None:
            return False
        return p <= self.power_off_threshold_w

    def _relay_suggests_tv_definitely_on(self):
        """True when relay draw indicates the full TV stack is still on."""
        if not self.tv_power_entity:
            return False
        p = self._read_tv_power_w()
        if p is None:
            return False
        return p > self.power_definitely_on_w

    def _hisense_blocks_wall(self):
        """
        Hisense 'on' blocks wall only when relay draw suggests the panel is actually on.
        At standby power, Hisense often still reports 'on' after the TV is physically off.
        """
        try:
            hisense_state = self.get_state(self.hisense_tv_entity)
        except Exception:
            return False
        if hisense_state != "on":
            return False
        if self.power_aware_wall_move_enabled and self._relay_suggests_tv_standby():
            return False
        return True

    def _wall_scheduling_pending(self):
        return bool(self._delayed_wall_handle or self._session_end_wall_handle)

    def _lift_already_at_wall(self):
        try:
            return self.get_state(self.lift_select_entity) == "Wall"
        except Exception:
            return False

    def _cancel_power_standby_monitor(self, reason=""):
        if self._power_standby_wall_handle:
            self._safe_cancel_timer(self._power_standby_wall_handle)
            self._power_standby_wall_handle = None
        self._power_standby_below_since = None
        if reason:
            self.log(f"Power standby wall monitor cancelled ({reason})", level="DEBUG")

    def _on_relay_power_changed(self, entity, attribute, old, new, kwargs):
        self._evaluate_power_standby_wall()

    def _evaluate_power_standby_wall(self):
        """
        If relay confirms standby while universal TV still looks active, start session-end wall path.
        Covers Hisense/HA lag after the physical TV is already off.
        """
        if not (
            self.power_aware_wall_move_enabled
            and self.tv_power_entity
            and self.power_standby_wall_enabled
        ):
            return

        if self.tv_operation_in_progress or self._lift_already_at_wall():
            self._cancel_power_standby_monitor("TV operation or lift already at Wall")
            return

        if self._wall_scheduling_pending():
            self._cancel_power_standby_monitor("wall move already scheduled")
            return

        try:
            tv_state = self.get_state(self.tv_entity)
        except Exception:
            tv_state = None

        if tv_state == "off":
            self._cancel_power_standby_monitor("universal TV already off")
            return

        if tv_state in (None, "unavailable", "unknown"):
            self._cancel_power_standby_monitor("universal TV state unusable")
            return

        if self._hisense_blocks_wall():
            self._cancel_power_standby_monitor("Hisense on with TV-level power draw")
            return

        if self._relay_suggests_tv_definitely_on():
            self._cancel_power_standby_monitor("relay still indicates TV on")
            return

        p = self._read_tv_power_w()
        if p is None:
            self._cancel_power_standby_monitor("relay power unreadable")
            return

        if p > self.power_off_threshold_w:
            self._cancel_power_standby_monitor("relay above standby threshold")
            return

        now = self.datetime()
        if self._power_standby_below_since is None:
            self._power_standby_below_since = now
            self.log(
                f"Power standby: relay {p:.1f} W but {self.tv_entity} is '{tv_state}' - "
                f"confirming {self.power_off_confirm_seconds:.0f}s before wall scheduling",
                level="INFO",
            )

        try:
            below_elapsed = (now - self._power_standby_below_since).total_seconds()
        except Exception:
            below_elapsed = 0

        if below_elapsed >= self.power_off_confirm_seconds:
            self._begin_session_end_wall_from_power(tv_state, p)
            return

        if not self._power_standby_wall_handle:
            try:
                self._power_standby_wall_handle = self.run_in(
                    self._power_standby_wall_tick,
                    self.power_recheck_interval_seconds,
                )
            except Exception as e:
                self.log(f"Failed to schedule power standby wall tick: {e}", level="WARNING")

    def _power_standby_wall_tick(self, kwargs=None):
        self._power_standby_wall_handle = None
        self._evaluate_power_standby_wall()

    def _begin_session_end_wall_from_power(self, tv_state, relay_w):
        """Relay confirmed standby while universal TV still active - reuse session-end wall scheduling."""
        self._cancel_power_standby_monitor("")
        if self._wall_scheduling_pending():
            return

        if self.session_end_grace_seconds > 0:
            self._cancel_session_end_wall_timer("")
            try:
                self._session_end_wall_handle = self.run_in(
                    self._session_end_wall_fired,
                    self.session_end_grace_seconds,
                    from_power=True,
                )
                self.log(
                    f"Power standby: relay {relay_w:.1f} W, universal TV '{tv_state}' - "
                    f"session-end grace {self.session_end_grace_seconds:.0f}s before wall",
                    level="INFO",
                )
            except Exception as e:
                self.log(f"Failed to schedule power standby session-end timer: {e}", level="WARNING")
            return

        self._fire_wall_after_session_over(from_power=True, from_init=False)

    def _fire_wall_after_session_over(self, from_power=False, from_init=False):
        """Shared wall scheduling after session is over (TV off path or relay standby path)."""
        if self.tv_operation_in_progress:
            self.log("Session-end wall: skipped (TV operation in progress)", level="INFO")
            return

        try:
            cur_tv = self.get_state(self.tv_entity)
        except Exception:
            cur_tv = None

        if not from_power and cur_tv != "off":
            self.log(
                f"Session-end wall: skipped (universal TV is '{cur_tv}', not off)",
                level="INFO",
            )
            return

        if from_power and cur_tv == "off":
            self.log(
                "Power standby wall: universal TV already off; normal off path will handle wall",
                level="DEBUG",
            )
            return

        if self._hisense_blocks_wall():
            self.log("Session-end wall: skipped (Hisense still on with TV-level power draw)", level="INFO")
            return

        if self.power_aware_wall_move_enabled and self.tv_power_entity:
            p = self._read_tv_power_w()
            if p is not None and p > self.power_definitely_on_w:
                self.log(
                    f"Session-end wall: skipped (relay {p:.1f} W > {self.power_definitely_on_w} W - still high draw)",
                    level="WARNING",
                )
                return

        try:
            apple_tv_state = self.get_state(self.apple_tv_entity)
        except Exception:
            apple_tv_state = None
        try:
            hisense_tv_state = self.get_state(self.hisense_tv_entity)
        except Exception:
            hisense_tv_state = None

        source = "power standby" if from_power else "session-end"
        self.log(
            f"{source.title()}: scheduling move to Wall "
            f"(universal='{cur_tv}', relay={self._read_tv_power_w()} W)",
            level="INFO",
        )
        mode = "init" if from_init else ("power_standby" if from_power else "debounced")
        self._schedule_wall_move_after_tv_off(apple_tv_state, hisense_tv_state, schedule_mode=mode)

    def _power_verify_state_reset(self):
        """Clear power-verify timers and stored states (does not cancel a running timer)."""
        self._power_verify_wait_start = None
        self._power_verify_below_since = None
        self._power_verify_apple_state = None
        self._power_verify_hisense_state = None
        self._power_verify_schedule_mode = "debounced"

    def _cancel_power_wall_verify(self, reason=""):
        """Cancel in-flight power verification and log reason if provided."""
        if self._power_wall_verify_handle:
            self._safe_cancel_timer(self._power_wall_verify_handle)
            self._power_wall_verify_handle = None
            self._power_verify_state_reset()
            if reason:
                self.log(reason, level="INFO")

    def _cancel_power_on_confirm(self, reason=None):
        """Cancel relay/indicator polling before running Sonos reset on TV power-on."""
        if self._power_on_confirm_handle:
            self._safe_cancel_timer(self._power_on_confirm_handle)
            self._power_on_confirm_handle = None
        self._power_on_confirm_wait_start = None
        if reason:
            self.log(reason, level="DEBUG")

    def _tv_indicators_suggest_viewing_active(self):
        """Apple TV / Hisense states that mean the stack is in use (same idea as false-off detection)."""
        try:
            apple_tv_state = self.get_state(self.apple_tv_entity)
        except Exception:
            apple_tv_state = None
        try:
            hisense_tv_state = self.get_state(self.hisense_tv_entity)
        except Exception:
            hisense_tv_state = None
        apple_active_states = ["playing", "paused", "idle", "on"]
        return (
            (apple_tv_state in apple_active_states) or
            (hisense_tv_state == "on")
        )

    def _tv_relay_power_suggests_tv_on(self):
        """True when relay draw is above the same threshold used for 'off enough' on the wall path."""
        p = self._read_tv_power_w()
        if p is None:
            return False
        return p > self.power_off_threshold_w

    def _tv_power_on_confirmed_for_speaker_reset(self):
        """Real TV-on: measurable draw OR indicators active (mirrors wall-off using power + indicators)."""
        if self._tv_relay_power_suggests_tv_on():
            return True
        return self._tv_indicators_suggest_viewing_active()

    def _begin_power_on_confirm_for_speaker_reset(self):
        """Poll until power/indicators confirm TV is really on, or time out (fail closed on Sonos reset)."""
        self._cancel_power_on_confirm()
        self._power_on_confirm_wait_start = self.datetime()
        self.log(
            f"Power-on confirm: waiting for relay > {self.power_off_threshold_w} W or Apple/Hisense active "
            f"(every {self.power_on_confirm_recheck_seconds}s, max {self.power_on_confirm_max_wait_seconds}s)",
            level="INFO",
        )
        try:
            # First tick almost immediately so confirm is usually sub-second when power/indicators update fast.
            self._power_on_confirm_handle = self.run_in(self._power_on_confirm_tick, 0.1)
        except Exception as e:
            self.log(f"Failed to schedule power-on confirm: {e}", level="WARNING")

    def _power_on_confirm_tick(self, kwargs=None):
        self._power_on_confirm_handle = None
        try:
            try:
                cur_tv = self.get_state(self.tv_entity)
            except Exception:
                cur_tv = None
            if cur_tv == "off":
                self._cancel_power_on_confirm(reason="Power-on confirm canceled: TV returned to off")
                return

            if self._tv_power_on_confirmed_for_speaker_reset():
                p = self._read_tv_power_w()
                self.log(
                    f"Power-on confirm OK (relay={p} W); running speaker reset path",
                    level="INFO",
                )
                self._power_on_confirm_wait_start = None
                self._execute_tv_power_on_actions()
                return

            now = self.datetime()
            try:
                elapsed = (now - self._power_on_confirm_wait_start).total_seconds()
            except Exception:
                elapsed = self.power_on_confirm_max_wait_seconds

            if elapsed >= self.power_on_confirm_max_wait_seconds:
                self.log(
                    f"Power-on confirm: timed out after {elapsed:.0f}s without relay > {self.power_off_threshold_w} W "
                    f"and without Apple/Hisense active; skipping Sonos reset and Apple TV remote cycle",
                    level="WARNING",
                )
                self._power_on_confirm_wait_start = None
                return

            # Reschedule the next poll (normal path). This was mis-indented inside the timeout
            # branch above, after its `return` - unreachable, so the loop never actually looped.
            # Dead until 2026-07-17: every observed power-on confirmed on the first tick (60/60
            # since April), so the bug never showed up in practice.
            self._power_on_confirm_handle = self.run_in(
                self._power_on_confirm_tick, self.power_on_confirm_recheck_seconds
            )
        except Exception as e:
            self.log(f"Power-on confirm tick error: {e}", level="WARNING")

    def _session_end_wall_fired(self, kwargs=None):
        """Grace period elapsed with session still 'over' - schedule wall (relay sanity check only)."""
        kwargs = kwargs or {}
        from_init = bool(kwargs.get("from_init", False))
        from_power = bool(kwargs.get("from_power", False))
        self._session_end_wall_handle = None
        try:
            self._fire_wall_after_session_over(from_power=from_power, from_init=from_init)
        except Exception as e:
            self.log(f"Session-end wall handler error: {e}", level="WARNING")

    def _execute_tv_power_on_actions(self):
        """Sonos targeted reset, family zone isolation, optional Apple TV remote refresh (after real power-on)."""
        self.log(
            f"TV power-on actions for {self.tv_entity}: resetting living room speaker + zone isolation",
            level="INFO",
        )
        try:
            self.fire_event(
                "sonos_reset_speakers",
                targets=[self.living_room_speaker_entity],
                source="Living room TV turned on",
            )
        except Exception as e:
            self.log(f"sonos_reset_speakers failed: {e}", level="WARNING")
        self.run_in(self._isolate_family_zone, 0.5)

        if self.apple_tv_reset_enabled:
            if self._apple_tv_reset_handle:
                self._safe_cancel_timer(self._apple_tv_reset_handle)
                self._apple_tv_reset_handle = None
            try:
                self._apple_tv_reset_handle = self.run_in(
                    self._apple_tv_remote_reset,
                    self.apple_tv_reset_delay_seconds,
                )
                self.log(
                    f"Scheduled Apple TV remote reset (off/on) in {self.apple_tv_reset_delay_seconds}s",
                )
            except Exception as e:
                self.log(f"Failed to schedule Apple TV remote reset: {e}", level="WARNING")
        else:
            self.log("Apple TV remote reset is disabled (apple_tv_reset_enabled: false)", level="DEBUG")

    def _begin_power_verify_then_wall(self, apple_tv_state, hisense_tv_state, schedule_mode="debounced"):
        """
        After Apple TV/Hisense indicators pass: optionally confirm power is low, then schedule wall move.
        schedule_mode: 'debounced' (normal off path) or 'init' (startup check; simpler delay).
        """
        if self._power_wall_verify_handle:
            self._safe_cancel_timer(self._power_wall_verify_handle)
            self._power_wall_verify_handle = None
            self._power_verify_state_reset()

        if not self.power_aware_wall_move_enabled:
            self._schedule_wall_move_after_tv_off(apple_tv_state, hisense_tv_state, schedule_mode)
            return
        if not self.tv_power_entity:
            self._schedule_wall_move_after_tv_off(apple_tv_state, hisense_tv_state, schedule_mode)
            return

        p = self._read_tv_power_w()
        if p is None:
            self.log(
                "Power check: unreadable sensor; skipping wall move (fail closed)",
                level="WARNING",
            )
            return

        if p <= self.power_off_threshold_w:
            self.log(
                f"Power check: {p:.1f} W at or below {self.power_off_threshold_w} W threshold; proceeding with wall scheduling",
                level="INFO",
            )
            self._schedule_wall_move_after_tv_off(apple_tv_state, hisense_tv_state, schedule_mode)
            return

        self.log(
            f"Power check: {p:.1f} W above {self.power_off_threshold_w} W threshold; "
            f"waiting up to {self.power_max_wait_seconds:.0f}s (confirm {self.power_off_confirm_seconds:.0f}s below threshold, "
            f"recheck every {self.power_recheck_interval_seconds:.0f}s)",
            level="INFO",
        )
        self._power_verify_wait_start = self.datetime()
        self._power_verify_below_since = None
        self._power_verify_apple_state = apple_tv_state
        self._power_verify_hisense_state = hisense_tv_state
        self._power_verify_schedule_mode = schedule_mode
        try:
            self._power_wall_verify_handle = self.run_in(
                self._power_wall_verify_tick,
                self.power_recheck_interval_seconds,
            )
        except Exception as e:
            self.error(f"Failed to schedule power verify tick: {e}")

    def _power_wall_verify_tick(self, kwargs=None):
        """Poll power until confirmed below threshold, timeout, or canceled."""
        self._power_wall_verify_handle = None

        try:
            cur_tv = self.get_state(self.tv_entity)
        except Exception:
            cur_tv = None
        if cur_tv != "off":
            self.log("Power check canceled: TV is no longer off", level="INFO")
            self._power_verify_state_reset()
            return

        now = self.datetime()
        try:
            elapsed = (now - self._power_verify_wait_start).total_seconds()
        except Exception:
            elapsed = self.power_max_wait_seconds

        if elapsed >= self.power_max_wait_seconds:
            self.log(
                "Power check timed out; wall move skipped (power did not stay below threshold in time)",
                level="WARNING",
            )
            self._power_verify_state_reset()
            return

        p = self._read_tv_power_w()
        if p is None:
            self.log(
                "Power check: unreadable power during wait; wall move skipped",
                level="WARNING",
            )
            self._power_verify_state_reset()
            return

        if p > self.power_off_threshold_w:
            self._power_verify_below_since = None
            self.log(
                f"Power check: {p:.1f} W still above {self.power_off_threshold_w} W; continuing to wait",
                level="DEBUG",
            )
            try:
                self._power_wall_verify_handle = self.run_in(
                    self._power_wall_verify_tick,
                    self.power_recheck_interval_seconds,
                )
            except Exception as e:
                self.error(f"Failed to reschedule power verify tick: {e}")
            return

        if self._power_verify_below_since is None:
            self._power_verify_below_since = now
            self.log(
                f"Power check: {p:.1f} W at or below threshold; need {self.power_off_confirm_seconds:.0f}s continuous",
                level="INFO",
            )

        try:
            below_elapsed = (now - self._power_verify_below_since).total_seconds()
        except Exception:
            below_elapsed = 0

        if below_elapsed >= self.power_off_confirm_seconds:
            self.log(
                f"Power check: confirmed <= {self.power_off_threshold_w} W for {self.power_off_confirm_seconds:.0f}s; proceeding with wall scheduling",
                level="INFO",
            )
            apple = self._power_verify_apple_state
            hisense = self._power_verify_hisense_state
            mode = self._power_verify_schedule_mode
            self._power_verify_state_reset()
            self._schedule_wall_move_after_tv_off(apple, hisense, mode)
            return

        try:
            self._power_wall_verify_handle = self.run_in(
                self._power_wall_verify_tick,
                self.power_recheck_interval_seconds,
            )
        except Exception as e:
            self.error(f"Failed to reschedule power verify tick: {e}")

    def _schedule_wall_move_after_tv_off(self, apple_tv_state, hisense_tv_state, schedule_mode="debounced"):
        """Stop media, apply smart/min delays, schedule or immediate move to Wall."""
        self._stop_living_room_media_and_reset(
            reason="tv_off_confirmed" if schedule_mode == "debounced" else "tv_off_confirmed_init",
        )
        if self._delayed_wall_handle:
            self._safe_cancel_timer(self._delayed_wall_handle)
            self._delayed_wall_handle = None

        if schedule_mode == "init":
            both_confirmed_off = (
                apple_tv_state == "off"
                and hisense_tv_state == "off"
                and apple_tv_state is not None
                and hisense_tv_state is not None
            )
            if both_confirmed_off:
                delay = int(self.smart_wall_move_delay_seconds)
                self.log(
                    f"Init: scheduling lift to 'Wall' in {delay}s (smart delay; power confirmed)",
                )
            else:
                delay = int(self.min_wall_move_delay_seconds)
                self.log(
                    f"Init: scheduling lift to 'Wall' in {delay}s (minimum delay; power confirmed)",
                )
            try:
                self._delayed_wall_handle = self.run_in(self._move_lift_to_wall, delay)
            except Exception as e:
                self.error(f"Error scheduling init move to 'Wall': {e}")
            return

        # debounced path: full grace + smart delay logic
        relay_standby = self._relay_suggests_tv_standby()
        both_confirmed_off = (
            apple_tv_state == "off"
            and hisense_tv_state == "off"
            and apple_tv_state is not None
            and hisense_tv_state is not None
        )
        relay_confirms_session_over = relay_standby and not self._hisense_blocks_wall()

        remaining_delay = 0
        try:
            if self._last_button_press_ts is not None:
                elapsed = (self.datetime() - self._last_button_press_ts).total_seconds()
                if elapsed < self.off_to_wall_delay_seconds:
                    remaining_delay = int(self.off_to_wall_delay_seconds - elapsed)
        except Exception:
            remaining_delay = 0

        if both_confirmed_off or relay_confirms_session_over or schedule_mode == "power_standby":
            if remaining_delay > 0 and remaining_delay < self.smart_wall_move_delay_seconds:
                remaining_delay = self.min_wall_move_delay_seconds
            elif remaining_delay == 0 or remaining_delay > self.smart_wall_move_delay_seconds:
                remaining_delay = self.smart_wall_move_delay_seconds
            if both_confirmed_off:
                detail = "Both Apple TV and Hisense confirmed 'off'"
            elif schedule_mode == "power_standby":
                detail = "Relay confirmed standby while HA TV state lagged"
            else:
                detail = "Relay confirmed standby (Hisense/Apple integration lag)"
            self.log(
                f"TV ({self.tv_entity}) turned off. {detail}. "
                f"Using smart delay of {remaining_delay}s before moving to 'Wall'."
            )
        else:
            if remaining_delay < self.min_wall_move_delay_seconds:
                remaining_delay = self.min_wall_move_delay_seconds
            self.log(
                f"TV ({self.tv_entity}) turned off. Using minimum delay of {remaining_delay}s before moving to 'Wall' "
                f"(Apple TV: '{apple_tv_state}', Hisense: '{hisense_tv_state}')"
            )

        if remaining_delay > 0:
            self.log(f"TV ({self.tv_entity}) turned off. Scheduling lift to 'Wall' in {remaining_delay}s")
            try:
                self._delayed_wall_handle = self.run_in(self._move_lift_to_wall, remaining_delay)
            except Exception as e:
                self.error(f"Error scheduling delayed move to 'Wall': {e}")
        else:
            lift_position = "Wall"
            self.log(
                f"TV ({self.tv_entity}) turned off. Setting lift position ({self.lift_select_entity}) to '{lift_position}' immediately",
            )
            try:
                self.call_service("select/select_option", entity_id=self.lift_select_entity, option=lift_position)
                self._report_house_event("Living room TV turned off", "TV moving to wall")
            except Exception as e:
                self.error(
                    f"Error setting lift position to '{lift_position}' when TV turned off: {e}",
                    exc_info=True,
                )

    def _on_tv_indicator_active(self, entity, attribute, old, new, kwargs):
        # If Apple TV becomes active-ish or Hisense is reported ON, cancel delayed wall moves
        try:
            is_apple = entity == self.apple_tv_entity
            is_hisense = entity == self.hisense_tv_entity

            # Relay shows standby draw: do not let Apple TV or stale Hisense cancel the wall move.
            if (
                self.power_aware_wall_move_enabled
                and self.ignore_apple_tv_when_relay_standby
                and self._relay_suggests_tv_standby()
            ):
                if is_hisense and new == "on":
                    self.log(
                        f"Ignoring Hisense '{old}' -> '{new}' for wall cancel "
                        f"(relay <= {self.power_off_threshold_w} W standby)",
                        level="DEBUG",
                    )
                elif is_apple:
                    self.log(
                        f"Ignoring Apple TV '{old}' -> '{new}' for wall cancel (relay <= {self.power_off_threshold_w} W standby)",
                        level="DEBUG",
                    )
                return

            apple_active_states = ["playing", "paused", "idle", "on"]
            indicator_active = (
                (is_apple and new in apple_active_states) or
                (is_hisense and new == "on")
            )
            if indicator_active:
                self._cancel_delayed_wall_move(reason=f"indicator active: {entity} -> {new}")
                if self._power_wall_verify_handle:
                    self._cancel_power_wall_verify(
                        reason="Power check canceled because TV indicators became active",
                    )
        except Exception as e:
            self.log(f"Error in _on_tv_indicator_active for {entity}: {e}", level="WARNING")


    def handle_tv_state_change(self, entity, attribute, old, new, kwargs):
        self.log(f"TV state changed: {entity} from '{old}' to '{new}'")
        
        # Debounce "off" state changes to avoid premature lift movement
        if new == "off" and old != "off":
            self._cancel_power_standby_monitor("universal TV off")
            self._cancel_power_on_confirm(reason="TV transitioning off: cancel pending power-on confirm")
            # Cancel any existing debounce timer
            if self._tv_off_debounce_handle:
                self._safe_cancel_timer(self._tv_off_debounce_handle)
            # Schedule debounced handler
            try:
                self._tv_off_debounce_handle = self.run_in(self._handle_tv_off_debounced, self.tv_off_debounce_seconds)
                self.log(f"TV state changed to 'off', debouncing for {self.tv_off_debounce_seconds}s before action")
            except Exception as e:
                self.log(f"Error scheduling TV off debounce: {e}", level="WARNING")
                # Fall through to immediate handling if scheduling fails
                self._handle_tv_off_debounced(None)
            return

        if new != "off":
            self._cancel_session_end_wall_timer("TV no longer off")
            if self._power_wall_verify_handle:
                self._cancel_power_wall_verify(
                    reason="Power check canceled: TV is no longer off",
                )
            self._evaluate_power_standby_wall()

        # For other state changes, no action needed
        if new != "off":
            return
    
    def _handle_tv_off_debounced(self, kwargs):
        """Handle TV off state after debounce period."""
        self._tv_off_debounce_handle = None
        
        # Re-check state to ensure it's still "off" after debounce
        try:
            current_state = self.get_state(self.tv_entity)
            if current_state != "off":
                self.log(f"TV state is no longer 'off' after debounce (current: '{current_state}'), skipping action")
                return
        except Exception as e:
            self.log(f"Error checking TV state after debounce: {e}", level="WARNING")
            # Continue anyway - better to act than miss an off event

        if self._lift_already_at_wall():
            self.log(f"TV ({self.tv_entity}) off after debounce but lift already at Wall", level="DEBUG")
            return

        if self._delayed_wall_handle:
            self.log(f"TV ({self.tv_entity}) off after debounce but wall move already scheduled", level="DEBUG")
            return
        
        # Move to wall immediately unless within the grace window from last button press
        if not self.tv_operation_in_progress:
            # Cancel pending Apple TV remote reset if any
            if self._apple_tv_reset_handle:
                self._safe_cancel_timer(self._apple_tv_reset_handle)
                self._apple_tv_reset_handle = None
            # Cancel any pending verify-on
            if self._apple_tv_remote_verify_handle:
                self._safe_cancel_timer(self._apple_tv_remote_verify_handle)
                self._apple_tv_remote_verify_handle = None
            # Hisense on = panel on - do not schedule wall. Apple TV is ignored here (flaky vs relay/Sonos).
            try:
                apple_tv_state = self.get_state(self.apple_tv_entity)
            except Exception:
                apple_tv_state = None
            try:
                hisense_tv_state = self.get_state(self.hisense_tv_entity)
            except Exception:
                hisense_tv_state = None

            if self._hisense_blocks_wall():
                self.log(
                    f"{self.tv_entity} reported 'off' but Hisense is on with TV-level power draw; skipping wall scheduling",
                    level="INFO",
                )
                self._cancel_delayed_wall_move(reason="false off: Hisense on")
                return

            if self.session_end_grace_seconds > 0:
                self._cancel_session_end_wall_timer("")
                try:
                    self._session_end_wall_handle = self.run_in(
                        self._session_end_wall_fired,
                        self.session_end_grace_seconds,
                    )
                    self.log(
                        f"TV ({self.tv_entity}) off after debounce: session-end grace "
                        f"{self.session_end_grace_seconds:.0f}s - will schedule Wall if still off (Hisense off, relay check at end)",
                        level="INFO",
                    )
                except Exception as e:
                    self.log(f"Failed to schedule session-end wall timer: {e}", level="WARNING")
                return

            self.log(
                f"TV ({self.tv_entity}) off detected after debounce; session-end disabled - power verify path",
                level="INFO",
            )
            self._begin_power_verify_then_wall(
                apple_tv_state,
                hisense_tv_state,
                schedule_mode="debounced",
            )
        else:
            self.log(f"TV ({self.tv_entity}) turned off but TV operation in progress - skipping scheduling/immediate wall move", level="INFO")

    def _move_lift_to_wall(self, kwargs):
        lift_position = "Wall"
        self.log(f"Executing delayed move: setting lift position ({self.lift_select_entity}) to '{lift_position}'")
        try:
            self.call_service("select/select_option", entity_id=self.lift_select_entity, option=lift_position)
            self._report_house_event("Living room TV turned off", "TV moving to wall")
        except Exception as e:
            self.error(f"Error executing delayed move to '{lift_position}': {e}", exc_info=True)
        finally:
            self._delayed_wall_handle = None

    def handle_tv_power_on(self, entity, attribute, old, new, kwargs):
        """Off -> on only (see listen_state). When power-aware, confirm relay + indicators like the wall-off path."""
        try:
            self._cancel_delayed_wall_move("TV turned on")

            if self._power_wall_verify_handle:
                self._cancel_power_wall_verify(
                    reason="Power check canceled: TV turned on",
                )

            self._cancel_power_on_confirm()

            self.log(
                f"TV state changed: {entity} from '{old}' to '{new}' -> evaluating real power-on (speaker reset path)",
                level="INFO",
            )

            use_power_gate = bool(self.power_aware_wall_move_enabled and self.tv_power_entity)
            if not use_power_gate:
                self._execute_tv_power_on_actions()
                return

            if self._tv_power_on_confirmed_for_speaker_reset():
                pw = self._read_tv_power_w()
                self.log(
                    f"Power-on: immediate confirm (relay={pw} W, indicators ok); running speaker reset path",
                    level="INFO",
                )
                self._execute_tv_power_on_actions()
                return

            self._begin_power_on_confirm_for_speaker_reset()
        except Exception as e:
            self.error(f"Failed to request living room speaker reset on TV power on: {e}")

    def _apple_tv_remote_reset(self, kwargs):
        # Toggle the Apple TV remote power to refresh state
        try:
            self._apple_tv_reset_handle = None
            remote_entity = "remote.living_room_apple_tv"
            self.log(f"Executing Apple TV remote reset: turning off {remote_entity}")
            try:
                self.call_service("remote/turn_off", entity_id=remote_entity)
                self.log(f"Apple TV remote turn_off completed for {remote_entity}")
            except Exception as e:
                self.log(f"Apple TV remote turn_off failed: {e}", level="WARNING")
            # Turn back on shortly after
            try:
                self.run_in(self._apple_tv_remote_turn_on, 2, remote_entity=remote_entity)
                self.log(f"Scheduled Apple TV remote turn_on in 2s for {remote_entity}")
            except Exception as e:
                self.log(f"Scheduling Apple TV remote turn_on failed: {e}", level="WARNING")
        except Exception as e:
            self.log(f"Error in Apple TV remote reset: {e}", level="WARNING")

    def _apple_tv_remote_turn_on(self, kwargs):
        # Complete the remote reset sequence by turning it back on
        try:
            remote_entity = kwargs.get("remote_entity", "remote.living_room_apple_tv")
            self.log(f"Completing Apple TV remote reset: turning on {remote_entity}")
            self.call_service("remote/turn_on", entity_id=remote_entity)
            self.log(f"Apple TV remote turn_on completed for {remote_entity}")
            # Verify the remote ends ON; retry once if needed
            try:
                if self._apple_tv_remote_verify_handle:
                    self._safe_cancel_timer(self._apple_tv_remote_verify_handle)
            except Exception:
                pass
            try:
                self._apple_tv_remote_verify_handle = self.run_in(
                    self._ensure_remote_on,
                    3,
                    remote_entity=remote_entity,
                    retries_remaining=0,
                )
                self.log(f"Scheduled Apple TV remote verify-on in 3s for {remote_entity}")
            except Exception as e:
                self.log(f"Scheduling Apple TV remote ensure-on failed: {e}", level="WARNING")
        except Exception as e:
            self.log(f"Apple TV remote turn_on failed: {e}", level="WARNING")

    def _ensure_remote_on(self, kwargs):
        # Ensure the Apple TV remote entity ends in the ON state
        try:
            remote_entity = kwargs.get("remote_entity", "remote.living_room_apple_tv")
            retries_remaining = int(kwargs.get("retries_remaining", 0))
            state = None
            try:
                state = self.get_state(remote_entity)
            except Exception:
                state = None
            if state != "on":
                self.log(f"Apple TV remote verify: state is '{state}', attempting turn_on for {remote_entity}")
                try:
                    self.call_service("remote/turn_on", entity_id=remote_entity)
                    self.log(f"Apple TV remote verify: turn_on completed for {remote_entity}")
                except Exception as e:
                    self.log(f"Apple TV remote verify: turn_on failed: {e}", level="WARNING")
            else:
                self.log(f"Apple TV remote verify: {remote_entity} is already ON")
            # Done; clear handle
            self._apple_tv_remote_verify_handle = None
        except Exception as e:
            self.log(f"Error ensuring Apple TV remote ON: {e}", level="WARNING")

    def _isolate_family_zone(self, kwargs=None):
        """
        When TV turns on, identify any active groups involving 'Family Zone' speakers 
        (Living, Kitchen, Dining + Rooftop if docked).
        If a group contains a Family Zone speaker:
        1. Stop playback for that group (or at least the family members)
        2. Break the group (unjoin everyone)
        
        This ensures the TV becomes the main entertainment and no conflicting music plays 
        in the open-plan area.
        
        NOTE: This can make many service calls, so it's deferred via run_in() to avoid
        blocking the TV power-on callback.
        """
        MAX_TARGETS = 12  # Safety limit to prevent excessive calls
        targets_processed = 0
        
        try:
            # 1. Determine current Family Zone membership
            current_family_zone = list(self.family_zone_speakers)
            
            # Check Rooftop status
            try:
                is_docked = self.get_state(self.rooftop_charging_sensor) == "on"
                if is_docked and self.rooftop_entity:
                    current_family_zone.append(self.rooftop_entity)
                    self.log(f"Family Zone Isolation: Rooftop is docked, adding to zone check.")
            except Exception as e:
                self.log(f"Family Zone Isolation: Error checking rooftop status: {e}", level="WARNING")

            self.log(f"Family Zone Isolation: Checking speakers: {current_family_zone}")
            
            processed_speakers = set() # Track who we've already handled to avoid duplicate calls

            for speaker in current_family_zone:
                # Check limit before processing next speaker
                if targets_processed >= MAX_TARGETS:
                    self.log(f"Family Zone Isolation: Reached max targets limit ({MAX_TARGETS}), stopping", level="WARNING")
                    break
                    
                if speaker in processed_speakers:
                    continue

                # Get group members for this speaker
                try:
                    group_members = self.get_state(speaker, attribute="group_members")
                except Exception:
                    group_members = []

                # Proceed if it's a group OR if the speaker is playing solo
                # (User: "That scenario is true EVERYTIME a family room speaker is part of the group")
                # We also want to stop solo playback in the family zone to clear the air for TV.
                
                is_grouped = isinstance(group_members, list) and len(group_members) > 1
                is_playing = self.get_state(speaker) == "playing"
                
                # If speaker is Living Room (TV), it might report as grouped or playing, but we shouldn't
                # stop it (as it's the TV!). However, if it was part of a music group, we DO want to 
                # break that group for the OTHERS.
                
                if not (is_grouped or is_playing):
                    continue

                # Determine the target list to stop/ungroup
                # If grouped, target the whole group. If solo, target just the speaker.
                targets = group_members if is_grouped else [speaker]
                
                # Check if this group/speaker is actually relevant to our operation
                # (i.e., does it involve a Family Zone speaker? Yes, because we started with 'speaker' in current_family_zone)
                
                self.log(f"Family Zone Isolation: Detected active audio involving {speaker} (Group: {targets}). Clearing...")

                for target in targets:
                    if target in processed_speakers:
                        continue
                    
                    # Safety limit to prevent excessive service calls
                    if targets_processed >= MAX_TARGETS:
                        self.log(f"Family Zone Isolation: Reached max targets limit ({MAX_TARGETS}), stopping", level="WARNING")
                        break
                        
                    # Skip stopping the Living Room itself as it is now playing TV audio
                    if target == self.living_room_speaker_entity:
                        # We might still want to 'unjoin' it to ensure it leaves any old music group,
                        # but often switching source does that. Explicit unjoin is safer.
                        # Do NOT call media_stop on Living Room.
                        pass
                    else:
                        # Stop others
                        try:
                            self.call_service("media_player/media_stop", entity_id=target)
                        except Exception:
                            try:
                                self.call_service("media_player/media_pause", entity_id=target)
                            except Exception:
                                pass
                    
                    # Unjoin everyone (including Living Room to ensure group split)
                    try:
                        self.call_service("media_player/unjoin", entity_id=target)
                    except Exception as e:
                        self.log(f"Family Zone Isolation: Failed to unjoin {target}: {e}", level="WARNING")

                    processed_speakers.add(target)
                    targets_processed += 1

        except Exception as e:
            self.error(f"Error during Family Zone Isolation: {e}")
    
    def _lift_position_verification_handler(self, entity, attribute, old, new, kwargs):
        """Handle lift position changes for command verification."""
        if not self.lift_verify_enabled or not self._expected_lift_position:
            return
        
        if new == self._expected_lift_position:
            self.log(f"Lift position verification: Successfully moved to '{new}'", level="INFO")
            # Cancel timeout
            if self._lift_verify_handle:
                self._safe_cancel_timer(self._lift_verify_handle)
                self._lift_verify_handle = None
            self._expected_lift_position = None
    
    def _lift_verify_timeout(self, kwargs):
        """Timeout handler if lift doesn't reach expected position."""
        if self._expected_lift_position:
            self.log(f"Lift position verification timeout: Expected '{self._expected_lift_position}' but didn't reach it within {self.lift_verify_timeout_s}s", level="WARNING")
            self._expected_lift_position = None
        self._lift_verify_handle = None

    def _power_on_tv_if_needed(self, reason=""):
        """Helper method to power on TV if it's currently off. Used by both Z-Wave button handler and manual lift position changes."""
        try:
            # Read states up-front
            universal_state = None
            hisense_state = None
            try:
                universal_state = self.get_state(self.tv_entity)
            except Exception as e:
                self.log(f"Could not read state for {self.tv_entity}: {e}", level="WARNING")
            try:
                hisense_state = self.get_state(self.hisense_tv_entity)
            except Exception as e:
                self.log(f"Could not read state for {self.hisense_tv_entity}: {e}", level="WARNING")

            tv_already_on = (
                (universal_state not in [None, "off", "unavailable", "unknown", "standby"]) or
                (hisense_state == "on")
            )
            reason_str = f" ({reason})" if reason else ""
            self.log(f"Power-on check{reason_str} -> Universal: {universal_state}, Hisense: {hisense_state}, already_on={tv_already_on}")

            if tv_already_on:
                self.log("TV already on; skipping WoL and power-on calls")
                return

            # WoL first if configured
            try:
                if self.wol_button_entity:
                    self.log(f"Triggering WOL helper button: {self.wol_button_entity}")
                    self.call_service("button/press", entity_id=self.wol_button_entity)
            except Exception as e:
                self.log(f"Failed to press WOL helper button: {e}", level="WARNING")

            # Universal TV turn on
            try:
                if universal_state in [None, "off", "unavailable", "unknown", "standby"]:
                    self.log(f"Powering universal TV: {self.tv_entity} (state: {universal_state})")
                    self.call_service("media_player/turn_on", entity_id=self.tv_entity)
                else:
                    self.log(f"Skipping universal TV turn_on; current state: {universal_state}")
            except Exception as e:
                self.log(f"Universal TV turn_on attempt failed: {e}", level="WARNING")

            # Hisense turn on
            try:
                if hisense_state == "unavailable":
                    self.log("Hisense TV is unavailable; skipping direct control", level="WARNING")
                elif hisense_state != "on":
                    self.log(f"Powering Hisense TV (HomeKit); state: {hisense_state}")
                    self.call_service("media_player/turn_on", entity_id=self.hisense_tv_entity)
                else:
                    self.log("Skipping Hisense TV turn_on; already on")
            except Exception as e:
                self.log(f"Hisense TV turn_on attempt failed: {e}", level="WARNING")

            # Apple TV wake only if not already active.
            # Apple TV does not support media_player.turn_on; use remote.turn_on to wake it.
            try:
                apple_tv_state = self.get_state(self.apple_tv_entity)
                if apple_tv_state in ["playing", "paused", "idle", "on"]:
                    self.log(f"Skipping Apple TV wake; current state: {apple_tv_state}")
                elif apple_tv_state == "unavailable":
                    self.log("Apple TV is unavailable; skipping wake", level="WARNING")
                else:
                    self.log(f"Waking Apple TV via remote; state: {apple_tv_state}")
                    remote_entity = "remote." + self.apple_tv_entity.split(".", 1)[-1]
                    self.call_service("remote/turn_on", entity_id=remote_entity)
            except Exception as e:
                self.log(f"Apple TV wake attempt failed: {e}", level="WARNING")
        except Exception as e:
            self.log(f"Error in _power_on_tv_if_needed: {e}", level="WARNING")

    def _on_lift_position_changed(self, entity, attribute, old, new, kwargs):
        """Handle lift position changes to cancel wall moves and power on TV when moved to viewing positions."""
        try:
            # If lift is moved away from Wall (to Kitchen or Living room), cancel any pending wall scheduling
            if new in ["Kitchen", "Living room"] and old == "Wall":
                self._cancel_delayed_wall_move(reason=f"lift manually moved to '{new}'")
                self.log(f"Lift position changed from 'Wall' to '{new}': cancelled pending wall scheduling")
            
            # If lift is moved to a viewing position (Kitchen or Living room), ensure TV is on
            # Skip if TV operation is in progress (Z-Wave button already handling it)
            if new in ["Kitchen", "Living room"]:
                if not self.tv_operation_in_progress:
                    self.log(f"Lift manually moved to '{new}' from '{old}': ensuring TV is powered on")
                    self._power_on_tv_if_needed(reason=f"lift moved to {new}")
                else:
                    self.log(f"Lift moved to '{new}' but TV operation in progress (Z-Wave button), skipping power-on", level="DEBUG")
        except Exception as e:
            self.log(f"Error in _on_lift_position_changed: {e}", level="WARNING")

    def _check_tv_state_on_init(self, kwargs):
        """
        Check TV state on AppDaemon init/restart.
        If TV is off and lift is not at Wall, schedule wall move.
        This ensures wall move still happens even if AppDaemon restarts while TV is off.
        """
        try:
            # Get current states
            tv_state = self.get_state(self.tv_entity)
            lift_state = self.get_state(self.lift_select_entity)
            apple_tv_state = self.get_state(self.apple_tv_entity)
            hisense_tv_state = self.get_state(self.hisense_tv_entity)
            
            self.log(f"Init check: TV={tv_state}, Lift={lift_state}, Apple TV={apple_tv_state}, Hisense={hisense_tv_state}")
            
            # Only act if TV is off and lift is not at Wall
            if tv_state != "off":
                self.log("Init check: TV is not off, no action needed")
                return
            
            if lift_state == "Wall":
                self.log("Init check: Lift already at Wall, no action needed")
                return
            
            if hisense_tv_state == "on" and not (
                self.power_aware_wall_move_enabled and self._relay_suggests_tv_standby()
            ):
                self.log("Init check: Hisense on - skipping wall move", level="INFO")
                return

            if self.session_end_grace_seconds > 0:
                self._cancel_session_end_wall_timer("")
                try:
                    self._session_end_wall_handle = self.run_in(
                        self._session_end_wall_fired,
                        self.session_end_grace_seconds,
                        from_init=True,
                    )
                    self.log(
                        f"Init check: TV off, lift at '{lift_state}'; session-end grace "
                        f"{self.session_end_grace_seconds:.0f}s before wall scheduling",
                        level="INFO",
                    )
                except Exception as e:
                    self.log(f"Init check: failed to schedule session-end timer: {e}", level="WARNING")
                return

            self.log(
                f"Init check: TV off, lift at '{lift_state}'; power verify before wall scheduling",
                level="INFO",
            )
            self._begin_power_verify_then_wall(
                apple_tv_state,
                hisense_tv_state,
                schedule_mode="init",
            )
            
        except Exception as e:
            self.log(f"Error in _check_tv_state_on_init: {e}", level="WARNING")

