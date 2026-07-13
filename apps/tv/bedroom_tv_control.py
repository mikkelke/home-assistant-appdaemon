"""
BedroomTVControl - Controls the bedroom TV integration and TV lift.
"""

import appdaemon.plugins.hass.hassapi as hass # type: ignore

class BedroomTVControl(hass.Hass):
    def _safe_cancel_timer(self, handle):
        """Cancel a timer only if still running (avoids invalid-handle warnings)."""
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
                return True
        except Exception:
            pass
        return False

    def initialize(self):
        # Initialize the flag for Apple TV connection reset
        self.apple_tv_reset_in_progress = False
        self.apple_tv_reset_start_time = None
        self.apple_tv_reset_timeout_s = 60  # Auto-reset if stuck >60s
        # Guard for the "sleep Apple TV to refresh state, then raise lift" auto-heal
        self._apple_refresh_in_progress = False
        # Debounce handles for scheduled lift commands
        self._pending_lower_handle = None
        self._pending_raise_handle = None
        self.tv_entity = "media_player.bedroom_tv"
        self.sony_tv_entity = "media_player.bedroom_sony_tv"
        self.apple_tv_entity = "media_player.bedroom_apple_tv"
        # Timer for delayed Apple TV remote reset (off/on) after TV turns on
        self._bedroom_apple_tv_reset_handle = None
        # Flag to track when Apple TV remote reset sequence is actively executing (off -> wait -> on -> verify)
        self._bedroom_apple_tv_reset_executing = False
        # Apple TV state correction delay (seconds)
        try:
            self.apple_tv_reset_delay_seconds = int(self.args.get("apple_tv_reset_delay_seconds", 30))
        except Exception:
            self.apple_tv_reset_delay_seconds = 30
        # Apple TV reset enabled/disabled
        self.apple_tv_reset_enabled = self.args.get("apple_tv_reset_enabled", True)
        # Verification handle to ensure Apple TV remote ends ON
        self._bedroom_apple_tv_remote_verify_handle = None
        # Startup guard: prevent actions during HA/entity reload churn
        self._ready = False
        try:
            startup_grace_seconds = int(self.args.get("startup_grace_seconds", 10))
            self.run_in(self._mark_ready, startup_grace_seconds)
        except Exception:
            self._ready = True
        # Debounce before raising lift on OFF (verified against Sony + Apple TV)
        try:
            self.tv_off_debounce_seconds = float(self.args.get("tv_off_debounce_seconds", 3))
        except Exception:
            self.tv_off_debounce_seconds = 3.0
        # Debounce before lowering lift on ON (fast reaction)
        try:
            self.tv_on_debounce_seconds = float(self.args.get("tv_on_debounce_seconds", 1))
        except Exception:
            self.tv_on_debounce_seconds = 1.0
        # Flag to track if lift actions are from our code (to avoid feedback loops)
        self._lift_action_in_progress = False
        self._lift_action_start_time = None
        self._lift_action_timeout_s = 10  # Auto-reset if stuck >10s
        # Lift command timing - prevent rapid-fire conflicts
        self._last_lift_command_time = None
        try:
            self.min_lift_command_interval_s = float(self.args.get("min_lift_command_interval_s", 5.0))
        except Exception:
            self.min_lift_command_interval_s = 5.0
        # Lift command timeout (how long to wait before clearing flag)
        try:
            self.lift_command_timeout_s = float(self.args.get("lift_command_timeout_s", 4.0))
        except Exception:
            self.lift_command_timeout_s = 4.0  # Script takes ~2s, add buffer
        # Lift command verification using position entity state changes
        self.lift_verify_enabled = self.args.get("lift_verify_enabled", True)  # Use state changes to verify commands
        self._expected_lift_state = None  # Track expected state transition ("Up" or "Down")
        self._lift_verify_timeout_handle = None  # Handle for verification timeout
        self._lift_verify_clear_handle = None  # Handle for waiting for "Clear command"
        # Persistent lift position tracking entity (created as input_select helper)
        # Falls back to in-memory tracking if entity doesn't exist
        self.lift_position_entity = self.args.get("lift_position_entity", "input_select.bedroom_tv_lift_position")
        # Track last known lift position (Up/Down) based on commands we send
        # This helps us know if lift needs to be lowered when TV is already active
        self._last_known_lift_position = None  # "Up", "Down", or None (unknown)
        # Raise-retry state (2026-07-13: the 01:05 raise evaporated in a Z-Wave
        # flap window and the TV stood exposed all night - UP now retries)
        self._raise_attempt = 1
        self._post_raise_flap = False
        self._post_raise_check_handle = None
        # Periodic verification when TV is active (optional, configurable)
        self._periodic_verification_handle = None
        try:
            self.periodic_verification_enabled = self.args.get("periodic_verification_enabled", True)
            self.periodic_verification_interval_s = float(self.args.get("periodic_verification_interval_s", 300.0))  # Default 5 minutes
        except Exception:
            self.periodic_verification_enabled = True
            self.periodic_verification_interval_s = 300.0
        
        # Get all configuration values from YAML
        self.zwave_device_id = self.args.get("zwave_device_id")
        self.zwave_command_class = self.args.get("zwave_command_class")
        self.zwave_endpoint = self.args.get("zwave_endpoint")
        
        # Button property keys
        self.button_1_property_key = self.args.get("button_1_property_key")  # Volume down
        self.button_2_property_key = self.args.get("button_2_property_key")  # Volume up
        self.button_4_property_key = self.args.get("button_4_property_key")  # Play/Pause and TV toggle
        
        # Volume adjustment step
        self.volume_step = self.args.get("volume_step")
        # Volume control entity (defaults to bedroom_sony_tv for backward compatibility)
        self.volume_entity = self.args.get("volume_entity", "media_player.bedroom_sony_tv")
        
        # Validate required configuration
        if not self.zwave_device_id:
            self.error("'zwave_device_id' is missing in configuration. Z-Wave control will be disabled.")
            return
        if not self.zwave_command_class:
            self.error("'zwave_command_class' is missing in configuration. Z-Wave control will be disabled.")
            return
        if self.zwave_endpoint is None:
            self.error("'zwave_endpoint' is missing in configuration. Z-Wave control will be disabled.")
            return
        if not self.volume_step:
            self.error("'volume_step' is missing in configuration. Volume control will be disabled.")
            
        # Listen for Z-Wave remote events - for direct remote control
        # Only listen for events from the bedroom's specific Z-Wave device
        self.listen_event(
            self.remote_button_handler,
            "zwave_js_value_notification",
            device_id=self.zwave_device_id,
            command_class=self.zwave_command_class,
            endpoint=self.zwave_endpoint
        )
        
        # Main state listener for the combined TV entity
        self.listen_state(self.tv_state_handler, "media_player.bedroom_tv", attribute = "state") # Listen to state changes only
        
        # Listen for manual lift position changes for bidirectional control
        # Also used for command verification when lift_verify_enabled is True
        self.listen_state(self.lift_position_handler, "select.bedroom_tv_lift_position_configuration")
        
        self.log(f"{self.__class__.__name__} initialized.", level="INFO")

    def _cancel_pending_raise(self):
        """Cancel a scheduled lift raise (e.g. TV came back on during OFF debounce)."""
        if self._pending_raise_handle is not None:
            self._safe_cancel_timer(self._pending_raise_handle)
            self._pending_raise_handle = None

    def _is_tv_actually_on(self):
        """
        TV is on if Sony looks on, or Apple TV has active playback.
        Apple idle/on only count when Sony is on - idle persists after Sony powers off.
        """
        try:
            sony_state = self.get_state(self.sony_tv_entity)
            apple_state = self.get_state(self.apple_tv_entity)
        except Exception:
            tv_state = self.get_state(self.tv_entity)
            return tv_state not in (None, "off", "unknown", "unavailable")

        sony_on = sony_state not in ("off", "unavailable", "unknown", "standby")

        # Playing/paused means content is active even if Sony briefly glitches off
        if apple_state in ("playing", "paused"):
            return True

        # idle/on only mean the TV is on when the Sony display is on
        if sony_on and apple_state in ("on", "idle"):
            return True

        return sony_on

    def _is_tv_actually_off(self):
        """
        TV is off when the universal entity is off and _is_tv_actually_on() is false.
        """
        tv_state = self.get_state(self.tv_entity)
        if tv_state not in ("off",):
            return False
        return not self._is_tv_actually_on()

    def tv_state_handler(self, entity, attribute, old, new, kwargs):
        """
        Event-based TV state handler: Reacts to TV on/off state changes.
        - TV ON (from off) -> Lower lift to down position
        - TV OFF (from on) -> Raise lift to up position
        """
        self.log(f"TV state changed: '{old}' -> '{new}'", level="INFO")

        # Ignore events during startup/reload grace period
        if not self._ready:
            self.log("Startup grace: ignoring state change until controller is ready", level="DEBUG")
            return

        # Helper: Check if state should be considered "off"
        # Note: "idle" for Apple TV means TV is ON but not playing - it's NOT off
        def is_off_state(state):
            return state in ["off"]
        
        # Helper: Check if state should be considered "on" (active)
        # Note: "idle" for Apple TV means TV is ON but not playing - it IS an active state
        def is_on_state(state):
            return state not in [None, "off", "unknown", "unavailable"]
        
        # Ignore noisy transitions from/into unknown/unavailable (but allow None -> on transitions)
        if (old in ["unknown", "unavailable"] and new in ["unknown", "unavailable"]) or \
           (old not in [None, "unknown", "unavailable"] and new in ["unknown", "unavailable"]):
            self.log(f"Ignoring transient state change ({old} -> {new})", level="DEBUG")
            return
        
        # TV turning OFF -> debounce, verify Sony + Apple TV, then raise lift
        if is_off_state(new) and is_on_state(old):
            self.log(
                f"TV turned OFF: scheduling lift raise in {self.tv_off_debounce_seconds}s "
                f"after Sony/Apple verification (state: '{old}' -> '{new}')",
                level="INFO",
            )
            if self._pending_lower_handle is not None:
                self._safe_cancel_timer(self._pending_lower_handle)
                self._pending_lower_handle = None
            self._cancel_pending_raise()
            try:
                self._pending_raise_handle = self.run_in(
                    self._raise_lift_if_still_off,
                    self.tv_off_debounce_seconds,
                    path_marker="tv_off",
                )
            except Exception as e:
                self._pending_raise_handle = None
                self.log(f"Error scheduling lift raise: {e}", level="ERROR")
        
        # TV turning ON -> Lower lift to DOWN position
        # Handle transitions from off/idle/None/unknown to on/playing/paused
        elif is_on_state(new) and (is_off_state(old) or old in [None, "unknown"]):
            self.log(f"TV turned ON: Lowering lift to DOWN position (state: '{old}' -> '{new}')", level="INFO")
            self._cancel_pending_raise()
            if self._pending_lower_handle is not None:
                self._safe_cancel_timer(self._pending_lower_handle)
            try:
                self._pending_lower_handle = self.run_in(
                    self._lower_lift_if_still_on,
                    self.tv_on_debounce_seconds,
                )
            except Exception as e:
                self._pending_lower_handle = None
                self.log(f"Error scheduling lift lower: {e}", level="ERROR")
        
        # TV staying ON (active state transitions: playing -> paused, etc.)
        # Ensure lift stays down when TV is active
        elif is_on_state(new) and is_on_state(old):
            self.log(f"TV active state transition ({old} -> {new}): Ensuring lift is down", level="DEBUG")
            self._cancel_pending_raise()
            if self._pending_lower_handle is not None:
                self._safe_cancel_timer(self._pending_lower_handle)
            try:
                self._pending_lower_handle = self.run_in(self._ensure_lift_down_if_tv_active, 2)
            except Exception as e:
                self._pending_lower_handle = None
                self.log(f"Error scheduling lift down check: {e}", level="WARNING")

    def _mark_ready(self, kwargs):
        self._ready = True
        self.log("BedroomTVControl ready; state-driven actions enabled", level="DEBUG")
        # Load persistent position on startup
        self._load_lift_position_on_startup()
        # Check current state on startup - if TV is off, raise lift
        self._check_initial_tv_state()
    
    def _load_lift_position_on_startup(self):
        """Load lift position from persistent entity on startup"""
        try:
            position = self._get_lift_position()
            if position:
                self.log(f"Loaded lift position from persistent entity: {position}", level="INFO")
            else:
                self.log("No persistent lift position found, will track from commands", level="DEBUG")
        except Exception as e:
            self.log(f"Error loading lift position on startup: {e}", level="WARNING")

    def _check_initial_tv_state(self):
        """Check TV state on startup and set lift position accordingly"""
        try:
            tv_state = self.get_state(self.tv_entity)
            
            if self._is_tv_actually_off():
                self.log(f"Initial state check: TV is off - raising lift to UP position", level="INFO")
                try:
                    self.run_in(self._raise_lift_after_stop, 2, path_marker="initial_state_check", allow_immediate_after_stop=True)
                except Exception as e:
                    self.log(f"Error scheduling initial lift raise: {e}", level="ERROR")
            else:
                self.log(f"Initial state check: TV is on (state: '{tv_state}') - lowering lift to DOWN position", level="INFO")
                # TV is on at startup - ensure lift is down
                try:
                    self.run_in(self._ensure_lift_down_if_tv_active, 2)
                except Exception as e:
                    self.log(f"Error scheduling initial lift down: {e}", level="WARNING")
        except Exception as e:
            self.log(f"Error checking initial TV state: {e}", level="WARNING")

    def _clear_lift_action_flag(self, kwargs):
        """Clear the lift action in progress flag after script execution"""
        self._lift_action_in_progress = False
        self._lift_action_start_time = None
        self._expected_lift_state = None
        if self._lift_verify_timeout_handle:
            self._safe_cancel_timer(self._lift_verify_timeout_handle)
            self._lift_verify_timeout_handle = None
        if self._lift_verify_clear_handle:
            self._safe_cancel_timer(self._lift_verify_clear_handle)
            self._lift_verify_clear_handle = None
        self.log("Lift action flag cleared", level="DEBUG")

    def _verify_lift_clear_command(self, kwargs):
        """Verify that 'Clear command' appeared (script completed)"""
        current_state = self.get_state("select.bedroom_tv_lift_position_configuration")
        if current_state == "Clear command":
            self.log("Lift command verification: Script completed ('Clear command' confirmed)", level="INFO")
            # Clear flag after script completion
            try:
                self.run_in(self._clear_lift_action_flag, max(1.0, self.lift_command_timeout_s - 1.5))
            except Exception:
                self._reset_lift_action_flag()
            self._expected_lift_state = None
        else:
            # "Clear command" didn't appear - script may have failed or is slow
            self.log(f"Lift command verification: Expected 'Clear command' but state is '{current_state}' - using timeout fallback", level="WARNING")
            # Fall back to timeout-based clearing
            try:
                self.run_in(self._clear_lift_action_flag, self.lift_command_timeout_s)
            except Exception:
                self._reset_lift_action_flag()
        if self._lift_verify_clear_handle:
            self._lift_verify_clear_handle = None

    def _lift_verify_timeout(self, kwargs):
        """
        Timeout handler if state doesn't change as expected.
        IMPORTANT: This ensures verification never blocks the main function.
        If verification fails, we fall back to timeout-based clearing so the
        next command (TV on/off) can proceed normally.
        """
        expected = self._expected_lift_state
        self.log(f"Lift command verification timeout: Expected state '{expected}' but it didn't appear - using timeout fallback", level="WARNING")
        # Fall back to timeout-based clearing - ensures main function is never blocked
        try:
            self.run_in(self._clear_lift_action_flag, self.lift_command_timeout_s)
        except Exception:
            self._reset_lift_action_flag()
        self._expected_lift_state = None
        self._lift_verify_timeout_handle = None
        # A missed DOWN self-heals via periodic verification while the TV is on;
        # a missed UP has no second chance - retry with backoff.
        if expected == "Up":
            self._schedule_raise_retry()

    RAISE_RETRY_DELAYS_S = (60, 300, 900)

    def _schedule_raise_retry(self):
        """Re-attempt a failed or deferred raise with backoff; notify when out of
        tries. A missed DOWN self-heals via periodic verification while the TV is
        on; a missed UP had no second chance until 2026-07-13 (the TV stood
        exposed all night after a LocalTuya flap ate the command)."""
        try:
            if self._is_tv_actually_on():
                self._raise_attempt = 1
                return  # TV is back on - nothing to raise
            attempt = int(getattr(self, "_raise_attempt", 1))
            if attempt > len(self.RAISE_RETRY_DELAYS_S):
                self.log("Lift raise failed after all retries - LocalTuya link likely down", level="ERROR")
                try:
                    notifier = self.get_app("MobileNotifier")
                    self.create_task(notifier.notify(
                        title="Bedroom TV lift",
                        message="The lift did not confirm going UP after several attempts - "
                                "the lift Tuya link looks down (WiFi or local key). Raise it manually or check the device.",
                        target="mikkel"))
                except Exception as e:
                    self.log(f"raise-failure notify failed: {e}", level="WARNING")
                self._raise_attempt = 1
                return
            delay = self.RAISE_RETRY_DELAYS_S[attempt - 1]
            self.log(f"Scheduling lift raise retry {attempt}/{len(self.RAISE_RETRY_DELAYS_S)} in {delay}s", level="WARNING")
            self.run_in(self._raise_lift_after_stop, delay,
                        path_marker=f"raise_retry_{attempt}",
                        allow_immediate_after_stop=True,
                        attempt=attempt + 1)
        except Exception as e:
            self.log(f"Error scheduling raise retry: {e}", level="ERROR")

    def _post_raise_check(self, kwargs):
        """2 min after a raise: if the node flapped (or is dead) and the TV is
        still off, the Up very likely never reached the motor - go around again."""
        self._post_raise_check_handle = None
        try:
            if self._is_tv_actually_on():
                return
            sel = self.get_state("select.bedroom_tv_lift_position_configuration")
            if self._post_raise_flap or sel in (None, "unknown", "unavailable"):
                self.log(f"Post-raise check: node flapped (flap={self._post_raise_flap}, select={sel}) - retrying raise", level="WARNING")
                self._schedule_raise_retry()
            else:
                self.log("Post-raise check: node stable - raise assumed delivered", level="DEBUG")
                self._raise_attempt = 1
        except Exception as e:
            self.log(f"Error in post-raise check: {e}", level="WARNING")

    def _reset_lift_action_flag(self):
        """Reset lift action flag (with timeout detection)"""
        if self._lift_action_in_progress and self._lift_action_start_time:
            elapsed = (self.datetime() - self._lift_action_start_time).total_seconds()
            if elapsed > self._lift_action_timeout_s:
                self.log(f"Resetting stuck _lift_action_in_progress flag (stuck for {elapsed:.1f}s)", level="WARNING")
                self._lift_action_in_progress = False
                self._lift_action_start_time = None
                self._expected_lift_state = None
        else:
            self._lift_action_in_progress = False
            self._lift_action_start_time = None
            self._expected_lift_state = None
        # Clean up verification handles
        if self._lift_verify_timeout_handle:
            self._safe_cancel_timer(self._lift_verify_timeout_handle)
            self._lift_verify_timeout_handle = None
        if self._lift_verify_clear_handle:
            self._safe_cancel_timer(self._lift_verify_clear_handle)
            self._lift_verify_clear_handle = None

    def _can_send_lift_command(self):
        """Check if enough time has passed since last lift command"""
        if self._last_lift_command_time is None:
            return True
        elapsed = (self.datetime() - self._last_lift_command_time).total_seconds()
        if elapsed < self.min_lift_command_interval_s:
            remaining = self.min_lift_command_interval_s - elapsed
            self.log(f"Lift command too soon (last was {elapsed:.1f}s ago), minimum interval is {self.min_lift_command_interval_s}s. Waiting {remaining:.1f}s", level="WARNING")
            return False
        return True

    def initiate_apple_tv_reset_sequence(self, path_marker=""):
        path_marker = path_marker or "default_initiate_reset"
        # Check for stuck flag with timeout detection
        if self.apple_tv_reset_in_progress:
            if self.apple_tv_reset_start_time:
                elapsed = (self.datetime() - self.apple_tv_reset_start_time).total_seconds()
                if elapsed > self.apple_tv_reset_timeout_s:
                    self.log(f"Resetting stuck apple_tv_reset_in_progress flag (stuck for {elapsed:.1f}s)", level="WARNING")
                    self.apple_tv_reset_in_progress = False
                    self.apple_tv_reset_start_time = None
                else:
                    self.log(f"Apple TV connection reset already in progress (path: {path_marker}). Skipping new request.", level="INFO")
                    return
            else:
                # Flag is set but no start time - reset it
                self.log(f"Resetting apple_tv_reset_in_progress flag (no start time recorded)", level="WARNING")
                self.apple_tv_reset_in_progress = False

        self.log(f"Starting Apple TV connection reset (path: {path_marker}). Setting flag to True.", level="INFO")
        self.apple_tv_reset_in_progress = True
        self.apple_tv_reset_start_time = self.datetime()
        
        try:
            # self.log(f"Attempting to schedule execute_remote_off_and_continue (path: {path_marker})", level="DEBUG")
            self.run_in(self.execute_remote_off_and_continue, 0.1, path_marker=path_marker)
            # self.log(f"Successfully scheduled execute_remote_off_and_continue (path: {path_marker})", level="DEBUG")
        except Exception as e:
            self.log(f"Error scheduling execute_remote_off_and_continue (path: {path_marker}): {e}", level="ERROR")
            self.apple_tv_reset_in_progress = False
            self.apple_tv_reset_start_time = None
            # self.log(f"Reset flag to False due to initial scheduling failure (path: {path_marker})", level="DEBUG")

    def execute_remote_off_and_continue(self, kwargs):
        path_marker = kwargs.get("path_marker", "unknown_exec_remote_off")
        self.log(f"execute_remote_off_and_continue CALLED (path: {path_marker})", level="INFO")
        
        schedule_successful = False
        try:
            # self.log(f"Sending remote OFF command (path: {path_marker})", level="DEBUG")
            self.call_service("remote/turn_off", entity_id="remote.bedroom_apple_tv")
            # self.log(f"Remote OFF command sent. Attempting to schedule complete_apple_tv_reset (path: {path_marker})", level="DEBUG")
            self.run_in(self.complete_apple_tv_reset, 2, path_marker=path_marker)
            schedule_successful = True
            # self.log(f"Scheduled complete_apple_tv_reset successfully (path: {path_marker})", level="DEBUG")
        except Exception as e:
            self.log(f"Error during remote OFF or scheduling complete_apple_tv_reset (path: {path_marker}): {e}", level="ERROR")
        finally:
            if not schedule_successful:
                self.apple_tv_reset_in_progress = False
                self.apple_tv_reset_start_time = None
                # self.log(f"Reset flag to False due to failure in execute_remote_off_and_continue (path: {path_marker})", level="DEBUG")

    def complete_apple_tv_reset(self, kwargs):
        path_marker = kwargs.get("path_marker", "unknown_complete_reset")
        self.log(f"complete_apple_tv_reset CALLED (path: {path_marker}). Attempting to turn remote ON.", level="INFO")
        
        next_step_scheduled = False
        try:
            self.call_service("remote/turn_on", entity_id="remote.bedroom_apple_tv")
            # self.log(f"Remote ON command sent (path: {path_marker}).", level="DEBUG")
            
            if path_marker == "system_off_reset_lift":
                # self.log(f"Path is '{path_marker}', scheduling lift_sequence_after_reset.", level="DEBUG")
                self.run_in(self.lift_sequence_after_reset, 1, path_marker=path_marker)
            else:
                # self.log(f"Path is '{path_marker}', scheduling finalize_reset_flag.", level="DEBUG")
                self.run_in(self.finalize_reset_flag, 1, path_marker=path_marker)
            next_step_scheduled = True
        except Exception as e:
            self.log(f"Error sending remote ON or scheduling next step (path: {path_marker}): {e}", level="ERROR")
        finally:
            if not next_step_scheduled:
                self.apple_tv_reset_in_progress = False
                self.apple_tv_reset_start_time = None
                # self.log(f"Reset flag to False due to error/failure in complete_apple_tv_reset scheduling next step (path: {path_marker})", level="DEBUG")

    def finalize_reset_flag(self, kwargs):
        path_marker = kwargs.get("path_marker", "unknown_finalize_flag")
        # self.log(f"finalize_reset_flag CALLED (path: {path_marker}). Resetting flag to False.", level="DEBUG")
        self.apple_tv_reset_in_progress = False
        self.apple_tv_reset_start_time = None

    def initiate_apple_tv_reset_and_lift_sequence(self):
        # The `callback` concept is removed here; logic is now driven by path_marker in complete_apple_tv_reset
        self.log("initiate_apple_tv_reset_and_lift_sequence: Calling initiate_apple_tv_reset_sequence for 'system_off_reset_lift' path.", level="INFO")
        self.initiate_apple_tv_reset_sequence(path_marker="system_off_reset_lift")

    def lift_sequence_after_reset(self, kwargs): # Added kwargs for run_in compatibility
        path_marker = kwargs.get("path_marker", "lift_after_reset_unknown_path") 
        self.log(f"Lift sequence ({path_marker}): Checking TV state after Apple TV reset.", level="INFO")
        tv_state = self.get_state(self.tv_entity)
        if not self._is_tv_actually_off():
            sony_state = self.get_state(self.sony_tv_entity)
            apple_state = self.get_state(self.apple_tv_entity)
            self.log(
                f"Lift sequence ({path_marker}): TV still active "
                f"(universal='{tv_state}', sony='{sony_state}', apple='{apple_state}') - skipping lift raise",
                level="INFO",
            )
        else:
            try:
                # Stop any current lift motion, then raise after a brief delay
                # Update command time so raise can check interval properly
                # Stop command doesn't use position entity, so no verification needed
                self._reset_lift_action_flag()
                self._lift_action_in_progress = True
                self._lift_action_start_time = self.datetime()
                self._last_lift_command_time = self.datetime()
                self.call_service("script/turn_on", entity_id="script.bedroom_tv_lift_stop")
                self.log(f"Lift sequence ({path_marker}): TV is off - stopping lift.", level="INFO")
                # Clear stop flag after script execution (stop uses direction entity, not position)
                try:
                    self.run_in(self._clear_lift_action_flag, self.lift_command_timeout_s)
                except Exception:
                    self._reset_lift_action_flag()
                # Schedule raise after stop (will auto-reschedule if too soon)
                self.run_in(self._raise_lift_after_stop, 1, path_marker=path_marker, allow_immediate_after_stop=False)
            except Exception as e:
                self.log(f"Lift sequence ({path_marker}): Error calling lift script: {e}", level="ERROR")
                self._reset_lift_action_flag()
        
        # Reset the flag after the lift sequence (or attempt) is complete
        self.apple_tv_reset_in_progress = False
        self.apple_tv_reset_start_time = None
        # self.log(f"Reset flag set to False after lift_sequence_after_reset (path: {path_marker}).", level="DEBUG")

    def _raise_lift_if_still_off(self, kwargs):
        """After OFF debounce, verify Sony + Apple TV before raising lift."""
        self._pending_raise_handle = None
        path_marker = kwargs.get("path_marker", "tv_off")

        if not self._is_tv_actually_off():
            tv_state = self.get_state(self.tv_entity)
            sony_state = self.get_state(self.sony_tv_entity)
            apple_state = self.get_state(self.apple_tv_entity)
            # Auto-heal: the screen (Sony) and the universal player read off, but the Apple TV
            # was left active (e.g. paused) and is blocking the raise. Sony state is unreliable,
            # so refresh via the Apple TV remote - sleeping it (remote.turn_off) clears the stale
            # active state - then raise the lift once it reports off.
            sony_off = sony_state in ("off", "unavailable", "unknown", "standby", None)
            apple_active = apple_state in ("paused", "idle", "on")  # NOT "playing": genuine playback must never be slept/refreshed/raised
            if tv_state == "off" and sony_off and apple_active and self.apple_tv_reset_enabled and not self._apple_refresh_in_progress:
                self._apple_refresh_in_progress = True
                self.log(
                    f"TV OFF but Apple TV still active (sony='{sony_state}', apple='{apple_state}') "
                    f"- sleeping Apple TV via remote to refresh, then raising lift",
                    level="INFO",
                )
                try:
                    self.call_service("remote/turn_off", entity_id="remote.bedroom_apple_tv")
                    self.run_in(self._raise_after_apple_refresh, 3, path_marker=path_marker)
                except Exception as e:
                    self._apple_refresh_in_progress = False
                    self.log(f"Error sleeping Apple TV for refresh: {e}", level="ERROR")
                return
            self.log(
                f"TV OFF debounce complete but TV still active "
                f"(universal='{tv_state}', sony='{sony_state}', apple='{apple_state}') - skipping lift raise",
                level="INFO",
            )
            return

        self.log("TV OFF verified (Sony + Apple TV): raising lift to UP position", level="INFO")
        try:
            self._reset_lift_action_flag()
            self._lift_action_in_progress = True
            self._lift_action_start_time = self.datetime()
            self._last_lift_command_time = self.datetime()
            self.call_service("script/turn_on", entity_id="script.bedroom_tv_lift_stop")
            self.log("Stopping lift before raising", level="DEBUG")
            try:
                self.run_in(self._clear_lift_action_flag, self.lift_command_timeout_s)
            except Exception:
                self._reset_lift_action_flag()
            self.run_in(self._raise_lift_after_stop, 1, path_marker=path_marker, allow_immediate_after_stop=False)
        except Exception as e:
            self.log(f"Error handling verified TV OFF: {e}", level="ERROR")
            self._reset_lift_action_flag()

    def _raise_after_apple_refresh(self, kwargs):
        """After sleeping the Apple TV (to clear a stale active state), raise the lift if the TV is now off."""
        self._apple_refresh_in_progress = False
        if self._is_tv_actually_off():
            self.log("Apple TV refreshed -> TV now off: raising lift", level="INFO")
            try:
                self.run_in(self._raise_lift_after_stop, 1, path_marker="apple_refresh", allow_immediate_after_stop=True)
            except Exception as e:
                self.log(f"Error scheduling raise after Apple TV refresh: {e}", level="ERROR")
        else:
            sony_state = self.get_state(self.sony_tv_entity)
            apple_state = self.get_state(self.apple_tv_entity)
            self.log(
                f"After Apple TV refresh, TV still not off (sony='{sony_state}', apple='{apple_state}') - not raising",
                level="WARNING",
            )

    def _raise_lift_after_stop(self, kwargs):
        """
        TV OFF -> Lift Up
        Called when TV turns off. Raises the lift to UP position.
        """
        path_marker = kwargs.get("path_marker", "tv_off")
        self._raise_attempt = int(kwargs.get("attempt", 1))
        # Reset flag first
        self._reset_lift_action_flag()
        # Check minimum interval between commands
        allow_immediate = kwargs.get("allow_immediate_after_stop", False)
        if not allow_immediate and not self._can_send_lift_command():
            # If too soon, reschedule for later
            remaining = self.min_lift_command_interval_s - (self.datetime() - self._last_lift_command_time).total_seconds()
            if remaining > 0:
                self.log(f"Too soon after last command, rescheduling raise in {remaining:.1f}s", level="INFO")
                try:
                    self.run_in(self._raise_lift_after_stop, remaining, path_marker=path_marker, allow_immediate_after_stop=True)
                except Exception as e:
                    self.log(f"Error rescheduling raise: {e}", level="ERROR")
            return

        # Commanding a flapping LocalTuya device is a lost command (2026-07-13
        # 00:37-01:10 the lift select flapped unavailable and the 01:05 raise
        # evaporated) - defer instead of firing into the void.
        if self.get_state("select.bedroom_tv_lift_position_configuration") in (None, "unknown", "unavailable"):
            self.log(f"Lift device unavailable - deferring raise (attempt {self._raise_attempt})", level="WARNING")
            self._schedule_raise_retry()
            return

        try:
            self._lift_action_in_progress = True
            self._lift_action_start_time = self.datetime()
            self._last_lift_command_time = self.datetime()
            # Set expected state for verification
            if self.lift_verify_enabled:
                self._expected_lift_state = "Up"
                try:
                    self._lift_verify_timeout_handle = self.run_in(self._lift_verify_timeout, 2.0)
                except Exception:
                    pass
            self.call_service("script/turn_on", entity_id="script.bedroom_tv_lift_position_up")
            self.log(f"TV OFF: Raising lift to UP position", level="INFO")
            # Update persistent position tracking
            self._update_lift_position("Up")
            # Stop periodic verification when TV is off
            self._stop_periodic_verification()
            # The select can echo Up -> Clear command and the node die right
            # after (2026-07-13 01:05): re-check in 2 min, retry if it flapped.
            self._post_raise_flap = False
            if self._post_raise_check_handle:
                self._safe_cancel_timer(self._post_raise_check_handle)
            try:
                self._post_raise_check_handle = self.run_in(self._post_raise_check, 120)
            except Exception:
                self._post_raise_check_handle = None
            # Clear flag after script execution (fallback if verification disabled)
            if not self.lift_verify_enabled:
                try:
                    self.run_in(self._clear_lift_action_flag, self.lift_command_timeout_s)
                except Exception:
                    self._reset_lift_action_flag()
        except Exception as e:
            self.log(f"Error raising lift: {e}", level="ERROR")
            self._reset_lift_action_flag()

    def _lower_lift_if_still_on(self, kwargs):
        """
        TV ON -> Lift Down
        Called when TV turns on. Lowers the lift to DOWN position.
        """
        # Clear the handle
        self._pending_lower_handle = None
        # Mark this as coming from a TV on transition to force the command
        kwargs["from_tv_on_transition"] = True
        kwargs["force_lower"] = True
        # Use the shared ensure function
        self._ensure_lift_down_if_tv_active(kwargs)

    def _ensure_lift_down_if_tv_active(self, kwargs):
        """
        TV ON -> Lift Down
        Ensure lift is down when TV is active.
        Called from TV ON transition, active state transitions, initial state check, periodic verification.
        """
        try:
            tv_state = self.get_state(self.tv_entity)
            if self._is_tv_actually_on():
                # Check if we think lift is already down
                current_position = self._get_lift_position()
                self.log(f"TV is active (state: '{tv_state}'), current lift position: '{current_position}'", level="DEBUG")
                
                # If this is from a TV on transition, always send the command (position entity might be stale)
                if kwargs.get("from_tv_on_transition", False):
                    self.log(f"TV just turned on, forcing lift down command (position entity: '{current_position}')", level="INFO")
                # Only skip if we're certain the lift is down AND we're not forcing
                elif current_position == "Down" and not kwargs.get("force_lower", False):
                    self.log(f"Lift already down (position: {current_position}), skipping command", level="DEBUG")
                    # Start periodic verification if enabled
                    self._start_periodic_verification()
                    return
                
                # Reset flag first
                self._reset_lift_action_flag()
                # Check minimum interval between commands
                if not self._can_send_lift_command():
                    # If too soon, reschedule for later
                    remaining = self.min_lift_command_interval_s - (self.datetime() - self._last_lift_command_time).total_seconds()
                    if remaining > 0:
                        self.log(f"Too soon after last command, rescheduling lower in {remaining:.1f}s", level="INFO")
                        try:
                            self._pending_lower_handle = self.run_in(self._ensure_lift_down_if_tv_active, remaining)
                        except Exception as e:
                            self.log(f"Error rescheduling lift lower: {e}", level="ERROR")
                    return
                
                self._lift_action_in_progress = True
                self._lift_action_start_time = self.datetime()
                self._last_lift_command_time = self.datetime()
                # Set expected state for verification
                if self.lift_verify_enabled:
                    self._expected_lift_state = "Down"
                    try:
                        self._lift_verify_timeout_handle = self.run_in(self._lift_verify_timeout, 2.0)
                    except Exception:
                        pass
                self.call_service("script/turn_on", entity_id="script.bedroom_tv_lift_position_down")
                self.log("TV ON: Lowering lift to DOWN position", level="INFO")
                # Update persistent position tracking
                self._update_lift_position("Down")
                # Start periodic verification if enabled
                self._start_periodic_verification()
                # Clear flag after script execution (fallback if verification disabled)
                if not self.lift_verify_enabled:
                    try:
                        self.run_in(self._clear_lift_action_flag, self.lift_command_timeout_s)
                    except Exception:
                        self._reset_lift_action_flag()
            else:
                self.log("TV is off, skipping lift lower", level="DEBUG")
                # Stop periodic verification when TV is off
                self._stop_periodic_verification()
        except Exception as e:
            self.log(f"Error lowering lift: {e}", level="ERROR")
            self._reset_lift_action_flag()

    def _start_periodic_verification(self):
        """Start periodic verification that lift is down when TV is active"""
        if not self.periodic_verification_enabled:
            return
        # Cancel existing verification if any
        if self._periodic_verification_handle is not None:
            self._safe_cancel_timer(self._periodic_verification_handle)
        try:
            self._periodic_verification_handle = self.run_in(self._periodic_lift_verification, self.periodic_verification_interval_s)
            self.log(f"Scheduled periodic lift verification in {self.periodic_verification_interval_s}s", level="DEBUG")
        except Exception as e:
            self.log(f"Error scheduling periodic verification: {e}", level="WARNING")
            self._periodic_verification_handle = None

    def _stop_periodic_verification(self):
        """Stop periodic verification when TV is off"""
        if self._periodic_verification_handle is not None:
            self._safe_cancel_timer(self._periodic_verification_handle)
            self._periodic_verification_handle = None

    def _periodic_lift_verification(self, kwargs):
        """Periodically verify lift is down when TV is active"""
        self._periodic_verification_handle = None
        try:
            if self._is_tv_actually_on():
                # TV is still active - check if lift should be down
                self.log("Periodic verification: TV is active, checking lift position", level="DEBUG")
                self._ensure_lift_down_if_tv_active(kwargs)
            else:
                # TV is off - stop periodic verification
                self._stop_periodic_verification()
        except Exception as e:
            self.log(f"Error in periodic lift verification: {e}", level="WARNING")

    def _apple_tv_remote_reset(self, kwargs):
        """Toggle the Bedroom Apple TV remote power to refresh state"""
        try:
            self._bedroom_apple_tv_reset_handle = None
            self._bedroom_apple_tv_reset_executing = True  # Mark reset sequence as executing
            remote_entity = "remote.bedroom_apple_tv"
            try:
                self.call_service("remote/turn_off", entity_id=remote_entity)
            except Exception as e:
                self.log(f"Bedroom Apple TV remote turn_off failed: {e}", level="WARNING")
            # Turn back on shortly after
            try:
                self.run_in(self._apple_tv_remote_turn_on, 2, remote_entity=remote_entity)
            except Exception as e:
                self.log(f"Scheduling Bedroom Apple TV remote turn_on failed: {e}", level="WARNING")
                self._bedroom_apple_tv_reset_executing = False  # Clear flag on error
        except Exception as e:
            self.log(f"Error in Bedroom Apple TV remote reset: {e}", level="WARNING")
            self._bedroom_apple_tv_reset_executing = False  # Clear flag on error

    def _apple_tv_remote_turn_on(self, kwargs):
        """Complete the remote reset sequence by turning it back on"""
        try:
            remote_entity = kwargs.get("remote_entity", "remote.bedroom_apple_tv")
            self.call_service("remote/turn_on", entity_id=remote_entity)
            # Verify the remote ends ON; retry once if needed
            if self._bedroom_apple_tv_remote_verify_handle is not None:
                self._safe_cancel_timer(self._bedroom_apple_tv_remote_verify_handle)
            try:
                self._bedroom_apple_tv_remote_verify_handle = self.run_in(
                    self._ensure_remote_on,
                    3,
                    remote_entity=remote_entity,
                    retries_remaining=0,
                )
            except Exception as e:
                self.log(f"Scheduling Bedroom Apple TV remote ensure-on failed: {e}", level="WARNING")
        except Exception as e:
            self.log(f"Bedroom Apple TV remote turn_on failed: {e}", level="WARNING")

    def _ensure_remote_on(self, kwargs):
        """Ensure the Bedroom Apple TV remote entity ends in the ON state"""
        try:
            remote_entity = kwargs.get("remote_entity", "remote.bedroom_apple_tv")
            retries_remaining = int(kwargs.get("retries_remaining", 0))
            state = None
            try:
                state = self.get_state(remote_entity)
            except Exception:
                state = None
            if state != "on":
                try:
                    self.call_service("remote/turn_on", entity_id=remote_entity)
                except Exception:
                    pass
            # Done; clear handles and flags
            self._bedroom_apple_tv_remote_verify_handle = None
            self._bedroom_apple_tv_reset_executing = False  # Reset sequence complete
        except Exception as e:
            self.log(f"Error ensuring Bedroom Apple TV remote ON: {e}", level="WARNING")
            self._bedroom_apple_tv_reset_executing = False  # Clear flag on error

    def remote_button_handler(self, event_name, data, kwargs):
        """Handle Z-Wave remote button presses."""
        if data.get("command_class") != self.zwave_command_class:
            return
            
        scene = data.get("property_key")
        value = data.get("value")
        
        if scene is None or value is None:
            return
            
        if scene == self.button_4_property_key and value == "KeyPressed": # Play/pause
            self.call_service("media_player/media_play_pause", entity_id="media_player.bedroom_tv")
        elif scene == self.button_4_property_key and value == "KeyHeldDown": # Toggle TV system
            current_tv_state = self.get_state("media_player.bedroom_tv")
            if current_tv_state == "off":
                self.call_service("media_player/turn_on", entity_id="media_player.bedroom_tv") 
            else:
                self.call_service("media_player/turn_off", entity_id="media_player.bedroom_tv")
        elif scene == self.button_1_property_key and value == "KeyPressed": # Volume down
            self.adjust_volume(-self.volume_step)
        elif scene == self.button_2_property_key and value == "KeyPressed": # Volume up
            self.adjust_volume(self.volume_step)

    def _tv_volume_controllable(self):
        """Sony Bravia rejects volume_set when the TV is off."""
        state = self.get_state(self.volume_entity)
        return state not in ("off", "unavailable", "unknown", None)

    def adjust_volume(self, delta, retry_count=0):
        """Adjust TV volume with retry logic."""
        max_retries = 2
        retry_delay = 0.5

        if not self._tv_volume_controllable():
            self.log(
                f"Skipping volume adjust: {self.volume_entity} is {self.get_state(self.volume_entity)!r}",
                level="DEBUG",
            )
            return

        try:
            current_volume = self.get_state(self.volume_entity, attribute="volume_level")
            if current_volume is None:
                current_volume = self.args.get("default_volume", 0.5)  # Get default volume from config
            
            new_volume = max(0, min(1, float(current_volume) + delta))
            self.call_service(
                "media_player/volume_set",
                entity_id=self.volume_entity,
                volume_level=new_volume
            )
        except Exception as e:
            if "turned off" in str(e).lower():
                self.log(f"Skipping volume adjust: {e}", level="DEBUG")
                return
            if retry_count < max_retries:
                self.log(f"Error adjusting volume (attempt {retry_count + 1}/{max_retries + 1}): {str(e)}, retrying in {retry_delay}s...", level="WARNING")
                try:
                    self.run_in(lambda kwargs: self.adjust_volume(delta, retry_count + 1), retry_delay)
                except Exception as retry_error:
                    self.log(f"Error scheduling volume retry: {str(retry_error)}", level="ERROR")
            else:
                self.log(f"Error adjusting volume after {max_retries + 1} attempts: {str(e)}", level="ERROR")

    def lift_position_handler(self, entity, attribute, old, new, kwargs):
        """Handle manual lift position changes for bidirectional control and command verification"""
        # LocalTuya flap bookkeeping for the post-raise check: a node that drops
        # unavailable shortly after an Up command probably never moved the motor.
        if new == "unavailable":
            self._post_raise_flap = True
        # Command verification: Check if this is the expected state change from our command
        if self._lift_action_in_progress and self.lift_verify_enabled and self._expected_lift_state:
            if new == self._expected_lift_state:
                # Command was sent successfully - state changed to expected value
                self.log(f"Lift command verification: State changed to '{new}' (command received)", level="INFO")
                # Update persistent position tracking
                self._update_lift_position(new)
                # Cancel verification timeout, wait for "Clear command"
                if self._lift_verify_timeout_handle:
                    self._safe_cancel_timer(self._lift_verify_timeout_handle)
                    self._lift_verify_timeout_handle = None
                # Schedule check for "Clear command" (script completes ~1s after state change)
                try:
                    self._lift_verify_clear_handle = self.run_in(self._verify_lift_clear_command, 1.5)
                except Exception as e:
                    self.log(f"Error scheduling lift clear verification: {e}", level="WARNING")
                return
            elif new == "Clear command" and self._lift_verify_clear_handle:
                # Script completed - "Clear command" appeared
                self.log(f"Lift command verification: Script completed ('Clear command' received)", level="INFO")
                if self._lift_verify_clear_handle:
                    self._safe_cancel_timer(self._lift_verify_clear_handle)
                    self._lift_verify_clear_handle = None
                # Clear flag after script completion (physical movement may still be in progress)
                # Use shorter timeout since we know script completed
                try:
                    self.run_in(self._clear_lift_action_flag, max(1.0, self.lift_command_timeout_s - 1.5))
                except Exception:
                    self._reset_lift_action_flag()
                self._expected_lift_state = None
                return
        
        # Manual lift position changes (bidirectional control)
        # Ignore if this change was triggered by our own code
        if self._lift_action_in_progress:
            self.log(f"Ignoring lift position change (from '{old}' to '{new}') - triggered by our code", level="DEBUG")
            return
        
        # Ignore transitions from "Clear command" (these are from script execution, not manual)
        if old == "Clear command":
            self.log(f"Ignoring lift position change (from 'Clear command' to '{new}') - from script execution", level="DEBUG")
            return
        
        # Ignore "Clear command" transitions and None/unknown states
        if new in [None, "Clear command", "unknown", "unavailable"]:
            return
        if old in [None, "unknown", "unavailable"]:
            return
        
        # Only react to manual position changes (not direction commands)
        # Position commands: "Up", "Down", "Temporary", "Save up position", "Save down position", "Save temporary position"
        if new == "Down":
            self.log(f"Manual lift DOWN detected: Turning TV on", level="INFO")
            # Update persistent position tracking
            self._update_lift_position("Down")
            try:
                tv_state = self.get_state("media_player.bedroom_tv")
                # Check if TV is in an off state
                # Note: Universal TV entity shows "on" when Apple TV is idle, so we only check for "off"
                if tv_state in ["off"]:
                    self.call_service("media_player/turn_on", entity_id="media_player.bedroom_tv")
                else:
                    self.log(f"TV already on (state: '{tv_state}'), no action needed", level="DEBUG")
            except Exception as e:
                self.log(f"Error turning TV on from manual lift down: {e}", level="ERROR")
        elif new == "Up":
            self.log(f"Manual lift UP detected: Turning TV off", level="INFO")
            # Update persistent position tracking
            self._update_lift_position("Up")
            try:
                tv_state = self.get_state("media_player.bedroom_tv")
                # Check if TV is in an active state (not off)
                # Note: Universal TV entity shows "on" when Apple TV is idle, so we only check for "off"
                if tv_state not in ["off"]:
                    self.call_service("media_player/turn_off", entity_id="media_player.bedroom_tv")
                else:
                    self.log(f"TV already off, no action needed", level="DEBUG")
            except Exception as e:
                self.log(f"Error turning TV off from manual lift up: {e}", level="ERROR")

    def _update_lift_position(self, position):
        """
        Update persistent lift position tracking.
        Uses input_select helper entity if available, falls back to in-memory tracking.
        """
        # Update in-memory tracking
        self._last_known_lift_position = position
        
        # Update persistent entity if it exists
        try:
            entity_state = self.get_state(self.lift_position_entity)
            if entity_state is not None:
                # Entity exists, update it
                if position in ["Up", "Down", "Temporary"]:
                    self.call_service("input_select/select_option", 
                                     entity_id=self.lift_position_entity, 
                                     option=position)
                    self.log(f"Updated persistent lift position to '{position}'", level="DEBUG")
                else:
                    self.log(f"Unknown position '{position}', not updating persistent entity", level="DEBUG")
            else:
                # Entity doesn't exist yet, log for manual creation
                self.log(f"Lift position entity '{self.lift_position_entity}' not found. Create it as input_select helper with options: Up, Down, Temporary, Unknown", level="WARNING")
        except Exception as e:
            # Entity doesn't exist or error accessing it - that's okay, we have in-memory fallback
            self.log(f"Could not update persistent lift position entity (will use in-memory tracking): {e}", level="DEBUG")
    
    def _get_lift_position(self):
        """
        Get current lift position from persistent entity or in-memory tracking.
        Returns: "Up", "Down", "Temporary", or None (unknown)
        """
        # Try persistent entity first
        try:
            entity_state = self.get_state(self.lift_position_entity)
            if entity_state and entity_state in ["Up", "Down", "Temporary", "Unknown"]:
                if entity_state != "Unknown":
                    return entity_state
        except Exception:
            pass
        
        # Fall back to in-memory tracking
        return self._last_known_lift_position

# End of class BedroomTVControl 