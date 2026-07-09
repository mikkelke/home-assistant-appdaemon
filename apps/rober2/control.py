"""
Rober2 Control - Simplified Version
Simple and reliable vacuum automation with door checking and room ordering.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import datetime
import asyncio
import os
import urllib.request
import urllib.error
import json

class Rober2Control(hass.Hass):
    """Simple vacuum control for Rober2 with door checking."""
    
    def initialize(self):
        """Initialize the app."""
        self.log("Rober2 Control initializing...")
        
        # Simple state tracking
        self.cleaning = False
        self.current_room = None
        self.last_state = "unknown"
        self.desired_state = "idle"
        self.room_start_time = None  # Track when room cleaning started
        self.fan_speed_enforced = False  # Track if fan speed is enforced
        self.peak_progress = 0  # Track highest progress reached during current room cleaning
        self.last_progress_update = None  # Track when we last updated progress
        # Track rooms actually observed while robot was in cleaning state
        self._visited_rooms = set()
        # Track when we initialized while robot was already cleaning (grace for completion)
        self._init_tracking_ts = None
        # Bin empty state is tracked in Home Assistant entity: input_boolean.rober2_bin_empty_triggered
        # Session-scoped: incremented when a room is fully completed; cleared after bin empty finishes.
        self._rooms_completed_this_session = 0
        
        # Get reference to MobileNotifier app (if available)
        try:
            self.mobile_notifier = self.get_app("MobileNotifier")
            self.log("Mobile Notifier app found", level="INFO")
        except Exception:
            self.mobile_notifier = None
            self.log("Mobile Notifier app not found - notifications will be disabled", level="WARNING")
        
        # Map image saving configuration
        self.map_image_entity = "image.rober2_rooftop"
        # Save directory: use Home Assistant's www folder for web access
        # Default: /www/rober2_maps (mounted from /data/homeassistant/www in docker-compose)
        # Accessible via /local/rober2_maps/ in Home Assistant frontend
        self.map_save_dir = self.args.get("map_save_dir", "/www/rober2_maps")
        # Retention: keep last N images per room (default: 3 images per room)
        self.map_retention_per_room = int(self.args.get("map_retention_per_room", 3))
        # Ensure directory exists
        try:
            os.makedirs(self.map_save_dir, exist_ok=True)
            self.log(f"Map save directory: {self.map_save_dir} (retention: {self.map_retention_per_room} images per room)", level="INFO")
        except Exception as e:
            self.log(f"Error creating map save directory: {e}", level="ERROR")
            self.map_save_dir = None  # Disable saving if directory creation fails
        
        # Set up daily cleanup task for old map images
        if self.map_save_dir:
            # Run cleanup daily at 2 AM
            self.run_daily(self.cleanup_old_map_images, datetime.time(2, 0))
            # Also run cleanup on startup (after a short delay)
            self.run_in(self.cleanup_old_map_images, 30)
        
        # Room configuration
        self.room_config = {
            "16": "kristines_room",
            "17": "hallway", 
            "18": "living_room",
            "19": "guest_bathroom",
            "20": "bedroom",
            "21": "dining_room",
            "22": "kitchen",
            "23": "claudias_room",
            "24": "bathroom",
            "25": "kitchen_2"
        }
        
        # Room cleaning order (priority order)
        self.room_order = [
            '17',  # hallway first
            '22',  # kitchen
            '25',  # kitchen_2
            '18',  # living_room
            '21',  # dining_room
            '23',  # claudias room (robot map segment still labeled Office)
            '16',  # kristines_room
            '20',  # bedroom
            '24',  # bathroom
            '19'   # guest_bathroom
        ]
        
        # Constants for immediate room queuing
        self.SEND_RETRY_SEC = 25          # retry if robot didn't leave after 25 s (increased for latency)
        
        # Door sensors for rooms that have doors
        self.door_sensors = {
            '16': 'binary_sensor.kristines_room_door_contact',
            '19': 'binary_sensor.guest_bathroom_door_contact',
            '20': 'binary_sensor.bedroom_door_contact',
            '23': 'binary_sensor.claudias_room_door_contact',
            '24': 'binary_sensor.bathroom_door_contact'
        }
        
        # Set up event listeners
        self.listen_state(self.handle_vacuum_state, "vacuum.rober2")
        self.listen_state(self.handle_presence, "zone.home")
        self.listen_state(self.handle_room_change, "input_text.rober2_current_room")
        self.listen_state(self.handle_battery, "sensor.rober2_battery")
        self.listen_state(self.handle_fan_speed, "vacuum.rober2", attribute="fan_speed")
        self._automation_enabled_entity = self.args.get(
            "automation_enabled_entity", "input_boolean.rober2_enabled"
        )
        self.listen_state(self.handle_automation_enabled, self._automation_enabled_entity)
        self.listen_state(self.handle_progress, "sensor.rober2_cleaning_progress")
        self.listen_state(self.handle_robot_status, "sensor.rober2_status")
        self.listen_state(self.handle_vacuum_error, "sensor.rober2_vacuum_error")
        
        # Track person entities for robust away detection (unknown/unavailable treated as away)
        self.person_entities = [
            "person.mikkel",
            "person.kristine",
        ]
        for person in self.person_entities:
            self.listen_state(self.handle_person_state, person)
        
        # Listen to room cleaning flag changes to update the job queue live
        self._room_flag_entity_to_id = {}
        try:
            for room_id, room_name in self.room_config.items():
                entity_id = f"input_boolean.rober2_clean_{room_name}"
                self._room_flag_entity_to_id[entity_id] = room_id
                self.listen_state(self.handle_room_flag_change, entity_id)
        except Exception as e:
            self.log(f"Error setting room flag listeners: {e}", level="ERROR")
        
        # Door sensors are only checked before starting cleaning, not during    
        
        # Set up quiet hours change listeners
        self.listen_state(self.handle_quiet_hours_change, "input_datetime.quiet_hours_start")
        self.listen_state(self.handle_quiet_hours_change, "input_datetime.quiet_hours_end")
        
        # Fan speed enforcement: ensure max_plus during cleaning (state-driven, no periodic polling)
        self.min_fan_set_interval_sec = int(self.args.get("min_fan_set_interval_sec", 5))
        self._last_fan_set = 0.0
        
        # Removed periodic heartbeat watchdog in favor of pure state-driven logic
        
        # Set up dynamic quiet hours triggers (will be updated during initialization)
        self.run_in(self.setup_quiet_hours_triggers, 0.5)
        
        # Manual override via input_boolean.clean_while_home
        self.listen_state(self.handle_clean_while_home, "input_boolean.clean_while_home")
        self.listen_state(self.handle_automation_paused_reset, "input_boolean.rober2_automation_paused")

        self.log("Rober2 Control initialized successfully")
        
        # Log current room schedule status
        self.run_in(self.log_room_schedule_status, 1)
        
        # Evaluate initial conditions after a short delay
        self.run_in(self.evaluate_cleaning_conditions, 2, trigger="initialization")

        # Per-room consecutive 0% interrupt counter (resets on success or manual re-enable)
        self._room_interrupt_counts = {}
        self.MAX_ROOM_RETRIES = int(self.args.get("max_room_retries", 3))
        self.MIN_DURATION_FOR_ZERO_PROGRESS = int(self.args.get("min_duration_zero_progress_sec", 10))
        # Max time (seconds) after segment command to count a dock as a "failed attempt". Prevents
        # unavailable->docked (e.g. network blip) hours later from being counted as attempt 3/3.
        self.MAX_ELAPSED_FOR_ZERO_PROGRESS_SEC = int(self.args.get("max_elapsed_zero_progress_sec", 30 * 60))

        # When we send a segment command but never get "cleaning" (robot blocked), we still want to count and notify
        self._last_attempted_room_id = None
        self._last_segment_command_time = None

        # Track whether robot ever physically left the dock room this session.
        # Stays False -> robot is blocked at dock (obstacle). Becomes True -> robot leaves freely.
        self._robot_has_left_dock = False
        self._dock_room_name = self.args.get("dock_room_name", "Kitchen1")
        self._dock_room_segment_id = next(
            (rid for rid, name in self.room_config.items() if name == "kitchen_2"),
            "25",
        )
        self.listen_state(self.handle_physical_room_change, "sensor.rober2_current_room")

        # Concurrency + timeout guards (prevents slow/overlapping callbacks)
        self._eval_in_progress = False
        self._ha_call_timeout_s = float(self.args.get("ha_call_timeout_s", 8))
        # Vacuum commands: hass_timeout on call_service waits for HA's WebSocket ack (integration may run long)
        self._vacuum_command_timeout_s = float(self.args.get("vacuum_command_timeout_s", 30))
        # Segment clean: prefer HA script (script.turn_on returns once the script is started, not when vacuum finishes)
        self._use_segment_clean_script = bool(self.args.get("use_segment_clean_script", True))
        self._segment_clean_script_entity = str(
            self.args.get("segment_clean_script_entity", "script.rober2_app_segment_clean")
        ).strip()
        self._segment_script_hass_timeout_s = float(self.args.get("segment_script_hass_timeout_s", 15))
        self._map_download_timeout_s = float(self.args.get("map_download_timeout_s", 10))
        # Segment send: avoid duplicate sends. When a room completes, handle_vacuum_state turns off
        # the room flag then calls queue_next_room; the turn_off triggers handle_room_flag_change
        # which also calls queue_next_room -> two app_segment_clean calls in parallel -> HA timeout.
        self._segment_send_lock = asyncio.Lock()
        self._last_scheduled_segment_room = None
        self._last_scheduled_segment_ts = None
        self._segment_dedupe_sec = float(self.args.get("segment_send_dedupe_sec", 5))

        # Human-readable cleaning narrative for UI (input_text in HA)
        raw_narrative = self.args.get("narrative_entity")
        if raw_narrative is None:
            self._narrative_entity = "input_text.rober2_cleaning_narrative"
        else:
            self._narrative_entity = str(raw_narrative).strip() or None
        self._narrative_labels = dict(self.args.get("narrative_labels") or {})
        self._last_narrative = None
        self._narrative_max_len = int(self.args.get("narrative_max_len", 255))

    # --- Small helpers ---
    def _is_no_error_state(self, value) -> bool:
        """Return True if a vacuum error state represents 'no error'."""
        return value in [None, "", "none", "None", "unknown", "unavailable"]

    async def _partition_accessible_rooms(self, room_ids: list[str]):
        """Return (accessible, blocked) room id lists, preserving order."""
        accessible: list[str] = []
        blocked: list[str] = []
        for rid in room_ids or []:
            try:
                if await self.is_room_accessible(rid):
                    accessible.append(rid)
                else:
                    blocked.append(rid)
            except Exception:
                blocked.append(rid)
        return accessible, blocked
        
    async def _safe_call_service(self, service: str, timeout_s: float = None, **kwargs) -> bool:
        """Call Home Assistant; wait for the Hass plugin WebSocket ack using hass_timeout.

        asyncio.wait_for around call_service does not extend the plugin's own wait - use
        hass_timeout so the per-call limit matches HA's response time (see AppDaemon HASS API).
        Vacuum/* defaults to _vacuum_command_timeout_s; pass timeout_s to override (e.g. script.turn_on).
        """
        try:
            if timeout_s is None:
                to = self._vacuum_command_timeout_s if service.startswith("vacuum/") else self._ha_call_timeout_s
            else:
                to = float(timeout_s)

            await self.call_service(service, hass_timeout=to, **kwargs)
            return True
        except Exception as e:
            self.log(f"Error calling service {service}: {e}", level="WARNING")
            return False

    def _should_skip_duplicate_segment_send(self, room_id: str) -> bool:
        """True if we already scheduled a segment send for this room within the dedupe window.
        Avoids double-send when room_completed and room_flag_change both call queue_next_room.
        Call only while holding _segment_send_lock (queue_next_room/start_cleaning), or for a
        best-effort hint when not racing."""
        if not room_id:
            return False
        last_room = getattr(self, "_last_scheduled_segment_room", None)
        last_ts = getattr(self, "_last_scheduled_segment_ts", None)
        if str(last_room) != str(room_id) or last_ts is None:
            return False
        try:
            elapsed = (datetime.datetime.now() - last_ts).total_seconds()
            return elapsed < self._segment_dedupe_sec
        except Exception:
            return False

    def _safe_cancel_timer(self, handle) -> bool:
        """Cancel a timer only if it is still running (avoids invalid-handle warnings)."""
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
                return True
        except Exception:
            pass
        return False

    def _pretty_room_name(self, internal: str) -> str:
        """Map room_config internal name (e.g. kitchen_2) to display string for narrative."""
        if not internal:
            return ""
        labels = getattr(self, "_narrative_labels", None) or {}
        if internal in labels:
            return str(labels[internal])
        return str(internal).replace("_", " ").title()

    def _pretty_room_from_segment_id(self, room_id) -> str:
        """Segment id (e.g. '22') -> pretty name via room_config."""
        if room_id is None:
            return ""
        rid = str(room_id)
        internal = self.room_config.get(rid, rid)
        return self._pretty_room_name(internal)

    def _dock_pretty_name(self) -> str:
        return self._pretty_room_name("kitchen_2")

    def _is_dock_room_attempt(self, room_id, room_name: str) -> bool:
        """True when the segment being cleaned is the dock room (kitchen dining side)."""
        rid = str(room_id) if room_id is not None else None
        return rid == self._dock_room_segment_id or (room_name or "").lower() == "kitchen_2"

    async def _is_automation_enabled(self) -> bool:
        """True when the user master enable toggle is on."""
        try:
            entity = getattr(self, "_automation_enabled_entity", "input_boolean.rober2_enabled")
            state = await self.get_state(entity)
            if state in [None, "unknown", "unavailable", ""]:
                return True
            return state == "on"
        except Exception:
            return True

    async def _set_narrative(self, text: str, force: bool = False) -> None:
        """Update input_text cleaning narrative; failures are non-fatal."""
        entity = getattr(self, "_narrative_entity", None)
        if not entity or not text:
            return
        if not force and text == getattr(self, "_last_narrative", None):
            return
        max_len = max(1, int(getattr(self, "_narrative_max_len", 255) or 255))
        if len(text) > max_len:
            text = text[: max_len - 1] + "..."
        try:
            ok = await self._safe_call_service(
                "input_text/set_value",
                entity_id=entity,
                value=text,
            )
            if ok:
                self._last_narrative = text
        except Exception as e:
            self.log(f"Narrative update skipped: {e}", level="DEBUG")

    # --- Home Assistant history helpers (strengthen tracking across restarts) ---
    def _read_recent_history(self, entity_id: str, minutes: int = 120, max_points: int = 50):
        """Return recent state objects from HA history for an entity. Returns [] on error/empty.
        Most recent entry is last in the list.
        """
        try:
            end_time = self.datetime()
            start_time = end_time - datetime.timedelta(minutes=minutes)
            result = self.get_history(entity_id, start_time=start_time, end_time=end_time)
            if not result:
                return []
            return result[0] if isinstance(result, list) else []
        except Exception as e:
            self.log(f"History read error for {entity_id}: {e}", level="ERROR")
            return []

    def _read_recent_text_states(self, entity_id: str, minutes: int = 120, max_points: int = 50):
        """Return recent non-empty text states (strings) from HA history for an entity."""
        series = self._read_recent_history(entity_id, minutes, max_points)
        values = []
        for item in series[-max_points:]:
            try:
                val = item.get("state")
                if isinstance(val, str) and val.strip() != "" and val != "unavailable":
                    values.append(val)
            except Exception:
                continue
        return values

    def _read_recent_numeric_states(self, entity_id: str, minutes: int = 120, max_points: int = 50):
        """Return recent numeric states (floats) from HA history for an entity."""
        series = self._read_recent_history(entity_id, minutes, max_points)
        values = []
        for item in series[-max_points:]:
            try:
                val = float(item.get("state"))
                values.append(val)
            except Exception:
                continue
        return values

    def log_state_conclusion(self, action, current_state, desired_state, conclusion, extra_info=None):
        """Log state changes with conclusions for smart home debugging."""
        try:
            # Build simple state summary
            state_summary = f"State: {action} | {current_state} to {desired_state}"
            if self.cleaning:
                state_summary += f" | cleaning: yes"
            if self.current_room:
                room_name = self.room_config.get(self.current_room, self.current_room)
                state_summary += f" | room: {room_name}"
            
            # Log the state summary
            self.log(state_summary, level="INFO")
            
            # Log simple conclusion
            self.log(f"Action: {conclusion}", level="INFO")
            
            # Update last state
            self.last_state = current_state
            self.desired_state = desired_state
            
        except Exception as e:
            self.log(f"Error in state logging: {e}", level="ERROR")
            
    async def is_away(self) -> bool:
        """Return True if home is considered away.
        New logic: zone.home must be "0" AND all tracked persons must be explicitly 'not_home'.
        Unknown/unavailable person states now BLOCK cleaning (treated as not-away).
        """
        try:
            home_state = await self.get_state("zone.home")
            if str(home_state) != "0":
                return False
            # All tracked persons must be explicitly 'not_home'. Any other state blocks cleaning.
            for person in getattr(self, 'person_entities', []):
                try:
                    person_state = await self.get_state(person)
                    if person_state != "not_home":
                        return False
                except Exception:
                    # On error reading a person entity, block cleaning conservatively
                    return False
            return True
        except Exception as e:
            self.log(f"Error computing away state: {e}", level="ERROR")
            # Conservative fallback: do not consider away on error
            return False

    async def has_active_error(self) -> tuple[bool, str]:
        """Check if robot has an active error. Returns (has_error, error_message).
        Ignores 'unavailable', 'None', 'none', and 'unknown' as these are not errors.
        """
        try:
            error_state = await self.get_state("sensor.rober2_vacuum_error")
            
            # No error if unavailable, None, none, unknown, or empty
            if error_state in [None, "unavailable", "None", "none", "unknown", ""]:
                return False, ""
            
            # Has active error
            return True, str(error_state)
        except Exception as e:
            self.log(f"Error checking vacuum error state: {e}", level="ERROR")
            # Conservative: assume no error on check failure
            return False, ""

    async def evaluate_cleaning_conditions(self, kwargs):
        """Centralized function to evaluate all cleaning conditions and decide what to do."""
        # Coalesce evaluations: if we are already evaluating, skip this trigger.
        # This prevents callback pile-ups when many listeners fire at once.
        if getattr(self, "_eval_in_progress", False):
            try:
                trig = (kwargs or {}).get("trigger", "unknown")
            except Exception:
                trig = "unknown"
            self.log(f"Skipping evaluation trigger '{trig}' (evaluation already in progress)", level="DEBUG")
            return

        self._eval_in_progress = True
        try:
            trigger = kwargs.get("trigger", "unknown")
            
            # Automation paused (e.g. blocked at dock) - do not start cleaning until user resets
            try:
                paused = await self.get_state("input_boolean.rober2_automation_paused")
                if paused == "on":
                    self.log_state_conclusion(f"{trigger}_eval", "automation_paused", "wait",
                                            "Automation paused (fix required), not starting")
                    try:
                        reason = await self.get_state("input_text.rober2_pause_reason") or ""
                        if reason:
                            await self._set_narrative(f"Automation paused - {reason}")
                        else:
                            await self._set_narrative("Automation paused")
                    except Exception:
                        await self._set_narrative("Automation paused")
                    return
            except Exception:
                pass

            # CRITICAL FIX: Check if robot is already cleaning during initialization
            if trigger == "initialization":
                robot_state = await self.get_state("vacuum.rober2")
                if robot_state == "cleaning" and not self.cleaning:
                    # Robot is already cleaning but app tracking is not set up
                    self.log("Robot already cleaning during initialization - setting up tracking", level="INFO")
                    
                    # Set up tracking variables
                    self.cleaning = True
                    self.room_start_time = datetime.datetime.now()  # Best guess; improve using history below
                    self._init_tracking_ts = datetime.datetime.now()
                    # Reset bin empty flag for ongoing cleaning session (persistent in HA)
                    await self.call_service("input_boolean/turn_off", entity_id="input_boolean.rober2_bin_empty_triggered")
                    
                    # CRITICAL FIX: Get current progress instead of resetting to 0
                    current_progress = await self.get_state("sensor.rober2_cleaning_progress")
                    if current_progress and current_progress != "unavailable":
                        try:
                            self.peak_progress = float(current_progress)
                            self.log(f"Initialized with current progress: {self.peak_progress}%", level="INFO")
                        except (ValueError, TypeError):
                            self.peak_progress = 0
                    else:
                        # Try to seed from recent history
                        hist = self._read_recent_numeric_states("sensor.rober2_cleaning_progress", minutes=90, max_points=60)
                        if hist:
                            try:
                                self.peak_progress = max(hist)
                                self.log(f"Initialized progress from history: {self.peak_progress}%", level="INFO")
                            except Exception:
                                self.peak_progress = 0
                        else:
                            self.peak_progress = 0
                    
                    self.fan_speed_enforced = False
                    
                    # Ensure fan speed is enforced immediately if robot already cleaning
                    try:
                        # Only enforce if not already max_plus
                        current_fan = await self.get_state("vacuum.rober2", attribute="fan_speed")
                        if current_fan != "max_plus":
                            now_ts = datetime.datetime.now().timestamp()
                            await self.set_fan_speed("max_plus")
                            self._last_fan_set = now_ts
                            self.fan_speed_enforced = True
                    except Exception:
                        pass
                    
                    # Try to determine which room is being cleaned
                    # Prefer explicit current room helper
                    current_room_text = await self.get_state("input_text.rober2_current_room")
                    if not current_room_text or current_room_text == "unavailable" or current_room_text not in self.room_config:
                        # Use history to find last non-empty valid selection
                        history_vals = self._read_recent_text_states("input_text.rober2_current_room", minutes=180, max_points=100)
                        # Walk from the end to find the last valid segment id
                        detected_room = None
                        for candidate in reversed(history_vals):
                            if candidate in self.room_config:
                                detected_room = candidate
                                break
                        if detected_room:
                            current_room_text = detected_room
                    if current_room_text and current_room_text in self.room_config:
                        self.current_room = current_room_text
                        room_name = self.room_config.get(current_room_text, current_room_text)
                        # Update helper to match
                        await self.call_service("input_text/set_value",
                            entity_id="input_text.rober2_current_room",
                            value=current_room_text
                        )
                        self.log(f"Detected robot cleaning room {current_room_text} ({room_name}) - tracking initialized", level="INFO")
                        self.log_state_conclusion("initialization_tracking", f"robot_cleaning_{room_name}", "track_progress", 
                                                f"Robot already cleaning {room_name} - tracking initialized")
                        await self._set_narrative(
                            f"Cleaning {self._pretty_room_from_segment_id(current_room_text)}"
                        )
                        return
                    
                    # Fallback to scheduled rooms priority only if history provided nothing
                    rooms = await self.get_rooms_to_clean()
                    if rooms:
                        detected_room = rooms[0]
                        self.current_room = detected_room
                        room_name = self.room_config.get(detected_room, detected_room)
                        await self.call_service("input_text/set_value",
                            entity_id="input_text.rober2_current_room",
                            value=detected_room
                        )
                        self.log(f"Assuming robot cleaning room {detected_room} ({room_name}) from schedule priority - tracking initialized", level="INFO")
                        self.log_state_conclusion("initialization_tracking", f"robot_cleaning_{room_name}", "track_progress", 
                                                f"Robot already cleaning {room_name} - tracking initialized (assumed)")
                        await self._set_narrative(
                            f"Cleaning {self._pretty_room_from_segment_id(detected_room)}"
                        )
                        return
                    else:
                        self.log("Robot cleaning but no rooms scheduled/history - cannot determine target room", level="WARNING")
                        return
            
            # Get current states - ALL with await inside async function
            away = await self.is_away()
            clean_while_home = await self.get_state("input_boolean.clean_while_home")
            battery = await self.get_state("sensor.rober2_battery")
            current_time = datetime.datetime.now().strftime("%H:%M")
            
            # Check if robot has active error (pauses all automation)
            has_error, error_msg = await self.has_active_error()
            if has_error:
                self.log_state_conclusion(f"{trigger}_eval", f"error_{error_msg}", "paused", 
                                        f"Robot has active error '{error_msg}' - automation paused")
                await self._set_narrative(f"Waiting - error {str(error_msg).replace('_', ' ')}")
                return
            
            # Check if user has disabled automation
            if not await self._is_automation_enabled():
                self.log_state_conclusion(f"{trigger}_eval", "automation_disabled", "wait",
                                        "Rober2 automation disabled, not starting cleaning")
                await self._set_narrative("Automation disabled")
                return

            # Check if we're already cleaning
            if self.cleaning:
                self.log_state_conclusion(f"{trigger}_eval", "already_cleaning", "continue", 
                                        "Already cleaning, no action needed")
                return
                
            # Check quiet hours
            if not await self.is_within_cleaning_hours():
                self.log_state_conclusion(f"{trigger}_eval", f"time_{current_time}", "wait_for_hours", 
                                        f"Outside cleaning hours ({current_time}), not starting cleaning")
                await self._set_narrative(f"Outside cleaning hours ({current_time})")
                return
                
            # Check battery
            if battery and battery != "unavailable":
                if float(battery) <= 20:
                    self.log_state_conclusion(f"{trigger}_eval", f"battery_low_{battery}%", "wait_for_charge", 
                                            f"Battery too low ({battery}%) for cleaning")
                    return
                    
            # Check if we have rooms to clean
            rooms_all = await self.get_rooms_to_clean()
            rooms, blocked_rooms = await self._partition_accessible_rooms(rooms_all)
            
            # If there are flagged rooms but none are accessible (closed doors), don't treat as "completed".
            if rooms_all and not rooms:
                try:
                    blocked_names = [self.room_config.get(r, r) for r in blocked_rooms]
                    blocked_pretty = ", ".join(
                        self._pretty_room_from_segment_id(r) for r in blocked_rooms
                    )
                    self.log_state_conclusion(
                        f"{trigger}_eval",
                        "rooms_blocked_by_doors",
                        "wait",
                        f"Rooms queued but inaccessible (doors closed/unavailable): {', '.join(blocked_names)}",
                    )
                    await self._set_narrative(f"Waiting - doors closed: {blocked_pretty}")
                except Exception:
                    self.log_state_conclusion(
                        f"{trigger}_eval",
                        "rooms_blocked_by_doors",
                        "wait",
                        "Rooms queued but inaccessible (doors closed/unavailable)",
                    )
                return

            if not rooms_all:
                self.log_state_conclusion(f"{trigger}_eval", "no_rooms", "wait",
                                        "No rooms need cleaning")
                
                # If this was a continue_cleaning trigger and no rooms left, turn off clean_while_home
                if trigger == "continue_cleaning" and clean_while_home == "on":
                    await self.call_service("input_boolean/turn_off", entity_id="input_boolean.clean_while_home")
                    self.log("Turned off clean_while_home - all rooms completed", level="INFO")
                
                # Check if robot is docked and all cleaning is complete - trigger bin emptying
                robot_state = await self.get_state("vacuum.rober2")
                robot_status = await self.get_state("sensor.rober2_status")
                bin_empty_triggered = await self.get_state("input_boolean.rober2_bin_empty_triggered")

                if robot_state == "docked":
                    await self._set_narrative("At dock - no rooms queued")
                
                if robot_state == "docked" and robot_status != "emptying_the_bin" and bin_empty_triggered != "on":
                    # All rooms complete and robot is docked - bin empty only if session had completions (inside helper)
                    self.log("All ordered cleaning complete and robot docked - evaluating bin empty", level="INFO")
                    await self.empty_bin_after_cleaning()
                elif bin_empty_triggered == "on":
                    self.log("Bin empty already triggered for this cleaning session completion", level="DEBUG")
                
                return
                
            # Presence logic with override (unknown/unavailable person states block "away")
            if not away:  # Someone is (explicitly) home
                if clean_while_home == "on":
                    self.log_state_conclusion(f"{trigger}_eval", "home_occupied_override", "start_cleaning", 
                                            f"Someone is home but clean_while_home is enabled, starting cleaning")
                    await self.start_cleaning(rooms[0])
                else:
                    # Minimize noisy presence logs when just idling
                    self.log("Presence: home occupied, idle monitoring", level="DEBUG")
            else:  # Away
                self.log_state_conclusion(f"{trigger}_eval", "home_empty", "start_cleaning", 
                                        "Home is empty, starting cleaning")
                await self.start_cleaning(rooms[0])
                
        except Exception as e:
            self.log(f"Error evaluating cleaning conditions: {e}", level="ERROR")
        finally:
            self._eval_in_progress = False
            
    async def handle_vacuum_state(self, entity, attribute, old, new, kwargs):
        """Handle vacuum state changes with progress-based room completion detection."""
        try:
            self.log(f"Vacuum state: {old} -> {new}")
            
            if new == "returning":
                # Use peak progress (not current progress which resets to 0% during returning)
                progress_value = self.peak_progress
                
                # First, handle current room completion if we have tracking data
                if self.current_room and self.room_start_time:
                    room_name = self.room_config.get(self.current_room, self.current_room)
                    cleaning_duration = (datetime.datetime.now() - self.room_start_time).total_seconds()
                    
                    # Room completion criteria: peak progress >= 90% AND (duration > 60s OR init grace)
                    allow_init_grace = False
                    try:
                        if getattr(self, '_init_tracking_ts', None):
                            if (datetime.datetime.now() - self._init_tracking_ts).total_seconds() < 300:
                                allow_init_grace = True
                    except Exception:
                        allow_init_grace = False

                    if progress_value >= 90 and (cleaning_duration > 60 or allow_init_grace):
                        await self._set_narrative(
                            f"{self._pretty_room_from_segment_id(self.current_room)} finished - returning"
                        )
                        self.log(f"Room {room_name} completed - progress: {progress_value}%, duration: {cleaning_duration:.1f}s", level="INFO")
                        # Reset unreachable counter on successful clean
                        self._room_interrupt_counts[self.current_room] = 0
                        # Turn off room cleaning flag
                        flag_entity = f"input_boolean.rober2_clean_{room_name}"
                        await self.call_service("input_boolean/turn_off", entity_id=flag_entity)
                        
                        # Mark room as cleaned (updates last_clean for schedule)
                        await self.mark_room_cleaned(self.current_room)
                        self._rooms_completed_this_session += 1
                        
                        # Clear input_text.rober2_current_room (ready for next room)
                        await self.call_service("input_text/set_value",
                            entity_id="input_text.rober2_current_room",
                            value=""
                        )
                        
                        # Reset room tracking - cleaning must go False so the next vacuum "cleaning"
                        # transition runs the init block (room_start_time, peak_progress, current_room).
                        # Otherwise self.cleaning stays True until "docked" and multi-room runs break.
                        self.current_room = None
                        self.room_start_time = None
                        self.peak_progress = 0  # Reset for next room
                        self._init_tracking_ts = None
                        self.cleaning = False
                        
                        self.log_state_conclusion("room_completed", f"room_{room_name}", "completed", 
                                                f"Room {room_name} completed ({progress_value}%) and flag turned off")
                        
                        # Check if any rooms remain after this completion
                        remaining_rooms = await self.get_rooms_to_clean()
                        if not remaining_rooms:
                            # All rooms complete - turn off clean_while_home if it's on
                            clean_while_home = await self.get_state("input_boolean.clean_while_home")
                            if clean_while_home == "on":
                                await self.call_service("input_boolean/turn_off", entity_id="input_boolean.clean_while_home")
                                self.log("All rooms completed - turned off clean_while_home", level="INFO")
                        
                        # Queue next segment BEFORE map download - save_map_image can block for many seconds
                        # (HTTP download) and misses the Q-series window to chain rooms.
                        await self.queue_next_room(trigger="room_completed")
                        await self.save_map_image(trigger_context=f"{room_name}")
                        
                    else:
                        # Low progress or short duration = manual return/interruption
                        await self._set_narrative(
                            f"{self._pretty_room_from_segment_id(self.current_room)} interrupted - returning"
                        )
                        self.log(f"Room {room_name} interrupted - progress: {progress_value}%, duration: {cleaning_duration:.1f}s (not queuing next)", level="INFO")
                        self.log_state_conclusion("room_interrupted", f"room_{room_name}", "interrupted", 
                                                f"Room {room_name} cleaning interrupted - will not queue next room")
                        # Don't reset tracking - room will be retried later
                        
                else:
                    # No room tracking - try to detect what room was being cleaned
                    self.log(f"Robot returning but no room tracking data - progress: {progress_value}%", level="WARNING")
                    
                    # Get the target room being cleaned (not current location)
                    current_room_text = await self.get_state("input_text.rober2_current_room")
                    
                    # If input_text is empty, we lost track - use fallback detection
                    if not current_room_text or current_room_text == "unavailable" or current_room_text not in self.room_config:
                        # Fallback: check what rooms are currently flagged for cleaning
                        rooms_to_clean = await self.get_rooms_to_clean()
                        if len(rooms_to_clean) == 1:
                            current_room_text = rooms_to_clean[0]
                            self.log(f"Fallback: detected room from cleaning queue: {current_room_text}", level="INFO")
                        elif len(rooms_to_clean) > 1:
                            # Take the first room in priority order
                            current_room_text = rooms_to_clean[0]
                            self.log(f"Fallback: multiple rooms queued, assuming first priority room: {current_room_text}", level="INFO")
                        else:
                            # Use HA history to find last non-empty selection
                            history_vals = self._read_recent_text_states("input_text.rober2_current_room", minutes=180, max_points=100)
                            for candidate in reversed(history_vals):
                                if candidate in self.room_config:
                                    current_room_text = candidate
                                    self.log(f"History fallback: last known target room {current_room_text}", level="INFO")
                                    break
                    
                    if current_room_text and current_room_text in self.room_config:
                        room_name = self.room_config.get(current_room_text, current_room_text)
                        
                        # For room completion detection when tracking is lost, be more lenient
                        # If robot was cleaning for reasonable time, assume completion
                        robot_status = await self.get_state("sensor.rober2_status")
                        # NEW GUARD: Only allow fallback completion if we actually observed the robot in this room
                        # during a cleaning state at least once in this cycle.
                        visited = current_room_text in self._visited_rooms
                        if robot_status == "returning_home" and progress_value >= 80 and visited:
                            await self._set_narrative(
                                f"{self._pretty_room_from_segment_id(current_room_text)} finished - returning"
                            )
                            self.log(f"Detected robot likely completed room {current_room_text} ({room_name}) - progress: {progress_value}%, status: {robot_status}", level="INFO")
                            flag_entity = f"input_boolean.rober2_clean_{room_name}"
                            await self.call_service("input_boolean/turn_off", entity_id=flag_entity)
                            await self.mark_room_cleaned(current_room_text)
                            self._rooms_completed_this_session += 1
                            await self.call_service("input_text/set_value",
                                entity_id="input_text.rober2_current_room",
                                value="")
                            self.current_room = None
                            self.room_start_time = None
                            self.peak_progress = 0
                            self._init_tracking_ts = None
                            self.cleaning = False
                            self.log_state_conclusion("room_completed_detected", f"room_{room_name}", "completed", 
                                                    f"Room {room_name} completed (detected, {progress_value}%) and flag turned off")
                            
                            # Check if any rooms remain after this completion
                            remaining_rooms = await self.get_rooms_to_clean()
                            if not remaining_rooms:
                                # All rooms complete - turn off clean_while_home if it's on
                                clean_while_home = await self.get_state("input_boolean.clean_while_home")
                                if clean_while_home == "on":
                                    await self.call_service("input_boolean/turn_off", entity_id="input_boolean.clean_while_home")
                                    self.log("All rooms completed - turned off clean_while_home", level="INFO")
                            
                            await self.queue_next_room(trigger="room_completed_detected")
                            await self.save_map_image(trigger_context=f"{room_name}")
                        elif progress_value >= 90 and visited:  # High confidence completion only if visited
                            await self._set_narrative(
                                f"{self._pretty_room_from_segment_id(current_room_text)} finished - returning"
                            )
                            self.log(f"Detected robot completed target room {current_room_text} ({room_name}) - progress: {progress_value}%", level="INFO")
                            flag_entity = f"input_boolean.rober2_clean_{room_name}"
                            await self.call_service("input_boolean/turn_off", entity_id=flag_entity)
                            await self.mark_room_cleaned(current_room_text)
                            self._rooms_completed_this_session += 1
                            await self.call_service("input_text/set_value",
                                entity_id="input_text.rober2_current_room",
                                value="")
                            self.current_room = None
                            self.room_start_time = None
                            self.peak_progress = 0
                            self._init_tracking_ts = None
                            self.cleaning = False
                            self.log_state_conclusion("room_completed_detected", f"room_{room_name}", "completed", 
                                                    f"Room {room_name} completed (detected, {progress_value}%) and flag turned off")
                            
                            await self.queue_next_room(trigger="room_completed_detected")
                            await self.save_map_image(trigger_context=f"{room_name}")
                        else:
                            # If we never visited this room in cleaning state, do NOT mark as completed.
                            if not visited:
                                self.log(f"Fallback guard: refusing to complete room {current_room_text} because it was never observed during cleaning.", level="WARNING")
                            else:
                                await self._set_narrative(
                                    f"{self._pretty_room_from_segment_id(current_room_text)} interrupted - returning"
                                )
                                self.log(f"Detected robot interrupted in room {current_room_text} ({room_name}) - progress: {progress_value}% (not completing)", level="INFO")
                                self.log_state_conclusion("room_interrupted_detected", f"room_{room_name}", "interrupted", 
                                                        f"Room {room_name} interrupted (detected, {progress_value}%) - will retry later")
                    else:
                        rooms_available = await self.get_rooms_to_clean()
                        self.log(f"Cannot detect target room - input_text: '{current_room_text}', available rooms: {rooms_available}", level="ERROR")
                
            elif new == "cleaning":
                # Job really started -> make flags official (next room re-enters after completion sets cleaning=False)
                if not self.cleaning:
                    self.cleaning = True
                    self.fan_speed_enforced = False
                    self.room_start_time = datetime.datetime.now()
                    self.peak_progress = 0  # Reset progress tracking for new room
                    # Reset bin empty flag for new cleaning session (persistent in HA)
                    await self.call_service("input_boolean/turn_off", entity_id="input_boolean.rober2_bin_empty_triggered")
                    
                    # Detect current room if not already set and record a visit
                    if not self.current_room:
                        current_room_text = await self.get_state("input_text.rober2_current_room")
                        if current_room_text and current_room_text != "unavailable" and current_room_text in self.room_config:
                            self.current_room = current_room_text
                            self._last_attempted_room_id = None  # We have proper tracking now
                            room_name = self.room_config.get(current_room_text, current_room_text)
                            self._visited_rooms.add(current_room_text)
                            self.log(f"Robot really left dock - tracking room {self.current_room} ({room_name})", level="INFO")
                        else:
                            self.log(f"Robot cleaning but input_text.rober2_current_room is '{current_room_text}' - cannot track", level="WARNING")
                    
                    
                    # Proactively enforce max_plus at cleaning start (in addition to attribute listener)
                    try:
                        current_fan = await self.get_state("vacuum.rober2", attribute="fan_speed")
                        if current_fan != "max_plus":
                            now_ts = datetime.datetime.now().timestamp()
                            await self.set_fan_speed("max_plus")
                            self._last_fan_set = now_ts
                            self.fan_speed_enforced = True
                    except Exception:
                        pass
                    
                    if self.current_room:
                        await self._set_narrative(
                            f"Cleaning {self._pretty_room_from_segment_id(self.current_room)}"
                        )
                    else:
                        await self._set_narrative("Cleaning - target unknown")

                    self.log_state_conclusion("vacuum_cleaning", "cleaning", "monitor_cleaning", 
                                            "Robot started cleaning")
                
            elif new == "docked":
                dock_room_seg = self.current_room
                # Safety net: if we somehow arrived docked with rooms left,
                # run final evaluation once the empty-bin cycle finishes
                self.cleaning = False
                self.fan_speed_enforced = False
                # Clear visited rooms when docking
                self._visited_rooms.clear()
                # _rooms_completed_this_session is reset only when bin empty completes (handle_robot_status)

                if dock_room_seg:
                    await self._set_narrative(
                        f"Docked - {self._pretty_room_from_segment_id(dock_room_seg)} interrupted, will retry"
                    )
                else:
                    await self._set_narrative("Docked")
                
                # Handle any remaining interruption scenarios
                if self.current_room:
                    room_name = self.room_config.get(self.current_room, self.current_room)
                    
                    # Do not auto-complete on docking based on duration alone; leave flag for retry
                    if self.room_start_time:
                        cleaning_duration = (datetime.datetime.now() - self.room_start_time).total_seconds()
                        self.log(f"Room {room_name} was interrupted after {cleaning_duration:.1f}s - leaving flag on for retry", level="INFO")
                        self.log_state_conclusion("room_interrupted_retry", f"room_{room_name}", "retry_later", 
                                                f"Room {room_name} interrupted - will retry later")
                        
                        # Track consecutive 0% attempts - detect unreachable rooms
                        if self.peak_progress == 0 and cleaning_duration >= self.MIN_DURATION_FOR_ZERO_PROGRESS:
                            count = self._room_interrupt_counts.get(self.current_room, 0) + 1
                            self._room_interrupt_counts[self.current_room] = count
                            self.log(f"Room {room_name} zero-progress attempt {count}/{self.MAX_ROOM_RETRIES}", level="INFO")
                            if count >= self.MAX_ROOM_RETRIES:
                                self._room_interrupt_counts[self.current_room] = 0
                                flag_entity = f"input_boolean.rober2_clean_{room_name}"
                                await self.call_service("input_boolean/turn_off", entity_id=flag_entity)
                                if not self._robot_has_left_dock:
                                    dock_pretty = self._dock_pretty_name()
                                    if self._is_dock_room_attempt(self.current_room, room_name):
                                        reason = f"Stuck in {dock_pretty}"
                                        self.log(f"Robot stuck cleaning {dock_pretty} after {count} attempts", level="WARNING")
                                        await self.send_notification(
                                            title=f"Rober2 - Stuck in {dock_pretty}",
                                            message=f"Robot got stuck trying to clean {dock_pretty} after {count} attempts.",
                                            target=["mikkel", "kristine"]
                                        )
                                    else:
                                        reason = f"Stuck trying to leave {dock_pretty}"
                                        self.log(f"Robot never left dock after {count} attempts - stuck trying to leave {dock_pretty}", level="WARNING")
                                        await self.send_notification(
                                            title=f"Rober2 - Stuck trying to leave {dock_pretty}",
                                            message=f"Robot cannot leave {dock_pretty} after {count} attempts. Something is blocking the exit.",
                                            target=["mikkel", "kristine"]
                                        )
                                    await self._set_automation_paused(reason)
                                    await self.save_map_image(trigger_context=reason.replace(" ", "_").lower())
                                else:
                                    self.log(f"Room {room_name} unreachable after {count} attempts - flag turned off", level="WARNING")
                                    await self.send_notification(
                                        title="Rober2 - Room unreachable",
                                        message=f"Could not reach {room_name.replace('_', ' ')} after {count} attempts. Skipping for this session.",
                                        target=["mikkel", "kristine"]
                                    )
                    else:
                        self.log(f"Room {room_name} interrupted with unknown cleaning duration - leaving flag on", level="WARNING")
                    
                    # Clear room tracking
                    self.current_room = None
                    self.room_start_time = None
                    self.peak_progress = 0
                    self._init_tracking_ts = None
                
                else:
                    # No current_room (e.g. segment command was "ignored" or we never got "cleaning") - still count if we had a target.
                    # Only count when this dock is part of a recent attempt: avoid counting unavailable->docked (reconnect) or
                    # docked->docked long after the last try (e.g. robot sat in dock for hours, then state refreshed).
                    if self._last_attempted_room_id and self.peak_progress == 0 and old != "unavailable":
                        elapsed = (datetime.datetime.now() - self._last_segment_command_time).total_seconds() if self._last_segment_command_time else 999
                        if self.MIN_DURATION_FOR_ZERO_PROGRESS <= elapsed <= self.MAX_ELAPSED_FOR_ZERO_PROGRESS_SEC:
                            room_id = self._last_attempted_room_id
                            room_name = self.room_config.get(room_id, room_id)
                            count = self._room_interrupt_counts.get(room_id, 0) + 1
                            self._room_interrupt_counts[room_id] = count
                            self.log(f"Room {room_name} zero-progress attempt (no tracking) {count}/{self.MAX_ROOM_RETRIES}", level="INFO")
                            if count >= self.MAX_ROOM_RETRIES:
                                self._room_interrupt_counts[room_id] = 0
                                self._last_attempted_room_id = None
                                flag_entity = f"input_boolean.rober2_clean_{room_name}"
                                await self.call_service("input_boolean/turn_off", entity_id=flag_entity)
                                if not self._robot_has_left_dock:
                                    dock_pretty = self._dock_pretty_name()
                                    if self._is_dock_room_attempt(room_id, room_name):
                                        reason = f"Stuck in {dock_pretty}"
                                        self.log(f"Robot stuck cleaning {dock_pretty} after {count} attempts", level="WARNING")
                                        await self.send_notification(
                                            title=f"Rober2 - Stuck in {dock_pretty}",
                                            message=f"Robot got stuck trying to clean {dock_pretty} after {count} attempts.",
                                            target=["mikkel", "kristine"]
                                        )
                                    else:
                                        reason = f"Stuck trying to leave {dock_pretty}"
                                        self.log(f"Robot never left dock after {count} attempts - stuck trying to leave {dock_pretty}", level="WARNING")
                                        await self.send_notification(
                                            title=f"Rober2 - Stuck trying to leave {dock_pretty}",
                                            message=f"Robot cannot leave {dock_pretty} after {count} attempts. Something is blocking the exit.",
                                            target=["mikkel", "kristine"]
                                        )
                                    await self._set_automation_paused(reason)
                                    await self.save_map_image(trigger_context=reason.replace(" ", "_").lower())
                                else:
                                    self.log(f"Room {room_name} unreachable after {count} attempts - flag turned off", level="WARNING")
                                    await self.send_notification(
                                        title="Rober2 - Room unreachable",
                                        message=f"Could not reach {room_name.replace('_', ' ')} after {count} attempts. Skipping for this session.",
                                        target=["mikkel", "kristine"]
                                    )
                            # When count < 3 we keep _last_attempted_room_id so next dock can count again
                
                # Wait for bin empty cycle to finish, then check for more rooms
                self.run_in(self.post_bin_empty_check, 5, trigger="post_bin_empty")  # Start checking in 5s
                    
            elif new == "idle":
                # Robot idle - clear cleaning flag and re-evaluate based on state changes only
                was_cleaning = self.cleaning
                idle_room = self.current_room
                self.cleaning = False
                self.fan_speed_enforced = False
                if was_cleaning and idle_room:
                    room_name = self.room_config.get(idle_room, idle_room)
                    await self._set_narrative(
                        f"Idle - {self._pretty_room_from_segment_id(idle_room)} cleaning interrupted"
                    )
                    self.log_state_conclusion("vacuum_idle", "idle", "retry_or_wait", f"Robot idle; cleaning interrupted for room {room_name}")
                else:
                    await self._set_narrative("Idle")
                    self.log_state_conclusion("vacuum_idle", "idle", "wait", "Robot idle")
                # Rely on listeners; kick an evaluation once to decide next action
                self.run_in(self.evaluate_cleaning_conditions, 1, trigger="idle_state")
            
            elif new in ["paused", "error"]:
                # Treat paused/error similar to idle to avoid stale cleaning flag
                was_cleaning = self.cleaning
                an_room = self.current_room
                self.cleaning = False
                self.fan_speed_enforced = False
                if was_cleaning and an_room:
                    room_name = self.room_config.get(an_room, an_room)
                    await self._set_narrative(
                        f"Robot {new} - {self._pretty_room_from_segment_id(an_room)} cleaning interrupted"
                    )
                    self.log_state_conclusion("vacuum_anomaly", new, "retry_or_wait", f"Robot {new}; cleaning interrupted for room {room_name}")
                else:
                    await self._set_narrative(f"Robot {new}")
                    self.log_state_conclusion("vacuum_anomaly", new, "wait", f"Robot {new}")
                self.run_in(self.evaluate_cleaning_conditions, 1, trigger=f"vacuum_{new}")
            
        except Exception as e:
            self.log(f"Error handling vacuum state: {e}", level="ERROR")
            
    async def queue_next_room(self, trigger: str):
        """Queue next room *before* robot docks - critical timing for Q-series."""
        try:
            # Check if robot has active error (pauses all automation)
            has_error, error_msg = await self.has_active_error()
            if has_error:
                self.log(f"{trigger}: Robot has active error '{error_msg}' - automation paused, not queuing next room", level="WARNING")
                return
            
            rooms_all = await self.get_rooms_to_clean()
            rooms, blocked_rooms = await self._partition_accessible_rooms(rooms_all)
            if not rooms_all:
                self.log("No rooms left, docking sequence finished", level="INFO")
                await self._set_narrative("All queued rooms finished")
                return
            if not rooms:
                try:
                    blocked_names = ", ".join([self.room_config.get(r, r) for r in blocked_rooms])
                    blocked_pretty = ", ".join(
                        self._pretty_room_from_segment_id(r) for r in blocked_rooms
                    )
                except Exception:
                    blocked_names = ", ".join(blocked_rooms or [])
                    blocked_pretty = blocked_names
                self.log(f"{trigger}: Rooms remain but all are inaccessible (doors closed/unavailable): {blocked_names}", level="INFO")
                await self._set_narrative(f"Waiting - doors closed: {blocked_pretty}")
                return

            # REMOVED: Do not filter out the current room. If it is the highest priority
            # and needs cleaning, we should ensure we are working on it, not skip it.
            filtered = rooms

            if not filtered:
                # Nothing to queue other than the current room
                return

            next_room = filtered[0]
            if self.cleaning and next_room == self.current_room:
                return  # already running

            # Check presence and clean_while_home before queuing
            away = await self.is_away()
            clean_while_home = await self.get_state("input_boolean.clean_while_home")

            # Safety checks
            if not await self._is_automation_enabled():
                self.log(f"{trigger}: Rober2 automation disabled, not queuing next room", level="INFO")
                return
                
            if not away and clean_while_home != "on":
                self.log(f"{trigger}: Someone home and clean_while_home disabled, not queuing next room", level="INFO")
                return
                
            if not await self.is_within_cleaning_hours():
                self.log(f"{trigger}: Outside cleaning hours, not queuing next room", level="INFO")
                return

            # Queue the next room immediately
            room_name = self.room_config.get(next_room, next_room)
            # Log a snapshot of selection for diagnosis
            try:
                snapshot = ", ".join([self.room_config.get(r, r) for r in rooms_all])
            except Exception:
                snapshot = ", ".join(rooms_all)

            # Dedupe must be atomic: turn_off(room flag) schedules handle_room_flag_change while
            # handle_vacuum_state still runs -> two queue_next_room coroutines can interleave at await
            # points and both pass a plain time check. Serialize check + _last_* update on the lock.
            async with self._segment_send_lock:
                if self._should_skip_duplicate_segment_send(next_room):
                    self.log(
                        f"{trigger}: skipping duplicate segment send for {room_name} "
                        f"(already scheduled; concurrent room_completed + room_flag_change)",
                        level="DEBUG",
                    )
                    self.run_in(self.verify_job_started, self.SEND_RETRY_SEC, room_id=next_room)
                    return
                self._last_scheduled_segment_room = next_room
                self._last_scheduled_segment_ts = datetime.datetime.now()
                # Fresh segment attempt - reset dock-leave tracking (only on real send, not dedupe skip)
                self._robot_has_left_dock = False

            self.log(
                f"{trigger}: sending segment {next_room} ({room_name}); candidates: [{snapshot}] current={self.current_room}",
                level="INFO",
            )

            # CRITICAL FIX: Set input_text.rober2_current_room to track the target room
            await self._safe_call_service(
                "input_text/set_value",
                entity_id="input_text.rober2_current_room",
                value=next_room,
            )

            await self._set_narrative(f"Going to {self._pretty_room_from_segment_id(next_room)}")

            # Record desired room as visited only once we observe cleaning state
            # Prevents false positives if the robot ignores the command.
            # Send segment command in a separate callback to avoid blocking (10s HA timeout / excessive callback time)
            self._last_attempted_room_id = next_room
            self._last_segment_command_time = datetime.datetime.now()
            self.run_in(self._do_send_segment_clean, 0, room_id=next_room)
            # Watchdog: verify the robot changed to 'cleaning'
            self.run_in(self.verify_job_started, self.SEND_RETRY_SEC, room_id=next_room)

        except Exception as e:
            self.log(f"Error queuing next room: {e}", level="ERROR")
            
    async def verify_job_started(self, kwargs):
        """Verify robot started the queued job, retry if command was ignored."""
        try:
            room_id = kwargs.get("room_id")
            robot_state = await self.get_state("vacuum.rober2")
            robot_status = await self.get_state("sensor.rober2_status")
            # Match handle_robot_status(segment_cleaning): status can lead vacuum state; self.cleaning
            # may already be True from that path before vacuum reports "cleaning".
            started = (
                robot_state == "cleaning"
                or robot_status == "segment_cleaning"
                or self.cleaning
            )
            if not started:
                room_name = self.room_config.get(room_id, room_id)
                self.log(f"Segment command for {room_name} ignored - retrying", level="WARNING")
                await self._set_narrative("Segment command ignored - retrying")
                self.cleaning = False
                self.current_room = None
                self.room_start_time = None
                self.peak_progress = 0
                self._init_tracking_ts = None
                self.run_in(self.evaluate_cleaning_conditions, 1, trigger="start_fail")
            else:
                room_name = self.room_config.get(room_id, room_id)
                self.log(f"Verified: robot started cleaning {room_name}", level="INFO")
                
        except Exception as e:
            self.log(f"Error verifying job start: {e}", level="ERROR")
            
    async def post_bin_empty_check(self, kwargs):
        """After dock: may run bin empty; decision is gated inside empty_bin_after_cleaning()."""
        try:
            robot_state = await self.get_state("vacuum.rober2")
            robot_status = await self.get_state("sensor.rober2_status")
            bin_empty_triggered = await self.get_state("input_boolean.rober2_bin_empty_triggered")

            if robot_state == "docked" and robot_status != "emptying_the_bin" and bin_empty_triggered != "on":
                self.log("Robot docked - evaluating bin empty", level="INFO")
                await self.empty_bin_after_cleaning()
            elif robot_status == "emptying_the_bin":
                # Still emptying - handle_robot_status() will catch completion
                self.log("Robot emptying bin - waiting for status change", level="INFO")
            else:
                self.cleaning = False
                self.run_in(self.evaluate_cleaning_conditions, 1, trigger="post_bin")

        except Exception as e:
            self.log(f"Error in post bin empty check: {e}", level="ERROR")
            self.cleaning = False
            self.run_in(self.evaluate_cleaning_conditions, 1, trigger="post_bin")
            
    
            
    async def handle_fan_speed(self, entity, attribute, old, new, kwargs):
        """Handle fan speed changes."""
        try:
            if new and new != "unavailable":
                # self.log(f"Fan speed changed: {old} -> {new}")
                
                # Enforce max_plus whenever it deviates
                if new != "max_plus":
                    # DEBOUNCE: Check if we set the fan speed recently (within last 15 seconds)
                    # This prevents fighting with the robot (e.g. Carpet Boost switching to turbo)
                    now_ts = datetime.datetime.now().timestamp()
                    if (now_ts - self._last_fan_set) < 15:
                        self.log(f"Fan changed to {new}, but ignoring (debounce active)", level="DEBUG")
                        self.run_in(self.enforce_fan_speed_check, 16)
                        return

                    self.log_state_conclusion("fan_speed_wrong", f"fan_{new}", "enforce_max_plus", 
                                            f"Fan speed reverted to {new}, enforcing max_plus")
                    await self.set_fan_speed("max_plus")
                    self._last_fan_set = datetime.datetime.now().timestamp()
                    self.fan_speed_enforced = False
                elif new == "max_plus":
                    self.fan_speed_enforced = True
                    
        except Exception as e:
            self.log(f"Error handling fan speed: {e}", level="ERROR")

    async def enforce_fan_speed_check(self, kwargs):
        """Re-check fan speed after debounce expires."""
        try:
            # Only enforce if cleaning
            if not self.cleaning:
                return
                
            current_fan = await self.get_state("vacuum.rober2", attribute="fan_speed")
            if current_fan != "max_plus":
                self.log(f"Debounce expired, fan still {current_fan} -> enforcing max_plus", level="INFO")
                await self.set_fan_speed("max_plus")
                self._last_fan_set = datetime.datetime.now().timestamp()
        except Exception as e:
            self.log(f"Error in fan speed re-check: {e}", level="ERROR")
            
    # Removed periodic enforcement; only react to actual robot fan speed changes
            
    async def check_room_completion(self):
        """Check if current room cleaning is completed.

        Not scheduled anywhere - primary logic is ``handle_vacuum_state`` (vacuum *returning*).
        If re-enabled, align with that path: ``cleaning``/``input_text``/``queue_next_room``/``save_map_image``.
        """
        try:
            if not self.current_room or not self.room_start_time:
                return
                
            # Calculate cleaning duration
            cleaning_duration = (datetime.datetime.now() - self.room_start_time).total_seconds()
            
            # Get robot state
            robot_state = await self.get_state("vacuum.rober2")
            
            # Room completion is detected when robot goes to "returning" state
            # This is the actual Roborock behavior: docked -> cleaning -> returning -> docked
            if robot_state == "returning" and cleaning_duration > 60:
                room_name = self.room_config.get(self.current_room, self.current_room)
                self.log_state_conclusion("room_completed", f"room_{room_name}_done", "mark_cleaned", 
                                        f"Room {room_name} completed after {cleaning_duration:.1f} seconds (robot returning)")
                
                # Turn off the room cleaning flag
                flag_entity = f"input_boolean.rober2_clean_{room_name}"
                self.log(f"Turning off cleaning flag: {flag_entity}", level="INFO")
                await self.call_service("input_boolean/turn_off", entity_id=flag_entity)
                
                # Verify the flag was turned off
                await self.sleep(0.5)  # Give HA time to process
                flag_state = await self.get_state(flag_entity)
                if flag_state == "off":
                    self.log(f"Successfully turned off cleaning flag for {room_name}", level="INFO")
                else:
                    self.log(f"ERROR: Failed to turn off cleaning flag for {room_name} - state is still {flag_state}", level="ERROR")
                
                # Mark room as cleaned (update last clean date for schedule)
                await self.mark_room_cleaned(self.current_room)
                self._rooms_completed_this_session += 1
                
                # Reset room tracking
                self.current_room = None
                self.room_start_time = None
                
                # Immediately start next room - this app is in charge of room-to-room transitions
                self.log(f"Room {room_name} completed, checking for next room immediately", level="INFO")
                self.run_in(self.evaluate_cleaning_conditions, 2, trigger="room_completed")
            
        except Exception as e:
            self.log(f"Error checking room completion: {e}", level="ERROR")
            
    async def handle_presence(self, entity, attribute, old, new, kwargs):
        """Handle presence changes."""
        try:
            away = await self.is_away()
            if away:  # Away
                self.log_state_conclusion("presence_empty", "home_empty", "check_start_conditions", 
                                        "Home is empty, checking if we can start cleaning")
                self.run_in(self.evaluate_cleaning_conditions, 1, trigger="presence_empty")
            else:  # Someone home
                # If currently cleaning, check if we should stop
                if self.cleaning:
                    clean_while_home = await self.get_state("input_boolean.clean_while_home")
                    if clean_while_home != "on":
                        # Check if room is nearly complete (>=93%) - don't interrupt if so
                        if self.peak_progress >= 93:
                            room_name = self.room_config.get(self.current_room, self.current_room) if self.current_room else "unknown"
                            self.log_state_conclusion("presence_detected", "home_occupied_near_complete", "continue_to_finish", 
                                                    f"Someone is home but room {room_name} is {self.peak_progress}% complete - letting it finish")
                        else:
                            self.log_state_conclusion("presence_detected", "home_occupied", "stop_cleaning", 
                                                    f"Someone is home, stopping cleaning (room {self.peak_progress}% complete)")
                            await self.stop_cleaning()
                            await self._set_narrative("Stopped - someone home")
                    else:
                        self.log_state_conclusion("presence_detected", "home_occupied_override", "continue_cleaning", 
                                                "Someone is home but clean_while_home is enabled, continuing cleaning")
                else:
                    # Not cleaning, evaluate if we should start (suppress noisy logs)
                    self.run_in(self.evaluate_cleaning_conditions, 1, trigger="presence_detected")
                    
        except Exception as e:
            self.log(f"Error handling presence: {e}", level="ERROR")
            
    async def handle_room_change(self, entity, attribute, old, new, kwargs):
        """Handle changes to input_text.rober2_current_room (tracks TARGET room id)."""
        try:
            if new and new != "unavailable":
                # This helper is set BEFORE sending a segment command. It's not the robot's
                # physical location. Avoid treating it as "robot moved rooms".
                if new not in self.room_config:
                    self.log(f"Target room helper set to unknown value '{new}' (ignoring)", level="DEBUG")
                    return

                room_name = self.room_config.get(new, new)
                self.log_state_conclusion(
                    "target_room_set",
                    f"target_{room_name}",
                    "wait_for_start",
                    f"Target room set to {room_name}",
                )
                
        except Exception as e:
            self.log(f"Error handling room change: {e}", level="ERROR")
            
    async def handle_battery(self, entity, attribute, old, new, kwargs):
        """Handle battery changes."""
        try:
            if new and new != "unavailable":
                battery_level = float(new)
                old_level = float(old) if old and old != "unavailable" else None
                
                # If battery is low and we're cleaning, stop
                if battery_level <= 20 and self.cleaning:
                    self.log_state_conclusion("battery_low", f"battery_{battery_level}%", "stop_and_charge", 
                                            f"Battery low ({battery_level}%), stopping cleaning")
                    await self.stop_cleaning()
                    await self._set_narrative("Stopped - battery low")
                # Auto-resume: If battery charged above 80% and we have pending rooms, re-evaluate
                elif old_level is not None and old_level < 80 and battery_level >= 80:
                    # Battery just crossed above 80% threshold - enough charge to resume cleaning
                    robot_state = await self.get_state("vacuum.rober2")
                    if robot_state in ["idle", "docked", "charging"]:
                        # Check if we have pending rooms
                        rooms = await self.get_rooms_to_clean()
                        if rooms:
                            self.log(f"Battery charged to {battery_level}% (was {old_level}%) - re-evaluating cleaning conditions", level="INFO")
                            self.run_in(self.evaluate_cleaning_conditions, 2, trigger="battery_charged")
                else:
                    # Reduce noise: only debug-log routine battery updates
                    self.log(f"Battery: {battery_level}%", level="DEBUG")
                    
        except Exception as e:
            self.log(f"Error handling battery: {e}", level="ERROR")
            
    async def handle_progress(self, entity, attribute, old, new, kwargs):
        """Handle cleaning progress changes and track peak progress."""
        try:
            if new and new != "unavailable" and self.cleaning:
                try:
                    progress_value = float(new)
                    
                    # Update peak progress if this is higher
                    if progress_value > self.peak_progress:
                        self.peak_progress = progress_value
                        self.last_progress_update = datetime.datetime.now()
                        # Proactively enforce max_plus here as well
                        try:
                            current_fan = await self.get_state("vacuum.rober2", attribute="fan_speed")
                            if current_fan != "max_plus":
                                now_ts = datetime.datetime.now().timestamp()
                                await self.set_fan_speed("max_plus")
                                self._last_fan_set = now_ts
                                self.fan_speed_enforced = True
                        except Exception:
                            pass
                        # Log significant progress milestones
                        if progress_value >= 93:
                            room_name = self.room_config.get(self.current_room, self.current_room) if self.current_room else "unknown"
                            self.log(f"Room {room_name} nearly complete: {progress_value}% - will not interrupt", level="INFO")
                        elif progress_value % 10 == 0 and progress_value >= 50:  # Log every 10% after 50%
                            room_name = self.room_config.get(self.current_room, self.current_room) if self.current_room else "unknown"
                            self.log(f"Room {room_name} progress: {progress_value}%", level="INFO")
                            
                except (ValueError, TypeError):
                    pass  # Ignore invalid progress values
                    
        except Exception as e:
            self.log(f"Error handling progress: {e}", level="ERROR")
            
    async def get_rooms_to_clean(self):
        """Get list of rooms that need cleaning, ordered by priority."""
        try:
            rooms_to_clean = []
            room_status_debug = []
            
            # Check each room in priority order
            for room_id in self.room_order:
                room_name = self.room_config[room_id]
                clean_flag = await self.get_state(f"input_boolean.rober2_clean_{room_name}")
                
                room_status_debug.append(f"{room_name}={clean_flag}")
                
                if clean_flag == "on":
                    rooms_to_clean.append(room_id)
            
            # Log all room states for debugging
            self.log(f"Room flags: {', '.join(room_status_debug)}", level="INFO")
                        
            if rooms_to_clean:
                room_names = [self.room_config[room_id] for room_id in rooms_to_clean]
                self.log(f"Rooms to clean (ordered): {', '.join(room_names)}")
            else:
                self.log("No rooms need cleaning", level="INFO")
                
            return rooms_to_clean
            
        except Exception as e:
            self.log(f"Error getting rooms to clean: {e}", level="ERROR")
            return []
            
    async def is_room_accessible(self, room_id):
        """Check if a room is accessible (door open if it has one)."""
        try:
            # If room doesn't have a door sensor, it's always accessible
            if room_id not in self.door_sensors:
                return True
                
            # Check door state
            door_sensor = self.door_sensors[room_id]
            door_state = await self.get_state(door_sensor)
            
            # Door is open if state is "on"
            return door_state == "on"
            
        except Exception as e:
            self.log(f"Error checking room accessibility: {e}", level="ERROR")
            return False
            
    async def _do_send_segment_clean(self, kwargs):
        """Start segment clean without blocking on vacuum.send_command completion.

        Prefer script.turn_on -> queued HA script (ack returns when the script starts). Fallback:
        vacuum.send_command with hass_timeout. Serialized with _segment_send_lock; dedupe in
        queue_next_room/start_cleaning avoids double-send when room_completed and room_flag_change
        both fire."""
        room_id = kwargs.get("room_id")
        if room_id is None:
            return
        async with self._segment_send_lock:
            try:
                if self._use_segment_clean_script:
                    await self._safe_call_service(
                        "script/turn_on",
                        timeout_s=self._segment_script_hass_timeout_s,
                        entity_id=self._segment_clean_script_entity,
                        variables={"room_id": int(room_id)},
                    )
                else:
                    await self._safe_call_service(
                        "vacuum/send_command",
                        entity_id="vacuum.rober2",
                        command="app_segment_clean",
                        params=[int(room_id)],
                    )
            except Exception as e:
                self.log(f"Error sending segment clean for room {room_id}: {e}", level="ERROR")

    async def start_cleaning(self, room_id):
        """Start cleaning a specific room."""
        try:
            # Get room name for logging
            room_name = self.room_config.get(room_id, room_id)
            
            # Check if robot has active error (pauses all automation)
            has_error, error_msg = await self.has_active_error()
            if has_error:
                self.log_state_conclusion("start_cleaning", f"error_{error_msg}", "paused", 
                                        f"Robot has active error '{error_msg}' - automation paused")
                return
            
            # Check if robot is ready
            robot_state = await self.get_state("vacuum.rober2")
            robot_status = await self.get_state("sensor.rober2_status")
            
            if robot_state not in ["idle", "docked"]:
                self.log_state_conclusion("start_cleaning", f"robot_{robot_state}", "wait_for_ready", 
                                        f"Robot not ready (state: {robot_state})")
                return
                
            # Don't start if robot is emptying bin or in other busy states
            if robot_status in ["emptying_the_bin", "washing_the_mop", "charging_problem", "error", "updating"]:
                self.log_state_conclusion("start_cleaning", f"robot_busy_{robot_status}", "wait_for_ready", 
                                        f"Robot busy (status: {robot_status})")
                return
                
            # Double-check room accessibility before starting
            if not await self.is_room_accessible(room_id):
                self.log_state_conclusion("start_cleaning", f"room_{room_name}_inaccessible", "skip_room", 
                                        f"Cannot clean {room_name} - door is closed")
                await self._set_narrative(
                    f"Skipped - door closed: {self._pretty_room_from_segment_id(room_id)}"
                )
                # Try another room immediately (don't get stuck on a closed-door room)
                self.run_in(self.evaluate_cleaning_conditions, 1, trigger="room_inaccessible")
                return
            
            # Do not set fan speed proactively; rely on handle_fan_speed if robot changes it
            
            # Do not mark visited yet; wait until we see cleaning state

            # Dedupe: avoid double-send if start_cleaning is triggered from multiple paths (atomic w.r.t. queue_next_room)
            async with self._segment_send_lock:
                if self._should_skip_duplicate_segment_send(room_id):
                    self.log(f"Skipping duplicate segment send for {room_name} (already scheduled)", level="DEBUG")
                    self.run_in(self.verify_job_started, self.SEND_RETRY_SEC, room_id=room_id)
                    self.log_state_conclusion("start_cleaning", "command_sent", "wait_for_start",
                                            f"Sent cleaning command for room: {room_name}")
                    return
                self._last_scheduled_segment_room = room_id
                self._last_scheduled_segment_ts = datetime.datetime.now()
                # Fresh segment attempt - reset dock-leave tracking (only on real send, not dedupe skip)
                self._robot_has_left_dock = False

            # CRITICAL FIX: Set input_text.rober2_current_room to track the target room
            await self._safe_call_service(
                "input_text/set_value",
                entity_id="input_text.rober2_current_room",
                value=room_id,
            )

            await self._set_narrative(f"Starting - going to {self._pretty_room_from_segment_id(room_id)}")

            # Send segment command in a separate callback; prefer HA script.turn_on so we do not
            # wait for vacuum.send_command to finish inside AppDaemon (verify_job_started + state handles success)
            self._last_attempted_room_id = room_id
            self._last_segment_command_time = datetime.datetime.now()
            self.run_in(self._do_send_segment_clean, 0, room_id=room_id)

            # DON'T set self.cleaning = True here - wait for robot to actually start
            # This prevents false "already cleaning" states
            self.log_state_conclusion("start_cleaning", "command_sent", "wait_for_start", 
                                    f"Sent cleaning command for room: {room_name}")
            
            # Watchdog: verify the robot starts within reasonable time
            self.run_in(self.verify_job_started, self.SEND_RETRY_SEC, room_id=room_id)
            
        except Exception as e:
            self.log(f"Error starting cleaning: {e}", level="ERROR")
            
    async def stop_cleaning(self):
        """Stop cleaning and return to dock."""
        try:
            # Just tell robot to return to dock - don't interrupt with stop command
            await self._safe_call_service("vacuum/return_to_base", entity_id="vacuum.rober2")
            
            self.cleaning = False
            # DON'T clear room tracking when interrupted - preserve it for potential resume/completion handling
            # self.current_room = None
            # self.room_start_time = None
            self.fan_speed_enforced = False
            
            # Log that cleaning was interrupted, not completed
            if self.current_room:
                room_name = self.room_config.get(self.current_room, self.current_room)
                await self._set_narrative(
                    f"Returning to dock - stopped ({self._pretty_room_from_segment_id(self.current_room)})"
                )
                self.log_state_conclusion("stop_cleaning", "interrupted", "docked", 
                                        f"Cleaning interrupted for room {room_name} - returning to dock")
            else:
                await self._set_narrative("Returning to dock - stopped")
                self.log_state_conclusion("stop_cleaning", "returning_to_dock", "docked", 
                                        "Stopped cleaning and returning to dock")
                
        except Exception as e:
            self.log(f"Error stopping cleaning: {e}", level="ERROR")
            
    async def empty_bin_after_cleaning(self):
        """Empty the dock bin when docked and should_empty_bin() allows (>=1 room completed this session)."""
        try:
            # Double-check robot is docked before triggering bin empty
            robot_state = await self.get_state("vacuum.rober2")
            robot_status = await self.get_state("sensor.rober2_status")
            
            if robot_state != "docked":
                self.log(f"Robot not docked (state: {robot_state}), skipping bin empty", level="WARNING")
                return
                
            if robot_status == "emptying_the_bin":
                self.log("Robot already emptying bin, skipping duplicate command", level="INFO")
                return

            if not await self.should_empty_bin():
                self.log("Skipping bin empty - should_empty_bin() false (no rooms completed this session)", level="DEBUG")
                return
            
            # Send bin empty command
            self.log("Triggering bin empty - robot is docked", level="INFO")
            success = await self._safe_call_service(
                "vacuum/send_command",
                entity_id="vacuum.rober2",
                command="app_start_collect_dust",
            )
            if success:
                # Mark bin empty as triggered in Home Assistant (persistent state)
                await self.call_service("input_boolean/turn_on", entity_id="input_boolean.rober2_bin_empty_triggered")
                
                # Note: bin_last_emptied date will be updated when emptying actually completes
                # (in handle_robot_status when status changes from emptying_the_bin)
                
                self.log_state_conclusion("empty_bin", "bin_empty_triggered", "waiting", 
                                        "Bin empty command sent - robot docked")
            else:
                self.log("Failed to send bin empty command, will retry on next evaluation", level="WARNING")
            
        except Exception as e:
            self.log(f"Error triggering bin empty: {e}", level="ERROR")
            
    async def set_fan_speed(self, speed):
        """Set fan speed."""
        try:
            await self._safe_call_service(
                "vacuum/set_fan_speed",
                entity_id="vacuum.rober2",
                fan_speed=speed,
            )
            self.log_state_conclusion("set_fan_speed", f"fan_{speed}", "ready", 
                                    f"Set fan speed to: {speed}")
            
        except Exception as e:
            self.log(f"Error setting fan speed: {e}", level="ERROR")
            
    async def save_map_image(self, trigger_context=""):
        """Save the current map image before starting next cleaning.
        
        Args:
            trigger_context: Optional context string for filename (e.g., room name)
        """
        try:
            if not self.map_save_dir:
                return  # Saving disabled due to directory error
            
            # Get image entity state and attributes
            image_state = await self.get_state(self.map_image_entity)
            if not image_state or image_state == "unavailable":
                self.log(f"Map image entity {self.map_image_entity} unavailable, skipping save", level="DEBUG")
                return
            
            # Try to get entity attributes for the image URL
            image_url = None
            try:
                attrs = await self.get_state(self.map_image_entity, attribute="all")
                if attrs and isinstance(attrs, dict):
                    # Image entities typically have the URL in entity_picture or as the state
                    entity_attrs = attrs.get("attributes", {})
                    # Check common attribute names for image URLs
                    for attr_name in ["entity_picture", "url", "image_url"]:
                        if attr_name in entity_attrs:
                            image_url = entity_attrs[attr_name]
                            break
            except Exception:
                pass
            
            # Fallback: use state as URL if attributes didn't provide one
            if not image_url:
                image_url = str(image_state)
            
            # Handle relative URLs (e.g., /local/... or /api/image_proxy/...)
            ha_url = self.args.get("ha_url", "http://localhost:8123")
            if image_url.startswith("/"):
                # Construct full Home Assistant URL
                image_url = f"{ha_url}{image_url}"
            elif not image_url.startswith("http"):
                # If it's not a full URL, try using Home Assistant's image proxy API
                entity_id_only = self.map_image_entity.split(".", 1)[1]
                image_url = f"{ha_url}/api/image_proxy/{entity_id_only}"
            
            # Generate filename with timestamp
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            if trigger_context:
                # Sanitize trigger_context for filename
                safe_context = "".join(c for c in trigger_context if c.isalnum() or c in ('_', '-')).strip()
                filename = f"rober2_map_{timestamp}_{safe_context}.png"
            else:
                filename = f"rober2_map_{timestamp}.png"
            
            filepath = os.path.join(self.map_save_dir, filename)
            
            # Download and save the image
            try:
                # Use urllib to download the image
                # Note: For Home Assistant API endpoints that require auth, this might fail
                # In that case, you may need to use a shell_command service or add auth headers
                # urlretrieve() is blocking; run in a background thread with a hard timeout
                await asyncio.wait_for(
                    asyncio.to_thread(urllib.request.urlretrieve, image_url, filepath),
                    timeout=self._map_download_timeout_s,
                )
                
                # Verify file was created and has content
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    self.log(f"Saved map image: {filename} (trigger: {trigger_context or 'unknown'})", level="INFO")
                    
                    # Save metadata to JSON index for easy React access
                    await self._update_map_index(filename, timestamp, trigger_context)
                else:
                    self.log(f"Map image file created but appears empty: {filename}", level="WARNING")
            except urllib.error.HTTPError as e:
                self.log(f"Failed to download map image (HTTP {e.code}): {e}. URL: {image_url}", level="WARNING")
                self.log("Note: If authentication is required, consider using a shell_command service instead", level="DEBUG")
            except asyncio.TimeoutError:
                self.log(f"Timed out downloading map image after {self._map_download_timeout_s}s: {image_url}", level="WARNING")
            except Exception as e:
                self.log(f"Error saving map image: {e}", level="WARNING")
                
        except Exception as e:
            self.log(f"Error in save_map_image: {e}", level="ERROR")
                
    async def _update_map_index(self, filename, timestamp, trigger_context):
        """Update JSON index file with map image metadata for easy React access."""
        try:
            index_file = os.path.join(self.map_save_dir, "index.json")
            
            # Load existing index or create new
            if os.path.exists(index_file):
                try:
                    with open(index_file, 'r') as f:
                        index_data = json.load(f)
                except (json.JSONDecodeError, IOError):
                    index_data = {"maps": []}
            else:
                index_data = {"maps": []}
            
            # Add new entry
            entry = {
                "filename": filename,
                "timestamp": timestamp,
                "datetime": datetime.datetime.now().isoformat(),
                "room": trigger_context if trigger_context else None,
                "url": f"/local/rober2_maps/{filename}"
            }
            
            # Add to beginning of list (most recent first)
            index_data["maps"].insert(0, entry)
            
            # Keep only last 100 entries to prevent index from growing too large
            if len(index_data["maps"]) > 100:
                index_data["maps"] = index_data["maps"][:100]
                # Optionally delete old files here if desired
            
            # Save updated index
            with open(index_file, 'w') as f:
                json.dump(index_data, f, indent=2)
                
        except Exception as e:
            self.log(f"Error updating map index: {e}", level="WARNING")
            
    async def cleanup_old_map_images(self, kwargs):
        """Clean up map images and metadata, keeping only the last N images per room."""
        try:
            if not self.map_save_dir:
                return
            
            deleted_count = 0
            index_file = os.path.join(self.map_save_dir, "index.json")
            
            # Load index if it exists
            index_data = {"maps": []}
            if os.path.exists(index_file):
                try:
                    with open(index_file, 'r') as f:
                        index_data = json.load(f)
                except (json.JSONDecodeError, IOError):
                    index_data = {"maps": []}
            
            # Group maps by room
            maps_by_room = {}
            for map_entry in index_data.get("maps", []):
                try:
                    room = map_entry.get("room")
                    if room is None:
                        room = "unknown"  # Group entries without room name
                    
                    if room not in maps_by_room:
                        maps_by_room[room] = []
                    
                    # Parse datetime for sorting
                    entry_datetime = datetime.datetime.fromisoformat(map_entry.get("datetime", ""))
                    maps_by_room[room].append((entry_datetime, map_entry))
                except (ValueError, KeyError) as e:
                    # Invalid entry, skip it
                    self.log(f"Skipping invalid map entry: {e}", level="DEBUG")
                    continue
            
            # Determine which files to keep (last N per room)
            files_to_keep = set()
            maps_to_keep = []
            
            for room, room_maps in maps_by_room.items():
                # Sort by datetime (most recent first)
                room_maps.sort(key=lambda x: x[0], reverse=True)
                
                # Keep only the last N images for this room
                keep_count = min(self.map_retention_per_room, len(room_maps))
                for i in range(keep_count):
                    _, map_entry = room_maps[i]
                    filename = map_entry.get("filename")
                    files_to_keep.add(filename)
                    maps_to_keep.append(map_entry)
                
                # Delete excess images for this room
                for i in range(keep_count, len(room_maps)):
                    _, map_entry = room_maps[i]
                    filename = map_entry.get("filename")
                    filepath = os.path.join(self.map_save_dir, filename)
                    
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                            deleted_count += 1
                            self.log(f"Deleted old map image for {room}: {filename}", level="DEBUG")
                        except Exception as e:
                            self.log(f"Error deleting map file {filename}: {e}", level="WARNING")
            
            # Also check for orphaned files (files not in index)
            if os.path.exists(self.map_save_dir):
                for filename in os.listdir(self.map_save_dir):
                    if filename == "index.json":
                        continue
                    
                    if not filename.startswith("rober2_map_") or not filename.endswith(".png"):
                        continue
                    
                    if filename not in files_to_keep:
                        # Delete orphaned file
                        filepath = os.path.join(self.map_save_dir, filename)
                        try:
                            os.remove(filepath)
                            deleted_count += 1
                            self.log(f"Deleted orphaned map image: {filename}", level="DEBUG")
                        except Exception as e:
                            self.log(f"Error deleting orphaned file {filename}: {e}", level="WARNING")
            
            # Sort maps_to_keep by datetime (most recent first) for consistent index
            maps_to_keep.sort(key=lambda x: datetime.datetime.fromisoformat(x.get("datetime", "")), reverse=True)
            
            # Update index with only kept entries
            index_data["maps"] = maps_to_keep
            try:
                with open(index_file, 'w') as f:
                    json.dump(index_data, f, indent=2)
            except Exception as e:
                self.log(f"Error updating index after cleanup: {e}", level="WARNING")
            
            if deleted_count > 0:
                self.log(f"Map cleanup: Deleted {deleted_count} old map image(s) (keeping {self.map_retention_per_room} per room)", level="INFO")
            else:
                self.log(f"Map cleanup: No old images to delete (retention: {self.map_retention_per_room} per room)", level="DEBUG")
                
        except Exception as e:
            self.log(f"Error in cleanup_old_map_images: {e}", level="ERROR")
            
    async def handle_clean_while_home(self, entity, attribute, old, new, kwargs):
        """Handle clean_while_home toggle."""
        try:
            if new == "on":
                self.log_state_conclusion("manual_override", "clean_while_home_on", "override_presence", 
                                        "Clean while home enabled - evaluating cleaning conditions")
                self.run_in(self.evaluate_cleaning_conditions, 1, trigger="clean_while_home_on")
            else:
                self.log_state_conclusion("manual_override", "clean_while_home_off", "normal_operation", 
                                        "Clean while home disabled - back to normal operation")
                # If currently cleaning and someone is home, stop cleaning
                # BUT don't interrupt if robot is already returning to dock
                away = await self.is_away()
                robot_state = await self.get_state("vacuum.rober2")
                
                if self.cleaning and (not away) and robot_state != "returning":
                    self.log_state_conclusion("manual_override_off", "home_occupied", "stop_cleaning", 
                                            "Clean while home disabled and someone is home, stopping cleaning")
                    await self.stop_cleaning()
                    await self._set_narrative("Stopped - clean while home off")
                elif robot_state == "returning":
                    self.log_state_conclusion("manual_override_off", "robot_returning", "let_finish", 
                                            "Clean while home disabled but robot already returning to dock - letting it finish")
                else:
                    self.log_state_conclusion("manual_override_off", "normal_operation", "continue", 
                                            "Clean while home disabled - resuming normal operation")
                
        except Exception as e:
            self.log(f"Error handling clean while home: {e}", level="ERROR")

    async def handle_person_state(self, entity, attribute, old, new, kwargs):
        """Handle tracked person state changes and re-evaluate conditions."""
        try:
            self.log(f"Person state changed: {entity} {old} -> {new}")
            # Re-evaluate quickly on any person state change
            self.run_in(self.evaluate_cleaning_conditions, 1, trigger="person_state")
        except Exception as e:
            self.log(f"Error handling person state: {e}", level="ERROR")
            
    async def handle_room_flag_change(self, entity, attribute, old, new, kwargs):
        """React to room cleaning flag toggles by updating the job queue in priority order.
        - If idle/docked and conditions allow -> may start immediately (first-priority room)
        - If returning -> (re)queue next room immediately respecting current flags and priority
        - If actively cleaning -> do not interrupt; next room will be chosen on completion/return
        - If flags remove last room and override is on -> turn override off
        """
        try:
            if new in [None, "unavailable"]:
                return
            
            # Map entity id back to room id/name
            room_id = self._room_flag_entity_to_id.get(entity)
            if not room_id:
                # Fallback derive from entity name
                try:
                    room_name = entity.replace("input_boolean.rober2_clean_", "")
                    for rid, rname in self.room_config.items():
                        if rname == room_name:
                            room_id = rid
                            break
                except Exception:
                    room_id = None
            room_name = self.room_config.get(room_id, room_id or entity)
            
            self.log(f"Room flag changed: {entity} {old} -> {new} (room: {room_name})", level="INFO")
            
            # Manual re-enable resets the unreachable counter and dock-blocked flag for that room
            if new == "on" and room_id:
                self._room_interrupt_counts[room_id] = 0
                # Also reset the dock-blocked flag - user may have cleared the obstacle
                self._robot_has_left_dock = False
            
            # Always recompute remaining rooms
            rooms_remaining = await self.get_rooms_to_clean()
            
            # If no rooms remain, auto-disable override to avoid unexpected runs
            if not rooms_remaining:
                try:
                    clean_while_home = await self.get_state("input_boolean.clean_while_home")
                    if clean_while_home == "on":
                        await self.call_service("input_boolean/turn_off", entity_id="input_boolean.clean_while_home")
                        self.log("No rooms remain after flag change - turned off clean_while_home", level="INFO")
                except Exception:
                    pass
            
            # Respect presence, quiet hours, and guest constraints
            away = await self.is_away()
            automation_enabled = await self._is_automation_enabled()
            within_hours = await self.is_within_cleaning_hours()
            robot_state = await self.get_state("vacuum.rober2")

            if not automation_enabled or not within_hours:
                self.log(
                    f"Flag change observed but cleaning blocked (enabled={automation_enabled}, within_hours={within_hours})",
                    level="INFO",
                )
                return
            
            # If robot is returning, immediately re-queue best next room per updated flags
            if robot_state == "returning":
                await self.queue_next_room(trigger="room_flag_change")
                return
            
            # If robot idle/docked and there are rooms, respect presence or override and start
            if robot_state in ["idle", "docked"]:
                clean_while_home = await self.get_state("input_boolean.clean_while_home")
                if rooms_remaining:
                    if away or clean_while_home == "on":
                        self.run_in(self.evaluate_cleaning_conditions, 1, trigger="room_flag_change")
                        return
                    else:
                        self.log("Flag change: rooms available but home occupied and override off", level="INFO")
                        return
                else:
                    self.log("Flag change: no rooms available; staying idle", level="INFO")
                    return
            
            # If currently cleaning, do not interrupt current job; allow next-room selection later
            if robot_state == "cleaning":
                # If user turned OFF the currently tracked room, do not stop; just log
                if self.current_room and room_id == self.current_room and new == "off":
                    self.log(f"Current room flag turned off during cleaning ({room_name}); will finish current run", level="INFO")
                else:
                    self.log("Flag change while cleaning; next room will be chosen on completion", level="INFO")
                return
            
            # For other states (paused/error/unknown), rely on existing handlers to converge
            self.run_in(self.evaluate_cleaning_conditions, 1, trigger="room_flag_change_fallback")
            
        except Exception as e:
            self.log(f"Error handling room flag change: {e}", level="ERROR")
            
    async def mark_room_cleaned(self, room_id):
        """Mark a room as cleaned."""
        try:
            room_name = self.room_config.get(room_id, room_id)
            
            # Update last cleaned date
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            await self.call_service("input_text/set_value",
                entity_id=f"input_text.{room_name}_last_clean",
                value=today
            )
            
            self.log_state_conclusion("mark_cleaned", f"room_{room_name}_cleaned", "update_schedule", 
                                    f"Marked room {room_name} as cleaned")
            
        except Exception as e:
            self.log(f"Error marking room as cleaned: {e}", level="ERROR")
    
    async def should_empty_bin(self):
        """Empty when docked after at least one room completed in the current session (not calendar-day based)."""
        try:
            if self._rooms_completed_this_session <= 0:
                self.log("No rooms completed in current session - bin should not be emptied", level="DEBUG")
                return False
            return True
        except Exception as e:
            self.log(f"Error checking if bin should be emptied: {e}", level="ERROR")
            return False
            
    async def handle_physical_room_change(self, entity, attribute, old, new, kwargs):
        """Track whether the robot ever physically leaves the dock room during a session."""
        try:
            if new and new not in ["unavailable", "unknown"] and self.cleaning:
                if new != self._dock_room_name:
                    self._robot_has_left_dock = True
        except Exception as e:
            self.log(f"Error in physical room change handler: {e}", level="ERROR")

    async def handle_automation_enabled(self, entity, attribute, old, new, kwargs):
        """Handle user master enable/disable toggle."""
        try:
            if new == "off":
                if self.cleaning:
                    self.log_state_conclusion("automation_disabled", "user_toggle", "stop_cleaning",
                                            "Rober2 automation disabled, stopping cleaning")
                    await self.stop_cleaning()
                    await self._set_narrative("Stopped - automation disabled")
                else:
                    self.log_state_conclusion("automation_disabled", "user_toggle", "wait",
                                            "Rober2 automation disabled")
                    await self._set_narrative("Automation disabled")
            elif new == "on":
                self.log_state_conclusion("automation_enabled", "user_toggle", "check_conditions",
                                        "Rober2 automation enabled, checking if we can start cleaning")
                self.run_in(self.evaluate_cleaning_conditions, 1, trigger="automation_enabled_on")
        except Exception as e:
            self.log(f"Error handling automation enabled toggle: {e}", level="ERROR")
            
    async def is_within_cleaning_hours(self):
        """Check if current time is within cleaning hours using dynamic quiet hours entities."""
        try:
            # Get quiet hours from Home Assistant entities
            quiet_start = await self.get_state("input_datetime.quiet_hours_start")
            quiet_end = await self.get_state("input_datetime.quiet_hours_end")
            
            # Safety check for async Task objects
            if hasattr(quiet_start, 'split') == False or hasattr(quiet_end, 'split') == False:
                self.log("Quiet hours entities returned Task objects, using default 07:00-22:00", level="WARNING")
                now = datetime.datetime.now()
                return 7 <= now.hour < 22
            
            if not quiet_start or quiet_start == "unavailable" or not quiet_end or quiet_end == "unavailable":
                # Fallback to default hours if entities not available
                self.log("Quiet hours entities not available, using default 07:00-22:00", level="WARNING")
                now = datetime.datetime.now()
                return 7 <= now.hour < 22
            
            # Parse times (format: "07:00:00" or "07:00")
            try:
                quiet_start_parts = quiet_start.split(":")
                quiet_end_parts = quiet_end.split(":")
                quiet_start_hour = int(quiet_start_parts[0])
                quiet_start_min = int(quiet_start_parts[1]) if len(quiet_start_parts) > 1 else 0
                quiet_end_hour = int(quiet_end_parts[0])
                quiet_end_min = int(quiet_end_parts[1]) if len(quiet_end_parts) > 1 else 0
            except (ValueError, IndexError) as e:
                self.log(f"Error parsing quiet hours: {e}, using defaults", level="ERROR")
                now = datetime.datetime.now()
                return 7 <= now.hour < 22
            
            # Get current time
            now = datetime.datetime.now()
            current_minutes = now.hour * 60 + now.minute
            quiet_start_minutes = quiet_start_hour * 60 + quiet_start_min
            quiet_end_minutes = quiet_end_hour * 60 + quiet_end_min
            
            # Check if current time is within cleaning hours (outside quiet hours)
            # Cleaning allowed from quiet_end to quiet_start
            if quiet_end_minutes <= quiet_start_minutes:
                # Normal day: e.g., 07:00 to 22:00
                is_within = quiet_end_minutes <= current_minutes < quiet_start_minutes
            else:
                # Overnight quiet hours: e.g., 22:00 to 07:00 next day
                is_within = current_minutes >= quiet_end_minutes or current_minutes < quiet_start_minutes
            
            if not is_within:
                self.log(f"Outside cleaning hours (current: {now.strftime('%H:%M')}, "
                        f"quiet: {quiet_start} - {quiet_end})", level="INFO")
            
            return is_within
                
        except Exception as e:
            self.log(f"Error checking cleaning hours: {e}", level="ERROR")
            return False
            
    async def check_quiet_hours_end(self, kwargs):
        """Check if cleaning should start when quiet hours end."""
        try:
            current_time = datetime.datetime.now().strftime("%H:%M")
            self.log_state_conclusion("quiet_hours_end", current_time, "check_conditions", 
                                    "Quiet hours ended, checking if cleaning can start")
            self.run_in(self.evaluate_cleaning_conditions, 1, trigger="quiet_hours_end")
            
        except Exception as e:
            self.log(f"Error checking quiet hours end: {e}", level="ERROR")
            
    async def check_quiet_hours_start(self, kwargs):
        """Stop cleaning when quiet hours start."""
        try:
            current_time = datetime.datetime.now().strftime("%H:%M")
            
            # If robot is currently cleaning, stop it for quiet hours
            if self.cleaning:
                self.log_state_conclusion("quiet_hours_start", current_time, "stop_for_quiet_hours", 
                                        "Quiet hours started, stopping cleaning for noise consideration")
                await self.stop_cleaning()
                await self._set_narrative("Stopped - quiet hours")
            else:
                self.log_state_conclusion("quiet_hours_start", current_time, "enter_quiet_mode", 
                                        "Quiet hours started, robot will remain docked")
                await self._set_narrative("Quiet hours - robot docked")
            
        except Exception as e:
            self.log(f"Error checking quiet hours start: {e}", level="ERROR")
            
    async def handle_quiet_hours_change(self, entity, attribute, old, new, kwargs):
        """Handle changes to quiet hours settings."""
        try:
            if new and new != "unavailable":
                self.log(f"Quiet hours changed: {entity} = {new}")
                # Update the daily triggers for quiet hours
                await self.setup_quiet_hours_triggers()
                
        except Exception as e:
            self.log(f"Error handling quiet hours change: {e}", level="ERROR")
            
    async def setup_quiet_hours_triggers(self, kwargs=None):
        """Set up daily triggers for when quiet hours start and end."""
        try:
            # Get quiet hours times
            quiet_start = await self.get_state("input_datetime.quiet_hours_start")
            quiet_end = await self.get_state("input_datetime.quiet_hours_end")
            
            # Cancel any existing triggers
            if hasattr(self, '_quiet_hours_end_trigger'):
                self._safe_cancel_timer(getattr(self, "_quiet_hours_end_trigger", None))
            if hasattr(self, '_quiet_hours_start_trigger'):
                self._safe_cancel_timer(getattr(self, "_quiet_hours_start_trigger", None))
            
            # Set up quiet hours END trigger (when cleaning can start)
            if not quiet_end or quiet_end == "unavailable":
                self.log("Quiet hours end entity not available, using default 07:30", level="WARNING")
                self._quiet_hours_end_trigger = self.run_daily(self.check_quiet_hours_end, datetime.time(7, 30))
            else:
                try:
                    time_parts = quiet_end.split(":")
                    hour = int(time_parts[0])
                    minute = int(time_parts[1]) if len(time_parts) > 1 else 0
                    self._quiet_hours_end_trigger = self.run_daily(self.check_quiet_hours_end, datetime.time(hour, minute))
                    self.log(f"Set quiet hours end trigger for {hour:02d}:{minute:02d}", level="INFO")
                except (ValueError, IndexError) as e:
                    self.log(f"Error parsing quiet hours end time: {e}, using default 07:30", level="ERROR")
                    self._quiet_hours_end_trigger = self.run_daily(self.check_quiet_hours_end, datetime.time(7, 30))
            
            # Set up quiet hours START trigger (when cleaning must stop)
            if not quiet_start or quiet_start == "unavailable":
                self.log("Quiet hours start entity not available, using default 22:00", level="WARNING")
                self._quiet_hours_start_trigger = self.run_daily(self.check_quiet_hours_start, datetime.time(22, 0))
            else:
                try:
                    time_parts = quiet_start.split(":")
                    hour = int(time_parts[0])
                    minute = int(time_parts[1]) if len(time_parts) > 1 else 0
                    self._quiet_hours_start_trigger = self.run_daily(self.check_quiet_hours_start, datetime.time(hour, minute))
                    self.log(f"Set quiet hours start trigger for {hour:02d}:{minute:02d}", level="INFO")
                except (ValueError, IndexError) as e:
                    self.log(f"Error parsing quiet hours start time: {e}, using default 22:00", level="ERROR")
                    self._quiet_hours_start_trigger = self.run_daily(self.check_quiet_hours_start, datetime.time(22, 0))
                
        except Exception as e:
            self.log(f"Error setting up quiet hours triggers: {e}", level="ERROR")
            # Fallback to defaults
            self._quiet_hours_end_trigger = self.run_daily(self.check_quiet_hours_end, datetime.time(7, 30))
            self._quiet_hours_start_trigger = self.run_daily(self.check_quiet_hours_start, datetime.time(22, 0))
            
    async def log_room_schedule_status(self, kwargs=None):
        """Log current room schedule status at startup."""
        try:
            # Get rooms that would be cleaned
            rooms = await self.get_rooms_to_clean()
            
            if rooms:
                room_names = [self.room_config[room_id] for room_id in rooms]
                self.log(f"ROOM SCHEDULE: {len(rooms)} rooms ready | Next: {room_names[0]} | Queue: {', '.join(room_names)}", level="INFO")
            else:
                self.log("ROOM SCHEDULE: No rooms currently scheduled for cleaning", level="INFO")
            
        except Exception as e:
            self.log(f"Error logging room schedule status: {e}", level="ERROR")
            
    async def handle_robot_status(self, entity, attribute, old, new, kwargs):
        """Handle changes to robot detailed status - especially bin emptying completion."""
        try:
            if new and new != "unavailable":
                self.log(f"Robot status changed: {old} -> {new}")

                # Ensure tracking is initialized when segment cleaning starts,
                # even if a vacuum state "cleaning" event is missed.
                if new == "segment_cleaning":
                    try:
                        if not self.cleaning:
                            self.cleaning = True
                            # Reset bin empty flag for new cleaning session (persistent in HA)
                            await self.call_service("input_boolean/turn_off", entity_id="input_boolean.rober2_bin_empty_triggered")
                        # If we don't have a current room, derive from input_text helper
                        if not self.current_room:
                            current_room_text = await self.get_state("input_text.rober2_current_room")
                            if current_room_text and current_room_text != "unavailable" and current_room_text in self.room_config:
                                self.current_room = current_room_text
                        # Initialize start time if missing
                        if not self.room_start_time:
                            self.room_start_time = datetime.datetime.now()
                        # Record that we have observed this room during cleaning
                        if self.current_room:
                            self._visited_rooms.add(self.current_room)
                        
                    except Exception as track_err:
                        self.log(f"Error initializing tracking on segment_cleaning: {track_err}", level="ERROR")
                
                # Handle bin emptying completion
                if old == "emptying_the_bin" and new != "emptying_the_bin":
                    self.log(f"Bin emptying complete (status: {old} -> {new}) - checking for next room", level="INFO")
                    await self._set_narrative("Bin empty complete - checking for next room")
                    
                    # Update bin last emptied date when emptying actually completes
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    await self.call_service("input_text/set_value",
                        entity_id="input_text.rober2_bin_last_emptied",
                        value=today
                    )
                    self.log(f"Updated bin last emptied date to {today}", level="INFO")
                    
                    # Session bin qualification: allow one empty per completed-work batch; reset after cycle finishes
                    self._rooms_completed_this_session = 0
                    
                    # Robot finished emptying bin - check for next room
                    self.cleaning = False  # make sure scheduler runs
                    self.run_in(self.evaluate_cleaning_conditions, 1, trigger="bin_empty_complete")
                
        except Exception as e:
            self.log(f"Error handling robot status: {e}", level="ERROR")
            
    async def handle_vacuum_error(self, entity, attribute, old, new, kwargs):
        """Handle vacuum error state changes and notify on clear.
        Treats 'none'/'unknown'/'unavailable'/None/'' as 'no error'.
        """
        try:
            old_no_error = self._is_no_error_state(old)
            new_no_error = self._is_no_error_state(new)

            # Error cleared (changed from real error -> no error)
            if (not old_no_error) and new_no_error:
                self.log(f"Vacuum error cleared: '{old}' -> '{new}' - automation can resume", level="INFO")
                self.log_state_conclusion("vacuum_error_cleared", f"error_{old}_cleared", "resume", 
                                        f"Error '{old}' cleared - automation resuming")
                await self._set_narrative(f"Error cleared - {str(old).replace('_', ' ')} - resuming")

                # Send notification that error is cleared (always to Mikkel and Kristine)
                await self.send_notification(
                    title="Rober2 Error Resolved",
                    message=f"Error '{old.replace('_', ' ').title()}' has been cleared.\nAutomation resuming.",
                    target=["mikkel", "kristine"],  # Always send regardless of presence
                    data={"data": {"importance": "default", "channel": "alerts"}}
                )
                
                # Re-evaluate cleaning conditions now that error is cleared
                self.run_in(self.evaluate_cleaning_conditions, 2, trigger="error_cleared")
                return

            # Still no error (ignore)
            if new_no_error:
                return
            
            # Log the error
            self.log(f"Vacuum error detected: {old} -> {new}", level="WARNING")
            
            # Critical errors that should stop cleaning immediately
            critical_errors = [
                "main_brush_jammed",
                "side_brush_jammed", 
                "wheels_jammed",
                "wheels_suspended",
                "robot_trapped",
                "robot_tilted",
                "no_dustbin",
                "battery_error",
                "charging_error",
                "internal_error"
            ]
            
            # Navigation/blocking errors that should stop cleaning
            blocking_errors = [
                "lidar_blocked",
                "bumper_stuck",
                "dock",  # Cannot find dock - might be stuck
                "cliff_sensor_error"
            ]
            
            # If currently cleaning and critical/blocking error, stop cleaning
            if self.cleaning and (new in critical_errors or new in blocking_errors):
                room_name = self.room_config.get(self.current_room, self.current_room) if self.current_room else "unknown"
                self.log(f"Critical/blocking error '{new}' detected during cleaning - stopping cleaning for room {room_name}", level="ERROR")
                self.log_state_conclusion("vacuum_error", f"error_{new}", "stop_cleaning", 
                                        f"Critical error '{new}' - stopping cleaning")
                err_pretty = str(new).replace("_", " ")
                if self.current_room:
                    await self._set_narrative(
                        f"Error - {err_pretty} - stopped ({self._pretty_room_from_segment_id(self.current_room)})"
                    )
                else:
                    await self._set_narrative(f"Error - {err_pretty} - stopped")
                
                # Send notification for critical error (always to Mikkel and Kristine)
                await self.send_notification(
                    title="Rober2 Error - Cleaning Stopped",
                    message=f"Critical error detected: {new.replace('_', ' ').title()}\nRoom: {room_name}\nCleaning has been stopped.",
                    target=["mikkel", "kristine"],  # Always send regardless of presence
                    data={"data": {"importance": "high", "channel": "alerts"}}
                )
                
                await self.stop_cleaning()
            elif new in critical_errors or new in blocking_errors:
                self.log(f"Critical/blocking error '{new}' detected (not cleaning)", level="WARNING")
                self.log_state_conclusion("vacuum_error", f"error_{new}", "monitor", 
                                        f"Error '{new}' detected - monitoring")
                await self._set_narrative(f"Error - {str(new).replace('_', ' ')} - idle")
                
                # Send notification for critical error (not cleaning, always to Mikkel and Kristine)
                await self.send_notification(
                    title="Rober2 Error Detected",
                    message=f"Critical error detected: {new.replace('_', ' ').title()}\nAutomation is paused.",
                    target=["mikkel", "kristine"],  # Always send regardless of presence
                    data={"data": {"importance": "high", "channel": "alerts"}}
                )
            else:
                # Non-critical errors (warnings, maintenance needed)
                self.log(f"Non-critical error '{new}' detected - monitoring", level="INFO")
                self.log_state_conclusion("vacuum_error", f"error_{new}", "monitor", 
                                        f"Non-critical error '{new}' - continuing")
                await self._set_narrative(f"Warning - {str(new).replace('_', ' ')} - continuing")
                
        except Exception as e:
            self.log(f"Error handling vacuum error: {e}", level="ERROR")
    
    async def _set_automation_paused(self, reason: str):
        """Set HA state to pause automation (e.g. after Blocked at dock)."""
        try:
            await self.call_service("input_boolean/turn_on", entity_id="input_boolean.rober2_automation_paused")
            await self.call_service("input_text/set_value", entity_id="input_text.rober2_pause_reason", value=reason)
            await self._set_narrative(f"Automation paused - {reason}")
        except Exception as e:
            self.log(f"Could not set automation paused state: {e}", level="DEBUG")

    async def handle_automation_paused_reset(self, entity, attribute, old, new, kwargs):
        """When user turns off the pause boolean, send robot home and clear reason."""
        try:
            if old == "on" and new == "off":
                self.log("Automation pause reset - sending robot home", level="INFO")
                await self._set_narrative("Pause cleared - sending robot home")
                await self._safe_call_service("vacuum/return_to_base", entity_id="vacuum.rober2")
                try:
                    await self.call_service("input_text/set_value", entity_id="input_text.rober2_pause_reason", value="")
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Error in automation paused reset: {e}", level="ERROR")
            
    async def send_notification(self, title: str, message: str, target: str = "user", data: dict = None):
        """Send notification via MobileNotifier app.
        
        Args:
            title: Notification title
            message: Notification message
            target: Who to send to:
                - "user": Send to user (default, for vacuum errors)
                - "home": Send to all people who are home
                - "all": Send to all configured devices
                - List of person names: Send to specific people
                - Notification service string: Send to specific service
            data: Optional additional data (e.g., {"data": {"importance": "high"}})
        """
        if not self.mobile_notifier:
            self.log("Mobile Notifier not available, skipping notification", level="DEBUG")
            return
        
        try:
            await self.mobile_notifier.notify(title=title, message=message, target=target, data=data)
        except Exception as e:
            self.log(f"Error sending notification: {e}", level="ERROR") 